# ADR-002 — Sentence Boundary Streaming with Hybrid Flush

**Status:** Accepted  
**Date:** 2026-06-23

## Context

TTS engines produce significantly more natural prosody when given complete phrases
rather than individual words or arbitrary token chunks. However, waiting for full
sentence boundaries introduces first-audio latency when sentences are long or
punctuation appears late in the LLM stream.

## Decision

LLM tokens are buffered in `llm.py` until one of three flush conditions fires:

1. **Hard sentence boundary** — period, question mark, or exclamation (with
   abbreviation/decimal disambiguation via `_has_sentence_boundary()`).
2. **Soft clause boundary** — coordinating conjunctions after commas (`, but`, `, so`),
   relative clauses (`, which`, `, that`), semicolons, or em-dashes, when the buffer
   contains ≥ 6 words (`_has_clause_boundary()`).
3. **Token cap** — 12 tokens for the first sentence (`EARLY_FLUSH_TOKENS`),
   10 tokens for subsequent sentences (`SUBSEQUENT_FLUSH_TOKENS`).
4. **Time-based fallback** — 200ms wall-clock elapsed since the first token in the
   current buffer (`TIME_BASED_FLUSH_SEC`), regardless of punctuation.

## Consequences

- First-audio latency reduced by 150–400ms on long or complex sentences compared to
  boundary-only flushing.
- TTS receives shorter text units on clause boundaries, which sounds natural because
  commas and conjunctions are natural prosodic break points.
- The 200ms fallback ensures worst-case latency is bounded even if the LLM produces
  an unusually long clause without any recognized boundary marker.
