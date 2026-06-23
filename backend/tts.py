"""
cascade/backend/tts.py

Text-to-Speech module supporting both Microsoft edge-tts (free) and Deepgram Aura (fast).

Responsibility: Accept sentence strings and stream audio bytes back to
the caller.

Latency Measurement:
  Each synthesise() call yields a metadata dict first, then audio chunks.
  Metadata includes:
    - t_tts_request_sent: time when API request is sent
    - t_first_audio_chunk: time when first audio byte is yielded
"""

import logging
import os
import time
import json
import asyncio
from typing import AsyncGenerator, Optional, Union, Dict, Any
from abc import ABC, abstractmethod

import edge_tts
import aiohttp

logger = logging.getLogger(__name__)


class BaseTTSEngine(ABC):
    """Abstract base class for TTS engines."""

    @abstractmethod
    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        """Convert text to speech and stream audio bytes.
        
        First yield: dict with metadata {"type": "tts_metadata", "t_tts_request_sent": ..., "t_first_audio_chunk": ...}
        Subsequent yields: bytes of audio data
        """
        yield {}

    async def close(self):
        """Clean up resources."""
        pass


class EdgeTTSEngine(BaseTTSEngine):
    """
    Manages text-to-speech using Microsoft edge-tts (free, no API key required).
    Streams raw MP3 audio bytes.
    """

    def __init__(self, voice: str = "en-US-AriaNeural"):
        self.voice = voice
        logger.info(f"[TTS] EdgeTTS Engine initialized with voice: {voice}")

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        if not text or not text.strip():
            logger.warning("[TTS] Empty text provided, skipping synthesis")
            return

        if len(text) > 2000:
            logger.warning(f"[TTS] Text too long ({len(text)} chars), truncating to 2000")
            text = text[:2000]

        try:
            logger.debug(f"[TTS] Synthesizing (Edge): {text[:60]}...")
            
            # Record request start time
            t_tts_request_sent = time.time()
            t_first_audio_chunk = None
            
            communicate = edge_tts.Communicate(text, self.voice)
            total_bytes = 0
            try:
                async with asyncio.timeout(timeout_sec):
                    async for chunk in communicate.stream():
                        if chunk.get("type") == "audio":
                            audio_data = chunk.get("data")
                            if audio_data:
                                # Record first audio chunk time (on first audio byte)
                                if t_first_audio_chunk is None:
                                    t_first_audio_chunk = time.time()
                                    # Yield metadata on first audio chunk
                                    yield {
                                        "type": "tts_metadata",
                                        "engine": "edge",
                                        "text": text[:60],
                                        "t_tts_request_sent": t_tts_request_sent,
                                        "t_first_audio_chunk": t_first_audio_chunk,
                                        "latency_ms": int((t_first_audio_chunk - t_tts_request_sent) * 1000),
                                    }
                                total_bytes += len(audio_data)
                                yield audio_data
            except Exception as e:
                logger.error(f"[TTS] EdgeTTS streaming error: {e}")
                raise

            if total_bytes:
                logger.info(f"[TTS] EdgeTTS audio complete: {total_bytes} bytes")

        except Exception as e:
            logger.error(f"[TTS] EdgeTTS synthesis error: {e}")
            raise


class DeepgramTTSEngine(BaseTTSEngine):
    """
    Manages text-to-speech using Deepgram Aura WebSocket streaming API.

    LAT-03 improvement: Uses a *persistent* WebSocket connection for the lifetime
    of the TTS engine (one per session). This eliminates the per-sentence WS
    handshake overhead (~20–50ms TCP/TLS + HTTP upgrade), which was the main
    remaining latency cost after switching from the REST API.

    Protocol (serial, one Speak+Flush in flight at a time):
      → {"type": "Speak", "text": "..."}
      → {"type": "Flush"}
      ← binary audio chunks (linear16 PCM)
      ← {"type": "Flushed"}   ← end of this sentence's audio

    asyncio.Lock serializes access so that concurrent pipeline sentences are
    queued rather than interleaved on the same connection.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "aura-asteria-en"):
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY")
        self.model = model
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_lock = asyncio.Lock()
        self._ws_url = (
            f"wss://api.deepgram.com/v1/speak"
            f"?model={model}&encoding=linear16&sample_rate=24000"
        )
        logger.info(f"[TTS] DeepgramTTS Engine initialized with model: {model}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                keepalive_timeout=60,  # Reuse TCP connections in the pool
                limit=4,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                trust_env=True,
            )
        return self._session

    async def _get_ws(self) -> aiohttp.ClientWebSocketResponse:
        """Return the persistent WS, reconnecting if it was closed or dirty."""
        if self._ws is None or self._ws.closed:
            session = await self._get_session()
            self._ws = await session.ws_connect(
                self._ws_url,
                headers={"Authorization": f"Token {self.api_key}"},
                heartbeat=30,        # Sends WS ping frames to keep connection alive
                receive_timeout=None,  # We control timeouts via asyncio.timeout()
            )
            logger.info("[TTS] Deepgram WS TTS persistent connection established")
        return self._ws

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        if not text or not text.strip():
            logger.warning("[TTS] Empty text provided, skipping synthesis")
            return

        if len(text) > 2000:
            logger.warning(f"[TTS] Text too long ({len(text)} chars), truncating to 2000")
            text = text[:2000]

        logger.debug(f"[TTS] Synthesizing (Deepgram WS persistent): {text[:60]}...")

        t_tts_request_sent = time.time()
        t_first_audio_chunk: Optional[float] = None

        # Serialize: only one Speak+Flush in flight at a time on this connection.
        # Lock is held across all yields — asyncio releases the event loop at each
        # yield point but the lock remains acquired until we break or raise.
        async with self._ws_lock:
            ws_completed_cleanly = False
            try:
                ws = await self._get_ws()

                await ws.send_json({"type": "Speak", "text": text})
                await ws.send_json({"type": "Flush"})

                async with asyncio.timeout(timeout_sec):
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            # First audio byte received — emit latency metadata
                            if t_first_audio_chunk is None:
                                t_first_audio_chunk = time.time()
                                yield {
                                    "type": "tts_metadata",
                                    "engine": "deepgram",
                                    "text": text[:60],
                                    "t_tts_request_sent": t_tts_request_sent,
                                    "t_first_audio_chunk": t_first_audio_chunk,
                                    "latency_ms": int(
                                        (t_first_audio_chunk - t_tts_request_sent) * 1000
                                    ),
                                }
                            yield bytes(msg.data)

                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue
                            msg_type = data.get("type")

                            if msg_type == "Flushed":
                                # All audio for this Flush has been sent — done.
                                # The WS stays open for the next sentence.
                                ws_completed_cleanly = True
                                break

                            elif msg_type == "Metadata":
                                # Deepgram sometimes sends Metadata before binary audio
                                if t_first_audio_chunk is None:
                                    t_first_audio_chunk = time.time()
                                    yield {
                                        "type": "tts_metadata",
                                        "engine": "deepgram",
                                        "text": text[:60],
                                        "t_tts_request_sent": t_tts_request_sent,
                                        "t_first_audio_chunk": t_first_audio_chunk,
                                        "latency_ms": int(
                                            (t_first_audio_chunk - t_tts_request_sent) * 1000
                                        ),
                                    }

                            elif msg_type == "Warning":
                                logger.warning(
                                    f"[TTS] Deepgram warning: {data.get('description')}"
                                )

                            elif msg_type == "Error":
                                self._ws = None  # Force reconnect
                                raise Exception(
                                    f"Deepgram TTS error: {data.get('description', 'unknown')}"
                                )

                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            logger.warning("[TTS] Deepgram WS closed unexpectedly")
                            self._ws = None
                            break

                if not ws_completed_cleanly:
                    # Connection ended before Flushed — mark dirty so next call reconnects
                    logger.warning("[TTS] Synthesis ended without Flushed — will reconnect")
                    self._ws = None

                logger.info("[TTS] DeepgramTTS sentence complete")

            except asyncio.CancelledError:
                # Task cancelled mid-synthesis (e.g., user interrupted). The WS is
                # in an unknown state — force a reconnect on the next sentence.
                logger.info("[TTS] Deepgram WS synthesis cancelled — reconnecting")
                self._ws = None
                raise

            except asyncio.TimeoutError:
                logger.error(f"[TTS] Deepgram WS synthesis timed out after {timeout_sec}s")
                self._ws = None
                raise

            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
            ) as e:
                logger.warning(f"[TTS] Deepgram WS connection error: {e} — reconnecting")
                self._ws = None
                raise

            except Exception as e:
                logger.error(f"[TTS] DeepgramTTS synthesis error: {e}")
                self._ws = None
                raise

    async def close(self):
        """Send a graceful Close to Deepgram and shut down the session."""
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_json({"type": "Close"})
                await asyncio.wait_for(self._ws.close(), timeout=2.0)
            except Exception:
                pass
            self._ws = None
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        logger.info("[TTS] DeepgramTTS engine closed")


class TTSEngine:
    """Wrapper that delegates to either EdgeTTSEngine or DeepgramTTSEngine based on config."""

    def __init__(
        self,
        engine: str = "edge",
        edge_voice: str = "en-US-AriaNeural",
        deepgram_api_key: Optional[str] = None,
        deepgram_model: str = "aura-asteria-en"
    ):
        if engine == "deepgram":
            self._engine: BaseTTSEngine = DeepgramTTSEngine(
                api_key=deepgram_api_key, model=deepgram_model
            )
            self.format = "linear16"
            self.sample_rate = 24000
        else:
            self._engine: BaseTTSEngine = EdgeTTSEngine(voice=edge_voice)
            self.format = "mp3"
            self.sample_rate = 24000

        logger.info(f"[TTS] TTSEngine wrapper initialized with: {engine}")

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        async for chunk in self._engine.synthesise(text, timeout_sec):
            yield chunk

    async def close(self):
        """Clean up resources used by the TTS engine."""
        await self._engine.close()
