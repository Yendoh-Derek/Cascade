"""
cascade/backend/pipeline.py

Core streaming pipeline orchestrator.

Responsibility: Wire STT → LLM → TTS together and manage the WebSocket
connection to the browser. Measure latency at each stage.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypeAlias, cast

from groq import AsyncGroq
from groq.types.chat import ChatCompletionMessageParam
from backend.math_speech import math_to_speech
from backend.stt import STTHandler
from backend.llm import LLMGenerator
from backend.tts import TTSEngine
from backend.tutor import TutorSession

logger = logging.getLogger(__name__)

MERGE_WINDOW_SEC = float(os.getenv("CASCADE_UTTERANCE_MERGE_SEC", "3.0"))

ChunkQueueItem: TypeAlias = bytes | Exception | None
ResponseQueueItem: TypeAlias = tuple[str, asyncio.Queue[ChunkQueueItem]] | Exception | None


def strip_markdown(text: str) -> str:
    """Strip common markdown formatting characters so TTS doesn't read them literally."""
    # Remove code blocks (fenced with ```)
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code (`code`)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove links [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove emphasis/strikethrough formatting markers while preserving the inner text,
    # ensuring we don't strip lone math asterisks or snake_case underscores.
    text = re.sub(r'(\*\*|__)(.*?)\1', r'\2', text)
    text = re.sub(r'(?<!\w)(\*|_)(?=\S)(.*?)(?<=\S)\1(?!\w)', r'\2', text)
    text = re.sub(r'~~(.*?)~~', r'\1', text)
    # Remove headers #, ##, etc.
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Replace horizontal rules (---, ***, ___) with space
    text = re.sub(r'^\s*[-*_]{3,}\s*$', ' ', text, flags=re.MULTILINE)
    # Remove blockquotes >
    text = re.sub(r'^\s*>\s+', '', text, flags=re.MULTILINE)
    # Remove standalone dashes used as separators (but preserve negative numbers like -3)
    text = re.sub(r'(?<!\w)(?<!\d)-(?!\w)(?!\d)', ' ', text)
    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _has_unclosed_markdown(text: str) -> bool:
    """True when text ends with a markdown construct that spans chunk boundaries."""
    temp = text
    while "**" in temp:
        start = temp.find("**")
        end = temp.find("**", start + 2)
        if end == -1:
            return True
        temp = temp[:start] + temp[end + 2:]
        
    while "__" in temp:
        start = temp.find("__")
        end = temp.find("__", start + 2)
        if end == -1:
            return True
        temp = temp[:start] + temp[end + 2:]

    if temp.count("*") % 2 == 1:
        return True
    
    if temp.count("_") % 2 == 1:
        return True

    if text.count("`") % 2 == 1:
        return True

    if re.search(r"\[[^\]]*$", text):
        return True
    if re.search(r"\[[^\]]+\]\([^)]*$", text):
        return True

    if text.count("~~") % 2 == 1:
        return True

    return False


class MarkdownStripper:
    """Stateful markdown stripper safe across streaming LLM chunk boundaries."""

    def __init__(self, stall_timeout: float = 0.5) -> None:
        self._buffer = ""
        self._unbalanced_since: Optional[float] = None
        self._stall_timeout = stall_timeout

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        if _has_unclosed_markdown(self._buffer):
            if self._unbalanced_since is None:
                self._unbalanced_since = time.perf_counter()
            elif time.perf_counter() - self._unbalanced_since > self._stall_timeout:
                logger.warning("[Pipeline] Markdown Stripper stall timeout: forcing flush")
                result = strip_markdown(self._buffer)
                self._buffer = ""
                self._unbalanced_since = None
                return result
            return ""
            
        result = strip_markdown(self._buffer)
        self._buffer = ""
        self._unbalanced_since = None
        return result

    def flush(self) -> str:
        result = strip_markdown(self._buffer)
        self._buffer = ""
        self._unbalanced_since = None
        return result


def _count_unescaped_dollars(text: str) -> int:
    count = 0
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "$":
            count += 1
    return count


class MathAwareChunkBuffer:
    """Buffers LLM chunks until math delimiters are balanced for safe conversion."""

    def __init__(self, stall_timeout: float = 0.5) -> None:
        self._pending = ""
        self._unbalanced_since: Optional[float] = None
        self._stall_timeout = stall_timeout

    def feed(self, chunk: str) -> str:
        self._pending += chunk
        if _count_unescaped_dollars(self._pending) % 2 == 1:
            if self._unbalanced_since is None:
                self._unbalanced_since = time.perf_counter()
            elif time.perf_counter() - self._unbalanced_since > self._stall_timeout:
                logger.warning("[Pipeline] Math Buffer stall timeout: forcing flush")
                ready, self._pending = self._pending, ""
                self._unbalanced_since = None
                return ready
            return ""
            
        ready, self._pending = self._pending, ""
        self._unbalanced_since = None
        return math_to_speech(ready)

    def flush(self) -> str:
        if not self._pending:
            return ""
        leftover, self._pending = self._pending, ""
        self._unbalanced_since = None
        if _count_unescaped_dollars(leftover) % 2 == 0:
            return math_to_speech(leftover)
        return leftover.replace("$", "")


# PCM16 mono @ 16 kHz — one 10 ms worklet frame is 320 bytes.
_PCM16_16K_MONO_BPS = 32_000

class RateLimiter:
    """Token-bucket rate limiter for per-session audio input.

    Prevents a single client from flooding the STT pipeline with more audio
    than is physically possible to speak. Default: 32KB/s (PCM16 at 16kHz mono)
    with a 5-second burst allowance (absorbs startup backlog after init).
    """

    def __init__(
        self,
        bytes_per_sec: int = _PCM16_16K_MONO_BPS,
        burst_sec: float = 5.0,
    ):
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
    """Latency tracking for a single pipeline turn."""
    utterance_end_time: Optional[float] = None
    last_stt_tail_ms: int = 0
    stt_endpointing_ms: int = 0
    last_llm_ms: int = 0
    llm_queue_ms: int = 0
    llm_ttft_ms: int = 0
    llm_streaming_ms: int = 0
    llm_retry_ms: int = 0
    tts_first_chunk_latency_ms: int = 0
    tts_metrics_sent: bool = False
    was_speculative: bool = False
    superseded_during_grace: bool = False


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
        tts_engine: str = "deepgram",
        llm_client: Optional[AsyncGroq] = None,
    ):
        self.api_keys = api_keys
        self.model_config = model_config
        self.tts_engine_choice = tts_engine
        self.outbound_queue = outbound_queue
        self._llm_client = llm_client

        self.tutor = TutorSession()
        self.stt_handler: Optional[STTHandler] = None
        self.llm_generator: Optional[LLMGenerator] = None
        self.tts_engine: Optional[TTSEngine] = None

        # Per-turn metrics (replaced on each new turn)
        self._metrics = TurnMetrics()

        # Interruption tracking
        self._final_turn_cutoff_time: Optional[float] = None

        # AI speaking state for dynamic VAD thresholding
        self._ai_speaking: bool = False
        self._playback_finished_time: Optional[float] = None

        # Turn tracking for interrupt safety
        self.turn_id: int = 0
        self._active_turn_id: Optional[int] = None

        # Prevent concurrent transcript processing
        self.is_processing_transcript = False
        self.processing_task: Optional[asyncio.Task] = None
        self._cancel_event = asyncio.Event()

        # Utterance merge: combine split speech_final fragments into one turn
        self._inflight_transcript: Optional[str] = None
        self._pending_merge_text: Optional[str] = None
        self._pending_merge_at: Optional[float] = None
        self._pending_merge_turn_id: Optional[int] = None

        # Per-session audio rate limiter
        self._rate_limiter = RateLimiter()
        # Bypass rate limiting briefly after init while draining WS/mic backlog.
        self._rate_limit_grace_until: float = 0.0
        self._last_rate_limit_notify: float = 0.0
        self._rate_limit_notify_interval_sec: float = 5.0

        self.has_spoken: bool = False

        logger.info(f"[Pipeline] Session initialized (tts_engine={tts_engine})")

    async def initialize(self):
        """Initialize all pipeline components."""
        self.stt_handler = STTHandler(
            api_key=self.api_keys["deepgram"],
            on_transcript=self._on_transcript_received,
            on_error=self._on_stt_error,
            on_status=self._on_stt_status,
            on_speech_interrupted=self._on_vad_interrupted,
            on_transcript_update=self._on_stt_update,
            on_speculative_transcript=self._on_speculative_transcript,
            is_ai_speaking=self.is_ai_speaking,
            model=self.model_config.get("deepgram_model", "nova-3"),
            language=self.model_config.get("deepgram_language", "en-US"),
            endpointing_ms=self.model_config.get("stt_endpointing_ms", 300),
            vad_threshold=self.model_config.get("vad_threshold", 0.5),
            vad_silence_ms=self.model_config.get("vad_silence_ms", 200),
            vad_min_speech_frames=self.model_config.get("vad_min_speech_frames", 3),
            enable_speculative_llm=self.model_config.get("enable_speculative_llm", False),
            speculative_stability_matches=self.model_config.get("speculative_stability_matches", 2),
        )
        await self.stt_handler.prepare_vad()
        self.llm_generator = LLMGenerator(
            api_key=self.api_keys["groq"],
            model=self.model_config["groq_model"],
            client=self._llm_client,
        )
        self.tts_engine = TTSEngine(
            engine=self.tts_engine_choice,
            edge_voice=self.model_config.get("edge_tts_voice", "en-US-AriaNeural"),
            deepgram_api_key=self.api_keys["deepgram"],
            deepgram_model=self.model_config.get("deepgram_tts_model", "aura-2-asteria-en")
        )

        # Send TTS config to frontend
        self.send_message({
            "type": "tts_config",
            "format": self.tts_engine.format,
            "sample_rate": self.tts_engine.sample_rate,
            "sampleRate": self.tts_engine.sample_rate
        })

        await self.stt_handler.connect()
        # Mic frames accumulate while initialize() runs; allow a short grace
        # window so the post-init backlog is not mistaken for abuse.
        self._rate_limit_grace_until = time.monotonic() + 4.0
        logger.info("[Pipeline] All components initialized and ready")

    async def handle_audio(self, audio_bytes: bytes):
        """Forward audio bytes to the STT handler, subject to per-session rate limiting."""
        if not self.stt_handler:
            return
        if time.monotonic() >= self._rate_limit_grace_until:
            if not self._rate_limiter.allow(len(audio_bytes)):
                logger.debug(
                    "[Pipeline] Audio rate limit exceeded (%sB dropped)",
                    len(audio_bytes),
                )
                now = time.monotonic()
                if (
                    now - self._last_rate_limit_notify
                    >= self._rate_limit_notify_interval_sec
                ):
                    self._last_rate_limit_notify = now
                    logger.warning(
                        "[Pipeline] Audio rate limit exceeded — throttling client"
                    )
                    self.send_message(
                        {
                            "type": "rate_limited",
                            "message": "Audio rate limit exceeded",
                        }
                    )
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

    def _stash_pending_merge(self, text: Optional[str], from_turn_id: Optional[int] = None) -> None:
        if text and text.strip():
            self._pending_merge_text = text.strip()
            self._pending_merge_at = time.perf_counter()
            if from_turn_id is not None:
                self._pending_merge_turn_id = from_turn_id

    def _clear_pending_merge(self) -> None:
        self._pending_merge_text = None
        self._pending_merge_at = None
        self._pending_merge_turn_id = None

    def _maybe_merge_with_pending(self, new_text: str) -> str:
        if (
            self._pending_merge_text
            and self._pending_merge_at is not None
            and (time.perf_counter() - self._pending_merge_at) < MERGE_WINDOW_SEC
        ):
            merged = f"{self._pending_merge_text} {new_text}".strip()
            self._clear_pending_merge()
            logger.info(f"[Pipeline] Merged split utterance: '{merged[:80]}'")
            return merged
        self._clear_pending_merge()
        return new_text

    def _on_stt_update(self, stable: str, tentative: str):
        """
        Live word-by-word streaming of the user's speech to the UI.
        Does NOT trigger the LLM.
        """
        self.send_message({"type": "transcript_update", "stable": stable, "tentative": tentative})
        self.has_spoken = True

    def _on_vad_interrupted(self, transcript: str):
        """
        Called when Silero VAD detects the user is speaking (local silence ended).
        Immediately cancels any in-progress AI response so the user is not talking
        over the AI. Does NOT trigger the LLM - that waits for Deepgram speech_final.
        STT buffer is cleared only on confirmed barge-in (AI actively playing back).
        """
        if self._active_turn_id is None and not self.is_processing_transcript:
            return

        self.has_spoken = True

        is_barge_in = self.is_ai_speaking()
        if is_barge_in:
            logger.info("[Pipeline] VAD barge-in - interrupting AI playback")
            if self.stt_handler:
                self.stt_handler.clear_buffer()
                self.stt_handler.last_stt_tail_ms = -1
        else:
            logger.info(
                "[Pipeline] VAD speech during turn processing - "
                "cancelling without clearing STT buffer"
            )
            self._stash_pending_merge(
                self._inflight_transcript, from_turn_id=self._active_turn_id
            )

        if is_barge_in:
            self._clear_pending_merge()

        old_turn = self._active_turn_id
        self._cancel_active_turn_tasks()
        if old_turn is not None:
            self.send_message({"type": "turn_cancelled", "turn_id": old_turn})
        self._active_turn_id = None
        self.is_processing_transcript = False
        self.processing_task = None

    def _on_speculative_transcript(self, transcript: str):
        """
        Speculative pipeline trigger from VAD + stable interim transcript.
        Treated identically to a confirmed transcript by the existing turn
        machinery. If Deepgram's speech_final later arrives with a correction,
        it supersedes this turn cleanly via the existing 'newest wins' logic.
        """
        logger.info(f"[Pipeline] Speculative trigger (VAD+stable): '{transcript[:60]}'")
        self._on_transcript_received(transcript, was_speculative=True)

    def set_ai_speaking(self, is_speaking: bool):
        """Update AI speaking state based on frontend playback signals."""
        self._ai_speaking = is_speaking
        if not is_speaking:
            self._playback_finished_time = time.perf_counter()

    def is_ai_speaking(self) -> bool:
        """Returns True if AI is currently speaking OR within the ~200ms grace period."""
        if self._ai_speaking:
            return True
        # Provide a 200ms grace period after playback finishes as a floor
        if self._playback_finished_time and (time.perf_counter() - self._playback_finished_time < 0.2):
            return True
        return False

    def _on_transcript_received(self, transcript: str, was_speculative: bool = False):
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

        stripped = transcript.strip()

        if self.is_processing_transcript or self._active_turn_id is not None:
            if getattr(self._metrics, "was_speculative", False) and stripped and stripped == (self._inflight_transcript or "").strip():
                logger.info(f"[Pipeline] Final transcript matches speculative precisely. Continuing turn {self._active_turn_id}.")
                return

            self._stash_pending_merge(
                self._inflight_transcript, from_turn_id=self._active_turn_id
            )
            old_turn = self._active_turn_id
            logger.info(f"[Pipeline] Turn {old_turn} cancelled by new transcript")
            self._final_turn_cutoff_time = time.perf_counter()
            self._cancel_active_turn_tasks()
            if old_turn is not None:
                self.send_message({"type": "turn_cancelled", "turn_id": old_turn})
            self.is_processing_transcript = False
            self.processing_task = None

        transcript = self._maybe_merge_with_pending(stripped)
        if not transcript:
            logger.info("[Pipeline] Empty transcript after merge — resetting client")
            if not self.is_processing_transcript:
                self.send_message({"type": "response_end"})
            return

        self.turn_id += 1
        self._active_turn_id = self.turn_id
        current_turn_id = self.turn_id

        self._cancel_event.clear()

        # Reset metrics for new turn
        self._metrics = TurnMetrics()
        self._metrics.was_speculative = was_speculative

        self._metrics.utterance_end_time = time.perf_counter()
        if self.stt_handler:
            self._metrics.last_stt_tail_ms = self.stt_handler.last_stt_tail_ms
            self._metrics.stt_endpointing_ms = self.stt_handler.endpointing_ms
        else:
            self._metrics.last_stt_tail_ms = 0
            self._metrics.stt_endpointing_ms = 0

        self.is_processing_transcript = True
        self._inflight_transcript = transcript
        logger.info(
            f"[Pipeline] Turn {current_turn_id} transcript: '{transcript[:60]}' "
            f"(STT tail: {self._metrics.last_stt_tail_ms}ms, endpointing: {self._metrics.stt_endpointing_ms}ms)"
        )

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._process_transcript(transcript, current_turn_id, was_speculative)
            )
            self.processing_task = task

            _captured_task = task

            def _on_done(t):
                # Always clear is_processing_transcript if this task is still
                # the active one.
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

    async def _process_transcript(
        self, transcript: str, turn_id: int, was_speculative: bool = False
    ):
        """Core pipeline: transcript → LLM streaming → TTS turn-batch → WebSocket."""
        full_response = ""
        full_response_parts: List[str] = []
        first_token_received = False
        llm_generator = self.llm_generator
        tts_engine = self.tts_engine

        try:
            if llm_generator is None or tts_engine is None:
                raise RuntimeError("Pipeline components are not initialized")

            self._send_for_turn(turn_id, {"type": "transcript", "text": transcript})

            grace_ms = 0 if was_speculative else self.model_config.get("speculative_grace_ms", 180)
            if transcript.strip() and transcript.strip()[-1] in ".?!":
                grace_ms = 0
            if grace_ms > 0:
                try:
                    await asyncio.wait_for(
                        self._cancel_event.wait(),
                        timeout=grace_ms / 1000,
                    )
                    # Cancel event fired during grace window — newer transcript incoming
                    self._metrics.superseded_during_grace = True
                    logger.info(f"[Pipeline] Turn {turn_id} superseded during grace window")
                    self._stash_pending_merge(transcript, from_turn_id=turn_id)
                    return
                except asyncio.TimeoutError:
                    pass   # Grace window elapsed with no interruption — proceed normally

            self.tutor.add_user_message(transcript)
            self.tutor.trim_history(max_turns=self.model_config.get("max_history_turns", 10))
            messages = cast(list[ChatCompletionMessageParam], self.tutor.get_messages())

            # chunk_queue carries clean string chunks from the LLM generator.
            # None is the sentinel that signals the stream is done.
            # produce_chunks() puts chunks here as they arrive from the LLM —
            # NOT after all sentences are complete — so consume_audio() / TTS can
            # start synthesising the first chunk immediately (true streaming).
            _SENTINEL = None
            chunk_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

            # Hoisted above produce_chunks so the closure reference is valid
            # at definition time, not just at call time.
            response_chunk_buffer: List[str] = []
            BATCH_THRESHOLD = 5
            BATCH_TIMEOUT = 0.08  # 80ms
            batch_timer_task: Optional[asyncio.Task] = None

            async def flush_response_chunks():
                nonlocal response_chunk_buffer, batch_timer_task
                if not response_chunk_buffer:
                    return
                # Combine into one message
                combined_text = "".join(response_chunk_buffer)
                self._send_for_turn(turn_id, {"type": "response_chunk", "text": combined_text})
                response_chunk_buffer = []
                # Cancel timer if we're flushing early
                if batch_timer_task and not batch_timer_task.done():
                    batch_timer_task.cancel()
                    batch_timer_task = None

            async def produce_chunks() -> None:
                nonlocal first_token_received, batch_timer_task
                stall_timeout = self.model_config.get("buffer_stall_ms", 500) / 1000.0
                markdown_stripper = MarkdownStripper(stall_timeout=stall_timeout)
                math_buffer = MathAwareChunkBuffer(stall_timeout=stall_timeout)
                gen = llm_generator.generate(
                    messages=messages,
                    timeout_sec=30,
                )
                try:
                    async for chunk in gen:
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

                                if (llm_generator.t_first_attempt_sent and
                                        self._metrics.utterance_end_time):
                                    queue_latency_ms = int(
                                        (llm_generator.t_first_attempt_sent -
                                         self._metrics.utterance_end_time) * 1000
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

                                retry_ms = llm_generator.retry_ms
                                total_llm_ms = queue_latency_ms + ttft_ms + streaming_delay_ms
                                self._metrics.last_llm_ms = total_llm_ms
                                self._metrics.llm_queue_ms = queue_latency_ms
                                self._metrics.llm_ttft_ms = ttft_ms
                                self._metrics.llm_streaming_ms = streaming_delay_ms
                                self._metrics.llm_retry_ms = retry_ms

                                logger.info(
                                    f"[Pipeline] LLM metrics: queue={queue_latency_ms}ms, "
                                    f"ttft={ttft_ms}ms, streaming={streaming_delay_ms}ms, "
                                    f"retry={retry_ms}ms, total={total_llm_ms}ms"
                                )

                                self._send_for_turn(turn_id, {
                                    "type": "llm_metrics",
                                    "queue_ms": queue_latency_ms,
                                    "ttft_ms": ttft_ms,
                                    "streaming_delay_ms": streaming_delay_ms,
                                    "retry_ms": retry_ms,
                                    "total_ms": total_llm_ms,
                                })

                        response_chunk_buffer.append(chunk)
                        # Start timer on first chunk in buffer
                        if len(response_chunk_buffer) == 1 and not batch_timer_task:
                            async def timer_callback():
                                await asyncio.sleep(BATCH_TIMEOUT)
                                await flush_response_chunks()
                            batch_timer_task = asyncio.create_task(timer_callback())
                        # Flush early if buffer reaches threshold
                        if len(response_chunk_buffer) >= BATCH_THRESHOLD:
                            await flush_response_chunks()

                        # Feed the clean chunk into the queue immediately so TTS
                        # can start synthesising it without waiting for the full LLM response.
                        clean_chunk = markdown_stripper.feed(chunk)
                        if clean_chunk:
                            speakable = math_buffer.feed(clean_chunk)
                            if speakable:
                                await chunk_queue.put(speakable)
                        full_response_parts.append(chunk)

                    remaining = markdown_stripper.flush()
                    if remaining:
                        speakable = math_buffer.feed(remaining)
                        if speakable:
                            await chunk_queue.put(speakable)
                    final_speakable = math_buffer.flush()
                    if final_speakable:
                        await chunk_queue.put(final_speakable)
                    # Flush any remaining response chunks
                    await flush_response_chunks()
                    # Signal end-of-stream to consume_audio
                    await chunk_queue.put(_SENTINEL)
                except Exception as e:
                    logger.error(f"[Pipeline] LLM generator error: {e}")
                    await chunk_queue.put(_SENTINEL)  # unblock consume_audio
                    raise
                finally:
                    await gen.aclose()
                    # Cancel any pending batch timer so it doesn't fire as an
                    # orphaned task after this coroutine is cancelled mid-turn.
                    if batch_timer_task and not batch_timer_task.done():
                        batch_timer_task.cancel()

            async def consume_audio() -> None:
                """Stream TTS audio as chunks arrive from the LLM.

                Calls tts.synthesise_streaming() which:
                  1. Drains chunk_queue, sending a Speak to Deepgram per chunk
                  2. Concurrently receives audio back from Deepgram
                  3. Sends Flush only after the sentinel is received (all chunks done)

                This means first audio arrives after the FIRST chunk is ready,
                not after the full LLM response — true streaming, no inter-chunk gaps.
                """
                first_audio_sent = False
                any_chunk_received = False
                audio_buffer = bytearray()
                AUDIO_CHUNK_MIN_SIZE = 4096

                def flush_audio_buffer():
                    if audio_buffer and self._active_turn_id == turn_id and not self._cancel_event.is_set():
                        # Route through _send_for_turn so _can_send() is
                        # evaluated before the message enters the outbound queue.
                        self._send_for_turn(turn_id, {"type": "audio", "data": bytes(audio_buffer)})
                    audio_buffer.clear()

                try:
                    async for chunk in tts_engine.synthesise_streaming(
                        chunk_queue, timeout_sec=30, cancel_event=self._cancel_event
                    ):
                        if self._cancel_event.is_set() or turn_id != self._active_turn_id:
                            logger.info("[Pipeline] TTS synthesis cancelled mid-turn")
                            break

                        if isinstance(chunk, dict) and chunk.get("type") == "tts_metadata":
                            any_chunk_received = True
                            latency_ms = chunk.get("latency_ms", 0)
                            if not isinstance(latency_ms, (int, float)) or latency_ms < 0 or latency_ms > 60000:
                                logger.warning(f"[Pipeline] Invalid TTS latency value: {latency_ms}ms, using 0")
                                latency_ms = 0
                            else:
                                latency_ms = int(latency_ms)

                            logger.debug(f"[Pipeline] TTS metadata received: latency={latency_ms}ms")
                            if not self._metrics.tts_first_chunk_latency_ms:
                                self._metrics.tts_first_chunk_latency_ms = latency_ms
                                if not self._metrics.tts_metrics_sent:
                                    self._metrics.tts_metrics_sent = True
                                    logger.info(f"[Pipeline] First TTS latency: {latency_ms}ms")
                                    self._send_for_turn(turn_id, {
                                        "type": "tts_metrics",
                                        "first_chunk_latency_ms": latency_ms,
                                        "engine": chunk.get("engine", "unknown"),
                                    })

                        elif isinstance(chunk, (bytes, bytearray, memoryview)):
                            if not self._cancel_event.is_set() and turn_id == self._active_turn_id:
                                audio_buffer.extend(chunk)
                                if not first_audio_sent:
                                    first_audio_sent = True
                                    self.set_ai_speaking(True)
                                    flush_audio_buffer()   # send immediately; don't wait for 4096 bytes
                                    if self._metrics.utterance_end_time and self._can_send(turn_id):
                                        total_ms = int((time.perf_counter() - self._metrics.utterance_end_time) * 1000)
                                        llm_ms = self._metrics.last_llm_ms
                                        tts_ms = self._metrics.tts_first_chunk_latency_ms or 0
                                        stt_tail_ms = self._metrics.last_stt_tail_ms
                                        stt_endpointing_ms = self._metrics.stt_endpointing_ms
                                        logger.info(
                                            f"[Pipeline] First audio sent: {total_ms}ms "
                                            f"(STT tail: {stt_tail_ms}ms, endpointing: {stt_endpointing_ms}ms, LLM: {llm_ms}ms, TTS: {tts_ms}ms)"
                                        )
                                        self._send_for_turn(turn_id, {
                                            "type": "latency",
                                            "total_ms": total_ms,
                                            "llm_ms": llm_ms,
                                            "tts_ms": tts_ms,
                                            "stt_tail_ms": stt_tail_ms,
                                            "endpointing_ms": stt_endpointing_ms,
                                            "ms": total_ms,
                                            "was_speculative": self._metrics.was_speculative,
                                        })
                                elif len(audio_buffer) >= AUDIO_CHUNK_MIN_SIZE:
                                    flush_audio_buffer()

                    # Flush any remaining audio after the stream ends
                    flush_audio_buffer()

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
                    # Zero-chunk detection: if TTS never received any chunk,
                    # the LLM produced no output.
                    # Only show the error if the turn was not intentionally cancelled
                    # (e.g. user spoke again or interrupted the AI).
                    if not any_chunk_received and self._can_send(turn_id) and not self._cancel_event.is_set():
                        logger.warning("[Pipeline] LLM generated no text chunks for this turn")
                        self._send_for_turn(turn_id, {
                            "type": "error",
                            "message": "No response was generated. Please try again.",
                        })

            await asyncio.gather(produce_chunks(), consume_audio())

            # Build full_response from collected parts
            full_response = "".join(full_response_parts).strip()

            # Save to history — even partial response from interrupted turns
            if full_response:
                self.tutor.add_assistant_message(full_response)

            logger.info(f"[Pipeline] Turn {turn_id} complete: '{full_response[:60]}'")

        except asyncio.CancelledError:
            logger.info("[Pipeline] Processing cancelled")
            # Save partial response even on interruption
            full_response = "".join(full_response_parts).strip()
            if full_response:
                self.tutor.add_assistant_message(full_response)
            else:
                # Remove orphaned user message to prevent back-to-back user turns
                if self.tutor.history and self.tutor.history[-1].get("role") == "user":
                    self.tutor.history.pop()
        except Exception as e:
            logger.error(f"[Pipeline] Unexpected error: {e}")
            if self._can_send(turn_id):
                self.send_message({"type": "error", "message": f"Pipeline error: {e}", "turn_id": turn_id})
        finally:
            if self._can_send(turn_id):
                self._send_for_turn(turn_id, {"type": "response_end"})
                self._clear_pending_merge()
            elif self._pending_merge_turn_id != turn_id:
                self._clear_pending_merge()
            if self._active_turn_id == turn_id:
                self._active_turn_id = None
                self._inflight_transcript = None

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

        self.set_ai_speaking(False)
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
