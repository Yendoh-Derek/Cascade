# Cascade Testing Guide

This guide captures the current verification workflow for the repository. It is intended to be practical for local development and for CI parity.

## Quick start

1. Create and activate a Python environment.
2. Install the runtime and dev dependencies:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```
3. Copy the sample env file and set your API keys:
   ```bash
   cp .env.example .env
   ```
4. Start the server locally:
   ```bash
   uvicorn backend.main:app --reload
   ```

## Recommended checks

Run the following before opening a pull request:

```bash
pytest tests/ -v && node tests/frontend/test_chart.js && node tests/frontend/test_playback_state.js
ruff check .
mypy backend/ --ignore-missing-imports
```

## Manual verification flow

### 1. Server startup

Verify that the FastAPI app boots and serves the health endpoint:

```bash
curl http://localhost:8000/health
```

The response should include a JSON payload with the server status and model metadata.

### 2. Browser smoke test

Open [http://localhost:8000](http://localhost:8000) and confirm that:

- the UI renders without console errors
- microphone permission prompts appear when starting a session
- the transcript area updates as the turn progresses

### 3. WebSocket diagnostics

Use [tests/diagnose_ws.py](../tests/diagnose_ws.py) when debugging transport or authentication handshakes.

### 4. Frontend chart logic

The Node-based smoke test at [tests/frontend/test_chart.js](../tests/frontend/test_chart.js) validates the chart math in isolation and should be run as part of local validation.

## Notes on latency expectations

The documented latency targets in [docs/LATENCY.md](LATENCY.md) are the authoritative numbers for tuning. If you change the STT endpointing window or speculative LLM settings, rerun the benchmark harness and update the relevant docs.

## Troubleshooting tips

- If the app fails to start, confirm that the required API keys are present in `.env`.
- If the browser cannot connect, verify that the WebSocket endpoint is reachable and that the server is not rejecting the host or origin.
- If the frontend math test fails, inspect [frontend/chart.js](../frontend/chart.js) and [tests/frontend/test_chart.js](../tests/frontend/test_chart.js) together.

**Server-Side Validation (history trimming):**

```
[Tutor] History: 10 messages (5 turns)
[Tutor] Estimated tokens: 1200
[Tutor] No trimming needed
```

After long session (10-15 turns):

```
[Tutor] History exceeded max turns, trimming...
[Tutor] History: 20 messages → 16 messages
[Tutor] History tokens: 4200 → 2800 (trimmed by tokens)
```

**Failure Scenarios:**

- If conversation loses context: History not saved properly
- If messages repeat: Review the message-accumulation flow in the LLM and tutor layers.
- If very slow after 5 turns: Review the conversation-history trimming flow.

---

### Reconnection Test

**Objective:** Test WebSocket reconnection with network interruption

**Prerequisites:** Advanced testing (requires network tools)

**Steps:**

1. Start session and let it speak
2. During audio playback, open DevTools Network tab
3. Right-click network tab → "Throttle" → "Offline"
   - Status should show network error
4. Browser should attempt reconnection (check logs)
5. Change back to "Online"
   - WebSocket should reconnect

**Validation Points:**

- ✓ Error message appears: "🌐 Failed to connect..."
- ✓ Reconnection attempts logged in console
- ✓ Exponential backoff timing: 1s, 2s, 4s
- ✓ Max 3 reconnection attempts
- ✓ Session restarts after reconnection

**Expected Console Output:**

```
[Client] WebSocket reconnection attempt 1 (delay: 1000ms)
[Client] WebSocket reconnection attempt 2 (delay: 2000ms)
[Client] WebSocket reconnection attempt 3 (delay: 4000ms)
[Client] Max reconnection attempts reached
```

**Failure Scenarios:**

- If no reconnection attempts: Review the client reconnection flow in app.js.
- If attempts too fast: Review the reconnect backoff calculation.
- If more than 3 attempts: Check max attempt limit

---

### Error Handling

**Objective:** Test error messages for various failure modes

**Test Cases:**

#### Test 9A: Invalid API Key

1. Temporarily modify DEEPGRAM_API_KEY to invalid value
2. Start session and try to speak
3. Expected error in browser: "❌ STT service error"
4. Server logs show authentication failure

#### Test 9B: No Network

1. Unplug network or enable airplane mode
2. Try to start session
3. Expected: Connection timeout or error message
4. Graceful degradation (doesn't crash)

#### Test 9C: Very Long Input

1. Speak a very long statement (>30 seconds)
2. Expected: Still processes without truncation
3. Response comes after LLM processes full transcript

**Validation Points:**

- ✓ Error messages are user-friendly
- ✓ Specific error types identified
- ✓ No generic "error" messages
- ✓ UI responsive after errors

---

### Extended Session

**Objective:** Test stability over longer duration

**Steps:**

1. Have 10+ turns of conversation
2. Monitor browser DevTools Memory tab for leaks
3. Check CPU usage stays reasonable
4. Monitor server memory usage

**Memory Monitoring:**

```
Initial: ~50MB
After 5 turns: ~60MB
After 10 turns: ~70MB (should not be growing linearly)
After 15 turns: ~75MB (stabilized)
```

**Validation Points:**

- ✓ No memory leaks (growth plateaus)
- ✓ No lag as session grows
- ✓ Audio playback still responsive
- ✓ Conversation quality maintained

**Failure Scenarios:**

- If memory keeps growing: Memory leak in history or buffers
- If UI lags: History trimming not working
- If the server crashes: Review the event loop and resource usage.

---

## Quick Test Checklist

Use this for rapid validation of the main workflow:

```
□ Server starts without errors
□ Frontend loads with no console errors
□ Microphone permission works
□ Audio captured and transcribed correctly
□ LLM response appears within 5 seconds
□ Sentences/words chunked properly (smooth streaming responses)
□ Latency displays (number in ms)
□ Multi-turn conversation maintains context
□ History trimming works (no slowdown after 10 turns)
□ Reconnection logic active on network disconnect
□ Error messages user-friendly and specific
□ 10+ turn session stable with no crashes
□ Memory usage reasonable over time
```

**Pass Criteria:** All items checked ✓

---

## Debugging Commands

If tests fail, use these commands for diagnostics:

```bash
# Check backend imports
python -c "from backend.main import app; print('OK')"

# Check frontend syntax
node -c frontend/app.js

# Check Python syntax for all files
python -m py_compile backend/stt.py backend/llm.py backend/tts.py backend/pipeline.py backend/tutor.py backend/main.py

# Run backend with debug logging
LOGLEVEL=DEBUG python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Check API keys present
python -c "from backend.config import *; print('Config loaded')"
```

---

## Controlled Performance Benchmarking

For reproducible, percentile-based latency measurement use the benchmark harness
instead of manual observation. It connects over WebSocket, streams synthetic audio,
and reports p50/p90/p95 for all pipeline stages.

```bash
# Primary benchmark — Deepgram Aura (default)
python tests/benchmark.py --trials 5 --barge-trials 3

# Full credible run for reporting
python tests/benchmark.py --trials 30 --barge-trials 10

# Edge-TTS fallback comparison
python tests/benchmark.py --trials 5 --barge-trials 3 --tts edge
```

Requirements: server running + `DEEPGRAM_API_KEY` + `GROQ_API_KEY`. No microphone needed.

See **README.md § Performance & Latency** for the canonical reference numbers.

---

## Performance Targets

All targets are calibrated against **Deepgram Aura** (primary TTS engine).
Edge-TTS latencies are roughly 2× higher due to its sentence-buffering requirement.

| Metric              | Target (p50)  | Acceptable (p90) | Warning       |
| ------------------- | ------------- | ---------------- | ------------- |
| STT tail            | 40 ms         | 40 ms            | >100 ms       |
| LLM TTFT            | <600 ms       | <1 500 ms        | >3 000 ms     |
| TTS first byte      | <400 ms       | <500 ms          | >800 ms       |
| **End-to-end TTFB** | **<1 400 ms** | **<2 500 ms**    | **>4 000 ms** |
| **Est. TTFA**       | **<1 500 ms** | **<2 600 ms**    | **>4 000 ms** |
| Barge-in new audio  | <1 600 ms     | <1 700 ms        | >3 000 ms     |
| Memory/turn         | +5 MB         | <10 MB           | >15 MB        |

---

## Success Criteria

Project successfully validated if:

1. ✅ All pipeline stages working end-to-end (STT → LLM → TTS)
2. ✅ Deepgram Aura TTS active as primary engine (check WebSocket URL param `tts_engine=deepgram`)
3. ✅ Conversation history / multi-turn context remains intact
4. ✅ TTFA p50 < 1 500 ms with Deepgram Aura from local/regional deployment
5. ✅ 10+ turn conversation stable
6. ✅ Graceful error handling for all failure modes
7. ✅ Frontend/backend communication reliable
8. ✅ Benchmark harness (`tests/benchmark.py`) produces valid output with 0 skipped trials
