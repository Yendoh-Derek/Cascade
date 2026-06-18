/**
 * Cascade — AI Voice Tutor Frontend
 */

const CONFIG = {
  WS_HOST: window.location.hostname || "localhost",
  WS_PORT: window.location.port || "8000",
};

const STATE = {
  IDLE: "IDLE",
  CONNECTING: "CONNECTING",
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
    this.sessionStartTime = null;
    this.speakingStartTime = null;
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
    this.latencyHistory = [];
    this.activeTurnId = null;
    this.playbackTurnId = null;
    this.audioEpoch = 0;
    this.decodeGeneration = 0;
    this._interrupting = false;
    this._pendingCancelTurnId = null;
    this._interruptTimeout = null;
    this.ttsConfig = { format: "linear16", sampleRate: 24000 };
    this.selectedTTSEngine =
      localStorage.getItem("cascade_tts_engine") || "deepgram";

    this.orb = document.getElementById("orb");
    this.transcriptPanel = document.getElementById("transcript-panel");
    this.statusText = document.getElementById("status-text");
    this.btnToggleSession = document.getElementById("btn-toggle-session");
    this.btnClearTranscript = document.getElementById("btn-clear-transcript");
    this.btnStats = document.getElementById("btn-stats");
    this.statsBar = document.getElementById("stats-bar");
    this.transcriptEmpty = document.getElementById("transcript-empty");

    this.isMuted = false;
    this._audioResumed = false;

    this._initUIListeners();
    this._initAudioContext();
    this._restoreTTSSelection();

    // Passively update relative timestamps every 30s
    setInterval(() => this._updateTimestamps(), 30000);
  }

  _restoreTTSSelection() {
    const activeBtn = document.querySelector(
      `.tts-toggle-btn[data-engine="${this.selectedTTSEngine}"]`,
    );
    if (activeBtn) {
      document
        .querySelectorAll(".tts-toggle-btn")
        .forEach((btn) => btn.classList.remove("active"));
      activeBtn.classList.add("active");
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

    [this.btnToggleSession, this.btnClearTranscript, this.btnStats].forEach(
      (btn) => {
        if (!btn) return;

        const createCircle = (x, y) => {
          const buttonWidth = btn.offsetWidth || 0;
          const xPos = x / buttonWidth;
          const color = `linear-gradient(to right, rgba(160, 217, 248, 0.8) ${xPos * 100}%, rgba(58, 91, 191, 0.8) ${xPos * 100}%)`;

          const circle = document.createElement("div");
          circle.className = "menu-btn-circle";
          circle.style.left = `${x}px`;
          circle.style.top = `${y}px`;
          circle.style.background = color;

          btn.appendChild(circle);

          setTimeout(() => {
            circle.classList.add("fade-in");
          }, 0);

          setTimeout(() => {
            circle.classList.remove("fade-in");
            circle.classList.add("fade-out");
          }, 1000);

          setTimeout(() => {
            if (circle.parentNode) {
              circle.parentNode.removeChild(circle);
            }
          }, 2200);
        };

        let isListening = false;
        let lastAdded = 0;

        btn.addEventListener("pointermove", (evt) => {
          if (!isListening) return;

          const currentTime = Date.now();
          if (currentTime - lastAdded > 100) {
            lastAdded = currentTime;
            const rect = btn.getBoundingClientRect();
            const x = evt.clientX - rect.left;
            const y = evt.clientY - rect.top;
            createCircle(x, y);
          }
        });

        btn.addEventListener("pointerenter", () => {
          isListening = true;
        });

        btn.addEventListener("pointerleave", () => {
          isListening = false;
        });

        if (btn.id === "btn-toggle-session") {
          btn.addEventListener("click", () => this.toggleSession());
        } else if (btn.id === "btn-clear-transcript") {
          btn.addEventListener("click", () => {
            if (this.transcriptPanel) this.transcriptPanel.innerHTML = "";
            this._resetTurnState();
            this._updateStatsBar();
            this._showEmptyStateIfNeeded();
          });
        } else if (btn.id === "btn-stats") {
          btn.addEventListener("click", () => this._openStatsPanel());
        }
      },
    );

    // Custom TTS Engine Selector Toggles
    document.querySelectorAll(".tts-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const engine = btn.getAttribute("data-engine");
        this.selectedTTSEngine = engine;
        localStorage.setItem("cascade_tts_engine", engine);
        document
          .querySelectorAll(".tts-toggle-btn")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        console.log(`[Client] TTS Engine changed to: ${engine}`);
      });
    });

    // Close stats panel listeners
    const btnCloseStats = document.getElementById("btn-close-stats");
    if (btnCloseStats) {
      btnCloseStats.addEventListener("click", () => this._closeStatsPanel());
    }
    const statsBackdrop = document.getElementById("stats-backdrop");
    if (statsBackdrop) {
      statsBackdrop.addEventListener("click", () => this._closeStatsPanel());
    }
  }

  _initAudioContext() {
    try {
      this.audioContext = new (
        window.AudioContext || window.webkitAudioContext
      )();
      this.playbackGain = this.audioContext.createGain();
      this.playbackGain.gain.value = 1;
      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 256;
      this.analyser.connect(this.playbackGain);
      this.playbackGain.connect(this.audioContext.destination);
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
      this.setState(STATE.CONNECTING);
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
      this._stopAllPlayback();
    }
    this.currentResponse = "";
    this.activeTurnId = null;
    this.playbackTurnId = null;
    this.audioEpoch += 1;
    this.decodeGeneration += 1;
    this._interrupting = false;
    this._pendingCancelTurnId = null;
    if (this._interruptTimeout) {
      clearTimeout(this._interruptTimeout);
      this._interruptTimeout = null;
    }
    if (this.playbackGain) {
      this.playbackGain.gain.value = 1;
    }
    this._resetTurnState();
    this.sessionStartTime = null;

    this.setState(STATE.IDLE);
  }

  _resetTurnState() {
    this.utteranceStartTime = null;
    this.firstAudioTime = null;
    this.lastUtteredTime = null;
    this.nextPlaybackTime = null;
    this.isAudioSourceEnded = false;
    this.activeSourceNodes = [];
    this.maxAudioLevel = 0;
  }

  _resetPlaybackOnly() {
    this.nextPlaybackTime = null;
    this.isAudioSourceEnded = false;
    this.activeSourceNodes = [];
    this._interruptionBuffer = [];
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
            this.bufferSize = 512; // ~32ms of audio at 16kHz
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

    if (!this.isMuted && this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(bytes);
    }

    if (
      this.state === STATE.LISTENING ||
      this.state === STATE.SPEAKING ||
      this.state === STATE.PROCESSING
    ) {
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

    if (this.orb && this.state === STATE.LISTENING)
      this.orb.style.setProperty("--rms", rms.toFixed(3));

    if (this.sessionStartTime && Date.now() - this.sessionStartTime < 1500)
      return;

    if (this.state === STATE.LISTENING) {
      if (rms >= threshold) {
        this.lastUtteredTime = Date.now();
      }
    } else if (
      this.state === STATE.SPEAKING ||
      this.state === STATE.PROCESSING
    ) {
      this._detectInterruption(rms);
    }
  }

  _isTurnActive(turnId) {
    return (
      turnId != null &&
      this.activeTurnId != null &&
      turnId === this.activeTurnId
    );
  }

  _detectInterruption(rms) {
    if (this.state !== STATE.SPEAKING && this.state !== STATE.PROCESSING)
      return;
    if (this._interrupting) return;
    if (
      this.state === STATE.SPEAKING &&
      this.speakingStartTime &&
      Date.now() - this.speakingStartTime < 150
    ) {
      return;
    }

    if (!this._interruptionBuffer) this._interruptionBuffer = [];

    this._interruptionBuffer.push(rms);
    if (this._interruptionBuffer.length > 3) this._interruptionBuffer.shift();

    const avgRms =
      this._interruptionBuffer.reduce((a, b) => a + b, 0) /
      this._interruptionBuffer.length;
    // Higher threshold while tutor is speaking to reduce echo false-triggers
    const multiplier = this.state === STATE.SPEAKING ? 0.25 : 0.15;
    const threshold = Math.max(0.05, this.maxAudioLevel * multiplier);

    if (avgRms > threshold) {
      this._triggerInterruption();
    }
  }

  _stopAllPlayback() {
    const now = this.audioContext ? this.audioContext.currentTime : 0;

    if (this.playbackGain && this.audioContext) {
      // First cancel any existing scheduled values
      this.playbackGain.gain.cancelScheduledValues(now);
      // Add 15ms fade-out instead of instant cut
      this.playbackGain.gain.setValueAtTime(this.playbackGain.gain.value, now);
      this.playbackGain.gain.linearRampToValueAtTime(0, now + 0.015);
    }

    // Stop sources after the fade-out completes
    const stopTime = now + 0.015;
    this.activeSourceNodes.forEach((source) => {
      try {
        source.stop(stopTime);
      } catch (_) {}
      try {
        source.disconnect();
      } catch (_) {}
    });
    this.activeSourceNodes = [];
    this.nextPlaybackTime = null;
    this.isPlaying = false;

    // Reset playback gain to 1 after fade-out completes
    if (this.playbackGain && this.audioContext) {
      setTimeout(() => {
        this.playbackGain.gain.cancelScheduledValues(
          this.audioContext.currentTime,
        );
        this.playbackGain.gain.setValueAtTime(1, this.audioContext.currentTime);
      }, 20);
    }
  }

  _triggerInterruption() {
    if (this.state !== STATE.SPEAKING && this.state !== STATE.PROCESSING)
      return;
    if (this._interrupting) return;
    this._interrupting = true;
    console.log(
      "[Client] Interruption triggered! Stopping playback and cancelling server pipeline.",
    );

    this._pendingCancelTurnId = this.activeTurnId ?? this.playbackTurnId;
    this.audioEpoch += 1;
    this.decodeGeneration += 1;
    this.activeTurnId = null;
    this.playbackTurnId = null;

    this._stopAllPlayback();
    this.isAudioSourceEnded = false;
    this._interruptionBuffer = [];

    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "cancel" }));
    }

    this.currentResponse = "";
    this.setState(STATE.LISTENING);
    this._resetPlaybackOnly();

    if (this._interruptTimeout) clearTimeout(this._interruptTimeout);
    this._interruptTimeout = setTimeout(() => {
      this._interrupting = false;
      this._interruptTimeout = null;
    }, 1000);
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
        this.ttsConfig = {
          format: msg.format,
          sampleRate: msg.sampleRate || msg.sample_rate,
        };
        console.log("[Client] Received TTS config:", this.ttsConfig);
        break;
      case "transcript":
        if (msg.text) {
          if (msg.turn_id != null) {
            this.activeTurnId = msg.turn_id;
            this.playbackTurnId = msg.turn_id;
          }
          this._interrupting = false;
          this._resetTurnState();
          this.addTranscriptItem("student", msg.text);
          this.totalTurns++;
          this._updateStatsBar();
          this.setState(STATE.PROCESSING);
        }
        break;
      case "response_chunk":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        if (msg.text)
          this.currentResponse = (this.currentResponse || "") + msg.text;
        break;
      case "response_end":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        if (this.currentResponse && this.currentResponse.trim()) {
          const bubble = this.addTranscriptItem(
            "tutor",
            this.currentResponse.trim(),
          );
          if (bubble) {
            bubble.classList.add("message-complete");
            setTimeout(() => bubble.classList.remove("message-complete"), 1200);
          }
        }
        this.currentResponse = "";
        this.isAudioSourceEnded = true;
        this._checkPlaybackFinished();
        break;
      case "turn_cancelled":
        if (msg.turn_id != null && this.activeTurnId === msg.turn_id) {
          this.activeTurnId = null;
          this.playbackTurnId = null;
          this.audioEpoch += 1;
          this.decodeGeneration += 1;
          this._stopAllPlayback();
          this.isAudioSourceEnded = true;
          this._checkPlaybackFinished();
        }
        if (msg.turn_id != null && msg.turn_id === this._pendingCancelTurnId) {
          this._pendingCancelTurnId = null;
          if (this._interruptTimeout) {
            clearTimeout(this._interruptTimeout);
            this._interruptTimeout = null;
          }
          this._interrupting = false;
        }
        break;
      case "latency":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        if (typeof msg.total_ms === "number") {
          this.lastLatencyMs = msg.total_ms;

          this.latencyHistory.push({
            turn: this.totalTurns,
            total: msg.total_ms,
            llm: msg.llm_ms || 0,
            tts: msg.tts_ms || 0,
            stt: msg.stt_ms || 0,
            timestamp: Date.now(),
          });
          if (this.latencyHistory.length > 20) this.latencyHistory.shift();
          this._updateStatsBar();

          const panel = document.getElementById("stats-panel");
          if (panel && panel.classList.contains("open")) {
            this._renderLatencyChart();
          }
        } else if (typeof msg.ms === "number") {
          this.lastLatencyMs = msg.ms;
          this._updateStatsBar();
        }
        break;
      case "busy":
        this.showToast(
          msg.message || "⏳ Still responding — please wait a moment.",
          4000,
          "info",
        );
        break;
      case "stt_reconnecting":
        this.showToast(
          `Reconnecting speech recognition (${msg.attempt}/${msg.max})…`,
          3000,
          "info",
        );
        break;
      case "stt_reconnected":
        this.showToast("Speech recognition reconnected.", 2500, "info");
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
    if (arrayBuffer.byteLength < 4) return;

    const view = new DataView(arrayBuffer);
    const turnId = view.getUint32(0, false);
    const audioPayload = arrayBuffer.slice(4);

    // Snapshot all guard values at the start
    const epoch = this.audioEpoch;
    const decodeGen = this.decodeGeneration;
    const activeTurnAtStart = this.activeTurnId;
    const stateAtStart = this.state;

    // Early guards
    if (!this._isTurnActive(turnId)) return;
    if (
      stateAtStart === STATE.LISTENING ||
      stateAtStart === STATE.IDLE ||
      stateAtStart === STATE.CONNECTING
    ) {
      return;
    }

    if (this.audioContext && this.audioContext.state === "suspended") {
      await this._resumeAudioContext();
    }

    // Check guards again after async resume
    if (
      epoch !== this.audioEpoch ||
      decodeGen !== this.decodeGeneration ||
      turnId !== this.activeTurnId
    ) {
      return;
    }

    try {
      let audioBuffer;
      if (this.ttsConfig.format === "linear16") {
        const int16Array = new Int16Array(audioPayload);
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
          audioPayload.slice(0),
        );
      }

      // FINAL guard check before playback
      if (
        audioBuffer &&
        epoch === this.audioEpoch &&
        decodeGen === this.decodeGeneration &&
        turnId === this.activeTurnId &&
        this.state !== STATE.LISTENING &&
        this.state !== STATE.IDLE
      ) {
        this._schedulePlayback(audioBuffer, epoch, turnId, decodeGen);
      }
    } catch (err) {
      console.error("Audio decode failed:", err);
    }
  }

  _schedulePlayback(audioBuffer, epoch, turnId, decodeGen) {
    if (
      epoch !== this.audioEpoch ||
      decodeGen !== this.decodeGeneration ||
      turnId !== this.activeTurnId ||
      this.state === STATE.LISTENING ||
      this.state === STATE.IDLE
    ) {
      return;
    }

    this.setState(STATE.SPEAKING);
    this.isPlaying = true;
    this.playbackTurnId = turnId;

    const currentTime = this.audioContext.currentTime;
    if (this.nextPlaybackTime === null || this.nextPlaybackTime < currentTime) {
      this.nextPlaybackTime = currentTime + 0.02;
    }

    const source = this.audioContext.createBufferSource();
    source.buffer = audioBuffer;

    // Create a gain node for fade-in/fade-out per audio chunk
    const chunkGain = this.audioContext.createGain();

    // Connect: source -> chunkGain -> analyser -> playbackGain -> destination
    if (this.analyser) {
      source.connect(chunkGain);
      chunkGain.connect(this.analyser);
    } else {
      source.connect(chunkGain);
      chunkGain.connect(this.audioContext.destination);
    }

    // Add fade-in (10ms)
    chunkGain.gain.setValueAtTime(0, this.nextPlaybackTime);
    chunkGain.gain.linearRampToValueAtTime(1, this.nextPlaybackTime + 0.01);

    // Add fade-out (10ms) at end
    const fadeOutStart = this.nextPlaybackTime + audioBuffer.duration - 0.01;
    chunkGain.gain.setValueAtTime(1, fadeOutStart);
    chunkGain.gain.linearRampToValueAtTime(
      0,
      this.nextPlaybackTime + audioBuffer.duration,
    );

    if (
      epoch !== this.audioEpoch ||
      decodeGen !== this.decodeGeneration ||
      turnId !== this.activeTurnId
    ) {
      return;
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
      "state-connecting",
      "state-listening",
      "state-processing",
      "state-speaking",
    );
    const classMap = {
      [STATE.IDLE]: "state-idle",
      [STATE.CONNECTING]: "state-connecting",
      [STATE.LISTENING]: "state-listening",
      [STATE.PROCESSING]: "state-processing",
      [STATE.SPEAKING]: "state-speaking",
    };
    if (classMap[newState]) this.orb.classList.add(classMap[newState]);

    if (prev === STATE.LISTENING) this.orb.style.setProperty("--rms", "0");
    if (prev === STATE.SPEAKING)
      this.orb.style.setProperty("--audio-level", "0");

    if (newState === STATE.SPEAKING) {
      this.speakingStartTime = Date.now();
    }

    if (this.statusText) {
      const statusLabels = {
        [STATE.IDLE]: "tap to begin",
        [STATE.CONNECTING]: "connecting",
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
  }

  _updateStatsBar() {
    if (!this.statsBar) return;
    this.statsBar.textContent = this.lastLatencyMs
      ? `${this.lastLatencyMs}ms`
      : `--ms`;
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
    return msg;
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

  showToast(message, duration = 4000, variant = "error") {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = `toast toast-${variant}`;
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
    this.showToast(`❌ ${message}`, 4000, "error");
  }

  _chartTickStep(maxMs) {
    const targetTicks = 5;
    const raw = maxMs / targetTicks;
    const mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1))));
    const norm = raw / mag;
    let nice;
    if (norm <= 1.5) nice = 1;
    else if (norm <= 3) nice = 2;
    else if (norm <= 7) nice = 5;
    else nice = 10;
    return nice * mag;
  }

  _chartNiceMax(peak) {
    const step = this._chartTickStep(peak);
    return Math.max(Math.ceil(peak / step) * step, step * 2);
  }

  _renderLatencyChart() {
    const canvas = document.getElementById("latency-chart");
    if (!canvas) return;

    const data = this.latencyHistory;
    const dpr = window.devicePixelRatio || 1;
    const W = 600;
    const H = 300;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = `${W}px`;
    canvas.style.height = `${H}px`;

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const PAD = { top: 36, right: 20, bottom: 40, left: 56 };
    const chartW = W - PAD.left - PAD.right;
    const chartH = H - PAD.top - PAD.bottom;
    const TARGET_MS = 600;
    const colors = { stt: "#818cf8", llm: "#c084fc", tts: "#34d399" };

    ctx.clearRect(0, 0, W, H);

    if (data.length === 0) {
      ctx.fillStyle = "rgba(255,255,255,0.2)";
      ctx.font = "13px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No data yet — start a conversation", W / 2, H / 2);
      return;
    }

    const peakTotal = Math.max(...data.map((d) => d.total), 100);
    const tickStep = this._chartTickStep(peakTotal);
    const maxMs = this._chartNiceMax(peakTotal);
    const scaleY = (v) => PAD.top + chartH - (v / maxMs) * chartH;
    const scaleX = (i) => PAD.left + ((i + 0.5) / data.length) * chartW;

    // Y-axis label
    ctx.save();
    ctx.translate(14, PAD.top + chartH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = "rgba(255,255,255,0.25)";
    ctx.font = "10px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Latency (ms)", 0, 0);
    ctx.restore();

    // In-chart legend (top-left)
    let legendX = PAD.left;
    const legendY = 12;
    ctx.font = "10px Inter, sans-serif";
    ctx.textAlign = "left";
    [
      { key: "llm", label: "LLM" },
      { key: "tts", label: "TTS" },
    ].forEach(({ key, label }) => {
      ctx.fillStyle = colors[key];
      ctx.beginPath();
      ctx.arc(legendX + 4, legendY + 4, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.45)";
      ctx.fillText(label, legendX + 12, legendY + 8);
      legendX += ctx.measureText(label).width + 28;
    });
    ctx.strokeStyle = colors.stt;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(legendX, legendY + 4);
    ctx.lineTo(legendX + 16, legendY + 4);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,255,255,0.45)";
    ctx.fillText("STT", legendX + 20, legendY + 8);
    legendX += ctx.measureText("STT").width + 36;
    ctx.strokeStyle = "rgba(255,255,255,0.3)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(legendX, legendY + 4);
    ctx.lineTo(legendX + 16, legendY + 4);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,255,255,0.45)";
    ctx.fillText("600ms target", legendX + 20, legendY + 8);

    // Y gridlines with adaptive step (5–7 labels max)
    for (let v = 0; v <= maxMs; v += tickStep) {
      const y = scaleY(v);
      ctx.beginPath();
      ctx.strokeStyle = "rgba(255,255,255,0.06)";
      ctx.lineWidth = 1;
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(PAD.left + chartW, y);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,0.25)";
      ctx.font = "10px JetBrains Mono, monospace";
      ctx.textAlign = "right";
      const label =
        v >= 1000 ? `${(v / 1000).toFixed(v % 1000 === 0 ? 0 : 1)}k` : `${v}`;
      ctx.fillText(label, PAD.left - 8, y + 4);
    }

    // Target line at 600ms
    if (TARGET_MS <= maxMs) {
      const targetY = scaleY(TARGET_MS);
      ctx.beginPath();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(255,255,255,0.3)";
      ctx.lineWidth = 1;
      ctx.moveTo(PAD.left, targetY);
      ctx.lineTo(PAD.left + chartW, targetY);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Stacked bars
    const BAR_W = Math.min(36, (chartW / data.length) * 0.55);

    data.forEach((d, i) => {
      const x = scaleX(i) - BAR_W / 2;
      let yBase = scaleY(0);
      let yTop = yBase;

      ["llm", "tts"].forEach((key) => {
        const val = d[key] || 0;
        const barH = (val / maxMs) * chartH;
        yBase -= barH;
        yTop = Math.min(yTop, yBase);
        ctx.fillStyle = colors[key];
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x, yBase, BAR_W, barH);
        ctx.globalAlpha = 1;
      });

      // Total label above bar
      ctx.fillStyle = "rgba(255,255,255,0.55)";
      ctx.font = "9px JetBrains Mono, monospace";
      ctx.textAlign = "center";
      ctx.fillText(`${d.total}ms`, scaleX(i), yTop - 6);

      // Turn label (STT endpointing shown separately — not part of pipeline total)
      ctx.fillStyle = "rgba(255,255,255,0.25)";
      ctx.font = "9px Inter, sans-serif";
      const sttNote = d.stt ? ` · stt ${d.stt}ms` : "";
      ctx.fillText(`T${d.turn}${sttNote}`, scaleX(i), H - PAD.bottom + 14);
    });
  }

  _openStatsPanel() {
    const panel = document.getElementById("stats-panel");
    const backdrop = document.getElementById("stats-backdrop");
    if (panel) {
      panel.classList.add("open");
      panel.setAttribute("aria-hidden", "false");
    }
    if (backdrop) backdrop.classList.add("open");
    this._renderLatencyChart();
  }

  _closeStatsPanel() {
    const panel = document.getElementById("stats-panel");
    const backdrop = document.getElementById("stats-backdrop");
    if (panel) {
      panel.classList.remove("open");
      panel.setAttribute("aria-hidden", "true");
    }
    if (backdrop) backdrop.classList.remove("open");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  window.cascadeClient = new CascadeClient();
  window.cascadeClient._updateStatsBar();
  window.cascadeClient._showEmptyStateIfNeeded();
});
