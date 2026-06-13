/**
 * Cascade — AI Tutor Frontend
 * Redesigned according to ui-ux-design.md
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
    this.analyser = null; // cached shared analyser node
    this.processor = null;
    this.sourceNode = null;
    this.sinkNode = null;
    this.mediaStream = null;
    this.isRecording = false;

    // Audio playback
    this.isPlaying = false;

    // Per-turn latency tracking — reset each turn
    this.utteranceStartTime = null;
    this.firstAudioTime = null;
    this.lastUtteredTime = null;
    this.silenceStartTime = null;
    this.sessionStartTime = null;

    // Precise audio scheduling
    this.nextPlaybackTime = null;
    this.isAudioSourceEnded = false;
    this.activeSourceNodes = [];

    // Adaptive silence threshold
    this.maxAudioLevel = 0;

    // Intentional disconnect flag
    this.intentionalDisconnect = false;

    // Reconnection
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 3;

    // Response accumulation
    this.currentResponse = "";

    // UI references
    this.orb = document.getElementById("orb");
    this.transcriptPanel = document.getElementById("transcript-panel");
    this.statusText = document.getElementById("status-text");
    this.btnToggleSession = document.getElementById("btn-toggle-session");
    this.btnClearTranscript = document.getElementById("btn-clear-transcript");
    this.btnMute = document.getElementById("btn-mute");

    // Microphone mute flag
    this.isMuted = false;

    this._initUIListeners();
    this._initAudioContext();
  }

  _initUIListeners() {
    if (this.orb) {
      const orbShell = this.orb.querySelector('.orb-shell');

      this.orb.addEventListener('pointerdown', () => {
        if (orbShell) {
          orbShell.style.transitionProperty = 'transform';
          orbShell.style.transitionDuration = '100ms';
          orbShell.style.transitionTimingFunction = 'ease-in';
          orbShell.style.transform = 'scale(0.92)';
        }
      });

      this.orb.addEventListener('pointerup', () => {
        if (orbShell) {
          orbShell.style.transitionProperty = 'transform';
          orbShell.style.transitionDuration = '350ms';
          orbShell.style.transitionTimingFunction = 'var(--spring)';
          orbShell.style.transform = 'scale(1.08)';
          setTimeout(() => {
            orbShell.style.transform = 'scale(1)';
          }, 200);
        }
        this.toggleSession();
      });

      this.orb.addEventListener('keydown', (evt) => {
        if (evt.key === ' ' || evt.key === 'Enter') {
          evt.preventDefault();
          if (orbShell) {
            orbShell.style.transitionProperty = 'transform';
            orbShell.style.transitionDuration = '100ms';
            orbShell.style.transitionTimingFunction = 'ease-in';
            orbShell.style.transform = 'scale(0.92)';
            setTimeout(() => {
              orbShell.style.transitionProperty = 'transform';
              orbShell.style.transitionDuration = '350ms';
              orbShell.style.transitionTimingFunction = 'var(--spring)';
              orbShell.style.transform = 'scale(1.08)';
              setTimeout(() => {
                orbShell.style.transform = 'scale(1)';
              }, 200);
            }, 100);
          }
          this.toggleSession();
        }
      });
    }

    // Menu: Start/End Session toggle
    if (this.btnToggleSession) {
      this.btnToggleSession.addEventListener('click', () => {
        this.toggleSession();
      });
    }

    // Menu: Clear transcript
    if (this.btnClearTranscript) {
      this.btnClearTranscript.addEventListener('click', () => {
        if (this.transcriptPanel) {
          this.transcriptPanel.innerHTML = "";
        }
        this._resetTurnState();
      });
    }

    // Menu: Mute Microphone
    if (this.btnMute) {
      this.btnMute.addEventListener('click', () => {
        this.toggleMute();
      });
    }
  }

  toggleMute() {
    this.isMuted = !this.isMuted;
    if (this.btnMute) {
      if (this.isMuted) {
        this.btnMute.classList.add("active");
        this.btnMute.querySelector(".btn-label").textContent = "Unmute Mic";
        this.btnMute.querySelector(".btn-icon").textContent = "🔇";
      } else {
        this.btnMute.classList.remove("active");
        this.btnMute.querySelector(".btn-label").textContent = "Mute Mic";
        this.btnMute.querySelector(".btn-icon").textContent = "🔊";
      }
    }
  }

  _initAudioContext() {
    try {
      this.audioContext = new (
        window.AudioContext || window.webkitAudioContext
      )({ sampleRate: 16000 });
      if (this.audioContext) {
        if (this.audioContext.sampleRate !== 16000) {
          console.warn(
            `AudioContext created at ${this.audioContext.sampleRate}Hz — ` +
              `expected 16000Hz. Transcription quality may be degraded.`
          );
        }
        // Initialize a single shared AnalyserNode for audio output reactivity
        this.analyser = this.audioContext.createAnalyser();
        this.analyser.fftSize = 256;
        this.analyser.connect(this.audioContext.destination);
      }
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

      if (this.audioContext && this.audioContext.state === "suspended") {
        await this.audioContext.resume();
      }

      this.maxAudioLevel = 0;

      await this._initAudioProcessing();
      this.intentionalDisconnect = false;
      await this._connectWebSocket();

      this.setState(STATE.LISTENING);
      this.sessionStartTime = Date.now();

      // Clear transcript for new session
      if (this.transcriptPanel) {
        this.transcriptPanel.innerHTML = "";
      }
    } catch (err) {
      console.error("startSession failed:", err);
      let msg = `Failed to start: ${err.message}`;
      if (err.name === "NotAllowedError") {
        msg = "🔒 Microphone permission denied. Enable it in browser settings.";
      } else if (err.name === "NotFoundError") {
        msg = "🎤 No microphone found on this device.";
      } else if (err.message && err.message.includes("WebSocket")) {
        msg = "🌐 Could not connect to server. Is the backend running?";
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

    // Reset mute state when session stops
    if (this.isMuted) {
      this.toggleMute();
    }

    this.setState(STATE.IDLE);
  }

  // ── Per-turn state reset ─────────────────────────────────────────────

  _resetTurnState() {
    this.utteranceStartTime = null;
    this.firstAudioTime = null;
    this.lastUtteredTime = null;
    this.silenceStartTime = null;
    this.nextPlaybackTime = null;
    this.isAudioSourceEnded = false;
    this.activeSourceNodes = [];
    this.maxAudioLevel = 0; // Reset adaptive threshold on each turn to avoid permanent desensitization
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
    if (!this.isRecording) return;

    let bytes;
    if (data && data.type === "audio" && data.data) {
      bytes = new Uint8Array(data.data);
    } else if (ArrayBuffer.isView(data)) {
      bytes = new Uint8Array(data.buffer);
    } else {
      return;
    }

    // Send audio to server during LISTENING and PROCESSING (if not muted).
    if (
      !this.isMuted &&
      this.state !== STATE.SPEAKING &&
      this.ws &&
      this.ws.readyState === WebSocket.OPEN
    ) {
      this.ws.send(bytes);
    }

    // Silence detection only matters while we're listening.
    if (this.state === STATE.LISTENING) {
      this._detectSilence(bytes);
    }
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

    // Update mic reactivity CSS var
    if (this.orb) {
      this.orb.style.setProperty('--rms', rms.toFixed(3));
    }

    // Ignore initial mic startup noise/clicks for the first 1.5s
    if (this.sessionStartTime && Date.now() - this.sessionStartTime < 1500) {
      return;
    }

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
      const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${wsProtocol}//${CONFIG.WS_HOST}:${CONFIG.WS_PORT}/ws`;

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
        console.log(
          `✓ WebSocket connected (AudioContext: ${this.audioContext?.sampleRate || "unknown"}Hz)`
        );
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
            `[Client] WebSocket reconnection attempt ${this.reconnectAttempts} (delay: ${delay}ms)`,
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
        this.isAudioSourceEnded = true;
        this._checkPlaybackFinished();
        break;

      case "latency":
        if (typeof msg.ms === "number") {
          this._displayLatency(msg.ms);
        }
        break;

      case "busy":
        this.showToast("⏳ Still responding — please wait a moment.");
        break;

      case "error":
        this.showError(msg.message || "Unknown server error");
        if (this.state !== STATE.IDLE) {
          const isSTTError = typeof msg.message === "string" && msg.message.includes("STT");
          if (isSTTError) {
            this.stopSession();
          } else {
            this.isPlaying = false;
            this.setState(STATE.LISTENING);
            this._resetTurnState();
          }
        }
        break;

      default:
        console.debug("Unhandled message type:", msg.type);
    }
  }

  // ── Audio playback ───────────────────────────────────────────────────

  _onAudioChunk(arrayBuffer) {
    if (this.firstAudioTime === null && this.utteranceStartTime !== null) {
      this.firstAudioTime = Date.now();
      const latency = this.firstAudioTime - this.utteranceStartTime;
      this._displayLatency(latency);
      console.log(`[Client] First audio: ${latency}ms`);
    }

    if (this.audioContext && this.audioContext.state === "suspended") {
      try {
        this.audioContext.resume();
      } catch (_) {}
    }

    this.audioContext.decodeAudioData(arrayBuffer)
      .then(audioBuffer => {
        this._schedulePlayback(audioBuffer);
      })
      .catch(err => {
        console.error("Audio decode failed:", err);
      });
  }

  _schedulePlayback(audioBuffer) {
    this.setState(STATE.SPEAKING);
    this.isPlaying = true;

    const currentTime = this.audioContext.currentTime;

    if (this.nextPlaybackTime === null || this.nextPlaybackTime < currentTime) {
      this.nextPlaybackTime = currentTime + 0.1; // 100ms buffering lookahead
    }

    const source = this.audioContext.createBufferSource();
    source.buffer = audioBuffer;

    // Route source through the single cached AnalyserNode
    if (this.analyser) {
      source.connect(this.analyser);
    } else {
      source.connect(this.audioContext.destination);
    }

    source.start(this.nextPlaybackTime);
    this.activeSourceNodes.push(source);

    // Audio reactivity loop using the single cached AnalyserNode
    if (this.analyser) {
      const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
      const tick = () => {
        if (this.state !== STATE.SPEAKING) return;
        this.analyser.getByteFrequencyData(dataArray);
        const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
        if (this.orb) {
          this.orb.style.setProperty('--audio-level', avg.toFixed(1));
        }
        requestAnimationFrame(tick);
      };
      tick();
    }

    source.onended = () => {
      const index = this.activeSourceNodes.indexOf(source);
      if (index > -1) {
        this.activeSourceNodes.splice(index, 1);
      }
      this._checkPlaybackFinished();

      // Reset reactive CSS var when playback ends
      if (this.activeSourceNodes.length === 0 && this.orb) {
        this.orb.style.setProperty('--audio-level', '0');
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
      }
    }
  }

  // ── UI helpers ───────────────────────────────────────────────────────

  setState(newState) {
    const prev = this.state;
    this.state = newState;

    if (!this.orb) return;

    // Remove all state classes
    this.orb.classList.remove(
      'state-idle', 'state-listening',
      'state-processing', 'state-speaking'
    );

    // Add current state class
    const classMap = {
      [STATE.IDLE]:       'state-idle',
      [STATE.LISTENING]:  'state-listening',
      [STATE.PROCESSING]: 'state-processing',
      [STATE.SPEAKING]:   'state-speaking',
    };
    if (classMap[newState]) this.orb.classList.add(classMap[newState]);

    // Reset reactive CSS vars on state exit
    if (prev === STATE.LISTENING) this.orb.style.setProperty('--rms', '0');
    if (prev === STATE.SPEAKING)  this.orb.style.setProperty('--audio-level', '0');

    // Update status text underneath the orb
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

    // Update button states
    if (this.btnToggleSession) {
      if (newState === STATE.IDLE) {
        this.btnToggleSession.classList.remove("active");
        this.btnToggleSession.querySelector(".btn-label").textContent = "Start Session";
        this.btnToggleSession.querySelector(".btn-icon").textContent = "🎙️";
      } else {
        this.btnToggleSession.classList.add("active");
        this.btnToggleSession.querySelector(".btn-label").textContent = "End Session";
        this.btnToggleSession.querySelector(".btn-icon").textContent = "🛑";
      }
    }

    if (this.btnMute) {
      this.btnMute.disabled = (newState === STATE.IDLE);
    }
  }

  _displayLatency(ms) {
    const messages = document.querySelectorAll('.message-tutor');
    const last = messages[messages.length - 1];
    if (!last) return;

    // Avoid double latency tags if already added
    if (last.querySelector('.latency-tag')) return;

    const tag = document.createElement('span');
    tag.className = 'latency-tag';
    tag.textContent = `↯ ${Math.round(ms)}ms`;
    last.appendChild(tag);

    setTimeout(() => tag.classList.add('fade-out'), 3000);
  }

  addTranscriptItem(type, text) {
    if (type === 'welcome') return;

    if (!this.transcriptPanel) return;
    const msg = document.createElement('div');

    if (type === 'student') {
      msg.className = 'message message-user';
      msg.innerHTML = `<p>${this._escapeHTML(text)}</p>`;
    } else if (type === 'tutor') {
      msg.className = 'message message-tutor';
      msg.innerHTML = `<p>${this._escapeHTML(text)}</p>`;
    } else {
      return; // info / error types now handled by toast
    }

    this.transcriptPanel.appendChild(msg);
    this.transcriptPanel.scrollTop = this.transcriptPanel.scrollHeight;
  }

  _escapeHTML(str) {
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(str));
    return d.innerHTML;
  }

  showToast(message, duration = 4000) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
      toast.style.transitionProperty = 'opacity, transform';
      toast.style.transitionDuration = '400ms, 400ms';
      toast.style.transitionTimingFunction = 'ease, ease';
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(8px)';
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
});
