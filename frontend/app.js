/**
 * Cascade — AI Voice Tutor Frontend
 */

const CONFIG = {
  WS_HOST: window.location.hostname || "localhost",
  WS_PORT: window.location.port || "8000",
  SILENCE_DURATION_MS: 450,
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
    this.analyser = null;
    this.processor = null;
    this.sourceNode = null;
    this.sinkNode = null;
    this.mediaStream = null;
    this.isRecording = false;

    this.isPlaying = false;
    this.utteranceStartTime = null;
    this.firstAudioTime = null;
    this.lastUtteredTime = null;
    this.silenceStartTime = null;
    this.sessionStartTime = null;
    this.nextPlaybackTime = null;
    this.isAudioSourceEnded = false;
    this.activeSourceNodes = [];
    this.maxAudioLevel = 0;
    this.intentionalDisconnect = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 3;
    this.currentResponse = "";
    this.totalTurns = 0;
    this.lastLatencyMs = null;
    this.lastModel = "edge-tts";
    this.ttsConfig = { format: "mp3", sampleRate: 24000 };
    this.selectedTTSEngine =
      localStorage.getItem("cascade_tts_engine") || "edge";

    this.orb = document.getElementById("orb");
    this.transcriptPanel = document.getElementById("transcript-panel");
    this.statusText = document.getElementById("status-text");
    this.btnToggleSession = document.getElementById("btn-toggle-session");
    this.btnClearTranscript = document.getElementById("btn-clear-transcript");
    this.btnMute = document.getElementById("btn-mute");
    this.statsBar = document.getElementById("stats-bar");
    this.transcriptEmpty = document.getElementById("transcript-empty");
    this.ttsEngineSelector = document.getElementById("tts-engine-selector");

    this.isMuted = false;
    this._audioResumed = false;

    this._initUIListeners();
    this._initAudioContext();
    this._restoreTTSSelection();

    // Passively update relative timestamps every 30s
    setInterval(() => this._updateTimestamps(), 30000);
  }

  _restoreTTSSelection() {
    if (this.ttsEngineSelector) {
      this.ttsEngineSelector.value = this.selectedTTSEngine;
      this.lastModel =
        this.selectedTTSEngine === "edge" ? "edge-tts" : "aura-asteria";
      this._updateStatsBar();
    }
  }

  _initUIListeners() {
    if (this.orb) {
      const orbShell = this.orb.querySelector(".orb-shell");
      this.orb.addEventListener("pointerdown", () => {
        if (orbShell) {
          orbShell.style.transitionProperty = "transform";
          orbShell.style.transitionDuration = "100ms";
          orbShell.style.transitionTimingFunction = "ease-in";
          orbShell.style.transform = "scale(0.92)";
        }
      });
      this.orb.addEventListener("pointerup", (evt) => {
        if (orbShell) {
          orbShell.style.transitionProperty = "transform";
          orbShell.style.transitionDuration = "350ms";
          orbShell.style.transitionTimingFunction = "var(--spring)";
          orbShell.style.transform = "scale(1.08)";
          setTimeout(() => {
            orbShell.style.transform = "scale(1)";
          }, 200);

          // Create tap ripple effect
          const rect = orbShell.getBoundingClientRect();
          const ripple = document.createElement("div");
          ripple.className = "tap-ripple";
          const x = evt.clientX - rect.left;
          const y = evt.clientY - rect.top;
          ripple.style.left = `${x}px`;
          ripple.style.top = `${y}px`;
          orbShell.appendChild(ripple);
          setTimeout(() => ripple.remove(), 500);
        }
        this.toggleSession();
      });
      this.orb.addEventListener("keydown", (evt) => {
        if (evt.key === " " || evt.key === "Enter") {
          evt.preventDefault();
          this.toggleSession();
        }
      });
    }

    if (this.btnToggleSession) {
      this.btnToggleSession.addEventListener("click", () =>
        this.toggleSession(),
      );
    }

    if (this.btnClearTranscript) {
      this.btnClearTranscript.addEventListener("click", () => {
        if (this.transcriptPanel) this.transcriptPanel.innerHTML = "";
        this._resetTurnState();
        this._updateStatsBar();
        this._showEmptyStateIfNeeded();
      });
    }

    if (this.btnMute) {
      this.btnMute.addEventListener("click", () => this.toggleMute());
    }

    if (this.ttsEngineSelector) {
      this.ttsEngineSelector.addEventListener("change", (evt) => {
        this.selectedTTSEngine = evt.target.value;
        localStorage.setItem("cascade_tts_engine", this.selectedTTSEngine);
        this.lastModel =
          this.selectedTTSEngine === "edge" ? "edge-tts" : "aura-asteria";
        this._updateStatsBar();
      });
    }
  }

  toggleMute() {
    this.isMuted = !this.isMuted;
    if (this.btnMute) {
      this.btnMute.classList.toggle("active", this.isMuted);
      const label = this.btnMute.querySelector(".btn-label");
      if (label) label.textContent = this.isMuted ? "Mic On" : "Mic Off";
    }
  }

  _initAudioContext() {
    try {
      this.audioContext = new (
        window.AudioContext || window.webkitAudioContext
      )();
      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 256;
      this.analyser.connect(this.audioContext.destination);
    } catch (err) {
      console.error("AudioContext not available:", err);
    }
  }

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
        },
      });

      if (this.audioContext && this.audioContext.state === "suspended") {
        await this._resumeAudioContext();
      }

      this.maxAudioLevel = 0;
      await this._initAudioProcessing();
      this.intentionalDisconnect = false;
      await this._connectWebSocket();
      this.setState(STATE.LISTENING);
      this.sessionStartTime = Date.now();
      if (this.transcriptPanel) this.transcriptPanel.innerHTML = "";
      this._showEmptyStateIfNeeded();
    } catch (err) {
      console.error("startSession failed:", err);
      let msg = `Failed to start: ${err.message}`;
      if (err.name === "NotAllowedError") {
        msg = "🔒 Microphone permission denied. Enable it in browser settings.";
      } else if (err.name === "NotFoundError") {
        msg = "🎤 No microphone found on this device.";
      }
      this.showError(msg);
      await this.stopSession();
    }
  }

  async stopSession() {
    this.isRecording = false;
    this.intentionalDisconnect = true;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send("stop");
        await new Promise((r) => setTimeout(r, 100));
      } catch (_) {}
      try {
        this.ws.close();
      } catch (_) {}
    }
    this.ws = null;

    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((t) => {
        try {
          t.stop();
        } catch (_) {}
      });
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

    if (this.sourceNode) {
      try {
        this.sourceNode.disconnect();
      } catch (_) {}
      this.sourceNode = null;
    }
    if (this.sinkNode) {
      try {
        this.sinkNode.disconnect();
      } catch (_) {}
      this.sinkNode = null;
    }

    if (this.activeSourceNodes) {
      this.activeSourceNodes.forEach((source) => {
        try {
          source.stop();
        } catch (_) {}
      });
    }
    this.activeSourceNodes = [];
    this.nextPlaybackTime = null;
    this.isAudioSourceEnded = false;
    this.isPlaying = false;
    this.currentResponse = "";
    this._resetTurnState();
    this.sessionStartTime = null;

    if (this.isMuted) this.toggleMute();

    this.setState(STATE.IDLE);
  }

  _resetTurnState() {
    this.utteranceStartTime = null;
    this.firstAudioTime = null;
    this.lastUtteredTime = null;
    this.silenceStartTime = null;
    this.nextPlaybackTime = null;
    this.isAudioSourceEnded = false;
    this.activeSourceNodes = [];
    this.maxAudioLevel = 0;
  }

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

      this.processor = new AudioWorkletNode(
        this.audioContext,
        "audio-processor",
      );
      this.processor.port.onmessage = (evt) => this._onAudioData(evt.data);
      source.connect(this.processor);
      this.processor.connect(sink);
      sink.connect(this.audioContext.destination);
      console.log("✓ AudioWorklet ready");
    } catch (_) {
      console.warn(
        "AudioWorklet unavailable — falling back to ScriptProcessor",
      );
      this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
      this.processor.onaudioprocess = (evt) => {
        const inputData = evt.inputBuffer.getChannelData(0);
        const ratio = this.audioContext.sampleRate / 16000;
        const downsampled = new Float32Array(
          Math.floor(inputData.length / ratio),
        );
        for (let i = 0; i < downsampled.length; i++) {
          downsampled[i] = inputData[Math.floor(i * ratio)];
        }
        const pcm16 = new Int16Array(downsampled.length);
        for (let i = 0; i < downsampled.length; i++) {
          const s = Math.max(-1, Math.min(1, downsampled[i]));
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
        constructor() {
          super();
          this.bufferSize = 2048; // ~128ms of audio at 16kHz
          this.buffer = new Int16Array(this.bufferSize);
          this.bufferWriteIndex = 0;
        }

        process(inputs) {
          const input = inputs[0][0];
          if (input && input.length > 0) {
            const ratio = sampleRate / 16000;
            const downsampled = new Float32Array(Math.floor(input.length / ratio));
            for (let i = 0; i < downsampled.length; i++) {
              downsampled[i] = input[Math.floor(i * ratio)];
            }
            
            for (let i = 0; i < downsampled.length; i++) {
              const s = Math.max(-1, Math.min(1, downsampled[i]));
              this.buffer[this.bufferWriteIndex++] = s < 0 ? s * 0x8000 : s * 0x7fff;
              
              if (this.bufferWriteIndex >= this.bufferSize) {
                const outBuffer = this.buffer;
                this.port.postMessage({ type: "audio", data: outBuffer.buffer }, [outBuffer.buffer]);
                this.buffer = new Int16Array(this.bufferSize);
                this.bufferWriteIndex = 0;
              }
            }
          }
          return true;
        }
      }
      registerProcessor("audio-processor", AudioProcessor);
    `;
  }

  _onAudioData(data) {
    if (!this.isRecording) return;
    let bytes;
    if (data && data.type === "audio" && data.data) {
      bytes = new Uint8Array(data.data);
    } else if (ArrayBuffer.isView(data)) {
      bytes = new Uint8Array(data.buffer);
    } else {
      return;
    }

    if (
      !this.isMuted &&
      this.state !== STATE.SPEAKING &&
      this.ws &&
      this.ws.readyState === WebSocket.OPEN
    ) {
      this.ws.send(bytes);
    }

    if (this.state === STATE.LISTENING) {
      this._detectSilence(bytes);
    }
  }

  _detectSilence(bytes) {
    if (!bytes || bytes.length < 4) return;

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

    if (this.orb) this.orb.style.setProperty("--rms", rms.toFixed(3));

    if (this.sessionStartTime && Date.now() - this.sessionStartTime < 1500)
      return;

    if (rms < threshold) {
      if (!this.silenceStartTime) {
        this.silenceStartTime = Date.now();
      } else if (
        Date.now() - this.silenceStartTime > CONFIG.SILENCE_DURATION_MS &&
        this.state === STATE.LISTENING &&
        this.lastUtteredTime
      ) {
        this.utteranceStartTime = Date.now();
        this.silenceStartTime = null;
        this.setState(STATE.PROCESSING);
      }
    } else {
      this.silenceStartTime = null;
      this.lastUtteredTime = Date.now();
    }
  }

  _connectWebSocket() {
    return new Promise((resolve, reject) => {
      const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${wsProtocol}//${CONFIG.WS_HOST}:${CONFIG.WS_PORT}/ws?tts_engine=${encodeURIComponent(this.selectedTTSEngine)}`;
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
        if (
          !this.intentionalDisconnect &&
          this.state !== STATE.IDLE &&
          this.reconnectAttempts < this.maxReconnectAttempts
        ) {
          this.reconnectAttempts++;
          const delay = 1000 * Math.pow(2, this.reconnectAttempts - 1);
          console.log(
            `[Client] Reconnect attempt ${this.reconnectAttempts} (delay ${delay}ms)`,
          );
          setTimeout(() => {
            this._connectWebSocket().catch(() => {});
          }, delay);
        } else if (!this.intentionalDisconnect && this.state !== STATE.IDLE) {
          this.showError("Connection lost. Please start a new session.");
          this.stopSession();
        }
      };
    });
  }

  _onServerMessage(msg) {
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case "tts_config":
        this.ttsConfig = { format: msg.format, sampleRate: msg.sampleRate || msg.sample_rate };
        console.log("[Client] Received TTS config:", this.ttsConfig);
        break;
      case "transcript":
        if (msg.text) {
          this._resetTurnState();
          this.addTranscriptItem("student", msg.text);
          this.totalTurns++;
          this._updateStatsBar();
        }
        break;
      case "response_chunk":
        if (msg.text)
          this.currentResponse = (this.currentResponse || "") + msg.text;
        break;
      case "response_end":
        if (this.currentResponse && this.currentResponse.trim()) {
          this.addTranscriptItem("tutor", this.currentResponse.trim());
        }
        this.currentResponse = "";
        this.isAudioSourceEnded = true;
        this._checkPlaybackFinished();
        break;
      case "latency":
        if (typeof msg.ms === "number") {
          this.lastLatencyMs = msg.ms;
          this._updateStatsBar();
        }
        break;
      case "busy":
        this.showToast("⏳ Still responding — please wait a moment.");
        break;
      case "error":
        this.showError(msg.message || "Unknown server error");
        if (this.state !== STATE.IDLE) {
          const isSTTError =
            typeof msg.message === "string" && msg.message.includes("STT");
          if (isSTTError) {
            this.stopSession();
          } else {
            this.isPlaying = false;
            this.isAudioSourceEnded = true;
            this._checkPlaybackFinished();
          }
        }
        break;
      default:
        console.debug("Unhandled message type:", msg.type);
    }
  }

  async _resumeAudioContext() {
    if (!this.audioContext) return;
    if (this.audioContext.state !== "suspended") return;
    if (this._audioResumed) return;
    try {
      await this.audioContext.resume();
      this._audioResumed = true;
    } catch (e) {
      console.error("Failed to resume AudioContext:", e);
    }
  }

  async _onAudioChunk(arrayBuffer) {
    if (this.firstAudioTime === null && this.utteranceStartTime !== null) {
      this.firstAudioTime = Date.now();
      const latency = this.firstAudioTime - this.utteranceStartTime;
      this.lastLatencyMs = latency;
      this._updateStatsBar();
      console.log(`[Client] First audio: ${latency}ms`);
    }

    if (this.audioContext && this.audioContext.state === "suspended") {
      await this._resumeAudioContext();
    }

    try {
      let audioBuffer;
      if (this.ttsConfig.format === "linear16") {
        const int16Array = new Int16Array(arrayBuffer);
        const float32Array = new Float32Array(int16Array.length);
        for (let i = 0; i < int16Array.length; i++) {
          float32Array[i] = int16Array[i] / 32768.0;
        }
        audioBuffer = this.audioContext.createBuffer(
          1,
          float32Array.length,
          this.ttsConfig.sampleRate,
        );
        audioBuffer.copyToChannel(float32Array, 0);
      } else {
        audioBuffer = await this.audioContext.decodeAudioData(
          arrayBuffer.slice(0),
        );
      }

      if (audioBuffer) {
        this._schedulePlayback(audioBuffer);
      }
    } catch (err) {
      console.error("Audio decode failed:", err);
    }
  }

  _schedulePlayback(audioBuffer) {
    this.setState(STATE.SPEAKING);
    this.isPlaying = true;

    const currentTime = this.audioContext.currentTime;
    if (this.nextPlaybackTime === null || this.nextPlaybackTime < currentTime) {
      this.nextPlaybackTime = currentTime + 0.1;
    }

    const source = this.audioContext.createBufferSource();
    source.buffer = audioBuffer;
    if (this.analyser) {
      source.connect(this.analyser);
    } else {
      source.connect(this.audioContext.destination);
    }

    source.start(this.nextPlaybackTime);
    this.activeSourceNodes.push(source);

    if (this.analyser) {
      const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
      const tick = () => {
        if (this.state !== STATE.SPEAKING) return;
        this.analyser.getByteFrequencyData(dataArray);
        const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
        if (this.orb)
          this.orb.style.setProperty("--audio-level", avg.toFixed(1));
        requestAnimationFrame(tick);
      };
      tick();
    }

    source.onended = () => {
      const index = this.activeSourceNodes.indexOf(source);
      if (index > -1) this.activeSourceNodes.splice(index, 1);
      this._checkPlaybackFinished();
      if (this.activeSourceNodes.length === 0 && this.orb) {
        this.orb.style.setProperty("--audio-level", "0");
      }
    };

    this.nextPlaybackTime = this.nextPlaybackTime + audioBuffer.duration;
  }

  _checkPlaybackFinished() {
    if (this.activeSourceNodes.length === 0 && this.isAudioSourceEnded) {
      this.isPlaying = false;
      if (this.state === STATE.SPEAKING) {
        this.setState(STATE.LISTENING);
        this._resetTurnState();
      } else if (this.state === STATE.PROCESSING) {
        // If we were stuck in processing, recover!
        this.setState(STATE.LISTENING);
        this._resetTurnState();
      }
    }
  }

  setState(newState) {
    const prev = this.state;
    this.state = newState;
    if (!this.orb) return;

    this.orb.classList.remove(
      "state-idle",
      "state-listening",
      "state-processing",
      "state-speaking",
    );
    const classMap = {
      [STATE.IDLE]: "state-idle",
      [STATE.LISTENING]: "state-listening",
      [STATE.PROCESSING]: "state-processing",
      [STATE.SPEAKING]: "state-speaking",
    };
    if (classMap[newState]) this.orb.classList.add(classMap[newState]);

    if (prev === STATE.LISTENING) this.orb.style.setProperty("--rms", "0");
    if (prev === STATE.SPEAKING)
      this.orb.style.setProperty("--audio-level", "0");

    if (this.statusText) {
      const statusLabels = {
        [STATE.IDLE]: "tap to begin",
        [STATE.LISTENING]: "listening",
        [STATE.PROCESSING]: "thinking",
        [STATE.SPEAKING]: "speaking",
      };
      this.statusText.textContent = statusLabels[newState] || "";
      this.statusText.className = `status-text state-${newState.toLowerCase()}`;
    }

    if (this.btnToggleSession) {
      if (newState === STATE.IDLE) {
        this.btnToggleSession.classList.remove("active");
        const label = this.btnToggleSession.querySelector(".btn-label");
        if (label) label.textContent = "Begin";
      } else {
        this.btnToggleSession.classList.add("active");
        const label = this.btnToggleSession.querySelector(".btn-label");
        if (label) label.textContent = "Stop";
      }
    }

    if (this.btnMute) this.btnMute.disabled = newState === STATE.IDLE;
  }

  _updateStatsBar() {
    if (!this.statsBar) return;
    const latencyText = this.lastLatencyMs
      ? `↯ ${this.lastLatencyMs}ms`
      : "↯ --ms";
    const turnsText = `${this.totalTurns} ${this.totalTurns === 1 ? "turn" : "turns"}`;
    this.statsBar.textContent = `${latencyText} · ${turnsText} · ${this.lastModel}`;
  }

  _showEmptyStateIfNeeded() {
    if (!this.transcriptPanel || !this.transcriptEmpty) return;
    const hasMessages = this.transcriptPanel.querySelector(".message");
    this.transcriptEmpty.style.display = hasMessages ? "none" : "block";
  }

  addTranscriptItem(type, text) {
    if (type === "welcome") return;
    if (!this.transcriptPanel) return;

    const msg = document.createElement("div");
    msg.setAttribute("data-timestamp", Date.now().toString());
    if (type === "student") {
      msg.className = "message message-user";
      msg.innerHTML = `<p>${this._escapeHTML(text)}</p><span class="message-timestamp">just now</span>`;
    } else if (type === "tutor") {
      msg.className = "message message-tutor";
      msg.innerHTML = `<p>${this._escapeHTML(text)}</p><span class="message-timestamp">just now</span>`;
    } else {
      return;
    }
    this.transcriptPanel.appendChild(msg);
    this.transcriptPanel.scrollTop = this.transcriptPanel.scrollHeight;
    this._showEmptyStateIfNeeded();
  }

  _updateTimestamps() {
    if (!this.transcriptPanel) return;
    const bubbles = this.transcriptPanel.querySelectorAll(".message");
    const now = Date.now();
    bubbles.forEach((bubble) => {
      const tsAttr = bubble.getAttribute("data-timestamp");
      if (!tsAttr) return;
      const ts = parseInt(tsAttr, 10);
      const diffSec = Math.floor((now - ts) / 1000);
      const span = bubble.querySelector(".message-timestamp");
      if (!span) return;

      if (diffSec < 60) {
        span.textContent = "just now";
      } else if (diffSec < 3600) {
        const mins = Math.max(1, Math.floor(diffSec / 60));
        span.textContent = `${mins}m ago`;
      } else {
        const hrs = Math.floor(diffSec / 3600);
        span.textContent = `${hrs}h ago`;
      }
    });
  }

  _escapeHTML(str) {
    const d = document.createElement("div");
    d.appendChild(document.createTextNode(str));
    return d.innerHTML;
  }

  showToast(message, duration = 4000) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.transitionProperty = "opacity, transform";
      toast.style.transitionDuration = "400ms, 400ms";
      toast.style.transitionTimingFunction = "ease, ease";
      toast.style.opacity = "0";
      toast.style.transform = "translateY(8px)";
      setTimeout(() => toast.remove(), 400);
    }, duration);
  }

  showError(message) {
    console.error("[Error]", message);
    this.showToast(`❌ ${message}`);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  window.cascadeClient = new CascadeClient();
  window.cascadeClient._updateStatsBar();
  window.cascadeClient._showEmptyStateIfNeeded();
});
