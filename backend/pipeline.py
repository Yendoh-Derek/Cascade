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
from typing import Callable, Dict, Optional

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
        self.stt_handler: Optional[STTHandler] = None
        self.llm_generator: Optional[LLMGenerator] = None
        self.tts_engine: Optional[TTSEngine] = None

        # Latency tracking
        self.utterance_end_time: Optional[float] = None

        # Prevent concurrent transcript processing
        self.is_processing_transcript = False

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
        if self.stt_handler:
            await self.stt_handler.send_audio(audio_bytes)

    def _on_transcript_received(self, transcript: str):
        """
        Called by STT when a complete utterance is confirmed.
        Schedules the pipeline processing on the event loop.
        """
        if not isinstance(transcript, str):
            return

        if not transcript.strip():
            logger.info("[Pipeline] Empty transcript received — resetting client")
            self.send_message({"type": "response_end"})
            return

        # ── FIX [M4] ─────────────────────────────────────────────────────
        # Previously, concurrent transcripts were dropped silently — the user
        # had no feedback. Now we send a "busy" message so the UI can show
        # the user that the tutor is still responding.
        if self.is_processing_transcript:
            logger.info("[Pipeline] Busy — dropping concurrent transcript")
            self.send_message(
                {
                    "type": "busy",
                    "message": "Still responding — please wait a moment.",
                }
            )
            return

        self.utterance_end_time = time.time()
        self.is_processing_transcript = True
        logger.info(f"[Pipeline] Transcript received: '{transcript[:60]}'")

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._process_transcript(transcript))
            task.add_done_callback(self._on_processing_done)
        except RuntimeError:
            logger.error("[Pipeline] No running event loop")
            self.is_processing_transcript = False

    def _on_processing_done(self, task: asyncio.Task):
        """Reset the processing flag when the pipeline task completes."""
        self.is_processing_transcript = False
        if task.exception():
            logger.error(f"[Pipeline] Processing task failed: {task.exception()}")

    async def _process_transcript(self, transcript: str):
        """Core pipeline: transcript → LLM streaming → TTS streaming → WebSocket."""
        full_response = ""
        first_token_received = False

        try:
            # Send confirmed transcript to frontend
            self.send_message({"type": "transcript", "text": transcript})

            # Update conversation history
            self.tutor.add_user_message(transcript)
            self.tutor.trim_history(max_turns=10)
            messages = self.tutor.get_messages()

            queue = asyncio.Queue()

            async def synthesize_sentence(text: str) -> bytes:
                try:
                    audio_data_list = []
                    async for chunk in self.tts_engine.synthesise(text):
                        audio_data_list.append(chunk)
                    return b"".join(audio_data_list)
                except Exception as e:
                    logger.error(f"[Pipeline] TTS synthesis failed for '{text[:20]}': {e}")
                    return b""

            async def produce_sentences():
                nonlocal first_token_received
                try:
                    async for sentence in self.llm_generator.generate(
                        transcript=transcript,
                        messages=messages,
                        timeout_sec=30,
                    ):
                        if not first_token_received:
                            first_token_received = True
                            if self.utterance_end_time:
                                llm_ms = (time.time() - self.utterance_end_time) * 1000
                                logger.info(f"[Pipeline] LLM first token: {llm_ms:.0f}ms")

                        self.send_message({"type": "response_chunk", "text": sentence})
                        # Start synthesis task concurrently in background
                        tts_task = asyncio.create_task(synthesize_sentence(sentence))
                        await queue.put((sentence, tts_task))
                    
                    # Push sentinel to indicate end of stream
                    await queue.put(None)
                except Exception as e:
                    logger.error(f"[Pipeline] LLM generator error: {e}")
                    await queue.put(e)

            async def consume_audio():
                nonlocal full_response
                first_audio_sent = False
                while True:
                    item = await queue.get()
                    if item is None:
                        queue.task_done()
                        break
                    if isinstance(item, Exception):
                        queue.task_done()
                        raise item

                    sentence, tts_task = item
                    try:
                        audio_bytes = await tts_task
                        if audio_bytes:
                            if not first_audio_sent:
                                first_audio_sent = True
                                if self.utterance_end_time:
                                    total_ms = (time.time() - self.utterance_end_time) * 1000
                                    logger.info(f"[Pipeline] First audio sent: {total_ms:.0f}ms")
                                    self.send_message({"type": "latency", "ms": int(total_ms)})
                            self.send_message({"type": "audio", "data": audio_bytes})
                        full_response += sentence
                    except Exception as e:
                        logger.error(f"[Pipeline] Error sending audio for sentence: {e}")
                    finally:
                        queue.task_done()

            # Run producer and consumer concurrently
            await asyncio.gather(produce_sentences(), consume_audio())

            if full_response:
                self.tutor.add_assistant_message(full_response)

            logger.info(f"[Pipeline] Turn complete: '{full_response[:60]}'")

        except asyncio.CancelledError:
            logger.info("[Pipeline] Processing cancelled")
        except Exception as e:
            logger.error(f"[Pipeline] Unexpected error: {e}")
            self.send_message({"type": "error", "message": f"Pipeline error: {e}"})
        finally:
            # ── FIX [Bug 5]: Always signal end-of-response ──
            # Ensures frontend can recover even if LLM yields nothing
            # or an error occurs during processing.
            self.send_message({"type": "response_end"})

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
        logger.info("[Pipeline] Session closed")
