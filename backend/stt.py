"""
cascade/backend/stt.py

Speech-to-Text module using Deepgram Nova-2 with streaming.

Responsibility: Accept a raw audio stream and emit confirmed transcripts
when end-of-utterance is detected.

The utterance is emitted as a complete string (not streamed). The streaming
happens at the audio input level; the output is a single clean string that
feeds into the LLM.
"""

import asyncio
from typing import Callable, Optional
import logging
import contextlib
import time

from deepgram import (
    DeepgramClient,
)
from deepgram.core.events import EventType

logger = logging.getLogger(__name__)


class STTHandler:
    """
    Manages a Deepgram live transcription connection.
    
    Flow:
    1. Audio bytes arrive via send_audio()
    2. Forwarded to Deepgram
    3. Partial transcripts arrive continuously
    4. On utterance end, buffer is confirmed and full transcript emitted
    5. Caller receives confirmed transcript string via callback
    """

    def __init__(
        self,
        api_key: str,
        on_transcript: Callable[[str], None],
        on_error: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialise the STT handler.

        Args:
            api_key: Deepgram API key
            on_transcript: Callback called with confirmed transcript string
            on_error: Optional callback for errors
        """
        self.api_key = api_key
        self.on_transcript = on_transcript
        self.on_error = on_error or self._default_error_handler
        self.client: Optional[DeepgramClient] = None
        self.connection = None
        self._ctx: Optional[contextlib.AbstractContextManager] = None
        self._connect_lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._send_queue: Optional[asyncio.Queue[bytes]] = None
        self._sender_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_activity_monotonic = time.monotonic()
        self.transcript_buffer = ""
        self.is_open = False

    def _default_error_handler(self, error: str):
        """Default error handler logs to logger."""
        logger.error(f"[STT] {error}")

    async def _cleanup(self):
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
                error_msg = f"Failed to send audio: {str(e)}"
                logger.error(f"[STT] {error_msg}")
                self.on_error(error_msg)
                self.is_open = False
                return

    async def _keepalive_loop(self, interval_sec: float = 5.0):
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
                error_msg = f"Failed to keep alive: {str(e)}"
                logger.error(f"[STT] {error_msg}")
                self.on_error(error_msg)
                self.is_open = False
                return

    async def connect(self):
        """Initialize Deepgram client and open connection."""
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
                }

                self._ctx = self.client.listen.v1.connect(**options)
                self.connection = self._ctx.__enter__()

                def on_message(result):
                    try:
                        loop = self._loop
                        if loop:
                            loop.call_soon_threadsafe(self._process_transcript, result)
                    except Exception as e:
                        logger.error(f"[STT] Error in message callback: {e}")

                def on_error(error):
                    error_msg = f"Deepgram connection error: {error}"
                    logger.error(f"[STT] {error_msg}")
                    self.is_open = False
                    self.on_error(error_msg)
                    loop = self._loop
                    if loop:
                        loop.call_soon_threadsafe(lambda: asyncio.create_task(self._cleanup()))

                def on_close(_close_info):
                    logger.info("[STT] Deepgram connection closed")
                    self.is_open = False
                    loop = self._loop
                    if loop:
                        loop.call_soon_threadsafe(lambda: asyncio.create_task(self._cleanup()))

                self.connection.on(EventType.MESSAGE, on_message)
                self.connection.on(EventType.ERROR, on_error)
                self.connection.on(EventType.CLOSE, on_close)

                self._send_queue = asyncio.Queue(maxsize=200)
                self.is_open = True
                self._sender_task = asyncio.create_task(self._send_loop())
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())
                logger.info("[STT] Deepgram connection established")

            except Exception as e:
                error_msg = f"Failed to connect: {str(e)}"
                logger.error(f"[STT] {error_msg}")
                self.on_error(error_msg)
                await self._cleanup()
                raise

    async def send_audio(self, audio_bytes: bytes):
        """
        Send raw audio bytes to Deepgram.

        Args:
            audio_bytes: Raw PCM audio data
        """
        if not audio_bytes or len(audio_bytes) == 0:
            return

        if not self.connection or not self.is_open:
            with contextlib.suppress(Exception):
                await self.connect()
            if not self.connection or not self.is_open:
                return

        try:
            if not self._send_queue:
                return

            if self._send_queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._send_queue.get_nowait()

            self._send_queue.put_nowait(audio_bytes)
        except Exception as e:
            error_msg = f"Failed to send audio: {str(e)}"
            logger.error(f"[STT] {error_msg}")
            self.on_error(error_msg)
            self.is_open = False

    async def close(self):
        """Close the connection cleanly."""
        try:
            async with self._connect_lock:
                await self._cleanup()
            logger.info("[STT] Connection closed")
        except Exception as e:
            logger.error(f"[STT] Error closing connection: {e}")

    def _process_transcript(self, result):
        """
        Process transcript results from Deepgram.
        Called from the background thread via call_soon_threadsafe.
        """
        try:
            if not result or not hasattr(result, 'channel'):
                return

            transcript = result.channel.alternatives[0].transcript
            if not transcript:
                return

            is_final = result.is_final
            speech_final = result.speech_final

            if is_final:
                # Accumulate final transcripts
                self.transcript_buffer += " " + transcript if self.transcript_buffer else transcript
                logger.debug(f"[STT] Final: {transcript}")

            if speech_final and self.transcript_buffer:
                # Utterance complete
                confirmed = self.transcript_buffer.strip()
                logger.info(f"[STT] Utterance complete: '{confirmed}'")
                self.on_transcript(confirmed)
                self.transcript_buffer = ""

        except Exception as e:
            logger.error(f"[STT] Error processing transcript: {e}")
