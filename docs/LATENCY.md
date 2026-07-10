# Latency Tuning Guide

Cascade is engineered for **sub-1 second Time-To-First-Audio (TTFA)** under
tuned conditions. Latency is geography-dependent — run the benchmark harness
from your deployment region before publishing performance claims.

## What is measured

| Term | Definition |
|------|------------|
| **TTFB** | Utterance end → first TTS audio byte at the server |
| **TTFA** | TTFB + ~75 ms browser decode and hardware output buffer |
| **Perceived latency** | Client VAD speech-end → first audio scheduled (reported in Charts) |

See [README.md](../README.md#performance--latency) for baseline numbers.

## Three-layer VAD stack

End-of-utterance and barge-in detection span three layers. All three contribute
to perceived latency:

| Layer | Location | Role |
|-------|----------|------|
| **Client RMS VAD** | `frontend/audio-input.js` | Sends early `finalize` after ~190 ms local silence (130 ms threshold + 60 ms timer). Also detects user speech during AI playback for client-side barge-in hints. |
| **Server Silero VAD** | `backend/vad.py` | Barge-in cancellation when user resumes speaking during AI playback. Optional speculative LLM trigger on `speech_stopped` + stable interim transcript. |
| **Deepgram endpointing** | Cloud STT | Emits `speech_final` after `CASCADE_STT_ENDPOINTING` ms of silence (default 300 ms). |

The client RMS layer is the fastest path to end-of-utterance — it fires before
Silero or Deepgram confirm silence. See [ARCHITECTURE.md](ARCHITECTURE.md) for
the full data-flow diagram.

## Sub-1s target

Achievable with **Deepgram Aura** (default TTS), **headphones**, and a server
region close to Deepgram/Groq (US/EU). Edge-TTS is a free fallback but adds
~800 ms to TTS first-byte latency — not suitable for sub-1s goals.

## Recommended benchmark matrix

Start the server, then run each configuration from your target region:

```bash
# 1. Baseline (current defaults)
python tests/benchmark.py --trials 30 --barge-trials 10

# 2. Aggressive endpointing — solo learners with headphones
CASCADE_STT_ENDPOINTING=200 python tests/benchmark.py --trials 30

# 3. Speculative LLM — starts pipeline before speech_final
CASCADE_ENABLE_SPECULATIVE_LLM=true python tests/benchmark.py --trials 30

# 4. Combined aggressive tuning
CASCADE_STT_ENDPOINTING=200 CASCADE_ENABLE_SPECULATIVE_LLM=true python tests/benchmark.py --trials 30
```

Record **p50** and **p90** for each run. Use p50 for headline claims; document
p90 for tail latency. When speculative LLM is enabled, the harness reports
`was_speculative` counts and turn-cancellation stats at the end of the run.

## Tunable environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `CASCADE_STT_ENDPOINTING` | `300` | Deepgram silence window (ms) after speech. Lower = faster turns, more false end-of-utterance. Try `200` with headphones. |
| `CASCADE_VAD_THRESHOLD` | `0.5` | Silero confidence threshold (0–1). Higher = fewer false speech detections. |
| `CASCADE_VAD_SILENCE_MS` | `200` | Server Silero silence window before `speech_stopped`. Lower = faster speculative trigger, more mid-sentence false stops. |
| `CASCADE_VAD_MIN_SPEECH_FRAMES` | `3` | Consecutive speech frames before Silero fires `speech_started`. |
| `CASCADE_ENABLE_SPECULATIVE_LLM` | `false` | Start LLM on VAD + stable interim transcript before `speech_final`. Saves up to ~300 ms when accurate. Grace window does **not** apply to speculative turns. |
| `CASCADE_SPECULATIVE_GRACE_MS` | `180` | Wait after a **confirmed** (`speech_final`) transcript before starting LLM. Not applied to speculative triggers. |
| `CASCADE_SPECULATIVE_STABILITY_MATCHES` | `2` | Identical consecutive interim results required before speculative start. |
| `CASCADE_GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq model used for LLM inference. Published benchmark numbers were measured against `llama-3.3-70b-versatile`. Swap for a smaller model (e.g. `llama-3.1-8b-instant`) to trade quality for lower latency. |
| `CASCADE_BUFFER_STALL_MS` | `500` | Maximum time (ms) the markdown/math post-processing buffers will hold output waiting for a closing delimiter before force-flushing. Prevents dead-air stalls when the LLM emits an unmatched `$` or `*`. |
| `CASCADE_MAX_HISTORY_TURNS` | `10` | Max turn-pairs retained in conversation history. |
| `CASCADE_IDLE_TIMEOUT_SEC` | `300` | WebSocket idle timeout before server closes the session. |

## Client-side factors

- **Headphones** — strongly recommended. Browser AEC may not prevent the tutor's
  voice from triggering false barge-in on speakers.
- **Mic permission** — grant before first session; denied permission blocks the pipeline.
- **Network** — WebSocket RTT to your server adds directly to perceived latency.
- **Client RMS finalize** — `localFinalizeSilenceMs` (130 ms) + 60 ms debounce timer
  in `audio-input.js` controls how quickly the browser sends `finalize` after local
  silence detection.

## Speculative LLM evaluation

When enabling speculative LLM, measure both:

1. **Benchmark p50 TTFA** — should decrease if endpointing was the bottleneck.
2. **False-start rate** — run `tests/benchmark.py` with speculative enabled and
   review the harness summary (`was_speculative` completions vs `turn_cancelled` count).
   Also manually run 20 conversational turns; count responses that start before
   you finished speaking. If >5%, keep speculative LLM off, increase
   `CASCADE_SPECULATIVE_STABILITY_MATCHES`, or raise `CASCADE_VAD_SILENCE_MS`.

**Tradeoff:** A mid-utterance pause longer than `CASCADE_VAD_SILENCE_MS` (200 ms
default) can fire `speech_stopped` and trigger speculative LLM on a partial
sentence if the interim transcript is stable. This is the main false-start vector.

### Mid-utterance splits (speech cut into multiple transcripts)

The client RMS layer sends `finalize` after roughly **130 ms** of local silence
(`localFinalizeSilenceMs` in `frontend/audio-input.js`), plus a 60 ms debounce.
A brief pause mid-sentence can therefore flush the first fragment before you
finish speaking. Cascade merges consecutive fragments within
`CASCADE_UTTERANCE_MERGE_SEC` (default **3 s**) on the server.

To reduce false splits (at the cost of slightly higher end-of-utterance latency):

- Raise `localFinalizeSilenceMs` in `frontend/audio-input.js` (e.g. 200–250 ms).
- Raise `CASCADE_STT_ENDPOINTING` (default 300 ms) so Deepgram waits longer
  before emitting `speech_final`.

## Edge-TTS fallback

Use only when Deepgram TTS is unavailable. Expect **~1s+ higher TTFA** vs Aura.
The UI labels Edge as "fallback" for this reason.
