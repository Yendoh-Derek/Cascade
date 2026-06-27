# ADR-002 — Word Boundary Streaming (Previously Sentence Boundary)

**Status:** Superseded (Updated to Word-Boundary Streaming)
**Date:** 2026-06-27

## Context

Initially, we used sentence-boundary streaming because some TTS engines (like EdgeTTS) required complete phrases for natural prosody. However, waiting for full sentence boundaries introduced a "First-Sentence Latency Floor" limitation. With our primary TTS engine (Deepgram) supporting word-by-word streaming over WebSockets, we can eliminate this artificial delay.

## Decision

We have migrated from sentence-boundary chunking to **word-boundary chunking** in the core pipeline (`llm.py` and `pipeline.py`).

1. **Word-Boundary Chunking**: The LLM buffers tokens only until it hits a whitespace character or punctuation mark, and then yields the chunk immediately.
2. **Direct Chunk Appending**: The frontend directly concatenates these chunks, enabling the backend to stream exact substrings (including punctuation and natural spacing) without formatting corruption.
3. **TTS Isolation**: Because EdgeTTS still requires complete strings, the sentence-boundary logic (`_has_sentence_boundary`) has been ported directly into `EdgeTTSEngine.synthesise_streaming`. This isolates EdgeTTS's limitations inside its own component, preventing it from throttling the core pipeline's performance when using native streaming engines like Deepgram.

## Consequences

- The "First-Sentence Latency Floor" is completely eliminated.
- Audio delay is minimized to absolute baseline hardware latency when using Deepgram TTS.
- The `sentence_queue` has been renamed to `chunk_queue` across the pipeline to reflect its new purpose of carrying word-sized buffers instead of full sentences.
- EdgeTTS functionality is preserved via isolated sentence-buffering within its specific engine class.
