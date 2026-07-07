"""
cascade/backend/stt.py

Speech-to-Text module using a raw WebSocket connection to the Deepgram API.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import aiohttp

from backend.vad import SileroVAD

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
        on_speech_interrupted: Optional[Callable[[str], None]] = None,
        on_transcript_update: Optional[Callable[[str], None]] = None,
        on_speculative_transcript: Optional[Callable[[str], None]] = None,
        is_ai_speaking: Optional[Callable[[], bool]] = None,
        model: str = "nova-2",
        language: str = "en-US",
        endpointing_ms: int = 300,
        vad_threshold: float = 0.5,
        vad_silence_ms: int = 200,
        vad_min_speech_frames: int = 3,
        enable_speculative_llm: bool = False,
        speculative_stability_matches: int = 2,
    ):
        self.api_key = api_key
        self.on_transcript = on_transcript
        self.on_error = on_error or (lambda e: logger.error(f"[STT] {e}"))
        self.on_status = on_status
        self.on_speech_interrupted = on_speech_interrupted
        self.on_transcript_update = on_transcript_update
        self.on_speculative_transcript = on_speculative_transcript
        self.is_ai_speaking = is_ai_speaking or (lambda: False)
        self.model = model
        self.language = language
        self.endpointing_ms = endpointing_ms
        self.enable_speculative_llm = enable_speculative_llm
        self.speculative_stability_matches = speculative_stability_matches

        self._vad: Optional[SileroVAD] = None
        self._vad_threshold = vad_threshold
        self._vad_silence_ms = vad_silence_ms
        self._vad_min_speech_frames = vad_min_speech_frames
        self._latest_interim: str = ""
        self._recent_interims: list[str] = []
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.is_open = False
        self.transcript_buffer = ""
        self._listen_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing_intentionally = False
        self._vad_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._vad_worker_task: Optional[asyncio.Task] = None

        self._last_audio_sent_time: Optional[float] = None
        self._utterance_start_time: Optional[float] = None
        # Timestamp of the last is_final result with actual content.
        # Used to measure endpointing latency (last recognized speech → speech_final)
        # rather than speaking duration. Typically ≈ 300ms (the endpointing window).
        self._last_speech_time: Optional[float] = None
        self.last_stt_tail_ms: int = 0

    async def prepare_vad(self):
        """Load per-session Silero state off the event loop (deepcopy is sync)."""
        if self._vad is None:
            self._vad = await asyncio.to_thread(
                SileroVAD,
                self._vad_threshold,
                self._vad_silence_ms,
                self._vad_min_speech_frames,
            )

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
            "endpointing": str(self.endpointing_ms),  # configurable via CASCADE_STT_ENDPOINTING
            "vad_events": "true",
            "utterance_end_ms": "1000",
        }
        return base_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    async def _get_or_create_session(self) -> aiohttp.ClientSession:
        """Return the existing aiohttp session, creating one if needed.

        Reuses the session across reconnects to avoid recreating the TCP
        connection pool on every STT WebSocket reconnect.
        """
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(trust_env=True)
        return self.session

    async def _establish_connection(self):
        """Open WebSocket and start listener/keepalive tasks."""
        session = await self._get_or_create_session()
        headers = {"Authorization": f"Token {self.api_key}"}
        self.ws = await session.ws_connect(
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
                        self.is_open = False  # Prevents duplicate reconnect schedule
                        if not self._closing_intentionally:
                            await self._schedule_reconnect()
                    break
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

                    if transcript:
                        if not self.transcript_buffer.strip() and self._utterance_start_time is None:
                            self._utterance_start_time = time.perf_counter()

                        if not speech_final:
                            # Update speech time on ALL transcripts (interim or final)
                            # EXCEPT the one bundled with speech_final.
                            self._last_speech_time = time.perf_counter()

                        # Stream live interim words to the UI (not is_final to avoid
                        # showing words twice — once as interim, once committed to buffer).
                        if self.on_transcript_update and not is_final:
                            live_text = (self.transcript_buffer + " " + transcript).strip()
                            if live_text:
                                self.on_transcript_update(live_text)

                    if transcript and not is_final:
                        self._latest_interim = transcript
                        self._recent_interims.append(transcript)
                        if len(self._recent_interims) > 4:  # Small buffer to prevent unbounded growth
                            self._recent_interims.pop(0)

                    if transcript and is_final:
                        self.transcript_buffer = (
                            self.transcript_buffer + " " + transcript
                            if self.transcript_buffer
                            else transcript
                        )
                        self._latest_interim = ""
                        if self.on_transcript_update and self.transcript_buffer.strip():
                            self.on_transcript_update(self.transcript_buffer.strip())
                        logger.debug(f"[STT] is_final: '{transcript}'")

                    if speech_final:
                        self._flush_buffer("speech_final")

            elif msg_type == "UtteranceEnd":
                self._flush_buffer("utterance_end")

            elif msg_type == "SpeechStarted":
                if self._utterance_start_time is None:
                    self._utterance_start_time = time.perf_counter()

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
            self._last_audio_sent_time = time.perf_counter()
            await self.ws.send_bytes(audio_bytes)

            # VAD inference is CPU-bound — queue chunks for a single worker so
            # Silero's mutable recurrent state is never touched concurrently.
            self._ensure_vad_worker()
            self._vad_queue.put_nowait(audio_bytes)
        except RuntimeError as e:
            self.is_open = False
            logger.warning(f"[STT] Transport closing mid-send: {e}")
            if not self._closing_intentionally:
                await self._schedule_reconnect()
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

    def _ensure_vad_worker(self):
        if self._vad_worker_task is None or self._vad_worker_task.done():
            self._vad_worker_task = asyncio.create_task(self._vad_worker())

    async def _vad_worker(self):
        """Serialize VAD inference off-thread; one chunk at a time per session."""
        while True:
            audio_bytes = await self._vad_queue.get()
            try:
                if audio_bytes is None:
                    break
                if not self.is_open or self._closing_intentionally:
                    continue
                if self._vad is None:
                    continue
                events = await asyncio.to_thread(
                    self._vad.feed, audio_bytes, self.is_ai_speaking()
                )
                if not self.is_open or self._closing_intentionally:
                    continue
                if "speech_started" in events:
                    self._on_vad_speech_started()
                if "speech_stopped" in events and self.enable_speculative_llm:
                    self._on_vad_speech_stopped_speculative()
            except Exception as e:
                logger.debug(f"[STT] VAD processing error: {e}")
            finally:
                self._vad_queue.task_done()

    async def _stop_vad_worker(self):
        worker = self._vad_worker_task
        if worker is None or worker.done():
            self._vad_worker_task = None
            return
        try:
            await self._vad_queue.put(None)
            try:
                await asyncio.wait_for(worker, timeout=2.0)
            except asyncio.TimeoutError:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug(f"[STT] VAD worker error during cancel: {e}")
        except Exception as e:
            logger.debug(f"[STT] VAD worker shutdown error: {e}")
        finally:
            self._vad_worker_task = None

    def _on_vad_speech_started(self):
        """
        Fires the instant Silero detects the user has started speaking.
        Signals the pipeline to cancel any in-progress AI response immediately
        (barge-in). Does NOT trigger the LLM - Deepgram speech_final handles that.
        Buffer mutation is deferred to the pipeline once it confirms a real interrupt.
        """
        if not self.on_speech_interrupted:
            return
        self.on_speech_interrupted("")

    def _on_vad_speech_stopped_speculative(self):
        """
        Fires when Silero detects the user has gone quiet.
        Speculatively starts the LLM if the interim transcript has been stable.
        """
        if not self.on_speculative_transcript:
            return
        if not self._is_transcript_stable():
            return
        text = (self.transcript_buffer + " " + self._latest_interim).strip()
        if text:
            self.on_speculative_transcript(text)

    def _is_transcript_stable(self) -> bool:
        """True if the last required_matches interim results were identical."""
        required = self.speculative_stability_matches
        return (
            len(self._recent_interims) >= required
            and len(set(self._recent_interims[-required:])) == 1
        )

    def clear_buffer(self):
        """Reset accumulated transcript buffer (e.g. after user interruption)."""
        self.transcript_buffer = ""
        self._latest_interim = ""
        self._recent_interims.clear()
        self._utterance_start_time = None
        if self._vad is not None:
            self._vad.reset()

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

        await self._stop_vad_worker()

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
        full_transcript = (self.transcript_buffer + " " + self._latest_interim).strip()
        if not full_transcript:
            return

        # Consume both buffers now to prevent a subsequent UtteranceEnd from
        # double-flushing the same text (e.g. after speech_final already fired).
        self.transcript_buffer = ""
        self._latest_interim = ""
        self._recent_interims.clear()

        now = time.perf_counter()
        if self._last_speech_time is not None:
            tail = (now - self._last_speech_time) * 1000 - self.endpointing_ms
            self.last_stt_tail_ms = max(0, int(tail))
        else:
            # UtteranceEnd fired without prior interim content — no reliable anchor.
            self.last_stt_tail_ms = 0

        self._utterance_start_time = None
        self._last_speech_time = None
        self._last_audio_sent_time = None

        logger.info(
            f"[STT] Utterance confirmed ({trigger}): '{full_transcript}' "
            f"(stt tail processing: {self.last_stt_tail_ms}ms, "
            f"endpointing window: {self.endpointing_ms}ms)"
        )
        self.on_transcript(full_transcript)

