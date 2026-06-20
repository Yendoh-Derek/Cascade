"""
cascade/backend/llm.py

LLM module using Groq for high-speed token generation.

Responsibility: Accept a transcript string and conversation history,
stream tokens from Groq, and emit complete sentence chunks as they form.

The chunker bridges LLM streaming and TTS — TTS needs complete sentences
for natural-sounding speech; yielding word-by-word would produce choppy audio.

Latency Measurement:
  - t_request_created: time when generate() is called
  - t_request_sent: time when API request is actually sent to Groq
  - t_first_token: time when first token is received from Groq (TTFT start)
  - t_first_sentence_emitted: time when first complete sentence is yielded
"""

import logging
import asyncio
import re
import time
from typing import AsyncGenerator, List, Dict, Optional, cast
from groq import AsyncGroq
from groq.types.chat import ChatCompletionMessageParam

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
        
        # Latency tracking (populated during generate())
        self.t_request_created: Optional[float] = None
        self.t_request_sent: Optional[float] = None
        self.t_first_token: Optional[float] = None
        self.t_first_sentence_emitted: Optional[float] = None

    async def generate(
        self,
        transcript: str,
        messages: List[ChatCompletionMessageParam],
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

        sentence_buffer = ""
        first_token_received = False
        
        # Record request creation time (start of generate() call)
        self.t_request_created = time.time()
        
        try:
            # Build the messages list - messages already contains full history
            # DO NOT append transcript again - it's already in the messages
            request_messages = cast(List[ChatCompletionMessageParam], list(messages))

            logger.debug(f"[LLM] Requesting {len(request_messages)} messages, model={self.model}")

            async with asyncio.timeout(timeout_sec):
                # Record the time when request is actually sent to Groq
                self.t_request_sent = time.time()
                stream = await self.client.chat.completions.create(
                    model=self.model,
                    messages=request_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )

                sentence_buffer = ""
                token_count = 0
                token_count_in_buffer = 0

                async for chunk in stream:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        # Record the time of first token received (marks TTFT start point)
                        if not first_token_received:
                            first_token_received = True
                            self.t_first_token = time.time()
                        
                        token = delta.content
                        token_count += 1

                        sentence_buffer += token
                        token_count_in_buffer += 1

                        if sentence_buffer:
                            last_chars = sentence_buffer[-3:].rstrip()
                            if any(c in last_chars for c in '.?!'):
                                if self._has_sentence_boundary(sentence_buffer):
                                    sentence = sentence_buffer.strip()
                                    logger.debug(f"[LLM] Yielding sentence ({token_count_in_buffer} tokens): {sentence[:60]}...")
                                    # Record first sentence emission time (for streaming delay calculation)
                                    if self.t_first_sentence_emitted is None:
                                        self.t_first_sentence_emitted = time.time()
                                    yield sentence
                                    sentence_buffer = ""
                                    token_count_in_buffer = 0

                # Yield any remaining buffer
                if sentence_buffer.strip():
                    sentence = sentence_buffer.strip()
                    # Ensure t_first_sentence_emitted is set for final buffer (edge case: no sentence boundaries)
                    if self.t_first_sentence_emitted is None:
                        self.t_first_sentence_emitted = time.time()
                    logger.info(f"[LLM] Final buffer yielded: {sentence[:60]}...")
                    yield sentence

                logger.info(f"[LLM] Stream complete: {token_count} tokens total")

        except asyncio.TimeoutError:
            logger.error(f"[LLM] Generation timed out after {timeout_sec}s")
            if sentence_buffer.strip():
                # Record timestamp for any buffered content on timeout (edge case)
                if self.t_first_sentence_emitted is None:
                    self.t_first_sentence_emitted = time.time()
                yield sentence_buffer.strip()
            raise
        except Exception as e:
            logger.error(f"[LLM] Error during generation: {e}")
            if sentence_buffer.strip():
                # Yield partial buffer on error before raising
                logger.warning(f"[LLM] Yielding partial buffer on error")
                # Record timestamp for any buffered content on error (edge case)
                if self.t_first_sentence_emitted is None:
                    self.t_first_sentence_emitted = time.time()
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

        # Only check the last 80 characters to improve performance
        tail = text[-80:] if len(text) > 80 else text

        # Strip trailing whitespace
        stripped = tail.rstrip()
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
