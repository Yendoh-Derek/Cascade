"""
cascade/backend/pipeline.py

Core streaming pipeline orchestrator.

Responsibility: Wire STT → LLM → TTS together and manage the WebSocket
connection to the browser. Measure latency at each stage.

Fixes applied:
  [M4]  When is_processing_transcript guard drops a concurrent transcript,
        a "busy" message is now sent to the client so the user knows to
        wait rather than thinking their question was heard.
  [0.5] Zero-sentence LLM output now emits an explicit error message to
        the client instead of silently ending the turn.
  [1.1] pipeline.py uses tts.synthesise_streaming() with Speak-many / Flush-once
        pattern via sentence queue. This eliminates per-sentence audio
        finalization gaps while still enabling true streaming.
  [1.4+N3] TTS semaphore and task set are now scoped per-turn, so a new
        turn always starts with fresh capacity instead of competing with
        dying tasks from the interrupted turn.
  [2.1] Latency tracking fields moved into TurnMetrics dataclass.
  [2.2] Partial assistant response from interrupted turns is saved to history
        to maintain user/assistant message pairing.
  [N8]  _on_done callback edge case fixed: is_processing_transcript is now
        cleared correctly when a task is replaced mid-flight.
  [N9]  Inner consume_audio() timeout aligned with outer LLM timeout (30s).
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypeAlias, cast

from groq import AsyncGroq
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
    # Remove code blocks (fenced with ```)
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code (`code`)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove links [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove emphasis/strikethrough *text*, **text**, ~~text~~
    text = re.sub(r'(\*\*|__|\*|_|~~)', '', text)
    # Remove headers #, ##, etc.
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Replace horizontal rules (---, ***, ___) with space
    text = re.sub(r'^\s*[-*_]{3,}\s*$', ' ', text, flags=re.MULTILINE)
    # Remove blockquotes >
    text = re.sub(r'^\s*>\s+', '', text, flags=re.MULTILINE)
    # Remove standalone dashes used as separators
    text = re.sub(r'(?<!\w)-(?!\w)', ' ', text)
    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


class RateLimiter:
    """Token-bucket rate limiter for per-session audio input.

    Prevents a single client from flooding the STT pipeline with more audio
    than is physically possible to speak. Default: 32KB/s (PCM16 at 16kHz mono)
    with a 2-second burst allowance — enough for normal microphone pre-buffering
    but not enough for a bulk-upload DoS attack (fix 0.6).
    """

    def __init__(self, bytes_per_sec: int = 32_000, burst_sec: float = 2.0):
        self.rate = bytes_per_sec
        self.capacity = bytes_per_sec * burst_sec
        self.tokens = self.capacity
        self.last = time.monotonic()

    def allow(self, n_bytes: int) -> bool:
        """Return True if n_bytes is within the current rate budget."""
        now = time.monotonic()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= n_bytes:
            self.tokens -= n_bytes
            return True
        return False


@dataclass
class TurnMetrics:
    """Latency tracking for a single pipeline turn.

    Extracted from PipelineSession to keep per-turn state self-contained
    and to declutter the session class (review item 2.1).
    """
    utterance_end_time: Optional[float] = None
    last_stt_ms: int = 0
    last_llm_ms: int = 0
    llm_queue_ms: int = 0
    llm_ttft_ms: int = 0
    llm_streaming_ms: int = 0
    tts_first_sentence_latency_ms: int = 0
    tts_metrics_sent: bool = False


class PipelineSession:
    """
    Manages one complete voice agent session.

    Owns: STT handler, LLM generator, TTS engine, conversation history,
    and the connection to the WebSocket.
    """

    def __init__(
        self,
        api_keys: Dict[str, str],
        model_config: Dict[str, Any],
        outbound_queue: asyncio.Queue[dict[str, Any] | None],
        subject: Optional[str] = None,
        tts_engine: str = "edge",
        llm_client: Optional[AsyncGroq] = None,
    ):
        self.api_keys = api_keys
        self.model_config = model_config
        self.tts_engine_choice = tts_engine
        self.outbound_queue = outbound_queue
        self._llm_client = llm_client

        self.tutor = TutorSession(subject=subject)
        self.stt_handler: Optional[STTHandler] = None
        self.llm_generator: Optional[LLMGenerator] = None
        self.tts_engine: Optional[TTSEngine] = None

        # Per-turn metrics (replaced on each new turn)
        self._metrics = TurnMetrics()

        # Interruption tracking
        self._final_turn_cutoff_time: Optional[float] = None

        # Turn tracking for interrupt safety
        self.turn_id: int = 0
        self._active_turn_id: Optional[int] = None

        # Prevent concurrent transcript processing
        self.is_processing_transcript = False
        self.processing_task: Optional[asyncio.Task] = None
        self._cancel_event = asyncio.Event()

        # Per-session audio rate limiter (fix 0.6)
        self._rate_limiter = RateLimiter()

        logger.info(f"[Pipeline] Session initialized (subject={self.tutor.subject}, tts_engine={tts_engine})")

    async def initialize(self):
        """Initialize all pipeline components."""
        self.stt_handler = STTHandler(
            api_key=self.api_keys["deepgram"],
            on_transcript=self._on_transcript_received,
            on_error=self._on_stt_error,
            on_status=self._on_stt_status,
            endpointing_ms=self.model_config.get("stt_endpointing_ms", 300),
        )
        self.llm_generator = LLMGenerator(
            api_key=self.api_keys["groq"],
            model=self.model_config["groq_model"],
            client=self._llm_client,
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
        """Forward audio bytes to the STT handler, subject to per-session rate limiting."""
        if not self.stt_handler:
            return
        if not self._rate_limiter.allow(len(audio_bytes)):
            logger.warning(
                f"[Pipeline] Audio rate limit exceeded ({len(audio_bytes)}B dropped)"
            )
            self.send_message({"type": "rate_limited", "message": "Audio rate limit exceeded"})
            return
        await self.stt_handler.send_audio(audio_bytes)

    def _can_send(self, turn_id: int) -> bool:
        """Return True if messages for this turn should still be delivered.

        Final atomic validation gate to prevent stale audio from reaching client.
        """
        if self._cancel_event.is_set():
            return False
        if turn_id != self._active_turn_id:
            return False
        return True


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
            self._final_turn_cutoff_time = time.perf_counter()
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
        self._metrics = TurnMetrics()

        self._metrics.utterance_end_time = time.perf_counter()
        if self.stt_handler:
            self._metrics.last_stt_ms = self.stt_handler.last_stt_processing_ms
        else:
            self._metrics.last_stt_ms = 0

        self.is_processing_transcript = True
        logger.info(
            f"[Pipeline] Turn {current_turn_id} transcript: '{transcript[:60]}' "
            f"(STT processing: {self._metrics.last_stt_ms}ms)"
        )

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._process_transcript(transcript, current_turn_id))
            self.processing_task = task

            _captured_task = task

            def _on_done(t):
                # Always clear is_processing_transcript if this task is still
                # the active one — prevents stuck state when a task is replaced
                # mid-flight (fix N8).
                if self.processing_task is _captured_task:
                    self.processing_task = None
                    self.is_processing_transcript = False
                if t.cancelled():
                    logger.info("[Pipeline] Processing task was cancelled")
                elif not t.cancelled() and t.exception() is not None:
                    logger.error(f"[Pipeline] Processing task failed: {t.exception()}")

            task.add_done_callback(_on_done)
        except RuntimeError:
            logger.error("[Pipeline] No running event loop")
            self.is_processing_transcript = False

    async def _process_transcript(self, transcript: str, turn_id: int):
        """Core pipeline: transcript → LLM streaming → TTS turn-batch → WebSocket."""
        full_response = ""
        first_token_received = False
        llm_generator = self.llm_generator
        tts_engine = self.tts_engine

        try:
            if llm_generator is None or tts_engine is None:
                raise RuntimeError("Pipeline components are not initialized")

            self._send_for_turn(turn_id, {"type": "transcript", "text": transcript})

            self.tutor.add_user_message(transcript)
            self.tutor.trim_history(max_turns=self.model_config.get("max_history_turns", 10))
            messages = cast(list[ChatCompletionMessageParam], self.tutor.get_messages())

            # sentence_queue carries clean sentence strings from the LLM generator.
            # None is the sentinel that signals the stream is done.
            # produce_sentences() puts sentences here as they arrive from the LLM —
            # NOT after all sentences are complete — so consume_audio() / TTS can
            # start synthesising the first sentence immediately (true streaming).
            _SENTINEL = None
            sentence_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

            # Hoisted above produce_sentences so the closure reference is valid
            # at definition time, not just at call time (fix BUG-01).
            full_response_parts: List[str] = []

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
                            if self._metrics.utterance_end_time:
                                # Compute LLM latency breakdown from generator timestamps
                                queue_latency_ms = 0
                                ttft_ms = 0
                                streaming_delay_ms = 0

                                if (llm_generator.t_request_sent and
                                        llm_generator.t_request_created):
                                    queue_latency_ms = int(
                                        (llm_generator.t_request_sent -
                                         llm_generator.t_request_created) * 1000
                                    )
                                    queue_latency_ms = max(0, min(queue_latency_ms, 30000))

                                if (llm_generator.t_first_token and
                                        llm_generator.t_request_sent):
                                    ttft_ms = int(
                                        (llm_generator.t_first_token -
                                         llm_generator.t_request_sent) * 1000
                                    )
                                    ttft_ms = max(0, min(ttft_ms, 30000))

                                if (llm_generator.t_first_sentence_emitted and
                                        llm_generator.t_first_token):
                                    streaming_delay_ms = int(
                                        (llm_generator.t_first_sentence_emitted -
                                         llm_generator.t_first_token) * 1000
                                    )
                                    streaming_delay_ms = max(0, min(streaming_delay_ms, 30000))

                                total_llm_ms = queue_latency_ms + ttft_ms + streaming_delay_ms
                                self._metrics.last_llm_ms = total_llm_ms
                                self._metrics.llm_queue_ms = queue_latency_ms
                                self._metrics.llm_ttft_ms = ttft_ms
                                self._metrics.llm_streaming_ms = streaming_delay_ms

                                logger.info(
                                    f"[Pipeline] LLM metrics: queue={queue_latency_ms}ms, "
                                    f"ttft={ttft_ms}ms, streaming={streaming_delay_ms}ms, "
                                    f"total={total_llm_ms}ms"
                                )

                                self._send_for_turn(turn_id, {
                                    "type": "llm_metrics",
                                    "queue_ms": queue_latency_ms,
                                    "ttft_ms": ttft_ms,
                                    "streaming_delay_ms": streaming_delay_ms,
                                    "total_ms": total_llm_ms,
                                })

                        self._send_for_turn(turn_id, {"type": "response_chunk", "text": sentence})

                        # Feed the clean sentence into the queue immediately so TTS
                        # can start synthesising it without waiting for the full LLM response.
                        clean_sentence = strip_markdown(sentence)
                        await sentence_queue.put(clean_sentence)
                        full_response_parts.append(sentence)

                    # Signal end-of-stream to consume_audio
                    await sentence_queue.put(_SENTINEL)
                except Exception as e:
                    logger.error(f"[Pipeline] LLM generator error: {e}")
                    await sentence_queue.put(_SENTINEL)  # unblock consume_audio
                    raise
                finally:
                    await gen.aclose()

            async def consume_audio() -> None:
                """Stream TTS audio as sentences arrive from the LLM.

                Calls tts.synthesise_streaming() which:
                  1. Drains sentence_queue, sending a Speak to Deepgram per sentence
                  2. Concurrently receives audio back from Deepgram
                  3. Sends Flush only after the sentinel is received (all sentences done)

                This means first audio arrives after the FIRST sentence is ready,
                not after the full LLM response — true streaming, no inter-sentence gaps.
                """
                first_audio_sent = False
                any_sentence_received = False
                try:
                    async for chunk in tts_engine.synthesise_streaming(
                        sentence_queue, timeout_sec=30, cancel_event=self._cancel_event
                    ):
                        if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                            logger.info("[Pipeline] TTS synthesis cancelled mid-turn")
                            break

                        if isinstance(chunk, dict) and chunk.get("type") == "tts_metadata":
                            any_sentence_received = True
                            latency_ms = chunk.get("latency_ms", 0)
                            if not isinstance(latency_ms, (int, float)) or latency_ms < 0 or latency_ms > 60000:
                                logger.warning(f"[Pipeline] Invalid TTS latency value: {latency_ms}ms, using 0")
                                latency_ms = 0
                            else:
                                latency_ms = int(latency_ms)

                            logger.debug(f"[Pipeline] TTS metadata received: latency={latency_ms}ms")
                            if not self._metrics.tts_first_sentence_latency_ms:
                                self._metrics.tts_first_sentence_latency_ms = latency_ms
                                if not self._metrics.tts_metrics_sent:
                                    self._metrics.tts_metrics_sent = True
                                    logger.info(f"[Pipeline] First TTS latency: {latency_ms}ms")
                                    self._send_for_turn(turn_id, {
                                        "type": "tts_metrics",
                                        "first_sentence_latency_ms": latency_ms,
                                        "engine": chunk.get("engine", "unknown"),
                                    })

                        elif isinstance(chunk, (bytes, bytearray, memoryview)):
                            if not self._cancel_event.is_set() and turn_id == self._active_turn_id:
                                if not first_audio_sent:
                                    first_audio_sent = True
                                    if self._metrics.utterance_end_time and self._can_send(turn_id):
                                        total_ms = int((time.perf_counter() - self._metrics.utterance_end_time) * 1000)
                                        llm_ms = self._metrics.last_llm_ms
                                        tts_ms = self._metrics.tts_first_sentence_latency_ms or 0
                                        stt_ms = self._metrics.last_stt_ms
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

                                # Route through _send_for_turn so _can_send() is
                                # evaluated before the message enters the outbound
                                # queue, closing the cancellation race window (fix P1-B).
                                self._send_for_turn(turn_id, {"type": "audio", "data": bytes(chunk)})

                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"[Pipeline] TTS synthesis error: {e}")
                    if self._can_send(turn_id):
                        self._send_for_turn(turn_id, {
                            "type": "tts_error",
                            "message": str(e),
                        })
                finally:
                    # Zero-sentence detection: if TTS never received any sentence,
                    # the LLM produced no output (fix 0.5)
                    if not any_sentence_received and self._can_send(turn_id):
                        logger.warning("[Pipeline] LLM generated no sentences for this turn")
                        self._send_for_turn(turn_id, {
                            "type": "error",
                            "message": "No response was generated. Please try again.",
                        })

            await asyncio.gather(produce_sentences(), consume_audio())

            # Build full_response from collected parts
            full_response = " ".join(full_response_parts).strip()

            # Save to history — even partial response from interrupted turns (fix 2.2)
            if full_response:
                self.tutor.add_assistant_message(full_response)
            elif not self._cancel_event.is_set():
                self.tutor.add_assistant_message("[No response generated]")

            logger.info(f"[Pipeline] Turn {turn_id} complete: '{full_response[:60]}'")

        except asyncio.CancelledError:
            logger.info("[Pipeline] Processing cancelled")
            # Save partial response even on interruption (fix 2.2)
            if full_response:
                self.tutor.add_assistant_message(full_response)
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
        self._final_turn_cutoff_time = time.perf_counter()

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
        # Wait a bit for the processing task to cancel before closing components
        if self.processing_task and not self.processing_task.done():
            try:
                await asyncio.wait_for(self.processing_task, timeout=0.5)
            except asyncio.TimeoutError:
                logger.warning("[Pipeline] Processing task didn't cancel within timeout during close")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[Pipeline] Error waiting for processing task to cancel: {e}")
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
