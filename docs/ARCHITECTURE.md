# Cascade AI Voice Tutor - Architecture Overview

Cascade is built on a full-duplex WebSocket-based streaming architecture to achieve low-latency voice tutoring interactions. The primary configuration uses **Deepgram Aura** for TTS, achieving a measured TTFA p50 of ~1 276 ms from West Africa to US-East cloud services. This document outlines the key components, data flow, and timing instrumentation.

---

## System Architecture

![Cascade system architecture](images/cascade_system_architecture.svg)

---

## Turn Processing Pipeline

Cascade processes a single interaction (turn) using a concurrent, asynchronous generator-based pipeline:

![Cascade turn processing pipeline](images/cascade_turn_processing_pipeline.svg)

**Diagram Notes:**

- Dashed lifelines represent each participant; solid arrows are blocking requests, dashed arrows are streams/fire-and-forget
- The outer dashed rectangle is the concurrent (par) block — key distinction from a sequential pipeline
- The `↻ repeats until LLM stream ends` line inside the par block communicates the core streaming loop

---

## Interruption and Turn Gate Logic

To prevent stale audio or responses from previous turns reaching the user after they have interrupted the tutor (VAD barge-in), Cascade employs strict epoch boundary gates:

1. **Turn ID Validation:** Every outgoing frame or JSON packet is stamped with a `turn_id`. The client and final WebSocket consumer only send/play frames if the `turn_id == active_turn_id`.
2. **Newest-Wins Policy:** If a new transcript starts processing while a previous turn is active or playing back, the active turn is aborted synchronously (tasks cancelled, LLM generator closed via `.aclose()`), and the pipeline starts the new turn immediately.
3. **Lock-Serialized TTS:** TTS access is controlled by `asyncio.Lock` (`DeepgramTTSEngine._ws_lock`), serializing turns on a single persistent WebSocket connection — fully serial by design, not 2-wide. Cleanup (Clear + WS teardown on failure) is performed *outside* the lock so a new turn can acquire it and start immediately while the previous turn's teardown completes in the background.

---

## Interruption Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server (PipelineSession)
    participant TTS as Deepgram TTS

    Note over C,S: Turn N active (S speaking)
    C->>S: {type: "cancel"} (user spoke / barged in)
    Note over S: _cancel_event.set() — all loops check this
    Note over S: processing_task.cancel()
    S->>TTS: {type: "Clear"} — stop synthesising Turn N audio
    S->>C: {type: "turn_cancelled", turn_id: N}
    Note over S: _active_turn_id = None, epoch incremented
    Note over C: audioEpoch++ → all queued Turn N audio dropped
    Note over S: STT delivers next transcript → Turn N+1 starts
    S->>C: {type: "transcript", turn_id: N+1}
```

---

## Security Model

| Control                | Mechanism                                                                   |
| ---------------------- | --------------------------------------------------------------------------- |
| Origin validation      | Hostname equality check via `urlsplit()` — not substring containment        |
| Pre-auth audio buffer  | 256KB cumulative cap + 10MB per-chunk cap during HMAC handshake window      |
| HMAC authentication    | Optional `CASCADE_AUTH_SECRET`; HMAC-SHA256 challenge-response              |
| CORS                   | Configurable via `CASCADE_CORS_ORIGINS` env var (default `*` for local dev) |
| Concurrency cap        | `CASCADE_MAX_CONCURRENT_SESSIONS` process-level semaphore                   |
| Per-session audio rate | Token-bucket: 32KB/s with 2s burst allowance                                |
