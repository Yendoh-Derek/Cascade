"""
cascade/backend/pipeline.py

Core streaming pipeline orchestrator.

Responsibility: Wire STT → LLM → TTS together and manage the WebSocket
connection to the browser. Measure latency at each stage.

Flow:
1. Audio arrives via handle_audio()
2. STT processes and emits confirmed transcript
3. Pipeline receives transcript via callback
4. LLM streams sentences
5. For each sentence, TTS streams audio chunks
6. Audio chunks sent immediately over WebSocket
7. Latency recorded at each boundary
"""

import asyncio
import logging
import time
from typing import Callable, List, Dict, Optional
from datetime import datetime

from backend.stt import STTHandler
from backend.llm import LLMGenerator
from backend.tts import TTSEngine

logger = logging.getLogger(__name__)


class PipelineSession:
    """
    Manages one complete voice agent session.

    Owns: STT handler, LLM generator, TTS engine, conversation history,
    and connection to WebSocket.
    """

    def __init__(
        self,
        api_keys: Dict[str, str],
        model_config: Dict[str, str],
        send_message: Callable,
        subject: Optional[str] = None,
    ):
        """
        Initialise a pipeline session.

        Args:
            api_keys: Dict with 'deepgram' and 'groq' keys
            model_config: Dict with 'deepgram_model', 'groq_model', etc.
            send_message: Callback to send WebSocket messages
            subject: Optional subject for tutoring context
        """
        self.api_keys = api_keys
        self.model_config = model_config
        self.send_message = send_message
        self.subject = subject

        self.history: List[Dict[str, str]] = []
        self.stt_handler: Optional[STTHandler] = None
        self.llm_generator: Optional[LLMGenerator] = None
        self.tts_engine: Optional[TTSEngine] = None

        # Latency tracking
        self.utterance_end_time: Optional[float] = None
        self.first_llm_token_time: Optional[float] = None
        self.first_audio_time: Optional[float] = None

        logger.info(f"[Pipeline] Session initialized (subject={subject})")

    async def initialize(self):
        """Initialize all components (STT, LLM, TTS)."""
        try:
            # Initialize STT
            self.stt_handler = STTHandler(
                api_key=self.api_keys["deepgram"],
                on_transcript=self._on_transcript_received,
                on_error=self._on_stt_error,
            )

            # Initialize LLM
            self.llm_generator = LLMGenerator(
                api_key=self.api_keys["groq"],
                model=self.model_config["groq_model"],
            )

            # Initialize TTS
            self.tts_engine = TTSEngine(
                voice=self.model_config.get("edge_tts_voice", "en-US-AriaNeural")
            )

            # Connect STT
            await self.stt_handler.connect()

            logger.info("[Pipeline] All components initialized and ready")

        except Exception as e:
            logger.error(f"[Pipeline] Initialization failed: {e}")
            raise

    async def handle_audio(self, audio_bytes: bytes):
        """
        Forward audio bytes to STT.

        Args:
            audio_bytes: Raw audio data
        """
        if not self.stt_handler:
            logger.warning("[Pipeline] STT not initialized, discarding audio")
            return

        await self.stt_handler.send_audio(audio_bytes)

    def _on_transcript_received(self, transcript: str):
        """
        Called by STT when a complete transcript is confirmed.

        This triggers the full pipeline: LLM → TTS → WebSocket send.

        Args:
            transcript: Confirmed transcript string
        """
        self.utterance_end_time = time.time()
        logger.info(f"[Pipeline] Transcript received: {transcript[:60]}...")

        # Run the async pipeline in an event loop
        try:
            asyncio.create_task(self._process_transcript(transcript))
        except RuntimeError:
            # If no event loop, create one (shouldn't happen in normal flow)
            asyncio.run(self._process_transcript(transcript))

    async def _process_transcript(self, transcript: str):
        """
        Core pipeline: transcript → LLM → TTS → WebSocket.

        Args:
            transcript: Confirmed transcript from STT
        """
        try:
            # Add user message to history
            self.history.append({"role": "user", "content": transcript})

            # Build system message with optional subject context
            system_message = (
                "You are Cascade, an expert AI tutor. Your role is to explain "
                "concepts clearly, ask guiding questions to check understanding, "
                "and adapt your explanations to the student's level. Keep "
                "responses concise and conversational — two to four sentences "
                "per turn. Never lecture at length. Always engage the student."
            )
            if self.subject:
                system_message += f"\n\nThe student is studying: {self.subject}."

            # Prepare messages for LLM
            messages = [{"role": "system", "content": system_message}] + self.history[:-1]

            logger.debug(f"[Pipeline] LLM processing with {len(messages)} messages")

            # Track LLM latency
            llm_start = time.time()
            first_token_received = False

            # Collect full response for history
            full_response = ""

            # Stream sentences from LLM
            async for sentence in self.llm_generator.generate(
                transcript=transcript,
                messages=messages,
            ):
                # Record first token time
                if not first_token_received:
                    self.first_llm_token_time = time.time()
                    llm_latency = (self.first_llm_token_time - self.utterance_end_time) * 1000
                    logger.info(f"[Pipeline] First LLM token: {llm_latency:.0f}ms")
                    first_token_received = True

                full_response += sentence

                # Stream TTS for this sentence
                logger.debug(f"[Pipeline] TTS: {sentence[:40]}...")
                tts_start = time.time()
                first_chunk_sent = False

                async for audio_chunk in self.tts_engine.synthesise(sentence):
                    # Record first audio sent time
                    if not first_chunk_sent:
                        self.first_audio_time = time.time()
                        total_latency = (self.first_audio_time - self.utterance_end_time) * 1000
                        logger.info(f"[Pipeline] First audio sent: {total_latency:.0f}ms")
                        first_chunk_sent = True

                    # Send audio chunk immediately to WebSocket
                    try:
                        self.send_message(
                            {
                                "type": "audio",
                                "data": audio_chunk.hex(),  # Send as hex for JSON compatibility
                            }
                        )
                    except Exception as e:
                        logger.error(f"[Pipeline] Error sending audio: {e}")

            # Add full response to history
            self.history.append({"role": "assistant", "content": full_response})

            # Send latency summary
            if self.first_audio_time and self.utterance_end_time:
                latency_ms = (self.first_audio_time - self.utterance_end_time) * 1000
                self.send_message(
                    {
                        "type": "latency",
                        "latency_ms": int(latency_ms),
                    }
                )
            logger.info(f"[Pipeline] Turn complete. Response: {full_response[:60]}...")

        except Exception as e:
            logger.error(f"[Pipeline] Error processing transcript: {e}")
            self.send_message(
                {
                    "type": "error",
                    "message": f"Pipeline error: {str(e)}",
                }
            )

    def _on_stt_error(self, error: str):
        """Handle STT errors."""
        logger.error(f"[Pipeline] STT error: {error}")
        self.send_message(
            {
                "type": "error",
                "message": f"STT error: {error}",
            }
        )

    async def close(self):
        """Close all connections cleanly."""
        try:
            if self.stt_handler:
                await self.stt_handler.close()
            logger.info("[Pipeline] Session closed")
        except Exception as e:
            logger.error(f"[Pipeline] Error closing session: {e}")
