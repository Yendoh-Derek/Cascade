"""
cascade/backend/stt.py

Speech-to-Text module using raw WebSocket connection to Deepgram API.
No SDK dependency issues!
"""

import asyncio
import json
import logging
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)


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
    ):
        self.api_key = api_key
        self.on_transcript = on_transcript
        self.on_error = on_error or (lambda e: logger.error(f"[STT] {e}"))

        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.is_open = False
        self.transcript_buffer = ""
        self._listen_task: Optional[asyncio.Task] = None

    async def connect(self):
        """Open an async WebSocket connection to Deepgram."""
        try:
            # Build Deepgram WebSocket URL with parameters
            base_url = "wss://api.deepgram.com/v1/listen"
            params = {
                "model": "nova-2",
                "language": "en-US",
                "smart_format": "true",
                "encoding": "linear16",
                "channels": "1",
                "sample_rate": "16000",
                "interim_results": "true",
                "endpointing": "700",
                "vad_events": "true",
                "utterance_end_ms": "1000",
            }
            url = base_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())

            # Create session and connect
            self.session = aiohttp.ClientSession()
            headers = {"Authorization": f"Token {self.api_key}"}
            self.ws = await self.session.ws_connect(url, headers=headers)
            self.is_open = True
            logger.info("[STT] Deepgram WebSocket connection established")

            # Start listening task
            self._listen_task = asyncio.create_task(self._listen())

        except Exception as e:
            logger.error(f"[STT] Connection failed: {e}")
            self.on_error(str(e))
            await self.close()
            raise

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
                    logger.warning(f"[STT] Unexpected binary message from Deepgram")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    if self.ws:
                        logger.error(f"[STT] WebSocket error: {self.ws.exception()}")
                        self.on_error(str(self.ws.exception()))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    logger.info("[STT] WebSocket closed")
                    self.is_open = False
                    break
        except Exception as e:
            logger.error(f"[STT] Listen task failed: {e}")
            self.on_error(str(e))
            self.is_open = False

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

                    # ── FIX [Bug 3]: Move speech_final check outside transcript guard ──
                    # Deepgram can emit speech_final with empty transcript. When it does,
                    # the buffer has content from earlier is_final messages that needs
                    # flushing. Previously, speech_final was only checked inside
                    # if transcript:, causing the flush to be skipped.
                    if transcript and is_final:
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

            elif msg_type == "Error":
                err_msg = data.get("description", "Unknown error")
                logger.error(f"[STT] Deepgram error: {err_msg}")
                self.on_error(err_msg)

        except Exception as e:
            logger.error(f"[STT] Error handling message: {e}")

    async def send_audio(self, audio_bytes: bytes):
        """
        Forward raw PCM16 audio bytes to Deepgram.
        """
        if not audio_bytes or not self.ws or not self.is_open:
            return
        try:
            await self.ws.send_bytes(audio_bytes)
        except Exception as e:
            logger.error(f"[STT] send failed: {e}")
            self.on_error(str(e))
            self.is_open = False

    async def close(self):
        """Close the WebSocket connection cleanly."""
        self.is_open = False

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

        if self.session:
            try:
                await self.session.close()
            except Exception as e:
                logger.debug(f"[STT] Session close error: {e}")
            self.session = None

        logger.info("[STT] Connection closed")

    # ── Internal ────────────────────────────────────────────────────────

    def _flush_buffer(self, trigger: str):
        """Emit the accumulated transcript buffer as a confirmed utterance."""
        confirmed = self.transcript_buffer.strip()
        if not confirmed:
            return
        self.transcript_buffer = ""
        logger.info(f"[STT] Utterance confirmed ({trigger}): '{confirmed}'")
        self.on_transcript(confirmed)
