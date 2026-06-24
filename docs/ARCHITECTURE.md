# Cascade AI Voice Tutor - Architecture Overview

Cascade is built on a full-duplex WebSocket-based streaming architecture to achieve sub-second voice tutoring interactions. This document outlines the key components, data flow, and timing instrumentation.

---

## System Architecture

```mermaid
graph TD
    Client[Client Browser]
    Server[FastAPI Server]
    STT[Deepgram Nova-2 STT]
    LLM[Groq Llama 3.3 LLM]
    TTS[TTS Engine: Edge / Deepgram]

    Client -- Raw Audio (PCM16 16kHz) --> Server
    Server -- Raw Audio Streams --> STT
    STT -- Transcript (speech_final) --> Server
    Server -- History + Prompt --> LLM
    LLM -- Streamed Tokens (Sentence Boundaries) --> Server
    Server -- Clean Text Sentences (batched) --> TTS
    TTS -- Streaming Audio Chunks (MP3/PCM) --> Server
    Server -- Audio Frame + Turn ID --> Client
```

---

## Turn Processing Pipeline

Cascade processes a single interaction (turn) using a concurrent, asynchronous generator-based pipeline:

```mermaid
sequenceDiagram
    participant C as Client (Browser)
    participant S as Server (FastAPI)
    participant STT as Deepgram STT
    participant LLM as Groq LLM
    participant TTS as TTS Engine

    Note over C,STT: Session Connected & Authorized via Secure Handshake
    C->>S: Raw Audio Stream (PCM16)
    S->>STT: Forward Audio Bytes
    Note over STT: Utterance start detected
    STT->>S: Interim results
    Note over STT: 300ms silence detected (endpointing)
    STT->>S: speech_final Transcript
    Note over S: Turn ID incremented (Turn N)
    S->>C: {"type": "transcript", "text": "..."}
    
    par Collect LLM sentences, then batch-synthesise
        S->>LLM: Generate response (timeout=30s)
        LLM->>S: Stream token chunk
        Note over S: Buffer tokens to sentence boundary
        S->>C: {"type": "response_chunk", "text": "First sentence..."}
        Note over S: All sentences collected
        S->>TTS: synthesise_turn(sentences) — Speak×N then Flush×1
        TTS->>S: Continuous audio stream
        S->>C: Binary Audio Frames (Turn N)
    end

    Note over S: LLM Stream completes
    S->>C: {"type": "response_end"}
```

---

## Interruption and Turn Gate Logic

To prevent stale audio or responses from previous turns reaching the user after they have interrupted the tutor (VAD barge-in), Cascade employs strict epoch boundary gates:

1. **Turn ID Validation:** Every outgoing frame or JSON packet is stamped with a `turn_id`. The client and final WebSocket consumer only send/play frames if the `turn_id == active_turn_id`.
2. **Newest-Wins Policy:** If a new transcript starts processing while a previous turn is active or playing back, the active turn is aborted synchronously (tasks cancelled, LLM generator closed via `.aclose()`), and the pipeline starts the new turn immediately.
3. **Semaphore-Gated TTS:** TTS concurrency is controlled by `asyncio.Semaphore(2)`, scoped per turn so a new turn always starts with fresh capacity (not competing with dying tasks from the interrupted turn).

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

| Control | Mechanism |
|---|---|
| Origin validation | Hostname equality check via `urlsplit()` — not substring containment |
| Pre-auth audio buffer | 256KB cumulative cap + 10MB per-chunk cap during HMAC handshake window |
| HMAC authentication | Optional `CASCADE_AUTH_SECRET`; HMAC-SHA256 challenge-response |
| CORS | Configurable via `CASCADE_CORS_ORIGINS` env var (default `*` for local dev) |
| Concurrency cap | `CASCADE_MAX_CONCURRENT_SESSIONS` process-level semaphore |
| Per-session audio rate | Token-bucket: 32KB/s with 2s burst allowance |
