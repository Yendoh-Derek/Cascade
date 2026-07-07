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
import time
from typing import AsyncGenerator, List, Optional, cast
from groq import AsyncGroq
from groq.types.chat import ChatCompletionMessageParam

logger = logging.getLogger(__name__)

# Wall-clock fallback: flush the buffer if no sentence has been emitted within
# this many seconds of the first token arriving in the current buffer.
# 150ms chosen to keep latency tight given Groq's high speed.
TIME_BASED_FLUSH_SEC: float = 0.200

EARLY_FLUSH_TOKENS: int = 6
SUBSEQUENT_FLUSH_TOKENS: int = 12


class LLMGenerator:
    """
    Manages Groq LLM streaming with sentence-level chunking.

    Flow:
    1. Accept transcript and conversation history
    2. Stream tokens from Groq
    3. Buffer tokens until word boundaries (spaces/punctuation)
    4. Yield small text chunks directly
    5. Yield any remaining buffer after stream ends
    """

    def __init__(self, api_key: str, model: str = "llama-3.1-8b-instant", client: Optional[AsyncGroq] = None):
        """
        Initialise the LLM generator.

        Args:
            api_key: Groq API key
            model: Model to use (default: llama-3.1-8b-instant)
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
        self.t_first_attempt_sent: Optional[float] = None  # First attempt only
        self.t_request_sent: Optional[float] = None
        self.retry_ms: int = 0
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
            Small string chunks containing words and whitespace
        """
        # Validate inputs
        if not messages or not isinstance(messages, list):
            logger.warning("[LLM] Invalid messages, skipping generation")
            return
        sentence_buffer = ""
        first_token_received = False
        
        # Record request creation time (start of generate() call)
        self.t_request_created = time.perf_counter()
        self.t_first_attempt_sent = None
        self.t_request_sent = None
        self.retry_ms = 0
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
                pending_task: Optional[asyncio.Task] = None
                retries = 3
                for attempt in range(retries):
                    try:
                        t_attempt_start = time.perf_counter()
                        if self.t_first_attempt_sent is None:
                            self.t_first_attempt_sent = t_attempt_start
                        self.t_request_sent = t_attempt_start
                        stream = await self.client.chat.completions.create(
                            model=self.model,
                            messages=request_messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            stream=True,
                        )
                        break
                    except Exception as e:
                        if getattr(e, "status_code", None) in {429, 503} and attempt < retries - 1:
                            logger.warning(f"[LLM] Groq {getattr(e, 'status_code', 'error')} error, retrying in 300ms... ({attempt + 1}/{retries})")
                            t_sleep_start = time.perf_counter()
                            await asyncio.sleep(0.3)
                            t_sleep_end = time.perf_counter()
                            self.retry_ms += int((t_sleep_end - t_sleep_start) * 1000)
                        else:
                            raise
                
                if stream is None:
                    raise Exception("Failed to get stream from Groq")

                sentence_buffer = ""
                token_count = 0
                token_count_in_buffer = 0
                t_buffer_start: Optional[float] = None  # per-buffer wall-clock timer
                stream_iterator = stream.__aiter__()
                stream_exhausted = False

                while not stream_exhausted:
                    # Function to get next chunk or raise StopAsyncIteration
                    async def get_next_chunk():
                        try:
                            return await stream_iterator.__anext__()
                        except StopAsyncIteration:
                            return None

                    if pending_task is None:
                        pending_task = asyncio.create_task(get_next_chunk())

                    # If we have content in buffer, race between next token and time-based flush
                    chunk = None
                    timeout_hit = False
                    if sentence_buffer and t_buffer_start:
                        remaining_time = max(0, TIME_BASED_FLUSH_SEC - (time.perf_counter() - t_buffer_start))
                        try:
                            chunk = await asyncio.wait_for(asyncio.shield(pending_task), timeout=remaining_time)
                            pending_task = None
                        except asyncio.TimeoutError:
                            timeout_hit = True  # Buffer flushes; pending_task remains alive and untouched
                    else:
                        # No buffer, just wait for next chunk
                        chunk = await pending_task
                        pending_task = None

                    if chunk is None and not timeout_hit:
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
                        # Time-based fallback: flush if 200ms has elapsed since the
                        time_based_flush = (
                            t_buffer_start is not None and
                            (time.perf_counter() - t_buffer_start) >= TIME_BASED_FLUSH_SEC
                        )

                        # We flush at a word boundary (whitespace/punctuation).
                        # To prevent event loop congestion, we enforce a minimum token count
                        # per chunk. The first chunk can be smaller to minimize initial latency.
                        token_cap = (
                            EARLY_FLUSH_TOKENS
                            if self.t_first_sentence_emitted is None
                            else SUBSEQUENT_FLUSH_TOKENS
                        )

                        ends_with_space_or_punct = sentence_buffer[-1] in " \n\t\r.,!?;:—-"
                        has_enough_tokens = token_count_in_buffer >= token_cap

                        if (ends_with_space_or_punct and has_enough_tokens) or time_based_flush:
                            # Yield exact chunk (including punctuation/spaces)
                            chunk = sentence_buffer
                            sentence_buffer = ""
                            token_count_in_buffer = 0
                            t_buffer_start = None

                            if self.t_first_sentence_emitted is None:
                                self.t_first_sentence_emitted = time.perf_counter()
                                ttft = (self.t_first_token - self.t_request_created) * 1000 if self.t_first_token else 0
                                chunk_lat = (self.t_first_sentence_emitted - self.t_request_created) * 1000
                                logger.info(
                                    f"[LLM] First chunk emitted in {chunk_lat:.0f}ms (TTFT: {ttft:.0f}ms)"
                                )
                            yield chunk

                # Yield any remaining buffer
                if sentence_buffer:
                    chunk = sentence_buffer
                    # Ensure t_first_sentence_emitted is set for final buffer (edge case: no boundaries)
                    if self.t_first_sentence_emitted is None:
                        self.t_first_sentence_emitted = time.perf_counter()
                    logger.info(f"[LLM] Final buffer yielded: {chunk[:60]}...")
                    yield chunk

                logger.info(f"[LLM] Stream complete: {token_count} tokens total")

        except asyncio.CancelledError:
            logger.info("[LLM] Generation cancelled")
            # pending_task may still be running inside asyncio.shield() — cancel it
            # explicitly so it doesn't outlive this coroutine and leak a Groq stream.
            if pending_task is not None and not pending_task.done():
                pending_task.cancel()
                try:
                    await pending_task
                except (asyncio.CancelledError, Exception):
                    pass
            if sentence_buffer:
                # Yielding here after catching CancelledError is a subtle async-generator
                # trick that safely flushes the partial LLM response up the pipeline 
                # (saving it into history) before re-raising the cancellation.
                if self.t_first_sentence_emitted is None:
                    self.t_first_sentence_emitted = time.perf_counter()
                yield sentence_buffer
            raise
        except asyncio.TimeoutError:
            logger.error(f"[LLM] Generation timed out after {timeout_sec}s")
            if sentence_buffer:
                # Record timestamp for any buffered content on timeout (edge case)
                if self.t_first_sentence_emitted is None:
                    self.t_first_sentence_emitted = time.perf_counter()
                yield sentence_buffer
            raise
        except Exception as e:
            logger.error(f"[LLM] Error during generation: {e}")
            if sentence_buffer:
                # Yield partial buffer on error before raising
                logger.warning("[LLM] Yielding partial buffer on error")
                # Record timestamp for any buffered content on error (edge case)
                if self.t_first_sentence_emitted is None:
                    self.t_first_sentence_emitted = time.perf_counter()
                yield sentence_buffer
            raise



    async def close(self):
        """Close the AsyncGroq client and its underlying HTTP client connection pool (only if we own it)."""
        if hasattr(self, "client") and self.client and self._owns_client:
            try:
                await self.client.close()
                logger.info("[LLM] AsyncGroq client closed successfully")
            except Exception as e:
                logger.error(f"[LLM] Error closing AsyncGroq client: {e}")
