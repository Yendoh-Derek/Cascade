"""
cascade/backend/stt.py

Speech-to-Text module using raw WebSocket connection to Deepgram API.
No SDK dependency issues!
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS = 3


class STTHandler:
    """
    Manages an async WebSocket connection to Deepgram's live transcription API.

    Flow:
    1. Audio bytes arrive via send_audio() — forwarded directly to Deepgram
    2. Deepgram streams partial transcripts (interim_results=True)
    3. On speech_final, buffer is flushed and transcript emitted via callback
    4. UtteranceEnd event used as fallback flush if available
    """

    def __init__(
        self,
        api_key: str,
        on_transcript: Callable[[str], None],
        on_error: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[str, dict], None]] = None,
        model: str = "nova-2",
        language: str = "en-US",
    ):
        self.api_key = api_key
        self.on_transcript = on_transcript
        self.on_error = on_error or (lambda e: logger.error(f"[STT] {e}"))
        self.on_status = on_status
        self.model = model
        self.language = language

        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.is_open = False
        self.transcript_buffer = ""
        self._listen_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing_intentionally = False

        self._last_audio_sent_time: Optional[float] = None
        self._utterance_start_time: Optional[float] = None
        self.last_stt_processing_ms: int = 0

    def _build_ws_url(self) -> str:
        base_url = "wss://api.deepgram.com/v1/listen"
        params = {
            "model": self.model,
            "language": self.language,
            "smart_format": "true",
            "encoding": "linear16",
            "channels": "1",
            "sample_rate": "16000",
            "interim_results": "true",
            "endpointing": "300",
            "vad_events": "true",
        }
        return base_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    async def _establish_connection(self):
        """Open WebSocket and start listener/keepalive tasks."""
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = aiohttp.ClientSession(trust_env=True)

        headers = {"Authorization": f"Token {self.api_key}"}
        self.ws = await self.session.ws_connect(
            self._build_ws_url(),
            headers=headers,
            heartbeat=10.0,
            receive_timeout=None,
        )
        self.is_open = True
        logger.info("[STT] Deepgram WebSocket connection established")

        self._listen_task = asyncio.create_task(self._listen())
        self._keepalive_task = asyncio.create_task(self._keepalive())

    async def connect(self):
        """Open an async WebSocket connection to Deepgram."""
        self._closing_intentionally = False
        try:
            await self._establish_connection()
        except Exception as e:
            logger.error(f"[STT] Connection failed: {e}")
            self.on_error(str(e))
            await self.close()
            raise

    async def _stop_tasks_and_ws(self):
        """Cancel listener/keepalive and close the WebSocket without ending the session."""
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[STT] Keepalive task cancel error: {e}")
            self._keepalive_task = None

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[STT] Listen task cancel error: {e}")
            self._listen_task = None

        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"[STT] WebSocket close error: {e}")
            self.ws = None

        self.is_open = False

    async def _schedule_reconnect(self):
        """Attempt to reconnect after an unexpected connection drop."""
        if self._closing_intentionally:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Reconnect with exponential backoff (1s, 2s, 4s)."""
        self.clear_buffer()
        await self._stop_tasks_and_ws()

        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            if self._closing_intentionally:
                return

            delay = 2 ** (attempt - 1)
            if self.on_status:
                self.on_status(
                    "stt_reconnecting",
                    {"attempt": attempt, "max": MAX_RECONNECT_ATTEMPTS},
                )
            logger.info(f"[STT] Reconnect attempt {attempt}/{MAX_RECONNECT_ATTEMPTS} in {delay}s")
            await asyncio.sleep(delay)

            if self._closing_intentionally:
                return

            try:
                await self._establish_connection()
                if self.on_status:
                    self.on_status("stt_reconnected", {})
                logger.info("[STT] Reconnected to Deepgram")
                return
            except Exception as e:
                logger.warning(f"[STT] Reconnect attempt {attempt} failed: {e}")
                await self._stop_tasks_and_ws()

        self.on_error("STT connection lost — please start a new session")

    async def _listen(self):
        """Listen for messages from Deepgram."""
        try:
            if not self.ws:
                return
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_message(data)
                    except json.JSONDecodeError as e:
                        logger.error(f"[STT] Invalid JSON from Deepgram: {e}")
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    logger.warning("[STT] Unexpected binary message from Deepgram")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    if self.ws:
                        logger.error(f"[STT] WebSocket error: {self.ws.exception()}")
                        if not self._closing_intentionally:
                            await self._schedule_reconnect()
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    close_data = getattr(msg, "data", None)
                    logger.warning(f"[STT] WebSocket closed by server (data={close_data})")
                    self.is_open = False
                    if not self._closing_intentionally:
                        await self._schedule_reconnect()
                    break

            if self.is_open and not self._closing_intentionally:
                logger.warning("[STT] _listen loop exited unexpectedly")
                self.is_open = False
                await self._schedule_reconnect()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[STT] Listen task failed: {e}")
            self.is_open = False
            if not self._closing_intentionally:
                await self._schedule_reconnect()

    async def _keepalive(self):
        """Send a KeepAlive message to Deepgram every 10 seconds to prevent inactivity timeouts."""
        try:
            while self.is_open and self.ws:
                await asyncio.sleep(10)
                if self.is_open and self.ws and not self.ws.closed:
                    logger.debug("[STT] Sending KeepAlive to Deepgram")
                    await self.ws.send_json({"type": "KeepAlive"})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[STT] KeepAlive task error: {e}")

    async def _handle_message(self, data: dict):
        """Handle a message from Deepgram."""
        try:
            msg_type = data.get("type")

            if msg_type == "Results":
                channel = data.get("channel", {})
                alternatives = channel.get("alternatives", [])
                if alternatives:
                    alt = alternatives[0]
                    transcript = alt.get("transcript", "")
                    is_final = data.get("is_final", False)
                    speech_final = data.get("speech_final", False)

                    if transcript and is_final:
                        if not self.transcript_buffer.strip():
                            self._utterance_start_time = time.time()
                        self.transcript_buffer = (
                            self.transcript_buffer + " " + transcript
                            if self.transcript_buffer
                            else transcript
                        )
                        logger.debug(f"[STT] is_final: '{transcript}'")

                    if speech_final:
                        self._flush_buffer("speech_final")

            elif msg_type == "UtteranceEnd":
                self._flush_buffer("utterance_end")

            elif msg_type == "SpeechStarted":
                if self._utterance_start_time is None:
                    self._utterance_start_time = time.time()

            elif msg_type == "Error":
                err_msg = data.get("description", "Unknown error")
                logger.error(f"[STT] Deepgram error: {err_msg}")
                self.on_error(err_msg)

        except Exception as e:
            logger.error(f"[STT] Error handling message: {e}")

    async def send_audio(self, audio_bytes: bytes):
        """Forward raw PCM16 audio bytes to Deepgram."""
        if not audio_bytes or not self.ws or not self.is_open or self.ws.closed:
            return
        try:
            self._last_audio_sent_time = time.time()
            await self.ws.send_bytes(audio_bytes)
        except RuntimeError as e:
            self.is_open = False
            logger.warning(f"[STT] Transport closing mid-send (expected during shutdown): {e}")
        except ConnectionResetError:
            self.is_open = False
            logger.warning("[STT] Connection reset by peer")
            if not self._closing_intentionally:
                await self._schedule_reconnect()
        except Exception as e:
            logger.error(f"[STT] Unexpected send error: {e}")
            self.is_open = False
            if not self._closing_intentionally:
                await self._schedule_reconnect()
            else:
                self.on_error(str(e))

    def clear_buffer(self):
        """Reset accumulated transcript buffer (e.g. after user interruption)."""
        self.transcript_buffer = ""
        self._utterance_start_time = None

    async def finalize(self):
        """Send a Finalize message to Deepgram to flush any remaining audio buffer."""
        if self.ws and self.is_open and not self.ws.closed:
            logger.info("[STT] Sending Finalize command to Deepgram")
            try:
                await self.ws.send_json({"type": "Finalize"})
            except Exception as e:
                logger.error(f"[STT] Error sending Finalize command: {e}")

    async def close(self):
        """Close the WebSocket connection cleanly."""
        self._closing_intentionally = True

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[STT] Reconnect task cancel error: {e}")
            self._reconnect_task = None

        await self._stop_tasks_and_ws()

        if self.session:
            try:
                await self.session.close()
            except Exception as e:
                logger.debug(f"[STT] Session close error: {e}")
            self.session = None

        logger.info("[STT] Connection closed")

    def _flush_buffer(self, trigger: str):
        """Emit the accumulated transcript buffer as a confirmed utterance."""
        confirmed = self.transcript_buffer.strip()
        if not confirmed:
            return

        if self._utterance_start_time is not None:
            self.last_stt_processing_ms = int(
                (time.time() - self._utterance_start_time) * 1000
            )
        elif self._last_audio_sent_time is not None:
            self.last_stt_processing_ms = int(
                (time.time() - self._last_audio_sent_time) * 1000
            )
        else:
            self.last_stt_processing_ms = 0
        self._utterance_start_time = None
        self._last_audio_sent_time = None

        self.transcript_buffer = ""
        logger.info(
            f"[STT] Utterance confirmed ({trigger}): '{confirmed}' "
            f"(processing: {self.last_stt_processing_ms}ms)"
        )
        self.on_transcript(confirmed)
