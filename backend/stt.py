"""
cascade/backend/stt.py

Speech-to-Text module using Deepgram Nova-2 with streaming.

Responsibility: Accept a raw audio stream and emit confirmed transcripts
when end-of-utterance is detected.

Fixes applied:
  [M3] Added utterance_end event handler as a fallback trigger for transcript
       emission. Previously only speech_final was used — if Deepgram's
       endpointing didn't fire (e.g. abrupt silence), the transcript buffer
       would never be flushed. Now UtteranceEnd events also flush the buffer.
"""

import asyncio
import contextlib
import logging
import time
from typing import Callable, Optional

from deepgram import DeepgramClient
from deepgram.core.events import EventType

logger = logging.getLogger(__name__)


class STTHandler:
    """
    Manages a Deepgram live transcription connection.

    Flow:
    1. Audio bytes arrive via send_audio()
    2. Forwarded to Deepgram over a persistent WebSocket
    3. Partial transcripts arrive continuously (interim_results=True)
    4. On utterance end (speech_final OR UtteranceEnd), buffer is confirmed
       and the full transcript string is emitted to the caller via callback
    """

    def __init__(
        self,
        api_key: str,
        on_transcript: Callable[[str], None],
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.api_key = api_key
        self.on_transcript = on_transcript
        self.on_error = on_error or (lambda e: logger.error(f"[STT] {e}"))

        self.client: Optional[DeepgramClient] = None
        self.connection = None
        self._ctx = None
        self._connect_lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._send_queue: Optional[asyncio.Queue] = None
        self._sender_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_activity_monotonic = time.monotonic()

        self.transcript_buffer = ""
        self.is_open = False

    # ── Internal helpers ────────────────────────────────────────────────

    async def _cleanup(self):
        """Cancel background tasks and close the Deepgram connection."""
        sender_task = self._sender_task
        keepalive_task = self._keepalive_task
        self._sender_task = None
        self._keepalive_task = None

        for task in (sender_task, keepalive_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(Exception):
                    await task

        self._send_queue = None
        connection = self.connection
        self.connection = None
        ctx = self._ctx
        self._ctx = None
        self.is_open = False

        if connection:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(connection.send_close_stream)
        if ctx:
            with contextlib.suppress(Exception):
                ctx.__exit__(None, None, None)

    async def _send_loop(self):
        """Drain the send queue and forward audio to Deepgram sequentially."""
        if not self._send_queue:
            return
        while self.is_open:
            try:
                audio_bytes = await self._send_queue.get()
            except asyncio.CancelledError:
                return
            if not self.is_open or not self.connection:
                return
            try:
                await asyncio.to_thread(self.connection.send_media, audio_bytes)
                self._last_activity_monotonic = time.monotonic()
            except Exception as e:
                logger.error(f"[STT] send_media failed: {e}")
                self.on_error(str(e))
                self.is_open = False
                return

    async def _keepalive_loop(self, interval_sec: float = 5.0):
        """Send periodic keep-alive pings to prevent Deepgram from timing out."""
        while self.is_open:
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                return
            if not self.is_open or not self.connection:
                return
            idle_for = time.monotonic() - self._last_activity_monotonic
            if idle_for < interval_sec:
                continue
            try:
                await asyncio.to_thread(self.connection.send_keep_alive)
                self._last_activity_monotonic = time.monotonic()
            except Exception as e:
                logger.error(f"[STT] keep-alive failed: {e}")
                self.on_error(str(e))
                self.is_open = False
                return

    # ── Public API ──────────────────────────────────────────────────────

    async def connect(self):
        """Initialize the Deepgram client and open a live transcription connection."""
        async with self._connect_lock:
            if self.is_open and self.connection:
                return
            await self._cleanup()
            try:
                self._loop = asyncio.get_running_loop()
                self._last_activity_monotonic = time.monotonic()
                self.client = DeepgramClient(api_key=self.api_key)

                options = {
                    "model": "nova-2",
                    "language": "en-US",
                    "smart_format": True,
                    "encoding": "linear16",
                    "channels": 1,
                    "sample_rate": 16000,
                    "interim_results": True,
                    "endpointing": 700,
                    "vad_events": True,
                    "utterance_end_ms": 1000,
                }

                self._ctx = self.client.listen.v1.connect(**options)
                self.connection = self._ctx.__enter__()

                def on_message(result):
                    loop = self._loop
                    if loop:
                        loop.call_soon_threadsafe(
                            self._process_transcript, result
                        )

                def on_error(error):
                    logger.error(f"[STT] Deepgram error: {error}")
                    self.is_open = False
                    self.on_error(str(error))

                def on_close(_):
                    logger.info("[STT] Deepgram connection closed")
                    self.is_open = False

                self.connection.on(EventType.MESSAGE, on_message)
                self.connection.on(EventType.ERROR, on_error)
                self.connection.on(EventType.CLOSE, on_close)

                # ── FIX [M3]: UtteranceEnd fallback ────────────────────
                # Deepgram fires UtteranceEnd when vad_events=True and the
                # user stops speaking. This is a second path to flush the
                # transcript buffer in case speech_final doesn't arrive
                # (e.g. very short utterances, network hiccup).
                try:
                    utterance_end_type = EventType.UTTERANCE_END
                    def on_utterance_end(_event):
                        loop = self._loop
                        if loop:
                            loop.call_soon_threadsafe(
                                self._flush_buffer, "utterance_end"
                            )
                    self.connection.on(utterance_end_type, on_utterance_end)
                    logger.info("[STT] UtteranceEnd handler registered")
                except AttributeError:
                    # Older SDK versions may not have UTTERANCE_END — safe to skip
                    logger.info("[STT] UtteranceEnd not available in this SDK version")

                self._send_queue = asyncio.Queue(maxsize=200)
                self.is_open = True
                self._sender_task = asyncio.create_task(self._send_loop())
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())
                logger.info("[STT] Deepgram connection established")

            except Exception as e:
                logger.error(f"[STT] Connection failed: {e}")
                self.on_error(str(e))
                await self._cleanup()
                raise

    async def send_audio(self, audio_bytes: bytes):
        """Forward raw PCM16 audio bytes to Deepgram."""
        if not audio_bytes:
            return
        if not self.is_open or not self.connection:
            with contextlib.suppress(Exception):
                await self.connect()
            if not self.is_open:
                return
        try:
            if not self._send_queue:
                return
            if self._send_queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._send_queue.get_nowait()
            self._send_queue.put_nowait(audio_bytes)
        except Exception as e:
            logger.error(f"[STT] Failed to queue audio: {e}")
            self.on_error(str(e))
            self.is_open = False

    async def close(self):
        """Close the Deepgram connection and cancel background tasks."""
        async with self._connect_lock:
            await self._cleanup()
        logger.info("[STT] Connection closed")

    # ── Transcript processing ───────────────────────────────────────────

    def _flush_buffer(self, trigger: str):
        """
        Emit whatever is in the transcript buffer as a confirmed utterance.
        Called by both speech_final and utterance_end events.
        """
        if not self.transcript_buffer.strip():
            return
        confirmed = self.transcript_buffer.strip()
        logger.info(f"[STT] Utterance confirmed ({trigger}): '{confirmed}'")
        self.transcript_buffer = ""
        self.on_transcript(confirmed)

    def _process_transcript(self, result):
        """
        Process a transcript result from Deepgram.
        Called on the event loop thread via call_soon_threadsafe.
        """
        try:
            if not result or not hasattr(result, "channel"):
                return

            transcript = result.channel.alternatives[0].transcript
            if not transcript:
                return

            is_final = result.is_final
            speech_final = result.speech_final

            if is_final:
                if self.transcript_buffer:
                    self.transcript_buffer += " " + transcript
                else:
                    self.transcript_buffer = transcript
                logger.debug(f"[STT] is_final: '{transcript}'")

            # speech_final is the primary flush trigger
            if speech_final:
                self._flush_buffer("speech_final")

        except Exception as e:
            logger.error(f"[STT] Error processing transcript: {e}")
