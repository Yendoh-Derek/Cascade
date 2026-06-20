# Cascade — AI Voice Tutor

A low-latency AI tutoring voice agent built on a fully streaming pipeline.
Students ask questions by voice and receive spoken responses in under 800ms,
demonstrating that voice agent latency is a pipeline design problem — not a
hardware or model problem.

---

## Features

* **Barge-in / Interruption Gating**: High-fidelity interruption model utilizing turn-id and audio epoch tracking to atomically suppress stale text/audio from interrupted turns at the network boundary.
* **Live Latency Dashboard**: Interactive, real-time stacked chart displaying STT, LLM (queue, TTFT, stream), and TTS latency breakdowns so you can analyze pipeline performance.
* **Dual TTS Engines**: Live toggle in the UI between high-speed **Deepgram Aura** (default, requires API key) and **Microsoft edge-tts** (free fallback, no key required).
* **Robust STT Reconnection**: Deepgram client connection automatically recovers on unexpected socket drops with capped exponential backoff and toast notifications.

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

---

## Architectural Decisions & Latency Measurement

### Why WebSockets?
To achieve sub-second voice interactions, HTTP requests are too heavy. Cascade uses a full-duplex WebSocket connection to stream raw PCM16 audio from the microphone to the server and stream back MP3/PCM audio chunks concurrently.

### Sentence-Level Chunking
TTS engines generate much more natural speech when they synthesize full sentences rather than word-by-word. Cascade buffers LLM streaming tokens server-side and yields complete sentences to the TTS queue as soon as a punctuation boundary (e.g. `.`, `?`, `!`) is detected. This introduces a slight latency floor for the first sentence, but guarantees premium audio quality.

### Latency Measurement Model
Latency in Cascade is measured server-side and client-side as follows:
1. **STT Processing**: The interval between the last audio frame sent and Deepgram returning the confirmed transcript (`speech_final`).
2. **LLM Generation**: Segmented into Queue Time, Time to First Token (TTFT), and Streaming Delay (time to emit the first complete sentence).
3. **TTS Synthesis**: Time to synthesize the first sentence.
4. **End-to-End Latency**: Measured from the instant the STT confirms the utterance to the time the first byte of TTS audio is received. This is visualized live in the frontend chart.

---

## Tech Stack

| Layer     | Service                                 | Role                        |
| --------- | --------------------------------------- | --------------------------- |
| STT       | Deepgram Nova-2                         | Streaming speech-to-text    |
| LLM       | Groq + Llama 3.3 70B                    | High-speed token generation |
| TTS       | **Deepgram Aura** (Default) / **edge-tts** (Fallback) | Streaming TTS options |
| Transport | WebSockets                              | Low-latency full-duplex     |
| Backend   | FastAPI                                 | Async pipeline server       |
| Frontend  | HTML + CSS + JS (Vanilla)               | UI + Audio processing       |

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
│   ├── app.js          # Coordinator ES6 module
│   ├── audio-input.js  # Audio capturing & VAD module
│   ├── audio-output.js # Audio playback & interruption module
│   ├── transport.js    # WebSocket connection module
│   ├── chart.js        # Canvas latency chart module
│   ├── ui.js           # UI layout and interactive elements module
│   └── style.css       # Styling
├── tests/
│   ├── verify_all.py   # Master verification runner
│   ├── test_stt.py     # Deepgram verification
│   ├── test_llm.py     # Groq verification
│   ├── test_tts.py     # edge-tts verification
│   └── test_tutor.py   # Tutor integration check
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

Open `.env` and fill in your API keys:

| Key | Required | Purpose |
| --- | --- | --- |
| `DEEPGRAM_API_KEY` | **Yes** (Default) | Used for STT and default Deepgram Aura TTS |
| `GROQ_API_KEY` | **Yes** | Used for LLM inference |

*Note: If you run with the **edge-tts** fallback engine selected in the UI, Cascade does not invoke Deepgram's TTS services, but `DEEPGRAM_API_KEY` is still required for speech recognition (STT).*

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

## Known Limitations

- **Sentence-Boundary Heuristic**: Boundary splitting in `llm.py` handles decimals and typical abbreviations (e.g. `Dr.`, `e.g.`) but may occasionally mis-split on atypical abbreviations (e.g. `approx. 12kg`, `No. 5`).
- **Per-Sentence TTS Latency Floor**: The TTS engines synthesize a full sentence before yielding the first byte (essential for natural phrasing). Consequently, a very long first sentence sets a higher latency floor.
- **Built-in Authentication**: Minimal access control is supported via the optional `CASCADE_AUTH_SECRET` environment variable for basic private setups, but this does not replace production-grade gateway access controls.
