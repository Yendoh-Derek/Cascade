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
    Server -- Clean Text Sentences --> TTS
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
    Note over STT: 600ms silence detected (endpointing)
    STT->>S: speech_final Transcript
    Note over S: Turn ID incremented (Turn N)
    S->>C: {"type": "transcript", "text": "..."}
    
    par Stream LLM & TTS Concurrently
        S->>LLM: Generate response (timeout=30s)
        LLM->>S: Stream token chunk
        Note over S: Buffer tokens to sentence boundary
        S->>C: {"type": "response_chunk", "text": "First sentence..."}
        S->>TTS: Synthesise (clean text)
        TTS->>S: Audio chunk stream
        S->>C: Binary Audio Frame (Turn N)
    and
        LLM->>S: Stream remaining sentences
        S->>TTS: Synthesise next sentences (Condition-gated concurrency)
        TTS->>S: Audio chunk stream
        S->>C: Binary Audio Frame (Turn N)
    end

    Note over S: LLM Stream completes
    S->>C: {"type": "response_end"}
```

---

## Interruption and Turn Gate Logic

To prevent stale audio or responses from previous turns reaching the user after they have interrupted the tutor (VAD barge-in), Cascade employs strict epoch boundary gates:

1. **Turn ID Validation:** Every outgoing frame or JSON packet is stamped with a `turn_id`. The client and final WebSocket consumer only send/play frames if the `turn_id == active_turn_id`.
2. **Newest-Wins Policy:** If a new transcript starts processing while a previous turn is active or playing back, the active turn is aborted synchronously (tasks cancelled, LLM generator closed via `.aclose()`), and the pipeline starts the new turn immediately.
3. **Condition-Gated TTS Concurrency:** Dynamic scheduling of TTS synthesis using `asyncio.Condition` limits network and CPU concurrency based on backlogs without resorting to CPU polling.
