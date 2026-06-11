/**
 * Cascade — AI Tutor Frontend
 *
 * Fixes applied:
 *  [C2] stopSession() now sends plain string "stop" (not JSON) — server
 *       check is `text == "stop"` so JSON object never matched.
 *  [H1] onAudioProcess now gates on STATE.LISTENING — audio is no longer
 *       forwarded to Deepgram during PROCESSING or SPEAKING, preventing
 *       phantom transcripts from the tutor's own TTS audio.
 *  [H2] firstAudioTime reset at start of every turn (not only on stopSession).
 *  [H3] lastUtteredTime, silenceStartTime reset at start of every turn so
 *       silence detection doesn't fire immediately after a response.
 *  [H4] maxAudioLevel reset on session start so adaptive threshold is fresh.
 *  [H5] AudioContext.resume() called before decoding audio — required by
 *       browser autoplay policy (context starts in 'suspended' state).
 *  [M1] ScriptProcessor fallback now sends raw PCM16 bytes (no WAV header)
 *       to match Deepgram's encoding:"linear16" expectation.
 *  [M2] State returns to LISTENING only when audio queue is empty (inside
 *       playNextAudioChunk) — not via a fixed 500ms timeout that could fire
 *       while audio is still playing and Deepgram picks up TTS output.
 */

const CONFIG = {
  WS_HOST: window.location.hostname || "localhost",
  WS_PORT: window.location.port || "8000",
  SILENCE_DURATION_MS: 800,
};

const STATE = {
  IDLE: "IDLE",
  LISTENING: "LISTENING",
  PROCESSING: "PROCESSING",
  SPEAKING: "SPEAKING",
};

class CascadeClient {
  constructor() {
    this.state = STATE.IDLE;
    this.ws = null;
    this.audioContext = null;
    this.processor = null;
    this.sourceNode = null;
    this.sinkNode = null;
    this.mediaStream = null;
    this.isRecording = false;

    // Audio playback
    this.audioPlaybackQueue = [];
    this.isPlaying = false;

    // Per-turn latency tracking — reset each turn [H2][H3]
    this.utteranceStartTime = null;
    this.firstAudioTime = null;
    this.lastUtteredTime = null;
    this.silenceStartTime = null;

    // Adaptive silence threshold — reset on session start [H4]
    this.maxAudioLevel = 0;

    // Intentional disconnect flag — prevents reconnection loop on manual stop
    this.intentionalDisconnect = false;

    // Reconnection
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 3;

    // Response accumulation
    this.currentResponse = "";

    // UI references
    this.startBtn = document.getElementById("start-btn");
    this.statusDot = document.getElementById("status-dot");
    this.statusText = document.getElementById("status-text");
    this.latencyValue = document.getElementById("latency-value");
    this.transcriptList = document.getElementById("transcript-list");
    this.subjectSelect = document.getElementById("subject-select");

    this.startBtn.addEventListener("click", () => this.toggleSession());
    this._initAudioContext();
  }

  _initAudioContext() {
    try {
      this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
    } catch (err) {
      console.error("AudioContext not available:", err);
    }
  }

  // ── Session lifecycle ────────────────────────────────────────────────

  async toggleSession() {
    if (this.state === STATE.IDLE) {
      await this.startSession();
    } else {
      await this.stopSession();
    }
  }

  async startSession() {
    try {
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: false,
          sampleRate: { ideal: 16000 },
        },
      });

      // ── FIX [H5]: Resume AudioContext (browser autoplay policy) ─────
      // Browsers start AudioContext in 'suspended' state until a user
      // gesture occurs. resuming here guarantees decodeAudioData works.
      if (this.audioContext && this.audioContext.state === "suspended") {
        await this.audioContext.resume();
      }

      // ── FIX [H4]: Reset adaptive threshold for fresh session ─────────
      this.maxAudioLevel = 0;

      await this._initAudioProcessing();
      this.intentionalDisconnect = false;
      await this._connectWebSocket();

      this.setState(STATE.LISTENING);
      this.startBtn.textContent = "Stop Session";
      this.subjectSelect.disabled = true;
      this.transcriptList.innerHTML = "";
      this.addTranscriptItem("welcome", "Connected — ask me a question.");
    } catch (err) {
      console.error("startSession failed:", err);
      let msg = `Failed to start: ${err.message}`;
      if (err.name === "NotAllowedError") {
        msg = "🔒 Microphone permission denied. Enable it in browser settings.";
      } else if (err.name === "NotFoundError") {
        msg = "🎤 No microphone found on this device.";
      } else if (err.message.includes("WebSocket")) {
        msg = "🌐 Could not connect to server. Is the backend running?";
      }
      this.showError(msg);
      await this.stopSession();
    }
  }

  async stopSession() {
    this.isRecording = false;
    this.intentionalDisconnect = true;

    // ── FIX [C2]: Send plain string "stop" — server checks text == "stop" ──
    // The original code sent JSON.stringify({type:"stop"}) which never
    // matched the server's string equality check.
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send("stop");
        await new Promise((r) => setTimeout(r, 100));
      } catch (_) {}
      try { this.ws.close(); } catch (_) {}
    }
    this.ws = null;

    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((t) => { try { t.stop(); } catch (_) {} });
      this.mediaStream = null;
    }

    if (this.processor) {
      try {
        if (this.processor.port) {
          this.processor.port.onmessage = null;
          this.processor.port.close();
        }
        this.processor.disconnect();
      } catch (_) {}
      this.processor = null;
    }

    if (this.sourceNode) { try { this.sourceNode.disconnect(); } catch (_) {} this.sourceNode = null; }
    if (this.sinkNode)   { try { this.sinkNode.disconnect();   } catch (_) {} this.sinkNode = null; }

    this.audioPlaybackQueue = [];
    this.isPlaying = false;
    this.currentResponse = "";
    this._resetTurnState();

    this.setState(STATE.IDLE);
    this.startBtn.textContent = "Start Session";
    this.subjectSelect.disabled = false;
    this.latencyValue.textContent = "—";
    this.latencyValue.classList.remove("active");
  }

  // ── Per-turn state reset ─────────────────────────────────────────────

  /**
   * Reset all per-turn latency and silence-detection state.
   * Called at the start of each new turn so previous values
   * don't corrupt the next turn's measurements [H2][H3].
   */
  _resetTurnState() {
    this.utteranceStartTime = null;
    this.firstAudioTime = null;
    this.lastUtteredTime = null;
    this.silenceStartTime = null;
  }

  // ── Audio capture ────────────────────────────────────────────────────

  async _initAudioProcessing() {
    if (!this.mediaStream || !this.audioContext) return;

    const source = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.sourceNode = source;
    const sink = this.audioContext.createGain();
    sink.gain.value = 0;
    this.sinkNode = sink;

    const workletCode = this._getWorkletCode();

    try {
      const blob = new Blob([workletCode], { type: "application/javascript" });
      const url = URL.createObjectURL(blob);
      await this.audioContext.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);

      this.processor = new AudioWorkletNode(this.audioContext, "audio-processor");
      this.processor.port.onmessage = (evt) => this._onAudioData(evt.data);
      source.connect(this.processor);
      this.processor.connect(sink);
      sink.connect(this.audioContext.destination);
      console.log("✓ AudioWorklet ready");
    } catch (_) {
      // ── FIX [M1]: ScriptProcessor fallback sends raw PCM16 ───────────
      // Original pcmEncode() wrapped samples in a 44-byte WAV header.
      // Deepgram expects encoding:"linear16" = raw signed-16-bit PCM.
      // The WAV header corrupted the first audio chunk of every session.
      console.warn("AudioWorklet unavailable — falling back to ScriptProcessor");
      this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
      this.processor.onaudioprocess = (evt) => {
        const float32 = evt.inputBuffer.getChannelData(0);
        const pcm16 = new Int16Array(float32.length);
        for (let i = 0; i < float32.length; i++) {
          const s = Math.max(-1, Math.min(1, float32[i]));
          pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this._onAudioData({ type: "audio", data: pcm16.buffer });
      };
      source.connect(this.processor);
      this.processor.connect(sink);
      sink.connect(this.audioContext.destination);
    }

    this.isRecording = true;
  }

  _getWorkletCode() {
    return `
      class AudioProcessor extends AudioWorkletProcessor {
        process(inputs) {
          const input = inputs[0][0];
          if (input && input.length > 0) {
            const pcm16 = new Int16Array(input.length);
            for (let i = 0; i < input.length; i++) {
              const s = Math.max(-1, Math.min(1, input[i]));
              pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
            }
            this.port.postMessage({ type: "audio", data: pcm16.buffer }, [pcm16.buffer]);
          }
          return true;
        }
      }
      registerProcessor("audio-processor", AudioProcessor);
    `;
  }

  _onAudioData(data) {
    // ── FIX [H1]: Only forward audio during LISTENING ────────────────
    // Original code sent audio in all non-IDLE states. During PROCESSING
    // and SPEAKING, the microphone was still open and feeding raw bytes
    // to Deepgram — including the tutor's own TTS audio through the
    // speakers, causing phantom transcripts and feedback loops.
    if (!this.isRecording || this.state !== STATE.LISTENING) return;

    let bytes;
    if (data && data.type === "audio" && data.data) {
      bytes = new Uint8Array(data.data);
    } else if (ArrayBuffer.isView(data)) {
      bytes = new Uint8Array(data.buffer);
    } else {
      return;
    }

    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(bytes);
    }

    this._detectSilence(bytes);
  }

  _detectSilence(bytes) {
    if (!bytes || bytes.length < 4) return;

    // Calculate RMS from PCM16 samples
    let sum = 0;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const numSamples = Math.floor(bytes.byteLength / 2);

    for (let i = 0; i < numSamples; i++) {
      const s = view.getInt16(i * 2, true) / 32768;
      sum += s * s;
    }
    const rms = numSamples > 0 ? Math.sqrt(sum / numSamples) : 0;

    if (rms > this.maxAudioLevel) this.maxAudioLevel = rms;

    const threshold = Math.max(0.02, this.maxAudioLevel * 0.05);

    if (rms < threshold) {
      if (!this.silenceStartTime) {
        this.silenceStartTime = Date.now();
      } else if (
        Date.now() - this.silenceStartTime > CONFIG.SILENCE_DURATION_MS &&
        this.state === STATE.LISTENING &&
        this.lastUtteredTime
      ) {
        // End of utterance — transition to PROCESSING
        this.utteranceStartTime = Date.now();
        this.silenceStartTime = null;
        this.setState(STATE.PROCESSING);
      }
    } else {
      this.silenceStartTime = null;
      this.lastUtteredTime = Date.now();
    }
  }

  // ── WebSocket ────────────────────────────────────────────────────────

  _connectWebSocket() {
    return new Promise((resolve, reject) => {
      const subject = this.subjectSelect.value || "";
      const wsUrl = `ws://${CONFIG.WS_HOST}:${CONFIG.WS_PORT}/ws${subject ? `?subject=${encodeURIComponent(subject)}` : ""}`;

      console.log(`Connecting to ${wsUrl}`);
      this.ws = new WebSocket(wsUrl);
      this.ws.binaryType = "arraybuffer";

      const timeout = setTimeout(() => {
        if (this.ws && this.ws.readyState !== WebSocket.OPEN) {
          reject(new Error("WebSocket connection timed out"));
        }
      }, 5000);

      this.ws.onopen = () => {
        clearTimeout(timeout);
        this.reconnectAttempts = 0;
        console.log("✓ WebSocket connected");
        resolve();
      };

      this.ws.onmessage = (evt) => {
        if (evt.data instanceof ArrayBuffer) {
          this._onAudioChunk(evt.data);
        } else {
          try {
            this._onServerMessage(JSON.parse(evt.data));
          } catch (_) {
            console.warn("Unparseable server message:", evt.data);
          }
        }
      };

      this.ws.onerror = (err) => {
        clearTimeout(timeout);
        reject(new Error("WebSocket error"));
      };

      this.ws.onclose = () => {
        clearTimeout(timeout);
        // Only reconnect if disconnection was unexpected (not user-initiated)
        if (
          !this.intentionalDisconnect &&
          this.state !== STATE.IDLE &&
          this.reconnectAttempts < this.maxReconnectAttempts
        ) {
          this.reconnectAttempts++;
          const delay = 1000 * Math.pow(2, this.reconnectAttempts - 1);
          console.log(
            `[Client] WebSocket reconnection attempt ${this.reconnectAttempts} (delay: ${delay}ms)`
          );
          setTimeout(() => {
            this._connectWebSocket().catch(() => {
              if (this.reconnectAttempts >= this.maxReconnectAttempts) {
                console.log("[Client] Max reconnection attempts reached");
                this.showError("Connection lost. Please start a new session.");
                this.stopSession();
              }
            });
          }, delay);
        } else if (!this.intentionalDisconnect && this.state !== STATE.IDLE) {
          this.showError("Connection lost. Please start a new session.");
          this.stopSession();
        }
      };
    });
  }

  // ── Server message handling ──────────────────────────────────────────

  _onServerMessage(msg) {
    if (!msg || typeof msg !== "object") return;

    switch (msg.type) {
      case "transcript":
        if (msg.text) {
          // ── FIX [H3]: Reset turn state when a new transcript arrives ──
          // Prevents stale lastUtteredTime from causing immediate silence
          // detection at the start of the next turn.
          this._resetTurnState();
          this.addTranscriptItem("student", msg.text);
        }
        break;

      case "response_chunk":
        if (msg.text) {
          this.currentResponse = (this.currentResponse || "") + msg.text;
        }
        break;

      case "response_end":
        if (this.currentResponse && this.currentResponse.trim()) {
          this.addTranscriptItem("tutor", this.currentResponse.trim());
        }
        this.currentResponse = "";
        // NOTE: Do NOT set state back to LISTENING here [M2 fix].
        // State transitions to LISTENING inside playNextAudioChunk()
        // when the queue drains — ensuring we only listen after audio ends.
        break;

      case "latency":
        if (typeof msg.ms === "number") {
          this._displayLatency(msg.ms);
        }
        break;

      case "busy":
        // [M4] Server dropped a concurrent transcript — show user feedback
        this.addTranscriptItem(
          "info",
          "⏳ Still responding — please wait a moment."
        );
        break;

      case "error":
        this.showError(msg.message || "Unknown server error");
        break;

      default:
        console.debug("Unhandled message type:", msg.type);
    }
  }

  // ── Audio playback ───────────────────────────────────────────────────

  _onAudioChunk(arrayBuffer) {
    // ── FIX [H2]: Track first audio time per turn ─────────────────────
    // Original: firstAudioTime only reset in stopSession() so turn 2+
    // latency was calculated from null or a stale value.
    if (this.firstAudioTime === null && this.utteranceStartTime !== null) {
      this.firstAudioTime = Date.now();
      const latency = this.firstAudioTime - this.utteranceStartTime;
      this._displayLatency(latency);
      console.log(`[Client] First audio: ${latency}ms`);
    }

    this.audioPlaybackQueue.push(arrayBuffer);
    if (!this.isPlaying) {
      this._playNextChunk();
    }
  }

  async _playNextChunk() {
    if (this.audioPlaybackQueue.length === 0) {
      this.isPlaying = false;

      // ── FIX [M2]: Return to LISTENING only when queue is empty ───────
      // Original: 500ms setTimeout in response_end handler returned to
      // LISTENING while audio could still be queued. Deepgram then picked
      // up the TTS audio output as a new utterance, creating a feedback
      // loop. Now the transition happens here — after the last chunk plays.
      if (this.state === STATE.SPEAKING) {
        this.setState(STATE.LISTENING);
        // Reset firstAudioTime so the next turn measures correctly [H2]
        this.firstAudioTime = null;
      }
      return;
    }

    this.isPlaying = true;
    this.setState(STATE.SPEAKING);

    // ── FIX [H5]: Ensure AudioContext is running before decoding ──────
    if (this.audioContext && this.audioContext.state === "suspended") {
      try { await this.audioContext.resume(); } catch (_) {}
    }

    const arrayBuffer = this.audioPlaybackQueue.shift();

    try {
      const decoded = await this.audioContext.decodeAudioData(arrayBuffer);
      const source = this.audioContext.createBufferSource();
      source.buffer = decoded;
      source.connect(this.audioContext.destination);
      await new Promise((resolve) => {
        source.onended = resolve;
        source.start(0);
      });
    } catch (err) {
      console.error("Audio decode failed:", err);
    }

    // Play next chunk (recursive — drains queue)
    this._playNextChunk();
  }

  // ── UI helpers ───────────────────────────────────────────────────────

  setState(newState) {
    this.state = newState;
    const map = {
      [STATE.IDLE]:       { cls: "",           text: "Ready" },
      [STATE.LISTENING]:  { cls: "listening",  text: "🎤 Listening" },
      [STATE.PROCESSING]: { cls: "processing", text: "⚙️ Processing" },
      [STATE.SPEAKING]:   { cls: "speaking",   text: "🔊 Speaking" },
    };
    const cfg = map[newState] || map[STATE.IDLE];
    this.statusDot.className = `status-dot ${cfg.cls}`;
    this.statusText.textContent = cfg.text;
  }

  _displayLatency(ms) {
    this.latencyValue.textContent = `${Math.round(ms)}ms`;
    this.latencyValue.classList.add("active");
  }

  addTranscriptItem(type, text) {
    // Remove welcome placeholder on first real message
    const welcome = this.transcriptList.querySelector(".welcome");
    if (welcome) welcome.remove();

    const item = document.createElement("div");
    item.className = `transcript-item ${type}`;
    const p = document.createElement("p");
    p.textContent = text; // textContent escapes HTML automatically
    item.appendChild(p);
    this.transcriptList.appendChild(item);
    this.transcriptList.scrollTop = this.transcriptList.scrollHeight;
  }

  showError(message) {
    console.error("[Error]", message);
    this.addTranscriptItem("error", `❌ ${message}`);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  window.cascadeClient = new CascadeClient();
});
