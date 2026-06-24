# ADR-004 — In-Memory Session Storage

**Status:** Accepted  
**Date:** 2026-06-23

## Context

Conversation history must be accessible across turns within a session. Options include
a persistent database, a distributed cache (Redis), or in-process Python objects.

## Decision

Conversation history is stored as a plain Python list inside `TutorSession`, which is
instantiated per WebSocket connection and lives for the duration of that connection.
No external storage dependency is required.

History is trimmed on every turn using a dual-pass strategy:
1. **Turn-count pass:** keep at most `CASCADE_MAX_HISTORY_TURNS` (default 10) turn pairs.
2. **Token-count pass:** estimate total tokens (4 chars ≈ 1 token); trim oldest pairs
   until under 16 000 tokens. Pairs are removed together (user + assistant) to maintain
   role ordering and avoid orphaned assistant messages at `history[0]`.

## Consequences

- Zero infrastructure dependencies for a single-server deployment.
- History is lost if the server restarts or the WebSocket drops — acceptable for a
  tutoring session where the student reconnects and starts fresh.
- Does not scale horizontally (two server instances cannot share a session).
  For multi-instance deployments, replace `TutorSession.history` with a Redis-backed
  store keyed on a session token.
- Token estimation is a rough heuristic (4 chars/token for English). A tiktoken-based
  counter would be more accurate but adds a dependency and per-turn CPU cost.
