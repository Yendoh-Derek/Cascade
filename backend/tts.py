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
    Manages text-to-speech using Deepgram Aura (fast, low-latency).
    Streams raw linear16 PCM audio bytes at 24kHz sample rate.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "aura-asteria-en"):
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY")
        self.model = model
        self._session: Optional[aiohttp.ClientSession] = None
        logger.info(f"[TTS] DeepgramTTS Engine initialized with model: {model}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[Union[Dict[str, Any], bytes], None]:
        if not text or not text.strip():
            logger.warning("[TTS] Empty text provided, skipping synthesis")
            return

        if len(text) > 2000:
            logger.warning(f"[TTS] Text too long ({len(text)} chars), truncating to 2000")
            text = text[:2000]

        try:
            logger.debug(f"[TTS] Synthesizing (Deepgram): {text[:60]}...")
            
            # Record request start time
            t_tts_request_sent = time.time()
            t_first_audio_chunk = None
            
            url = f"https://api.deepgram.com/v1/speak?model={self.model}&encoding=linear16&sample_rate=24000"
            headers = {
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "application/json",
            }
            data = {"text": text}

            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=timeout_sec)
            async with session.post(url, json=data, headers=headers, timeout=timeout) as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_chunked(4096):
                    if chunk:
                        # Record first audio chunk time (on first audio byte)
                        if t_first_audio_chunk is None:
                            t_first_audio_chunk = time.time()
                            # Yield metadata on first audio chunk
                            yield {
                                "type": "tts_metadata",
                                "engine": "deepgram",
                                "text": text[:60],
                                "t_tts_request_sent": t_tts_request_sent,
                                "t_first_audio_chunk": t_first_audio_chunk,
                                "latency_ms": int((t_first_audio_chunk - t_tts_request_sent) * 1000),
                            }
                        yield chunk

            logger.info("[TTS] DeepgramTTS audio complete")

        except Exception as e:
            logger.error(f"[TTS] DeepgramTTS synthesis error: {e}")
            raise


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
