# Cascade — AI Voice Tutor

A low-latency AI tutoring voice agent built on a fully streaming pipeline.
Students ask questions by voice and receive spoken responses in under 600ms,
demonstrating that voice agent latency is a pipeline design problem — not a
hardware or model problem.

---

## How It Works

Standard voice agents wait for each stage to fully complete before starting
the next. Cascade streams between stages concurrently:

```
Standard:   [STT ████████] → [LLM ████████████] → [TTS ████████]  ~3–5s

Cascade:    [STT ██▒▒▒▒▒▒]
                   ↓ partial transcript
                [LLM ██████▒▒▒▒]
                         ↓ first sentence
                       [TTS ████]  ← student hears this at ~400ms
```

## Tech Stack

| Layer     | Service              | Role                        |
|-----------|----------------------|-----------------------------|
| STT       | Deepgram Nova-2      | Streaming speech-to-text    |
| LLM       | Groq + Llama 3.3 70B | High-speed token generation |
| TTS       | OpenAI TTS (tts-1)   | Streaming text-to-speech    |
| Transport | WebSockets           | Low-latency full-duplex     |
| Backend   | FastAPI              | Async pipeline server       |
| Frontend  | HTML + JavaScript    | Browser mic + audio player  |

---

## Project Structure

```
cascade/
├── backend/
│   ├── config.py       # Env vars and model configuration
│   ├── main.py         # FastAPI app + health endpoints
│   ├── pipeline.py     # Core streaming pipeline (Phase 2)
│   ├── stt.py          # Deepgram integration (Phase 2)
│   ├── llm.py          # Groq integration + chunker (Phase 2)
│   ├── tts.py          # OpenAI TTS integration (Phase 2)
│   └── tutor.py        # Tutor persona + context (Phase 3)
├── frontend/
│   ├── index.html      # Main UI (Phase 4)
│   ├── app.js          # WebSocket client (Phase 4)
│   └── style.css       # Styling (Phase 4)
├── tests/
│   ├── verify_all.py   # Master verification runner
│   ├── test_stt.py     # Deepgram verification
│   ├── test_llm.py     # Groq verification
│   └── test_tts.py     # OpenAI TTS verification
├── .env.example        # API key template
├── requirements.txt
└── README.md
```

---

## Setup — Phase 1

### 1. Clone and create a virtual environment

```bash
git clone <your-repo-url> cascade
cd cascade
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure API keys

```bash
cp .env.example .env
```

Open `.env` and fill in your three API keys:

| Key                  | Where to get it                          | Cost               |
|----------------------|------------------------------------------|--------------------|
| `DEEPGRAM_API_KEY`   | console.deepgram.com                     | Free $200 credit   |
| `GROQ_API_KEY`       | console.groq.com                         | Free tier          |
| `OPENAI_API_KEY`     | platform.openai.com → Billing → Load $10 | ~$4.50 for demo    |

> **Important:** After loading funds on OpenAI, set a hard usage limit of
> $10 under **Settings → Billing → Usage Limits** to prevent overruns.

### 4. Verify all API connections

```bash
python tests/verify_all.py
```

Expected output:

```
════════════════════════════════════════════════════════
  CASCADE — Phase 1 API Verification
════════════════════════════════════════════════════════
  Checking all required API connections...

── Deepgram STT Verification ─────────────────────────────
  [1/3] Checking API key...
        ✓ Key found: sk-abc123...wxyz
  [2/3] Initialising Deepgram client...
        ✓ Client initialised
  [3/3] Opening live transcription connection...
        ✓ Live connection opened and closed cleanly
  ✓ Deepgram STT — ALL CHECKS PASSED

── Groq LLM Verification ─────────────────────────────────
  [1/4] Checking API key...
        ✓ Key found: gsk-abc1...wxyz
  [2/4] Initialising Groq client...
        ✓ Client initialised
  [3/4] Testing standard completion (model: llama-3.3-70b-versatile)...
        ✓ Response received in 312ms
        → "The Pythagorean theorem states that..."
  [4/4] Testing streaming completion...
        ✓ First token in 187ms
        ✓ 24 tokens received via stream
  ✓ Groq LLM — ALL CHECKS PASSED

── OpenAI TTS Verification ───────────────────────────────
  [1/4] Checking API key...
        ✓ Key found: sk-abc123...wxyz
  [2/4] Initialising OpenAI client...
        ✓ Client initialised
  [3/4] Testing standard TTS (model: tts-1, voice: nova)...
        ✓ Audio received in 891ms
        ✓ Audio size: 48,320 bytes
  [4/4] Testing streaming TTS...
        ✓ First chunk in 412ms
        ✓ 12 chunks, 48,320 bytes total
  ✓ OpenAI TTS — ALL CHECKS PASSED

════════════════════════════════════════════════════════
  VERIFICATION SUMMARY
════════════════════════════════════════════════════════
  ✓  Deepgram STT       PASSED
  ✓  Groq LLM           PASSED
  ✓  OpenAI TTS         PASSED

  Completed in 6.43s
════════════════════════════════════════════════════════
  ✓  All checks passed. Ready to build Phase 2.
```

### 5. Start the API server (optional for Phase 1)

```bash
uvicorn backend.main:app --reload
```

Then open: [http://localhost:8000/health](http://localhost:8000/health)

---

## Phases

| Phase | Description                          | Status      |
|-------|--------------------------------------|-------------|
| 1     | Project setup & API verification     | ✓ Complete  |
| 2     | Streaming pipeline core              | Upcoming    |
| 3     | Tutor logic & context management     | Upcoming    |
| 4     | Frontend & latency demo UI           | Upcoming    |

---

## Budget

| Service      | Estimated Cost | Notes                    |
|--------------|----------------|--------------------------|
| Deepgram STT | $0.86          | Covered by free credit   |
| Groq LLM     | ~$0.30         | Covered by free tier     |
| OpenAI TTS   | ~$4.50         | Paid from your $10 load  |
| **Total**    | **~$5.66**     | Well within $10 budget   |
