"""
cascade/backend/tts.py

Text-to-Speech module supporting both Microsoft edge-tts (free) and Deepgram Aura (fast).

Responsibility: Accept sentence strings and stream audio bytes back to
the caller.
"""

import logging
import asyncio
import os
from typing import AsyncGenerator, Optional
from abc import ABC, abstractmethod

import edge_tts
import aiohttp

logger = logging.getLogger(__name__)


class BaseTTSEngine(ABC):
    """Abstract base class for TTS engines."""

    @abstractmethod
    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[bytes, None]:
        """Convert text to speech and stream audio bytes."""
        pass


class EdgeTTSEngine(BaseTTSEngine):
    """
    Manages text-to-speech using Microsoft edge-tts (free, no API key required).
    Streams raw MP3 audio bytes.
    """

    def __init__(self, voice: str = "en-US-AriaNeural"):
        self.voice = voice
        logger.info(f"[TTS] EdgeTTS Engine initialized with voice: {voice}")

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[bytes, None]:
        if not text or not text.strip():
            logger.warning("[TTS] Empty text provided, skipping synthesis")
            return

        if len(text) > 2000:
            logger.warning(f"[TTS] Text too long ({len(text)} chars), truncating to 2000")
            text = text[:2000]

        try:
            logger.debug(f"[TTS] Synthesizing (Edge): {text[:60]}...")
            communicate = edge_tts.Communicate(text, self.voice)
            audio_data_list = []
            try:
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio":
                        audio_data = chunk.get("data")
                        if audio_data:
                            audio_data_list.append(audio_data)
            except Exception as e:
                logger.error(f"[TTS] EdgeTTS streaming error: {e}")
                raise

            if audio_data_list:
                complete_audio = b"".join(audio_data_list)
                logger.info(f"[TTS] EdgeTTS audio complete: {len(complete_audio)} bytes")
                yield complete_audio

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
        logger.info(f"[TTS] DeepgramTTS Engine initialized with model: {model}")

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[bytes, None]:
        if not text or not text.strip():
            logger.warning("[TTS] Empty text provided, skipping synthesis")
            return

        if len(text) > 2000:
            logger.warning(f"[TTS] Text too long ({len(text)} chars), truncating to 2000")
            text = text[:2000]

        try:
            logger.debug(f"[TTS] Synthesizing (Deepgram): {text[:60]}...")
            url = f"https://api.deepgram.com/v1/speak?model={self.model}&encoding=linear16&sample_rate=24000"
            headers = {
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "application/json",
            }
            data = {"text": text}

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, headers=headers, timeout=timeout_sec) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.content.iter_chunked(4096):
                        if chunk:
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

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[bytes, None]:
        async for chunk in self._engine.synthesise(text, timeout_sec):
            yield chunk
