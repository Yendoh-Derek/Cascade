"""
cascade/backend/tts.py

Text-to-Speech module using Microsoft edge-tts (free, no API key required).

Responsibility: Accept sentence strings and stream audio bytes back to
the caller. Each call to synthesise() handles one sentence and yields
raw MP3 audio chunks as they arrive.
"""

import logging
import asyncio
from typing import AsyncGenerator
import edge_tts

logger = logging.getLogger(__name__)


class TTSEngine:
    """
    Manages text-to-speech using edge-tts.

    Flow:
    1. Receive sentence string
    2. Initialize edge-tts Communicate
    3. Stream audio chunks as MP3 bytes
    4. Caller receives bytes immediately (no buffering)
    """

    def __init__(self, voice: str = "en-US-AriaNeural"):
        """
        Initialise TTS engine.

        Args:
            voice: Edge-tts voice name (e.g., "en-US-AriaNeural")
        """
        self.voice = voice
        logger.info(f"[TTS] Engine initialized with voice: {voice}")

    async def synthesise(self, text: str, timeout_sec: int = 15) -> AsyncGenerator[bytes, None]:
        """
        Convert text to speech and stream audio chunks.

        Args:
            text: Text to synthesize (typically a complete sentence)
            timeout_sec: Timeout for synthesis in seconds

        Yields:
            Raw MP3 audio bytes as chunks arrive
        """
        if not text or not text.strip():
            logger.warning("[TTS] Empty text provided, skipping synthesis")
            return

        if len(text) > 2000:
            logger.warning(f"[TTS] Text too long ({len(text)} chars), truncating to 2000")
            text = text[:2000]

        try:
            logger.debug(f"[TTS] Synthesizing: {text[:60]}...")

            # Create Communicate object
            communicate = edge_tts.Communicate(text, self.voice)

            chunk_count = 0
            byte_count = 0

            # edge-tts Communicate.stream() is an async generator in recent versions
            # If it's not, we wrap it properly
            try:
                async for chunk in communicate.stream():
                    # Check chunk type - Use get() for TypedDict safety
                    if chunk.get("type") == "audio":
                        audio_data = chunk.get("data")
                        if audio_data:  # Skip empty chunks
                            chunk_count += 1
                            byte_count += len(audio_data)
                            logger.debug(f"[TTS] Chunk {chunk_count}: {len(audio_data)} bytes")
                            yield audio_data
                            
                            # Add small delay to prevent overwhelming the client
                            await asyncio.sleep(0.01)
            except Exception as e:
                logger.error(f"[TTS] Error during streaming: {e}")
                raise

            logger.info(f"[TTS] Audio complete: {chunk_count} chunks, {byte_count} bytes total")

        except Exception as e:
            logger.error(f"[TTS] Error during synthesis: {e}")
            raise
