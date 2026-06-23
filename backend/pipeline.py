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
import re
import time
from typing import Any, Dict, Optional, Set, TypeAlias, cast

from groq.types.chat import ChatCompletionMessageParam
from backend.stt import STTHandler
from backend.llm import LLMGenerator
from backend.tts import TTSEngine
from backend.tutor import TutorSession

logger = logging.getLogger(__name__)

SentenceQueueItem: TypeAlias = bytes | Exception | None
ResponseQueueItem: TypeAlias = tuple[str, asyncio.Queue[SentenceQueueItem]] | Exception | None


def strip_markdown(text: str) -> str:
    """Strip common markdown formatting characters so TTS doesn't read them literally."""
    text = re.sub(r'(?<!\w)-(?!\w)', ' ', text)
    cleaned = re.sub(r'[*_#`\[\]]', '', text)
    return cleaned.strip()


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
        outbound_queue: asyncio.Queue[dict[str, Any] | None],
        subject: Optional[str] = None,
        tts_engine: str = "edge",
    ):
        self.api_keys = api_keys
        self.model_config = model_config
        self.tts_engine_choice = tts_engine
        self.outbound_queue = outbound_queue

        self.tutor = TutorSession(subject=subject)
        self.stt_handler: Optional[STTHandler] = None
        self.llm_generator: Optional[LLMGenerator] = None
        self.tts_engine: Optional[TTSEngine] = None

        # Latency tracking
        self.utterance_end_time: Optional[float] = None
        self._last_stt_ms: int = 0
        self._last_llm_ms: int = 0
        
        # LLM latency breakdown (computed during first sentence)
        self._llm_queue_ms: int = 0
        self._llm_ttft_ms: int = 0
        self._llm_streaming_ms: int = 0
        
        # TTS latency per-sentence tracking
        self._tts_first_sentence_latency_ms: int = 0
        self._tts_metrics_sent: bool = False
        
        # Interruption tracking
        self._final_turn_cutoff_time: Optional[float] = None

        # Turn tracking for interrupt safety
        self.turn_id: int = 0
        self._active_turn_id: Optional[int] = None
        self._tts_tasks: Set[asyncio.Task] = set()
        
        # TTS concurrency — Semaphore(2) for O(1) wakeup (replaces Condition)
        self._tts_semaphore = asyncio.Semaphore(2)
        self._first_audio_sent_turn_id: Optional[int] = None

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
            on_status=self._on_stt_status,
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

    def _can_send(self, turn_id: int) -> bool:
        """Return True if messages for this turn should still be delivered.
        
        Final atomic validation gate to prevent stale audio from reaching client.
        Checks are:
        1. turn_id matches active turn (or active turn is None for closed turns)
        2. Cancel event not set
        """
        if self._cancel_event.is_set():
            return False
        if turn_id != self._active_turn_id:
            return False
        return True
    
    def _compute_ideal_concurrency(self) -> int:
        return 2

    def send_message(self, msg: dict):
        self.outbound_queue.put_nowait(msg)

    def can_send_message(self, msg: dict) -> bool:
        """Gate outbound WebSocket messages at send time (closes fire-and-forget race)."""
        turn_id = msg.get("turn_id")
        if turn_id is None:
            return True
        return self._can_send(turn_id)

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
            if not self.is_processing_transcript:
                self.send_message({"type": "response_end"})
            return

        # Newest wins: if a new transcript arrives while we are already processing a turn,
        # cancel the active turn cleanly and prepare to start the new one.
        if self.is_processing_transcript or self._active_turn_id is not None:
            old_turn = self._active_turn_id
            logger.warning(f"[Pipeline] Turn {old_turn} interrupted by new transcript")
            self._final_turn_cutoff_time = time.time()
            self._cancel_active_turn_tasks()
            if old_turn is not None:
                self.send_message({"type": "turn_cancelled", "turn_id": old_turn})
            self.is_processing_transcript = False
            self.processing_task = None

        self.turn_id += 1
        current_turn_id = self.turn_id
        
        self._cancel_event.clear()
        self._active_turn_id = current_turn_id
        
        # Reset metrics for new turn
        self._tts_first_sentence_latency_ms = 0
        self._tts_metrics_sent = False

        self.utterance_end_time = time.time()
        if self.stt_handler:
            self._last_stt_ms = self.stt_handler.last_stt_processing_ms
        else:
            self._last_stt_ms = 0

        self.is_processing_transcript = True
        logger.info(
            f"[Pipeline] Turn {current_turn_id} transcript: '{transcript[:60]}' "
            f"(STT processing: {self._last_stt_ms}ms)"
        )

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._process_transcript(transcript, current_turn_id))
            self.processing_task = task
            
            _captured_task = task
            def _on_done(t):
                if t is _captured_task and self.processing_task is _captured_task:
                    self.processing_task = None
                    self.is_processing_transcript = False
                elif t.cancelled():
                    logger.info("[Pipeline] Processing task was cancelled")
                elif t.exception():
                    logger.error(f"[Pipeline] Processing task failed: {t.exception()}")
                    
            task.add_done_callback(_on_done)
        except RuntimeError:
            logger.error("[Pipeline] No running event loop")
            self.is_processing_transcript = False

    async def _process_transcript(self, transcript: str, turn_id: int):
        """Core pipeline: transcript → LLM streaming → TTS streaming → WebSocket."""
        full_response = ""
        first_token_received = False
        llm_generator = self.llm_generator
        tts_engine = self.tts_engine

        try:
            if llm_generator is None or tts_engine is None:
                raise RuntimeError("Pipeline components are not initialized")

            self._send_for_turn(turn_id, {"type": "transcript", "text": transcript})

            self.tutor.add_user_message(transcript)
            self.tutor.trim_history(max_turns=10)
            messages = cast(list[ChatCompletionMessageParam], self.tutor.get_messages())

            queue: asyncio.Queue[ResponseQueueItem] = asyncio.Queue()

            async def synthesize_sentence_to_queue(
                text: str,
                sentence_queue: asyncio.Queue[SentenceQueueItem],
            ) -> None:
                try:
                    async with self._tts_semaphore:
                        if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                            logger.debug(f"[Pipeline] TTS slot acquisition cancelled for '{text[:40]}'")
                            return
                        
                        logger.debug("[Pipeline] TTS slot acquired")
                        
                        async for chunk in tts_engine.synthesise(text):
                            if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                                logger.info("[Pipeline] TTS synthesis cancelled")
                                break
                            
                            # Check if chunk is metadata dict (first yield) or audio bytes
                            if isinstance(chunk, dict) and chunk.get("type") == "tts_metadata":
                                # Defensive: Validate latency value (should be positive and reasonable)
                                latency_ms = chunk.get("latency_ms", 0)
                                if not isinstance(latency_ms, (int, float)) or latency_ms < 0 or latency_ms > 60000:
                                    logger.warning(f"[Pipeline] Invalid TTS latency value: {latency_ms}ms, using 0")
                                    latency_ms = 0
                                else:
                                    latency_ms = int(latency_ms)
                                
                                logger.debug(f"[Pipeline] TTS metadata received: latency={latency_ms}ms")
                                if not self._tts_first_sentence_latency_ms:
                                    self._tts_first_sentence_latency_ms = latency_ms
                                    # Send first-sentence TTS metrics to client (only once per turn)
                                    if not self._tts_metrics_sent:
                                        self._tts_metrics_sent = True
                                        logger.info(f"[Pipeline] First TTS latency: {latency_ms}ms")
                                        self._send_for_turn(turn_id, {
                                            "type": "tts_metrics",
                                            "first_sentence_latency_ms": latency_ms,
                                            "engine": chunk.get("engine", "unknown"),
                                        })
                            elif isinstance(chunk, (bytes, bytearray, memoryview)):
                                # Stream audio chunk directly to queue instead of buffering
                                if not self._cancel_event.is_set() and turn_id == self._active_turn_id:
                                    await sentence_queue.put(bytes(chunk))
                        
                        await sentence_queue.put(None)
                        logger.debug("[Pipeline] TTS slot released")
                        
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"[Pipeline] TTS synthesis failed for '{text[:20]}': {e}")
                    await sentence_queue.put(e)

            async def produce_sentences() -> None:
                nonlocal first_token_received
                gen = llm_generator.generate(
                    messages=messages,
                    timeout_sec=30,
                )
                try:
                    async for sentence in gen:
                        if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                            logger.info("[Pipeline] Cancelled during LLM generation")
                            break

                        if not first_token_received:
                            first_token_received = True
                            if self.utterance_end_time:
                                # Compute LLM latency breakdown from generator timestamps
                                queue_latency_ms = 0
                                ttft_ms = 0
                                streaming_delay_ms = 0
                                
                                if (llm_generator.t_request_sent and 
                                    llm_generator.t_request_created):
                                    queue_latency_ms = int((llm_generator.t_request_sent - 
                                                          llm_generator.t_request_created) * 1000)
                                    # Defensive: Clamp to reasonable range (clock skew protection)
                                    queue_latency_ms = max(0, min(queue_latency_ms, 30000))
                                
                                if (llm_generator.t_first_token and 
                                    llm_generator.t_request_sent):
                                    ttft_ms = int((llm_generator.t_first_token - 
                                                 llm_generator.t_request_sent) * 1000)
                                    # Defensive: Clamp to reasonable range (TTFT typically <2s)
                                    ttft_ms = max(0, min(ttft_ms, 30000))
                                
                                if (llm_generator.t_first_sentence_emitted and 
                                    llm_generator.t_first_token):
                                    streaming_delay_ms = int((llm_generator.t_first_sentence_emitted - 
                                                            llm_generator.t_first_token) * 1000)
                                    # Defensive: Clamp to reasonable range (streaming typically <5s to first sentence)
                                    streaming_delay_ms = max(0, min(streaming_delay_ms, 30000))
                                
                                # Compute total LLM time (first sentence latency from utterance end)
                                total_llm_ms = queue_latency_ms + ttft_ms + streaming_delay_ms
                                self._last_llm_ms = total_llm_ms
                                
                                # Store breakdown for metrics reporting
                                self._llm_queue_ms = queue_latency_ms
                                self._llm_ttft_ms = ttft_ms
                                self._llm_streaming_ms = streaming_delay_ms
                                
                                logger.info(f"[Pipeline] LLM metrics: queue={queue_latency_ms}ms, "
                                           f"ttft={ttft_ms}ms, streaming={streaming_delay_ms}ms, "
                                           f"total={total_llm_ms}ms")
                                
                                # Send detailed LLM metrics to client
                                self._send_for_turn(turn_id, {
                                    "type": "llm_metrics",
                                    "queue_ms": queue_latency_ms,
                                    "ttft_ms": ttft_ms,
                                    "streaming_delay_ms": streaming_delay_ms,
                                    "total_ms": total_llm_ms,
                                })

                        self._send_for_turn(turn_id, {"type": "response_chunk", "text": sentence})

                        # Sanitise markdown from sentence before TTS
                        clean_sentence = strip_markdown(sentence)
                        sentence_queue: asyncio.Queue[SentenceQueueItem] = asyncio.Queue()
                        tts_task = asyncio.create_task(
                            synthesize_sentence_to_queue(clean_sentence, sentence_queue)
                        )
                        self._tts_tasks.add(tts_task)
                        tts_task.add_done_callback(self._tts_tasks.discard)
                        await queue.put((sentence, sentence_queue))

                    await queue.put(None)
                except Exception as e:
                    logger.error(f"[Pipeline] LLM generator error: {e}")
                    await queue.put(e)
                finally:
                    await gen.aclose()

            async def consume_audio() -> None:
                nonlocal full_response
                first_audio_sent = False
                while True:
                    if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        logger.warning("[Pipeline] consume_audio: Timeout waiting for next sentence queue item")
                        break
                    if item is None:
                        queue.task_done()
                        break
                    if isinstance(item, Exception):
                        queue.task_done()
                        raise item

                    sentence, sentence_queue = item
                    full_response = f"{full_response} {sentence}".strip() if full_response else sentence
                    try:
                        while True:
                            if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                                break
                            try:
                                chunk = await asyncio.wait_for(sentence_queue.get(), timeout=15.0)
                            except asyncio.TimeoutError:
                                logger.warning("[Pipeline] consume_audio: Timeout waiting for audio chunk")
                                try:
                                    sentence_queue.task_done()
                                except ValueError:
                                    pass
                                break
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
                                    tts_ms = self._tts_first_sentence_latency_ms or 0
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
                                # Log for latency diagnostics
                                if turn_id != self._first_audio_sent_turn_id:
                                    self._first_audio_sent_turn_id = turn_id
                                    utterance_end_time = self.utterance_end_time
                                    if utterance_end_time is not None:
                                        elapsed = (time.time() - utterance_end_time) * 1000
                                        logger.debug(f"[Pipeline] First audio sent: turn={turn_id}, time_since_utterance_end={elapsed:.1f}ms")
                            sentence_queue.task_done()
                    except Exception as e:
                        logger.error(f"[Pipeline] Error sending audio for sentence: {e}")
                        self._send_for_turn(turn_id, {
                            "type": "tts_error",
                            "message": str(e),
                            "sentence": sentence,
                        })
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
                self._active_turn_id = None

    def _cancel_active_turn_tasks(self):
        """Synchronously cancels all running tasks of the active turn.
        
        This is safe to call synchronously from STT callbacks or WebSocket handlers.
        It doesn't await the tasks, but schedules their cancellation immediately.
        """
        # Set cancel event to abort loop iterations
        self._cancel_event.set()
        
        # Cancel all TTS tasks
        if self._tts_tasks:
            logger.debug(f"[Pipeline] Cancelling {len(self._tts_tasks)} TTS tasks")
            for task in list(self._tts_tasks):
                task.cancel()
        
        # Cancel processing task
        if self.processing_task and not self.processing_task.done():
            logger.info("[Pipeline] Cancelling active processing task")
            self.processing_task.cancel()

    async def cancel(self):
        """Cancel the active processing task on user interruption.
        
        This is now synchronous and non-blocking under the hood, so it returns
        immediately and prevents blocking the main WebSocket message receive loop.
        """
        cancelled_turn = self._active_turn_id
        
        # 1. Synchronously cancel all active tasks
        self._cancel_active_turn_tasks()
        
        # 2. Invalidate active turn ID immediately (atomic)
        self._active_turn_id = None
        self._final_turn_cutoff_time = time.time()
        
        self.is_processing_transcript = False

        # Send turn cancelled message
        if cancelled_turn:
            self.send_message({"type": "turn_cancelled", "turn_id": cancelled_turn})
            logger.info(f"[Pipeline] Cancellation completed for turn {cancelled_turn}")

    def _on_stt_status(self, status: str, data: dict):
        """Surface STT reconnect status to the frontend."""
        self.send_message({"type": status, **data})

    def _on_stt_error(self, error: str):
        """Surface STT errors to the frontend."""
        logger.error(f"[Pipeline] STT error: {error}")
        self.send_message({"type": "error", "message": f"STT error: {error}"})

    async def close(self):
        """Shut down all pipeline components cleanly."""
        self._cancel_active_turn_tasks()
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
        if self.llm_generator:
            try:
                await self.llm_generator.close()
            except Exception as e:
                logger.error(f"[Pipeline] LLM close error: {e}")
        logger.info("[Pipeline] Session closed")
