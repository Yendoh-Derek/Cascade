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
from typing import Callable, Dict, Optional
from datetime import datetime

from backend.stt import STTHandler
from backend.llm import LLMGenerator
from backend.tts import TTSEngine
from backend.tutor import TutorSession

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

        self.tutor = TutorSession(subject=subject)
        self.stt_handler: Optional[STTHandler] = None
        self.llm_generator: Optional[LLMGenerator] = None
        self.tts_engine: Optional[TTSEngine] = None

        # Latency tracking
        self.utterance_end_time: Optional[float] = None
        self.first_llm_token_time: Optional[float] = None
        self.first_audio_time: Optional[float] = None

        logger.info(f"[Pipeline] Session initialized (subject={self.tutor.subject})")

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
        # Validate transcript
        if not transcript or not isinstance(transcript, str):
            logger.warning("[Pipeline] Invalid transcript received, skipping")
            return

        self.utterance_end_time = time.time()
        logger.info(f"[Pipeline] Transcript received: {transcript[:60]}...")

        # Schedule async processing using asyncio.create_task
        # which is safe to call from sync context when already in event loop
        try:
            task = asyncio.create_task(self._process_transcript(transcript))
            # Don't wait for task - let it run in background
        except RuntimeError as e:
            # This shouldn't happen in normal FastAPI context, but log if it does
            logger.error(f"[Pipeline] Failed to create task: {e}")
            self.send_message(
                {
                    "type": "error",
                    "message": f"Pipeline error: Failed to create processing task",
                }
            )

    async def _process_transcript(self, transcript: str):
        """
        Core pipeline: transcript → LLM → TTS → WebSocket.

        Handles backpressure, errors, and cancellation gracefully.

        Args:
            transcript: Confirmed transcript from STT
        """
        try:
            # Send transcript to frontend
            self.send_message(
                {
                    "type": "transcript",
                    "text": transcript,
                }
            )

            # Add user message to tutor session
            self.tutor.add_user_message(transcript)

            # Trim history BEFORE LLM call to keep inference fast
            # This prevents context window from growing during session
            self.tutor.trim_history(max_turns=10)

            # Get messages from tutor session (includes system prompt and history)
            messages = self.tutor.get_messages()

            logger.debug(f"[Pipeline] LLM processing with {len(messages)} messages")

            # Track LLM latency
            llm_start = time.time()
            first_token_received = False

            # Collect full response for history
            full_response = ""

            # Stream sentences from LLM
            try:
                async for sentence in self.llm_generator.generate(
                    transcript=transcript,
                    messages=messages,
                    timeout_sec=30,  # 30 second timeout on LLM generation
                ):
                    # Record first token time
                    if not first_token_received:
                        self.first_llm_token_time = time.time()
                        llm_latency = (self.first_llm_token_time - self.utterance_end_time) * 1000
                        logger.info(f"[Pipeline] First LLM token: {llm_latency:.0f}ms")
                        first_token_received = True

                    full_response += sentence

                    # Send response chunk to frontend as text is generated
                    self.send_message(
                        {
                            "type": "response_chunk",
                            "text": sentence,
                        }
                    )

                    # Stream TTS for this sentence
                    logger.debug(f"[Pipeline] TTS: {sentence[:40]}...")
                    tts_start = time.time()
                    first_chunk_sent = False

                    try:
                        async for audio_chunk in self.tts_engine.synthesise(sentence):
                            # Record first audio sent time
                            if not first_chunk_sent:
                                self.first_audio_time = time.time()
                                total_latency = (self.first_audio_time - self.utterance_end_time) * 1000
                                logger.info(f"[Pipeline] First audio sent: {total_latency:.0f}ms")
                                first_chunk_sent = True

                            # Send audio chunk immediately to WebSocket as binary
                            try:
                                self.send_message(
                                    {
                                        "type": "audio",
                                        "data": audio_chunk,  # Send as binary
                                    }
                                )
                            except Exception as e:
                                logger.error(f"[Pipeline] Error sending audio: {e}")
                                # Don't stop generation on send error; backpressure is client's job
                                break  # But do stop this sentence if send failed

                    except Exception as e:
                        logger.error(f"[Pipeline] TTS error for sentence: {e}")
                        # Log but continue with next sentence
                        continue

            except asyncio.TimeoutError:
                logger.error(f"[Pipeline] LLM generation timed out")
                # Send partial response if we have one
                if full_response:
                    self.tutor.add_assistant_message(full_response)
                self.send_message(
                    {
                        "type": "error",
                        "message": "LLM response generation timed out",
                    }
                )
                return
            except asyncio.CancelledError:
                logger.info(f"[Pipeline] Generation cancelled (client disconnected?)")
                return
            except Exception as e:
                logger.error(f"[Pipeline] Error in LLM generation: {e}")
                if full_response:
                    self.tutor.add_assistant_message(full_response)
                self.send_message(
                    {
                        "type": "error",
                        "message": f"LLM error: {str(e)}",
                    }
                )
                return

            # Add full response to tutor session
            if full_response:
                self.tutor.add_assistant_message(full_response)

            # Signal end of response to frontend
            self.send_message(
                {
                    "type": "response_end",
                }
            )

            # Send latency summary
            if self.first_audio_time and self.utterance_end_time:
                latency_ms = (self.first_audio_time - self.utterance_end_time) * 1000
                self.send_message(
                    {
                        "type": "latency",
                        "ms": int(latency_ms),
                    }
                )
            logger.info(f"[Pipeline] Turn complete. Response: {full_response[:60]}...")

        except Exception as e:
            logger.error(f"[Pipeline] Unexpected error processing transcript: {e}")
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
