# Cascade AI Tutor - Validation & Testing Guide

## Pre-Testing Checklist

### Environment Setup

- [ ] Python 3.11+ installed
- [ ] Node.js 14+ installed (for frontend validation)
- [ ] DEEPGRAM_API_KEY set in `.env` file
- [ ] GROQ_API_KEY set in `.env` file
- [ ] Dependencies installed: `pip install -r requirements.txt`

### Code Validation

- [x] Backend imports successful
- [x] Frontend JavaScript syntax valid
- [x] No Python syntax errors
- [x] All file changes applied

---

## Testing Strategy

### Phase 1: Server Startup (5 min)

**Objective:** Verify backend starts and loads all fixed modules

**Steps:**

1. Open terminal in project root
2. Run: `python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload`
3. Expected output:
   ```
   Uvicorn running on http://0.0.0.0:8000
   Application startup complete
   ```
4. Check logs for any warnings/errors (should be clean)

**Validation Points:**

- ✓ No import errors
- ✓ STT handler initializes
- ✓ LLM generator initializes
- ✓ TTS engine initializes
- ✓ FastAPI CORS middleware loads

**Failure Scenarios:**

- If API key validation fails: Check `.env` file format
- If port 8000 in use: Change port in command
- If Deepgram/Groq errors: Validate API keys have correct permissions

---

### Phase 2: Frontend Load (2 min)

**Objective:** Verify frontend loads without errors

**Steps:**

1. Open browser: `http://localhost:8000`
2. Expected: Page loads with Cascade tutor interface
3. Open Browser DevTools (F12)
4. Check Console tab for errors

**Validation Points:**

- ✓ Index.html loads (200 OK)
- ✓ CSS stylesheet loads (200 OK)
- ✓ app.js loads (200 OK)
- ✓ No console errors
- ✓ UI renders: header, subject dropdown, start button, transcript area

**Failure Scenarios:**

- If CSS not loaded: Check file serves correctly (`/style.css` returns 200)
- If JS errors: Check browser console for specific messages
- If button disabled: Check for JavaScript initialization errors

---

### Phase 3: Microphone Permission (3 min)

**Objective:** Test microphone access flow with fix for permission denied

**Steps:**

1. Click "Start Session" button
2. Browser should prompt for microphone permission
3. **Test Case A (Allow):** Click "Allow"
   - Expected: Status changes to "🎤 Listening"
   - Latency shows "—"
4. **Test Case B (Deny):** Click "Deny"
   - Expected: Error message "🔒 Microphone permission denied..."

**Validation Points (Allow Case):**

- ✓ Permission prompt appears
- ✓ Status updates to "Listening" within 500ms
- ✓ AudioContext created
- ✓ Microphone active (should see audio levels if monitoring)

**Validation Points (Deny Case):**

- ✓ Error message specific to permission denied
- ✓ Start button re-enabled
- ✓ No partial initialization

**Failure Scenarios:**

- If browser never shows permission prompt: Check browser microphone settings
- If status doesn't update: Check JavaScript console for errors
- If wrong error message: Verify error handling fix in app.js

---

### Phase 4: Audio Capture (5 min)

**Objective:** Test STT receives audio correctly

**Steps:**

1. With "Listening" status, speak clearly: "What is photosynthesis?"
2. Expected behavior:
   - Status changes to "⚙️ Processing" after ~300ms of silence (Deepgram endpointing)
   - Server log shows: `[STT] Utterance confirmed (speech_final): what is photosynthesis?`

**Server Log Validation:**
Watch backend terminal for:

```
[STT] Deepgram WebSocket connection established
[STT] Utterance confirmed (speech_final): [your spoken text]
```

**Browser Console Validation:**
Check for:

```
[Client] Transcript: [your spoken text]
```

**Validation Points:**

- ✓ Deepgram endpointing triggers turn end (~300ms silence after speech)
- ✓ Text appears in transcript panel as "Student" message
- ✓ Server receives correct transcription
- ✓ No duplicate messages in logs

**Failure Scenarios:**

- If status never changes to Processing: Check Deepgram endpointing / mic levels
- If transcript wrong/missing: Deepgram API issue or audio quality low
- If duplicate transcripts: Verify LLM fix was applied (no append in generate())

---

### Phase 5: LLM Response (10 min)

**Objective:** Test LLM streaming and word-boundary chunking

**Steps:**

1. After transcript appears, wait for "⚙️ Processing" status
2. Expected: Status changes to "🔊 Speaking" within 5 seconds
3. Audio plays through speakers
4. Tutor response appears in transcript as it streams

**Server Log Validation:**

```
[LLM] Streaming response for: what is photosynthesis?
[LLM] Yielding chunk (3 tokens, reason=boundary): Photosynthesis is...
[LLM] Yielding chunk (2 tokens, reason=boundary): It converts...
[TTS] First TTS latency: 311ms   ← Deepgram Aura (primary)
```

**Browser Validation:**

- [ ] Response appears in transcript panel (tutor message, left-aligned)
- [ ] Response updates in real-time as it streams
- [ ] Audio plays (speaker icon should be active if volume on)

**Validation Points (Word-Boundary Chunking):**

- ✓ Response chunks yield on punctuation or whitespace
- ✓ Streaming bubble updates smoothly without spacing corruption
- ✓ No sentences cut off at decimals (e.g., "3.14") or abbreviations

**Expected Response Quality:**

- 2-4 sentences (per system prompt)
- Conversational tone
- Relevant to question

**Failure Scenarios:**

- If no response: Check Groq API key and limits
- If response very slow: May indicate event loop blocking (TTS fix issue)
- If sentence chunking wrong: Boundary detection regex issue
- If duplicate in context: Verify LLM fix (no duplicate append)

---

### Phase 6: Latency Measurement (5 min)

**Objective:** Verify latency display and timing accuracy

**Steps:**

1. Ask another question: "Define photosynthesis"
2. Watch latency display in real-time
3. Expected: Number appears and updates
4. First audio should arrive within target (<600ms for optimized)

**Latency targets (Deepgram Aura, measured from West Africa):**

| Stage | Measured p50 | Measured p90 | Warning |
|---|---|---|---|
| STT tail | 40 ms | 40 ms | >100 ms |
| LLM TTFT | 484 ms | 2 786 ms¹ | >3 000 ms |
| LLM stream | 18 ms | 56 ms | >200 ms |
| TTS first byte | 334 ms | 351 ms | >600 ms |
| **Network TTFB** | **1 201 ms** | **1 994 ms** | >4 000 ms |
| **Est. TTFA** | **1 276 ms** | **2 069 ms** | >4 000 ms |

¹ High p90 caused by occasional Groq rate-limit retries; p50 is representative.

> These are measured from **Accra, Ghana → US-East** (worst-case geographic
> origin). Local or same-region deployments will see ~500–700 ms TTFA.

**Failure Scenarios:**

- If TTFA > 4 000 ms consistently: Check for event loop blocking or STT reconnection loop
- If TTS first byte > 600 ms: Deepgram Aura may be falling back to Edge-TTS — check `tts_engine` param in WebSocket URL
- If latency shows 0 ms: Check timestamp calculation in `pipeline.py`

---

### Phase 7: Multi-Turn Conversation (10 min)

**Objective:** Test conversation history and history trimming

**Steps:**

1. Have a 5-turn conversation:
   - Q1: "What is photosynthesis?"
   - Q2: "What are the two main stages?"
   - Q3: "Explain the light reactions"
   - Q4: "What about the Calvin cycle?"
   - Q5: "How do these connect?"

2. Watch transcript panel accumulate messages

**Validation Points:**

- ✓ Tutor responses reference previous context
- ✓ No repetition of earlier explanations
- ✓ Conversation flows naturally
- ✓ Tutor doesn't forget Q1 when answering Q5

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
- If messages repeat: Duplicate append issue (check LLM fix)
- If very slow after 5 turns: History not trimmed (check tutor fix)

---

### Phase 8: Reconnection Test (5 min)

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

- If no reconnection attempts: Check reconnection fix in app.js
- If attempts too fast: Backoff calculation issue
- If more than 3 attempts: Check max attempt limit

---

### Phase 9: Error Handling (5 min)

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

### Phase 10: Extended Session (15 min)

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
- If server crashes: Event loop issue or resource exhaustion

---

## Quick Test Checklist

Use this for rapid validation of all fixes:

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

| Metric | Target (p50) | Acceptable (p90) | Warning |
|---|---|---|---|
| STT tail | 40 ms | 40 ms | >100 ms |
| LLM TTFT | <600 ms | <1 500 ms | >3 000 ms |
| TTS first byte | <400 ms | <500 ms | >800 ms |
| **End-to-end TTFB** | **<1 400 ms** | **<2 500 ms** | **>4 000 ms** |
| **Est. TTFA** | **<1 500 ms** | **<2 600 ms** | **>4 000 ms** |
| Barge-in new audio | <1 600 ms | <1 700 ms | >3 000 ms |
| Memory/turn | +5 MB | <10 MB | >15 MB |

---

## Success Criteria

Project successfully validated if:

1. ✅ All pipeline stages working end-to-end (STT → LLM → TTS)
2. ✅ Deepgram Aura TTS active as primary engine (check WebSocket URL param `tts_engine=deepgram`)
3. ✅ No regression in conversation history / multi-turn context
4. ✅ TTFA p50 < 1 500 ms with Deepgram Aura from local/regional deployment
5. ✅ 10+ turn conversation stable
6. ✅ Graceful error handling for all failure modes
7. ✅ Frontend/backend communication reliable
8. ✅ Benchmark harness (`tests/benchmark.py`) produces valid output with 0 skipped trials
