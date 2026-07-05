# ADR-002 — Word-Boundary Streaming

**Status:** Accepted
**Date:** 2026-06-27

## Context

The core streaming pipeline benefits from emitting response text as soon as a natural boundary is detected. For the primary Deepgram TTS path, word-sized chunks preserve low end-to-end latency while keeping the text stream readable.

## Decision

Cascade uses **word-boundary chunking** in the core pipeline (`llm.py` and `pipeline.py`).

1. **Word-Boundary Chunking**: The LLM buffers tokens until a whitespace or punctuation boundary is reached, then yields the chunk immediately.
2. **Direct Chunk Appending**: The frontend concatenates these chunks directly so the backend can stream the exact text substrings without introducing spacing or punctuation artifacts.
3. **Engine Isolation**: EdgeTTS remains supported through its own buffering path, while the core pipeline continues to stream at the word boundary level when native streaming TTS is available.

## Consequences

- The pipeline can start audio playback sooner when using streaming-capable TTS engines.
- The response stream remains readable and naturally segmented.
- The chunk transport uses a `chunk_queue` abstraction rather than sentence-sized buffering for the main path.
