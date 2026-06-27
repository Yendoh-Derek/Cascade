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
from typing import AsyncGenerator, List, Optional, cast
from groq import AsyncGroq
from groq.types.chat import ChatCompletionMessageParam

logger = logging.getLogger(__name__)

# Tuning constant: number of tokens to buffer before force-flushing the first
# sentence chunk. Reduces first-audio latency on long opening sentences.
EARLY_FLUSH_TOKENS: int = 12

# For all sentences after the first: flush after this many tokens even without a
# sentence boundary. Prevents long clauses from stalling TTS (fix P2-B).
SUBSEQUENT_FLUSH_TOKENS: int = 10

# Wall-clock fallback: flush the buffer if no sentence has been emitted within
# this many seconds of the first token arriving in the current buffer (fix P2-B).
# 200ms chosen as best fit given Groq TTFT of 80–120ms.
TIME_BASED_FLUSH_SEC: float = 0.200

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

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile", client: Optional[AsyncGroq] = None):
        """
        Initialise the LLM generator.

        Args:
            api_key: Groq API key
            model: Model to use (default: llama-3.3-70b-versatile)
            client: Optional existing AsyncGroq client to use (for sharing across sessions)
        """
        self.api_key = api_key
        self.model = model
        if client:
            self.client = client
            self._owns_client = False
        else:
            self.client = AsyncGroq(api_key=api_key)
            self._owns_client = True
        
        # Latency tracking (populated during generate())
        self.t_request_created: Optional[float] = None
        self.t_request_sent: Optional[float] = None
        self.t_first_token: Optional[float] = None
        self.t_first_sentence_emitted: Optional[float] = None

    async def generate(
        self,
        messages: List[ChatCompletionMessageParam],
        temperature: float = 0.3,
        max_tokens: int = 500,
        timeout_sec: int = 30,
    ) -> AsyncGenerator[str, None]:
        """
        Stream tokens from Groq and yield complete sentences.

        Args:
            messages: Full conversation history (list of {"role": "...", "content": "..."})
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Max tokens to generate
            timeout_sec: Timeout for entire generation in seconds

        Yields:
            Complete sentence strings (including punctuation)
        """
        # Validate inputs
        if not messages or not isinstance(messages, list):
            logger.warning("[LLM] Invalid messages, skipping generation")
            return
        sentence_buffer = ""
        first_token_received = False
        
        # Record request creation time (start of generate() call)
        self.t_request_created = time.perf_counter()
        self.t_request_sent = None
        self.t_first_token = None
        self.t_first_sentence_emitted = None
        
        try:
            # Build the messages list - messages already contains full history
            # DO NOT append transcript again - it's already in the messages
            request_messages = cast(List[ChatCompletionMessageParam], list(messages))

            logger.debug(f"[LLM] Requesting {len(request_messages)} messages, model={self.model}")

            async with asyncio.timeout(timeout_sec):
                # Retry loop for Groq 503s
                stream = None
                retries = 3
                for attempt in range(retries):
                    try:
                        self.t_request_sent = time.perf_counter()
                        stream = await self.client.chat.completions.create(
                            model=self.model,
                            messages=request_messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            stream=True,
                        )
                        break
                    except Exception as e:
                        if getattr(e, "status_code", None) == 503 and attempt < retries - 1:
                            logger.warning(f"[LLM] Groq 503 error, retrying in 300ms... ({attempt + 1}/{retries})")
                            await asyncio.sleep(0.3)
                        else:
                            raise
                
                if stream is None:
                    raise Exception("Failed to get stream from Groq")

                sentence_buffer = ""
                token_count = 0
                token_count_in_buffer = 0
                t_buffer_start: Optional[float] = None  # per-buffer wall-clock timer (P2-B)
                stream_iterator = stream.__aiter__()
                stream_exhausted = False

                while not stream_exhausted:
                    # Function to get next chunk or raise StopAsyncIteration
                    async def get_next_chunk():
                        try:
                            return await stream_iterator.__anext__()
                        except StopAsyncIteration:
                            return None

                    # If we have content in buffer, race between next token and time-based flush
                    chunk = None
                    if sentence_buffer and t_buffer_start:
                        remaining_time = max(0, TIME_BASED_FLUSH_SEC - (time.perf_counter() - t_buffer_start))
                        # Wait for either next chunk or flush timeout
                        get_next_task = asyncio.create_task(get_next_chunk())
                        get_next_task.set_name("get_next_chunk")
                        sleep_task = asyncio.create_task(asyncio.sleep(remaining_time))
                        sleep_task.set_name("sleep")
                        done, pending = await asyncio.wait(
                            [get_next_task, sleep_task],
                            return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in done:
                            if task.get_name() == "get_next_chunk":
                                chunk = task.result()
                        # Cancel pending tasks
                        for task in pending:
                            task.cancel()
                    else:
                        # No buffer, just wait for next chunk
                        chunk = await get_next_chunk()

                    if chunk is None:
                        stream_exhausted = True
                    else:
                        delta = chunk.choices[0].delta
                        if delta.content:
                            # Record the time of first token received (marks TTFT start point)
                            if not first_token_received:
                                first_token_received = True
                                self.t_first_token = time.perf_counter()

                            token = delta.content
                            token_count += 1

                            # Start the per-buffer timer on the first token of each new chunk.
                            if t_buffer_start is None:
                                t_buffer_start = time.perf_counter()

                            sentence_buffer += token
                            token_count_in_buffer += 1

                    # Check flush conditions (either we got a token or timed out)
                    if sentence_buffer:
                        last_chars = sentence_buffer[-3:].rstrip()

                        is_boundary = (
                            any(c in last_chars for c in '.?!') and self._has_sentence_boundary(sentence_buffer)
                        ) or self._has_clause_boundary(sentence_buffer)
                        is_first_sentence = (self.t_first_sentence_emitted is None)

                        # Token cap: first sentence uses EARLY_FLUSH_TOKENS;
                        # subsequent sentences use the tighter SUBSEQUENT_FLUSH_TOKENS.
                        token_cap = EARLY_FLUSH_TOKENS if is_first_sentence else SUBSEQUENT_FLUSH_TOKENS

                        # Time-based fallback: flush if 200ms has elapsed since the
                        # first token in this buffer, regardless of punctuation.
                        time_based_flush = (
                            t_buffer_start is not None and
                            (time.perf_counter() - t_buffer_start) >= TIME_BASED_FLUSH_SEC
                        )

                        # UX/Audio Fix: Only allow token cap or time-based flush if the
                        # buffer ends in a space or punctuation. Otherwise we might split
                        # a word in half, causing the TTS to pronounce fragments weirdly.
                        ends_with_space_or_punct = bool(sentence_buffer) and sentence_buffer[-1] in " \n\t\r.,!?;:—-"
                        can_flush_safely = is_boundary or ends_with_space_or_punct

                        if is_boundary or (can_flush_safely and ((token_count_in_buffer >= token_cap) or time_based_flush)):
                            sentence = sentence_buffer.strip()
                            flush_reason = "boundary" if is_boundary else ("time" if time_based_flush else "token_cap")
                            logger.debug(f"[LLM] Yielding sentence ({token_count_in_buffer} tokens, reason={flush_reason}): {sentence[:60]}...")
                            # Record first sentence emission time (for streaming delay calculation)
                            if self.t_first_sentence_emitted is None:
                                self.t_first_sentence_emitted = time.perf_counter()
                            yield sentence
                            sentence_buffer = ""
                            token_count_in_buffer = 0
                            t_buffer_start = None  # reset timer for next chunk

                # Yield any remaining buffer
                if sentence_buffer.strip():
                    sentence = sentence_buffer.strip()
                    # Ensure t_first_sentence_emitted is set for final buffer (edge case: no sentence boundaries)
                    if self.t_first_sentence_emitted is None:
                        self.t_first_sentence_emitted = time.perf_counter()
                    logger.info(f"[LLM] Final buffer yielded: {sentence[:60]}...")
                    yield sentence

                logger.info(f"[LLM] Stream complete: {token_count} tokens total")

        except asyncio.TimeoutError:
            logger.error(f"[LLM] Generation timed out after {timeout_sec}s")
            if sentence_buffer.strip():
                # Record timestamp for any buffered content on timeout (edge case)
                if self.t_first_sentence_emitted is None:
                    self.t_first_sentence_emitted = time.perf_counter()
                yield sentence_buffer.strip()
            raise
        except Exception as e:
            logger.error(f"[LLM] Error during generation: {e}")
            if sentence_buffer.strip():
                # Yield partial buffer on error before raising
                logger.warning("[LLM] Yielding partial buffer on error")
                # Record timestamp for any buffered content on error (edge case)
                if self.t_first_sentence_emitted is None:
                    self.t_first_sentence_emitted = time.perf_counter()
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
        if before_period[-1].isdigit():
            return False

        # Check for URLs (e.g., "example.com")
        if re.search(r'\w+\.\w{2,}$', before_period):
            return False

        # Check for email-like patterns
        if re.search(r'\w+\.\w+@', before_period):
            return False

        # Check for common abbreviations
        words = before_period.split()
        if words:
            last_word = words[-1].lower()
            if last_word in ABBREVIATIONS or len(last_word) <= 2:
                return False

        # Likely a real sentence boundary
        return True

    def _has_clause_boundary(self, text: str) -> bool:
        """
        Check if text ends at a natural clause boundary suitable for TTS flushing.

        Soft-break heuristics (P5-C): fires when the buffer contains a complete
        phrase that TTS can render naturally, even without a full sentence.
        This reduces first-audio latency on complex sentences with late punctuation.

        Minimum 6 words required to avoid flushing on short fragments.

        Recognised patterns:
          - ", but", ", so", ", and", ", or"  — coordinating clause
          - ", which", ", that", ", where"   — relative clause
          - "; "                               — semicolon separation
          - " — "                             — em-dash pause
        """
        if not text or len(text.split()) < 6:
            return False

        # Check last 100 chars for clause markers
        tail = text[-100:] if len(text) > 100 else text
        stripped = tail.rstrip()

        # Coordinating conjunctions after comma
        coord_patterns = [", but", ", so", ", and", ", or", ", yet", ", nor"]
        for pattern in coord_patterns:
            if stripped.endswith(pattern):
                return True

        # Relative / subordinate clauses after comma
        relative_patterns = [", which", ", that", ", where", ", when", ", who"]
        for pattern in relative_patterns:
            if stripped.endswith(pattern):
                return True

        # Semicolon
        if stripped.endswith(";"):
            return True

        # Em-dash (spoken pause)
        if stripped.endswith(" —") or stripped.endswith(" --"):
            return True

        return False

    async def close(self):
        """Close the AsyncGroq client and its underlying HTTP client connection pool (only if we own it)."""
        if hasattr(self, "client") and self.client and self._owns_client:
            try:
                await self.client.close()
                logger.info("[LLM] AsyncGroq client closed successfully")
            except Exception as e:
                logger.error(f"[LLM] Error closing AsyncGroq client: {e}")
