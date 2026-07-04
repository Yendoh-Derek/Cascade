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

# 3. Speculative LLM — starts pipeline before speech_final (test false-start rate manually)
CASCADE_ENABLE_SPECULATIVE_LLM=true python tests/benchmark.py --trials 30

# 4. Combined aggressive tuning
CASCADE_STT_ENDPOINTING=200 CASCADE_ENABLE_SPECULATIVE_LLM=true python tests/benchmark.py --trials 30
```

Record **p50** and **p90** for each run. Use p50 for headline claims; document
p90 for tail latency.

## Tunable environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `CASCADE_STT_ENDPOINTING` | `300` | Deepgram silence window (ms) after speech. Lower = faster turns, more false end-of-utterance. Try `200` with headphones. |
| `CASCADE_ENABLE_SPECULATIVE_LLM` | `false` | Start LLM on VAD + stable interim transcript before `speech_final`. Saves up to ~300 ms when accurate. |
| `CASCADE_SPECULATIVE_GRACE_MS` | `180` | Wait after speculative trigger before starting LLM. |
| `CASCADE_SPECULATIVE_STABILITY_MATCHES` | `2` | Interim transcript stability checks before speculative start. |

## Client-side factors

- **Headphones** — strongly recommended. Browser AEC may not prevent the tutor's
  voice from triggering false barge-in on speakers.
- **Mic permission** — grant before first session; denied permission blocks the pipeline.
- **Network** — WebSocket RTT to your server adds directly to perceived latency.

## Speculative LLM evaluation

When enabling speculative LLM, measure both:

1. **Benchmark p50 TTFA** — should decrease if endpointing was the bottleneck.
2. **False-start rate** — manually run 20 conversational turns; count responses
   that start before you finished speaking. If >5%, keep speculative LLM off or
   increase `CASCADE_SPECULATIVE_STABILITY_MATCHES`.

## Edge-TTS fallback

Use only when Deepgram TTS is unavailable. Expect **~1s+ higher TTFA** vs Aura.
The UI labels Edge as "fallback" for this reason.
