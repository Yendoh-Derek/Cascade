# Cascade — AI Voice Tutor

A low-latency AI tutoring voice agent built on a fully streaming pipeline.
Students ask questions by voice and receive spoken responses in under 800ms,
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
                       [TTS ████]  ← student hears this at ~400–800ms
```

## Tech Stack

| Layer     | Service                        | Role                        |
|-----------|--------------------------------|-----------------------------|
| STT       | Deepgram Nova-2                | Streaming speech-to-text    |
| LLM       | Groq + Llama 3.3 70B           | High-speed token generation |
| TTS       | edge-tts (Microsoft Neural)    | Free streaming TTS, no key  |
| Transport | WebSockets                     | Low-latency full-duplex     |
| Backend   | FastAPI                        | Async pipeline server       |
| Frontend  | HTML + JavaScript              | Browser mic + audio player  |

---

## Project Structure

```
cascade/
├── backend/
│   ├── config.py       # Env vars and model configuration
│   ├── main.py         # FastAPI app, health check, WebSocket endpoint
│   ├── pipeline.py     # Core streaming pipeline orchestrator
│   ├── stt.py          # Deepgram Nova-2 integration
│   ├── llm.py          # Groq streaming + sentence chunker
│   ├── tts.py          # edge-tts streaming integration
│   └── tutor.py        # Tutor persona + conversation history
├── frontend/
│   ├── index.html      # Main UI
│   ├── app.js          # WebSocket client, mic, audio playback, latency timer
│   └── style.css       # Styling
├── tests/
│   ├── verify_all.py   # Master verification runner
│   ├── test_stt.py     # Deepgram verification
│   ├── test_llm.py     # Groq verification
│   └── test_tts.py     # edge-tts verification
├── .env.example
├── requirements.txt
└── README.md
```

---

## Setup

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

Open `.env` and fill in your two API keys:

| Key                | Where to get it         | Cost             |
|--------------------|-------------------------|------------------|
| `DEEPGRAM_API_KEY` | console.deepgram.com    | Free $200 credit |
| `GROQ_API_KEY`     | console.groq.com        | Free tier        |

> edge-tts requires no API key or account.

### 4. Verify all API connections

```bash
python tests/verify_all.py
```

### 5. Start the server

```bash
uvicorn backend.main:app --reload
```

Open: [http://localhost:8000](http://localhost:8000)

---

## Phases

| Phase | Description                      | Status     |
|-------|----------------------------------|------------|
| 1     | Project setup & API verification | ✓ Complete |
| 2     | Streaming pipeline core          | ✓ Complete |
| 3     | Tutor logic & context management | ✓ Complete |
| 4     | Frontend & latency demo UI       | ✓ Complete |

---

## Budget

| Service      | Estimated Cost | Notes                  |
|--------------|----------------|------------------------|
| Deepgram STT | $0.86          | Free $200 credit       |
| Groq LLM     | ~$0.30         | Free tier              |
| edge-tts     | $0.00          | Free, no account       |
| **Total**    | **~$1.16**     | Well within $10 budget |
