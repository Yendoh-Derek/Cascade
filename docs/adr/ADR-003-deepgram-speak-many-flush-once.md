# ADR-003 — Deepgram Speak-Many / Flush-Once (Updated for Word-Boundary)

**Status:** Accepted  
**Date:** 2026-06-27

## Context

Early versions of the TTS pipeline sent one `Speak` + one `Flush` per sentence (or per word-chunk).
Deepgram's documentation warns that frequent flushing causes the engine to finalize
each segment independently, introducing micro-gaps (discontinuities) between segments
that are audible as clicks or stutters.

## Decision

All word-chunks for a single pipeline turn are batched and sent to the Deepgram WebSocket
as N `Speak` messages followed by a single `Flush`. Deepgram synthesizes them as one
continuous audio stream, terminated by a single `Flushed` event.

The pipeline uses `synthesise_streaming()` in `tts.py`, which runs a concurrent
**feeder task** that drains the `chunk_queue` as text chunks arrive from the LLM,
sending each as a `Speak`. When the LLM sentinel arrives, the feeder sends `Flush`.
The main coroutine reads binary audio back from the same WebSocket concurrently, so
first audio starts flowing after the first `Speak` — not after all chunks are ready.

A **persistent WebSocket** is maintained for the lifetime of the session, eliminating
per-turn TCP/TLS handshake overhead (~20–50ms).

## Consequences

- No inter-chunk audio gaps; continuous natural-sounding speech.
- First audio latency is bounded by first-word generation, not full-turn generation.
- A failed Deepgram connection requires reconnection, which adds one-turn latency.
  The engine detects unclean shutdown (missing `Flushed`) and reconnects automatically.
