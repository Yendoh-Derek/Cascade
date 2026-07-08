# WebSocket Protocol Reference

Cascade uses a single WebSocket endpoint at `/ws` for the full-duplex voice pipeline.

This document is the **authoritative** wire-protocol reference. See
[ARCHITECTURE.md](ARCHITECTURE.md) for system design and data-flow diagrams.

## Connection

- **Endpoint:** `/ws`
- **Query parameter:** `tts_engine=deepgram|edge` (default: `deepgram`)
- **Transport:** Binary PCM16 audio frames (client → server) and mixed binary
  audio + JSON control messages (server → client).
- **Turn gating:** Most server messages include a `turn_id`. The client must
  ignore stale frames where `turn_id != active_turn_id` after a cancellation.

## Authentication handshake

If `CASCADE_AUTH_SECRET` is set, the server sends a `challenge` message with a
nonce before any pipeline work begins. The client must reply with:

```json
{ "type": "auth", "response": "<hmac-sha256(nonce, secret)>" }
```

On success, the server replies with `auth_ok`. If authentication fails or times
out, the server closes the connection with an error message.

---

## Client → server messages

### Binary audio

Raw PCM16 mono audio bytes captured from the browser microphone (16 kHz). The
server forwards these bytes to Deepgram STT and the local Silero VAD.

### JSON control messages

| Type | Fields | Description |
|------|--------|-------------|
| `auth` | `response` | HMAC challenge response (when auth is enabled). |
| `cancel` | — | Interrupt the active turn and discard in-progress AI output. |
| `finalize` | `reason` (optional) | Flush pending STT audio for the current utterance. Sent by client RMS VAD after local silence (~190 ms). |
| `pong` | — | Reply to a server `ping` keepalive. |
| `playback_finished` | — | Notify server that browser playback of the current turn has ended. Used for anti-echo barge-in debounce. |
| `client_latency` | `first_audio_played_ms`, `turn_id` | Report client-perceived latency; server echoes as `perceived_latency`. |

---

## Server → client messages

### Session / gateway

| Type | Fields | Description |
|------|--------|-------------|
| `challenge` | `nonce` | Auth challenge (when `CASCADE_AUTH_SECRET` is set). |
| `auth_ok` | — | Authentication succeeded. |
| `busy` | — | Server at `CASCADE_MAX_CONCURRENT_SESSIONS` capacity. |
| `ping` | — | Periodic keepalive (every ~15 s). Client should reply with `pong`. |
| `error` | `message`, `turn_id` (optional) | Fatal or turn-scoped error (init failure, idle timeout, pipeline/STT error). |
| `rate_limited` | `message` | Client audio exceeded per-session token-bucket rate (32 KB/s). |

### Pipeline lifecycle

| Type | Fields | Description |
|------|--------|-------------|
| `tts_config` | `format`, `sample_rate`, `sampleRate` | Sent once after pipeline init; tells client how to decode TTS audio. |
| `transcript_update` | `stable`, `tentative` | Live interim transcript. `stable` is confirmed word prefix; `tentative` is the fluid trailing words (rendered in dimmed italics). |
| `transcript` | `text`, `turn_id` | Final transcript for a turn; triggers LLM processing. |
| `response_chunk` | `text`, `turn_id` | Incremental streaming text chunk from the LLM. |
| `response_end` | `turn_id` (optional) | End of assistant response for the turn. |
| `turn_cancelled` | `turn_id` | In-progress turn was interrupted (barge-in or superseded). |

### STT status

| Type | Fields | Description |
|------|--------|-------------|
| `stt_reconnecting` | `attempt`, `max` | Deepgram WebSocket reconnect in progress. |
| `stt_reconnected` | — | Deepgram connection restored. |

### Metrics

| Type | Fields | Description |
|------|--------|-------------|
| `llm_metrics` | `queue_ms`, `ttft_ms`, `streaming_delay_ms`, `retry_ms`, `total_ms`, `turn_id` | LLM latency breakdown for the turn. |
| `tts_metrics` | `first_chunk_latency_ms`, `engine`, `turn_id` | TTS first-chunk latency. |
| `latency` | `total_ms`, `llm_ms`, `tts_ms`, `stt_tail_ms`, `endpointing_ms`, `ms`, `was_speculative`, `turn_id` | End-to-end server-side latency at first audio byte. |
| `perceived_latency` | `perceived_ms`, `turn_id` | Echo of client-reported felt latency for the dashboard. |
| `tts_error` | `message`, `turn_id` | TTS synthesis failed for the turn. |

### Binary audio

Synthesized TTS audio chunks. Each binary frame is prefixed with a **4-byte big-endian `turn_id`** so the client can associate audio with the active turn and discard frames from cancelled turns:

```
[0..3]  uint32 big-endian  turn_id
[4..]   audio bytes        PCM16 / MP3 depending on TTS engine (see tts_config)
```

Format and sample rate are defined in the initial `tts_config` message.

---

## Protocol notes

- The server uses a **turn-id gate** so outdated frames and stale audio are
  dropped after a turn is cancelled or replaced.
- For local development, leave `CASCADE_AUTH_SECRET` unset to skip the auth handshake.
- Client-side RMS VAD in `frontend/audio-input.js` sends `finalize` before
  Deepgram's endpointing window completes — see [LATENCY.md](LATENCY.md) for the
  three-layer VAD stack.
