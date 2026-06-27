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

Architecture — Deepgram Speak-many / Flush-once:
  pipeline.py uses synthesise_streaming() which reads sentences from a queue
  and sends Speak messages as they arrive, followed by a single Flush when
  the queue ends (None sentinel). This eliminates per-sentence audio
  finalization gaps while still enabling true streaming (no need to wait
  for all sentences before starting TTS).

  All exception handlers that previously set self._ws = None now first
  attempt to send a Clear message and close the socket gracefully. This
  stops Deepgram from continuing to synthesize audio after an interruption,
  eliminating a wasted API compute leak.
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

        First yield: dict with metadata {"type": "tts_metadata", ...}
        Subsequent yields: bytes of audio data
        """
        yield {}

    async def synthesise_streaming(
        self,
        sentence_queue: asyncio.Queue,
        timeout_sec: int = 30,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        """Synthesise sentences from a queue as they arrive (true streaming).

        Default implementation (used by EdgeTTSEngine): drains the queue and
        calls synthesise() per sentence sequentially. Subclasses override this
        to implement Speak-many/Flush-once patterns.

        The queue should contain str sentences with a None sentinel at the end.
        """
        while True:
            sentence = await sentence_queue.get()
            if sentence is None:   # sentinel — stream is done
                break
            if cancel_event and cancel_event.is_set():
                break
            async for chunk in self.synthesise(sentence, timeout_sec):
                yield chunk

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
            t_tts_request_sent = time.perf_counter()
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
                                    t_first_audio_chunk = time.perf_counter()
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

    Protocol — Speak-many / Flush-once:
      synthesise_turn() sends N Speak messages (one per sentence), then a
      single Flush. Deepgram returns audio for all sentences as one continuous
      stream, terminated by a single Flushed event. This avoids per-sentence
      finalization gaps that the previous Speak+Flush-per-sentence pattern caused.

    asyncio.Lock serializes access so that concurrent pipeline turns are
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

    async def _clear_and_close_ws(self):
        """Send Clear + close before discarding the WS reference.

        Calling Clear tells Deepgram to stop synthesizing immediately, which
        avoids wasting API quota on audio that has already been discarded
        client-side (e.g., on interruption).
        """
        ws = self._ws
        self._ws = None
        if ws is not None and not ws.closed:
            try:
                await ws.send_json({"type": "Clear"})
                await asyncio.wait_for(ws.close(), timeout=1.0)
            except Exception:
                pass

    async def synthesise_streaming(
        self,
        sentence_queue: asyncio.Queue,
        timeout_sec: int = 30,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        """
        True streaming synthesis: feed sentences to Deepgram as they arrive
        from the LLM, and receive audio back concurrently.

        Architecture:
          - A feeder task reads from sentence_queue, sending one Speak per
            sentence to the Deepgram WS as soon as each sentence is available.
            When the sentinel (None) is received, it sends Flush.
          - The main coroutine (this generator) reads binary audio from the
            Deepgram WS concurrently with the feeder.
          - Audio starts flowing from Deepgram after the FIRST Speak is sent
            (not after all sentences are ready), preserving sub-second TTFA.
          - A single Flush means no inter-sentence audio gaps.

        This is the correct Speak-many/Flush-once pattern with true streaming.
        """
        t_tts_request_sent: Optional[float] = None
        t_first_audio_chunk: Optional[float] = None
        first_sentence_text = ""
        ws = None
        ws_completed_cleanly = False
        feeder_task: Optional[asyncio.Task] = None
        needs_reconnect = False

        pending_exc: Optional[BaseException] = None

        async with self._ws_lock:
            try:
                ws = await self._get_ws()

                async def feeder():
                    """Drain sentence_queue, sending Speak per sentence, Flush at end."""
                    nonlocal first_sentence_text, t_tts_request_sent
                    while True:
                        sentence = await sentence_queue.get()
                        if sentence is None:   # sentinel
                            await ws.send_json({"type": "Flush"})
                            break
                        if cancel_event and cancel_event.is_set():
                            await ws.send_json({"type": "Flush"})
                            break
                        sentence = sentence.strip()
                        if not sentence:
                            continue
                        if len(sentence) > 2000:
                            sentence = sentence[:2000]
                        if not first_sentence_text:
                            first_sentence_text = sentence[:60]
                        if t_tts_request_sent is None:
                            t_tts_request_sent = time.perf_counter()
                        await ws.send_json({"type": "Speak", "text": sentence})

                feeder_task = asyncio.create_task(feeder())

                # Receive audio from Deepgram while feeder is still sending sentences
                async with asyncio.timeout(timeout_sec):
                    async for msg in ws:
                        if cancel_event and cancel_event.is_set():
                            break

                        if msg.type == aiohttp.WSMsgType.BINARY:
                            if t_first_audio_chunk is None:
                                t_first_audio_chunk = time.perf_counter()
                                yield {
                                    "type": "tts_metadata",
                                    "engine": "deepgram",
                                    "text": first_sentence_text,
                                    "t_tts_request_sent": t_tts_request_sent or time.perf_counter(),
                                    "t_first_audio_chunk": t_first_audio_chunk,
                                    "latency_ms": int(
                                        (t_first_audio_chunk - (t_tts_request_sent or t_first_audio_chunk)) * 1000
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
                                ws_completed_cleanly = True
                                break
                            elif msg_type == "Warning":
                                logger.warning(
                                    f"[TTS] Deepgram warning: {data.get('description')}"
                                )
                            elif msg_type == "Error":
                                needs_reconnect = True
                                raise Exception(
                                    f"Deepgram TTS error: {data.get('description', 'unknown')}"
                                )

                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            logger.warning("[TTS] Deepgram WS closed unexpectedly")
                            needs_reconnect = True
                            break

                if not ws_completed_cleanly:
                    logger.warning("[TTS] Streaming synthesis ended without Flushed — reconnecting")
                    needs_reconnect = True

            except asyncio.CancelledError as e:
                logger.info("[TTS] Deepgram streaming synthesis cancelled — sending Clear")
                needs_reconnect = True
                pending_exc = e
            except asyncio.TimeoutError as e:
                logger.error(f"[TTS] Deepgram streaming synthesis timed out after {timeout_sec}s")
                needs_reconnect = True
                pending_exc = e
            except (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError) as e:
                logger.warning(f"[TTS] Deepgram WS connection error: {e} — reconnecting")
                needs_reconnect = True
                pending_exc = e
            except Exception as e:
                logger.error(f"[TTS] Deepgram streaming synthesis error: {e}")
                needs_reconnect = True
                pending_exc = e
            finally:
                if feeder_task and not feeder_task.done():
                    feeder_task.cancel()
                    try:
                        await feeder_task
                    except (asyncio.CancelledError, Exception):
                        pass

        # Do slow WS cleanup outside the lock so new turns can proceed immediately
        if needs_reconnect or not ws_completed_cleanly:
            await self._clear_and_close_ws()

        if pending_exc is not None:
            raise pending_exc

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        """Synthesise a single sentence via the streaming queue interface."""
        q: asyncio.Queue = asyncio.Queue()
        await q.put(text)
        await q.put(None)   # sentinel
        async for chunk in self.synthesise_streaming(q, timeout_sec):
            yield chunk

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
        self._engine: BaseTTSEngine
        valid_engines = {"deepgram", "edge"}
        if engine not in valid_engines:
            raise ValueError(f"Invalid TTS engine: {engine}. Must be one of {valid_engines}")
        if engine == "deepgram":
            self._engine = DeepgramTTSEngine(
                api_key=deepgram_api_key, model=deepgram_model
            )
            self.format = "linear16"
            self.sample_rate = 24000
        else:
            self._engine = EdgeTTSEngine(voice=edge_voice)
            self.format = "mp3"
            self.sample_rate = 24000

        logger.info(f"[TTS] TTSEngine wrapper initialized with: {engine}")

    async def synthesise(
        self, text: str, timeout_sec: int = 15
    ) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        async for chunk in self._engine.synthesise(text, timeout_sec):
            yield chunk

    async def synthesise_streaming(
        self,
        sentence_queue: asyncio.Queue,
        timeout_sec: int = 30,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        """True streaming synthesis — sentences fed as they arrive, audio received concurrently."""
        async for chunk in self._engine.synthesise_streaming(
            sentence_queue, timeout_sec, cancel_event
        ):
            yield chunk

    async def close(self):
        """Clean up resources used by the TTS engine."""
        await self._engine.close()
