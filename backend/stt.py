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

from deepgram import DeepgramClient

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
        self.transcript_buffer = ""
        self.is_open = False

    def _default_error_handler(self, error: str):
        """Default error handler logs to logger."""
        logger.error(f"[STT] {error}")

    async def connect(self):
        """Initialize Deepgram client and open connection."""
        try:
            self.client = DeepgramClient(api_key=self.api_key)
            logger.info("[STT] Deepgram client initialized")

            # Create live connection with v1 API
            # Using the callback-based API for v7.3.1
            def listen_callback(msg):
                """Callback for incoming messages from Deepgram"""
                asyncio.create_task(self._handle_message(msg))

            # Start listening (this returns a V1SocketClient iterator)
            # We need to run this in a way that allows continuous operation
            self.connection = self.client.listen.v1.connect(
                model="nova-2",
                language="en-US",
                sample_rate=16000,
                channels=1,
                encoding="linear16",
                interim_results=True,
                utterance_end_ms=700,
                vad_events=True,
                callback=listen_callback,
            )

            self.is_open = True
            logger.info("[STT] Connection established")

        except Exception as e:
            error_msg = f"Failed to connect: {str(e)}"
            logger.error(f"[STT] {error_msg}")
            self.on_error(error_msg)
            raise

    async def send_audio(self, audio_bytes: bytes):
        """
        Send raw audio bytes to Deepgram.

        Args:
            audio_bytes: Raw PCM audio data
        """
        if not self.connection or not self.is_open:
            logger.warning("[STT] Connection not open, discarding audio")
            return

        try:
            # The V1SocketClient has a send method for streaming audio
            self.connection.send(audio_bytes)
        except Exception as e:
            error_msg = f"Failed to send audio: {str(e)}"
            logger.error(f"[STT] {error_msg}")
            self.on_error(error_msg)

    async def close(self):
        """Close the connection cleanly."""
        if self.connection:
            try:
                self.connection.close()
                self.is_open = False
                logger.info("[STT] Connection closed")
            except Exception as e:
                logger.error(f"[STT] Error closing connection: {e}")

    async def _handle_message(self, message):
        """
        Handle incoming messages from Deepgram.

        Message structure varies, but typically includes transcript results.
        """
        try:
            if not message:
                return

            # Extract transcript information from the message
            # The exact structure depends on Deepgram's response format
            if isinstance(message, dict):
                self._process_dict_message(message)

        except Exception as e:
            logger.error(f"[STT] Error processing message: {e}")

    def _process_dict_message(self, message: dict):
        """Process a dictionary message from Deepgram."""
        try:
            # Check if this is a transcript result
            if message.get("type") != "Results":
                return

            # Extract transcript from channel results
            channel = message.get("channel", {})
            results = channel.get("results", [])

            if not results:
                return

            latest_result = results[-1]
            transcript_text = ""

            # Extract text from alternatives
            alternatives = latest_result.get("alternatives", [])
            if alternatives:
                transcript_text = alternatives[0].get("transcript", "").strip()

            is_final = latest_result.get("is_final", False)

            if transcript_text:
                if is_final:
                    # Final transcript - accumulate
                    self.transcript_buffer += (
                        " " + transcript_text
                        if self.transcript_buffer
                        else transcript_text
                    )
                    logger.debug(f"[STT] Final: {transcript_text}")

            # Check for utterance end
            speech_final = latest_result.get("speech_final", False)
            if speech_final and self.transcript_buffer:
                # Utterance complete - emit the full transcript
                confirmed = self.transcript_buffer.strip()
                logger.info(f"[STT] Utterance complete: '{confirmed}'")
                self.on_transcript(confirmed)
                self.transcript_buffer = ""

        except Exception as e:
            logger.error(f"[STT] Error processing dict message: {e}")
