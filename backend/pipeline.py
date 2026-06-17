"""
cascade/backend/pipeline.py

Core streaming pipeline orchestrator.

Responsibility: Wire STT → LLM → TTS together and manage the WebSocket
connection to the browser. Measure latency at each stage.

Fixes applied:
  [M4] When is_processing_transcript guard drops a concurrent transcript,
       a "busy" message is now sent to the client so the user knows to
       wait rather than thinking their question was heard.
"""

import asyncio
import logging
import time
from typing import Callable, Dict, Optional, Set

from backend.stt import STTHandler
from backend.llm import LLMGenerator
from backend.tts import TTSEngine
from backend.tutor import TutorSession

logger = logging.getLogger(__name__)


class PipelineSession:
    """
    Manages one complete voice agent session.

    Owns: STT handler, LLM generator, TTS engine, conversation history,
    and the connection to the WebSocket.
    """

    def __init__(
        self,
        api_keys: Dict[str, str],
        model_config: Dict[str, str],
        send_message: Callable,
        subject: Optional[str] = None,
        tts_engine: str = "edge"
    ):
        self.api_keys = api_keys
        self.model_config = model_config
        self.send_message = send_message
        self.tts_engine_choice = tts_engine

        self.tutor = TutorSession(subject=subject)
        self.stt_handler: STTHandler
        self.llm_generator: LLMGenerator
        self.tts_engine: TTSEngine

        # Latency tracking
        self.utterance_end_time: Optional[float] = None
        self.first_audio_time: Optional[float] = None
        self._last_stt_ms: int = 0
        self._last_llm_ms: int = 0

        # Turn tracking for interrupt safety
        self.turn_id: int = 0
        self._active_turn_id: int = 0
        self._tts_tasks: Set[asyncio.Task] = set()

        # Prevent concurrent transcript processing
        self.is_processing_transcript = False
        self.processing_task: Optional[asyncio.Task] = None
        self._cancel_event = asyncio.Event()

        logger.info(f"[Pipeline] Session initialized (subject={self.tutor.subject}, tts_engine={tts_engine})")

    async def initialize(self):
        """Initialize all pipeline components."""
        self.stt_handler = STTHandler(
            api_key=self.api_keys["deepgram"],
            on_transcript=self._on_transcript_received,
            on_error=self._on_stt_error,
        )
        self.llm_generator = LLMGenerator(
            api_key=self.api_keys["groq"],
            model=self.model_config["groq_model"],
        )
        self.tts_engine = TTSEngine(
            engine=self.tts_engine_choice,
            edge_voice=self.model_config.get("edge_tts_voice", "en-US-AriaNeural"),
            deepgram_api_key=self.api_keys["deepgram"],
            deepgram_model=self.model_config.get("deepgram_tts_model", "aura-asteria-en")
        )
        
        # Send TTS config to frontend
        self.send_message({
            "type": "tts_config",
            "format": self.tts_engine.format,
            "sample_rate": self.tts_engine.sample_rate,
            "sampleRate": self.tts_engine.sample_rate
        })
        
        await self.stt_handler.connect()
        logger.info("[Pipeline] All components initialized and ready")

    async def handle_audio(self, audio_bytes: bytes):
        """Forward audio bytes to the STT handler."""
        # Only measure STT window while the user is speaking (not during tutor response)
        if not self.is_processing_transcript and self.first_audio_time is None:
            self.first_audio_time = time.time()
        if self.stt_handler:
            await self.stt_handler.send_audio(audio_bytes)

    def _can_send(self, turn_id: int) -> bool:
        """Return True if messages for this turn should still be delivered."""
        return turn_id == self._active_turn_id and not self._cancel_event.is_set()

    def _send_for_turn(self, turn_id: int, msg: dict):
        """Send a JSON message tagged with turn_id, gated on cancel state."""
        if not self._can_send(turn_id):
            return
        payload = {**msg, "turn_id": turn_id}
        self.send_message(payload)

    def _on_transcript_received(self, transcript: str):
        """
        Called by STT when a complete utterance is confirmed.
        Schedules the pipeline processing on the event loop.
        """
        if not isinstance(transcript, str):
            return

        if not transcript.strip():
            logger.info("[Pipeline] Empty transcript received — resetting client")
            self.first_audio_time = None
            self.send_message({"type": "response_end"})
            return

        if self.is_processing_transcript:
            logger.info("[Pipeline] Busy — dropping concurrent transcript")
            self.send_message(
                {
                    "type": "busy",
                    "message": "Still responding — please wait a moment.",
                }
            )
            return

        self.turn_id += 1
        current_turn_id = self.turn_id
        self._active_turn_id = current_turn_id

        self.utterance_end_time = time.time()
        if self.first_audio_time:
            self._last_stt_ms = int((self.utterance_end_time - self.first_audio_time) * 1000)
        else:
            self._last_stt_ms = 0
        self.first_audio_time = None

        self.is_processing_transcript = True
        logger.info(
            f"[Pipeline] Turn {current_turn_id} transcript: '{transcript[:60]}' "
            f"(STT duration: {self._last_stt_ms}ms)"
        )

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._process_transcript(transcript, current_turn_id))
            self.processing_task = task
            task.add_done_callback(self._on_processing_done)
        except RuntimeError:
            logger.error("[Pipeline] No running event loop")
            self.is_processing_transcript = False

    def _on_processing_done(self, task: asyncio.Task):
        """Reset the processing flag when the pipeline task completes."""
        self.is_processing_transcript = False
        self.first_audio_time = None
        if task == self.processing_task:
            self.processing_task = None
        if task.cancelled():
            logger.info("[Pipeline] Processing task was cancelled")
        elif task.exception():
            logger.error(f"[Pipeline] Processing task failed: {task.exception()}")

    async def _process_transcript(self, transcript: str, turn_id: int):
        """Core pipeline: transcript → LLM streaming → TTS streaming → WebSocket."""
        full_response = ""
        first_token_received = False
        self._cancel_event.clear()

        try:
            self._send_for_turn(turn_id, {"type": "transcript", "text": transcript})

            self.tutor.add_user_message(transcript)
            self.tutor.trim_history(max_turns=10)
            messages = self.tutor.get_messages()

            queue = asyncio.Queue()

            async def synthesize_sentence_to_queue(text: str, sentence_queue: asyncio.Queue):
                try:
                    async for chunk in self.tts_engine.synthesise(text):
                        if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                            logger.info("[Pipeline] TTS synthesis cancelled")
                            break
                        if chunk:
                            await sentence_queue.put(chunk)
                    await sentence_queue.put(None)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"[Pipeline] TTS synthesis failed for '{text[:20]}': {e}")
                    await sentence_queue.put(e)

            async def produce_sentences():
                nonlocal first_token_received
                try:
                    async for sentence in self.llm_generator.generate(
                        transcript=transcript,
                        messages=messages,
                        timeout_sec=30,
                    ):
                        if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                            logger.info("[Pipeline] Cancelled during LLM generation")
                            break

                        if not first_token_received:
                            first_token_received = True
                            if self.utterance_end_time:
                                llm_ms = int((time.time() - self.utterance_end_time) * 1000)
                                self._last_llm_ms = llm_ms
                                logger.info(f"[Pipeline] LLM first token: {llm_ms}ms")

                        self._send_for_turn(turn_id, {"type": "response_chunk", "text": sentence})

                        sentence_queue = asyncio.Queue()
                        tts_task = asyncio.create_task(
                            synthesize_sentence_to_queue(sentence, sentence_queue)
                        )
                        self._tts_tasks.add(tts_task)
                        tts_task.add_done_callback(self._tts_tasks.discard)
                        await queue.put((sentence, sentence_queue))

                    await queue.put(None)
                except Exception as e:
                    logger.error(f"[Pipeline] LLM generator error: {e}")
                    await queue.put(e)

            async def consume_audio():
                nonlocal full_response
                first_audio_sent = False
                while True:
                    if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                        break
                    item = await queue.get()
                    if item is None:
                        queue.task_done()
                        break
                    if isinstance(item, Exception):
                        queue.task_done()
                        raise item

                    sentence, sentence_queue = item
                    full_response += sentence
                    try:
                        while True:
                            if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                                break
                            chunk = await sentence_queue.get()
                            if chunk is None:
                                sentence_queue.task_done()
                                break
                            if isinstance(chunk, Exception):
                                sentence_queue.task_done()
                                raise chunk

                            if not first_audio_sent:
                                first_audio_sent = True
                                if self.utterance_end_time and self._can_send(turn_id):
                                    total_ms = int((time.time() - self.utterance_end_time) * 1000)
                                    llm_ms = getattr(self, '_last_llm_ms', 0)
                                    tts_ms = total_ms - llm_ms
                                    stt_ms = getattr(self, '_last_stt_ms', 0)
                                    logger.info(
                                        f"[Pipeline] First audio sent: {total_ms}ms "
                                        f"(STT: {stt_ms}ms, LLM: {llm_ms}ms, TTS: {tts_ms}ms)"
                                    )
                                    self._send_for_turn(turn_id, {
                                        "type": "latency",
                                        "total_ms": total_ms,
                                        "llm_ms": llm_ms,
                                        "tts_ms": tts_ms,
                                        "stt_ms": stt_ms,
                                        "ms": total_ms,
                                    })
                            if self._can_send(turn_id):
                                self.send_message({"type": "audio", "data": chunk, "turn_id": turn_id})
                            sentence_queue.task_done()
                    except Exception as e:
                        logger.error(f"[Pipeline] Error sending audio for sentence: {e}")
                    finally:
                        queue.task_done()

            await asyncio.gather(produce_sentences(), consume_audio())

            if full_response and self._can_send(turn_id):
                self.tutor.add_assistant_message(full_response)

            logger.info(f"[Pipeline] Turn {turn_id} complete: '{full_response[:60]}'")

        except asyncio.CancelledError:
            logger.info("[Pipeline] Processing cancelled")
        except Exception as e:
            logger.error(f"[Pipeline] Unexpected error: {e}")
            if self._can_send(turn_id):
                self.send_message({"type": "error", "message": f"Pipeline error: {e}", "turn_id": turn_id})
        finally:
            if self._can_send(turn_id):
                self._send_for_turn(turn_id, {"type": "response_end"})

    async def cancel(self):
        """Cancel the active processing task on user interruption."""
        cancelled_turn = self._active_turn_id
        self._cancel_event.set()

        for task in list(self._tts_tasks):
            task.cancel()

        if self.processing_task and not self.processing_task.done():
            logger.info("[Pipeline] Cancelling active processing task")
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                logger.info("[Pipeline] Active processing task cancelled successfully")
            except Exception as e:
                logger.error(f"[Pipeline] Error waiting for cancelled task: {e}")
            self.processing_task = None

        self.is_processing_transcript = False

        if self.stt_handler:
            self.stt_handler.clear_buffer()

        self.first_audio_time = None

        if cancelled_turn:
            self.send_message({"type": "turn_cancelled", "turn_id": cancelled_turn})

        logger.info(f"[Pipeline] Cancellation complete for turn {cancelled_turn}")

    def _on_stt_error(self, error: str):
        """Surface STT errors to the frontend."""
        logger.error(f"[Pipeline] STT error: {error}")
        self.send_message({"type": "error", "message": f"STT error: {error}"})

    async def close(self):
        """Shut down all pipeline components cleanly."""
        if self.stt_handler:
            try:
                await self.stt_handler.close()
            except Exception as e:
                logger.error(f"[Pipeline] STT close error: {e}")
        if self.tts_engine:
            try:
                await self.tts_engine.close()
            except Exception as e:
                logger.error(f"[Pipeline] TTS close error: {e}")
        logger.info("[Pipeline] Session closed")
