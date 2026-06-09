"""
cascade/backend/llm.py

LLM module using Groq for high-speed token generation.

Responsibility: Accept a transcript string and conversation history,
stream tokens from Groq, and emit complete sentence chunks as they form.

The chunker bridges LLM streaming and TTS — TTS needs complete sentences
for natural-sounding speech; yielding word-by-word would produce choppy audio.
"""

import logging
import asyncio
import re
from typing import AsyncGenerator, List, Dict
from groq import AsyncGroq

logger = logging.getLogger(__name__)

# Common abbreviations that end with period (excluding sentences ending with these)
ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "ave", "blvd", "etc",
    "vs", "co", "inc", "ltd", "corp", "gov", "gen", "col", "maj", "capt",
    "fig", "vol", "no", "p", "pp", "art", "viz", "ed", "eds", "al", "eg",
    "ie", "approx", "dept", "econ", "e.g", "i.e", "cf", "ibid", "idem",
}


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
        timeout_sec: int = 30,
    ) -> AsyncGenerator[str, None]:
        """
        Stream tokens from Groq and yield complete sentences.

        Args:
            transcript: The user's confirmed transcript
            messages: Full conversation history (list of {"role": "...", "content": "..."})
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Max tokens to generate
            timeout_sec: Timeout for entire generation in seconds

        Yields:
            Complete sentence strings (including punctuation)
        """
        # Validate inputs
        if not transcript or not isinstance(transcript, str):
            logger.warning("[LLM] Empty transcript, skipping generation")
            return

        if not messages or not isinstance(messages, list):
            logger.warning("[LLM] Invalid messages, skipping generation")
            return

        try:
            # Build the messages list - messages already contains full history
            # DO NOT append transcript again - it's already in the messages
            request_messages = list(messages)

            logger.debug(f"[LLM] Requesting {len(request_messages)} messages, model={self.model}")

            # Stream from Groq with timeout
            stream = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=self.model,
                    messages=request_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                ),
                timeout=timeout_sec,
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

        except asyncio.TimeoutError:
            logger.error(f"[LLM] Generation timed out after {timeout_sec}s")
            if sentence_buffer.strip():
                yield sentence_buffer.strip()
            raise
        except Exception as e:
            logger.error(f"[LLM] Error during generation: {e}")
            if sentence_buffer.strip():
                # Yield partial buffer on error before raising
                logger.warning(f"[LLM] Yielding partial buffer on error")
                yield sentence_buffer.strip()
            raise

    def _has_sentence_boundary(self, text: str) -> bool:
        """
        Check if text ends with a sentence boundary.

        More sophisticated than simple punctuation check:
        - Must end with . ? ! or ...
        - If . then must not be a decimal number or abbreviation
        - Typically followed by space and capital or end-of-string

        Args:
            text: Text to check

        Returns:
            True if text ends with a likely sentence boundary
        """
        if not text:
            return False

        # Strip trailing whitespace
        stripped = text.rstrip()
        if not stripped:
            return False

        # Check for ellipsis (...)
        if stripped.endswith("..."):
            return True

        # Check for question mark or exclamation
        if stripped[-1] in "?!":
            return True

        # Check for period - more sophisticated logic needed
        if not stripped.endswith("."):
            return False

        # Period at end - check context to distinguish from decimals/abbreviations
        before_period = stripped[:-1].strip()
        if not before_period:
            return False

        # Check for decimal numbers (e.g., "3.14")
        if re.search(r'\d+\.\d+$', stripped):
            return False

        # Check for URLs (e.g., "example.com")
        if re.search(r'\w+\.\w{2,}$', stripped):
            return False

        # Check for email-like patterns
        if re.search(r'\w+\.\w+@', stripped):
            return False

        # Check for common abbreviations
        words = before_period.split()
        if words:
            last_word = words[-1].lower()
            if last_word in ABBREVIATIONS or len(last_word) <= 2:
                return False

        # Likely a real sentence boundary
        return True
