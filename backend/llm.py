"""
cascade/backend/llm.py

LLM module using Groq for high-speed token generation.

Responsibility: Accept a transcript string and conversation history,
stream tokens from Groq, and emit complete sentence chunks as they form.

The chunker bridges LLM streaming and TTS — TTS needs complete sentences
for natural-sounding speech; yielding word-by-word would produce choppy audio.
"""

import logging
from typing import AsyncGenerator, List, Dict
from groq import AsyncGroq

logger = logging.getLogger(__name__)


class LLMGenerator:
    """
    Manages Groq LLM streaming with sentence-level chunking.

    Flow:
    1. Accept transcript and conversation history
    2. Stream tokens from Groq
    3. Buffer tokens into sentences (split on . ? !)
    4. Yield complete sentences as they form
    5. Yield any remaining buffer after stream ends
    """

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        """
        Initialise the LLM generator.

        Args:
            api_key: Groq API key
            model: Model to use (default: llama-3.3-70b-versatile)
        """
        self.api_key = api_key
        self.model = model
        self.client = AsyncGroq(api_key=api_key)

    async def generate(
        self,
        transcript: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> AsyncGenerator[str, None]:
        """
        Stream tokens from Groq and yield complete sentences.

        Args:
            transcript: The user's confirmed transcript
            messages: Full conversation history (list of {"role": "...", "content": "..."})
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Max tokens to generate

        Yields:
            Complete sentence strings (including punctuation)
        """
        try:
            # Build the messages list - ensure transcript is appended
            request_messages = list(messages)
            request_messages.append({"role": "user", "content": transcript})

            logger.debug(f"[LLM] Requesting {len(request_messages)} messages, model={self.model}")

            # Stream from Groq
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=request_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )

            sentence_buffer = ""
            token_count = 0

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    token = delta.content
                    token_count += 1
                    sentence_buffer += token

                    # Check for sentence boundary
                    if self._has_sentence_boundary(sentence_buffer):
                        # Sentence complete - yield it
                        sentence = sentence_buffer.strip()
                        logger.debug(f"[LLM] Yielding sentence ({token_count} tokens): {sentence[:60]}...")
                        yield sentence
                        sentence_buffer = ""

            # Yield any remaining buffer
            if sentence_buffer.strip():
                sentence = sentence_buffer.strip()
                logger.info(f"[LLM] Final buffer yielded: {sentence[:60]}...")
                yield sentence

            logger.info(f"[LLM] Stream complete: {token_count} tokens total")

        except Exception as e:
            logger.error(f"[LLM] Error during generation: {e}")
            raise

    @staticmethod
    def _has_sentence_boundary(text: str) -> bool:
        """
        Check if text ends with a sentence boundary.

        Looks for . ? ! followed by a space or end of string.

        Args:
            text: Text to check

        Returns:
            True if text ends with sentence boundary
        """
        if not text:
            return False

        # Find last character
        last_char = text.rstrip()[-1] if text.rstrip() else ""

        # Check for sentence-ending punctuation followed by space or end
        if last_char in ".?!":
            # Make sure there's either a space after or it's end of string
            return text.endswith(" ") or len(text.rstrip()) == len(text)

        return False
