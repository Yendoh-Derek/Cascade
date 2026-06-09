/**
 * Cascade — AI Tutor Frontend
 * Handles microphone input, WebSocket communication, audio playback, and latency measurement
 */

// Configuration
const CONFIG = {
  WS_HOST: window.location.hostname || "localhost",
  WS_PORT: window.location.port || "8000",
  AUDIO_SAMPLE_RATE: 16000,
  SILENCE_THRESHOLD: 0.02,
  SILENCE_DURATION_MS: 800,
};

// Application state
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
    this.audioWorkletLoaded = false;
    this.audioWorkletModuleUrl = null;

    // Audio playback state
    this.audioPlaybackQueue = [];
    this.isPlaying = false;
    this.audioBuffer = null;

    // Latency tracking
    this.utteranceStartTime = null;
    this.firstAudioTime = null;
    this.lastUtteredTime = null;
    this.silenceStartTime = null;

    // UI references
    this.startBtn = document.getElementById("start-btn");
    this.statusBadge = document.getElementById("status-badge");
    this.statusDot = document.getElementById("status-dot");
    this.statusText = document.getElementById("status-text");
    this.latencyValue = document.getElementById("latency-value");
    this.transcriptList = document.getElementById("transcript-list");
    this.subjectSelect = document.getElementById("subject-select");
    this.debugText = document.getElementById("debug-text");

    // Bind event listeners
    this.startBtn.addEventListener("click", () => this.toggleSession());

    this.init();
  }

  async init() {
    console.log("Cascade Client initializing...");
    try {
      this.audioContext = new (
        window.AudioContext || window.webkitAudioContext
      )();
      console.log("✓ AudioContext created");
    } catch (err) {
      console.error("Failed to create AudioContext:", err);
      this.showError("Browser audio support not available");
    }
  }

  /**
   * Toggle session start/stop
   */
  async toggleSession() {
    if (this.state === STATE.IDLE) {
      await this.startSession();
    } else {
      await this.stopSession();
    }
  }

  /**
   * Start a new tutoring session
   */
  async startSession() {
    try {
      // Request microphone permission
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: false,
          sampleRate: { ideal: 16000 },
        },
      });
      console.log("✓ Microphone permission granted");

      // Initialize Web Audio processing
      await this.initAudioProcessing();

      // Connect to WebSocket
      await this.connectWebSocket();

      // Update UI
      this.setState(STATE.LISTENING);
      this.startBtn.textContent = "Stop Session";
      this.subjectSelect.disabled = true;
      this.transcriptList.innerHTML = "";
      this.addTranscriptItem("welcome", "Connected. Ask me a question.");
    } catch (err) {
      console.error("Failed to start session:", err);

      // Provide specific error messages
      let errorMsg = "Failed to start session: " + err.message;
      if (err.name === "NotAllowedError") {
        errorMsg =
          "🔒 Microphone permission denied. Please enable it in your browser settings.";
      } else if (err.name === "NotFoundError") {
        errorMsg = "🎤 No microphone found on your device.";
      } else if (err.message.includes("WebSocket")) {
        errorMsg = "🌐 Failed to connect to server. Check your connection.";
      }

      this.showError(errorMsg);
      await this.stopSession();
    }
  }

  /**
   * Initialize Web Audio API for mic capture
   */
  async initAudioProcessing() {
    if (!this.mediaStream || !this.audioContext) {
      throw new Error("MediaStream or AudioContext not available");
    }

    const source = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.sourceNode = source;

    const sink = this.audioContext.createGain();
    sink.gain.value = 0;
    this.sinkNode = sink;

    const workletUrl = this.getAudioWorkletCode();

    // Try to use AudioWorklet (modern approach)
    try {
      if (!this.audioWorkletLoaded) {
        const blob = new Blob([workletUrl], {
          type: "application/javascript",
        });
        const url = URL.createObjectURL(blob);
        this.audioWorkletModuleUrl = url;
        await this.audioContext.audioWorklet.addModule(url);
        this.audioWorkletLoaded = true;
      }

      this.processor = new AudioWorkletNode(
        this.audioContext,
        "audio-processor",
      );
      this.processor.port.onmessage = (evt) => this.onAudioProcess(evt.data);
      source.connect(this.processor);
      this.processor.connect(sink);
      sink.connect(this.audioContext.destination);
      console.log("✓ AudioWorklet processor created");
    } catch (err) {
      console.warn(
        "AudioWorklet not supported, falling back to ScriptProcessor",
      );
      // Fallback to ScriptProcessor
      this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
      this.processor.onaudioprocess = (evt) =>
        this.onAudioProcess(this.pcmEncode(evt.inputBuffer.getChannelData(0)));
      source.connect(this.processor);
      this.processor.connect(sink);
      sink.connect(this.audioContext.destination);
    }

    this.isRecording = true;
  }

  /**
   * Get AudioWorklet processor code as inline string
   */
  getAudioWorkletCode() {
    return `
            class AudioProcessor extends AudioWorkletProcessor {
                process(inputs, outputs) {
                    const input = inputs[0][0];  // Mono input
                    if (input && input.length > 0) {
                        // Convert Float32Array to PCM16 bytes
                        const pcmBytes = new Int16Array(input.length);
                        for (let i = 0; i < input.length; i++) {
                            // Clamp to [-1, 1] range and convert to 16-bit
                            const s = Math.max(-1, Math.min(1, input[i]));
                            pcmBytes[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
                        }
                        // Send as message
                        this.port.postMessage({
                            type: 'audio',
                            data: pcmBytes.buffer
                        });
                    }
                    return true;  // Keep processor alive
                }
            }

            registerProcessor('audio-processor', AudioProcessor);
        `;
  }

  /**
   * PCM encoding helper (for ScriptProcessor fallback)
   */
  pcmEncode(samples) {
    let offset = 0;
    const length = samples.length * 2 + 44;
    const arrayBuffer = new ArrayBuffer(length);
    const view = new DataView(arrayBuffer);
    const channels = [samples];

    const setUint16 = (data) => {
      view.setUint16(offset, data, true);
      offset += 2;
    };
    const setUint32 = (data) => {
      view.setUint32(offset, data, true);
      offset += 4;
    };

    setUint32(0x46464952);
    setUint32(length - 8);
    setUint32(0x45564157);
    setUint32(0x20746d66);
    setUint32(16);
    setUint16(1);
    setUint16(channels.length);
    setUint32(16000);
    setUint32(16000 * 2);
    setUint16(channels.length * 2);
    setUint16(16);
    setUint32(0x61746164);
    setUint32(length - offset - 4);

    const volume = 0.8;
    let audioOffset = offset;
    for (let i = 0; i < samples.length; i++, audioOffset += 2) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(
        offset + audioOffset,
        s < 0 ? s * 0x8000 : s * 0x7fff,
        true,
      );
    }

    return new Uint8Array(arrayBuffer);
  }

  /**
   * Handle audio from processor
   */
  onAudioProcess(data) {
    if (!this.isRecording || this.state === STATE.IDLE) return;

    // Handle both AudioWorklet and ScriptProcessor formats
    let audioData = data;
    if (data && typeof data === "object") {
      if (data.type === "audio" && data.data) {
        audioData = new Uint8Array(data.data);
      } else if (ArrayBuffer.isView(data)) {
        audioData = new Uint8Array(data);
      }
    }

    // Send audio to server
    if (this.ws && this.ws.readyState === WebSocket.OPEN && audioData) {
      this.ws.send(audioData);
    }

    // Detect silence for end-of-utterance
    this.detectSilence(audioData);
  }

  /**
   * Detect silence to determine end-of-utterance
   */
  detectSilence(audioData) {
    if (!audioData || audioData.length < 4) {
      return; // Not enough data for RMS calculation
    }

    // Calculate RMS (Root Mean Square) from PCM16 samples
    let sum = 0;
    let sampleCount = 0;

    try {
      // Handle both Uint8Array and ArrayBuffer
      let bytes = audioData;
      if (audioData instanceof ArrayBuffer) {
        bytes = new Uint8Array(audioData);
      }

      const view = new DataView(bytes.buffer || bytes);
      const numSamples = Math.floor(bytes.length / 2);

      for (let i = 0; i < numSamples; i++) {
        const sample = view.getInt16(i * 2, true) / 32768;
        sum += sample * sample;
        sampleCount++;
      }

      const rms = Math.sqrt(sum / sampleCount);

      // Use adaptive thresholding: scale threshold based on observed audio level
      // Ensure we don't trigger on noise at the start
      if (!this.maxAudioLevel) {
        this.maxAudioLevel = 0.1; // Initialize
      }

      // Track maximum audio level seen
      if (rms > this.maxAudioLevel) {
        this.maxAudioLevel = rms;
      }

      // Silence threshold is 5% of maximum level seen, with minimum of 0.02
      const silenceThreshold = Math.max(0.02, this.maxAudioLevel * 0.05);

      if (rms < silenceThreshold) {
        if (!this.silenceStartTime) {
          this.silenceStartTime = Date.now();
        } else if (
          Date.now() - this.silenceStartTime >
          CONFIG.SILENCE_DURATION_MS
        ) {
          if (this.state === STATE.LISTENING && this.lastUtteredTime) {
            this.setState(STATE.PROCESSING);
            this.utteranceStartTime = Date.now();
            this.silenceStartTime = null;
          }
        }
      } else {
        // Audio detected - reset silence timer
        this.silenceStartTime = null;
        this.lastUtteredTime = Date.now();
        if (this.state === STATE.IDLE) {
          this.setState(STATE.LISTENING);
        }
      }
    } catch (err) {
      console.debug("Error in silence detection:", err);
    }
  }

  /**
   * Connect to WebSocket server with reconnection logic
   */
  connectWebSocket() {
    return new Promise((resolve, reject) => {
      const subject = this.subjectSelect.value || "";
      const wsUrl = `ws://${CONFIG.WS_HOST}:${CONFIG.WS_PORT}/ws${subject ? `?subject=${encodeURIComponent(subject)}` : ""}`;

      console.log(`Connecting to ${wsUrl}`);
      this.ws = new WebSocket(wsUrl);
      this.ws.binaryType = "arraybuffer";

      // Initialize reconnection tracking
      this.reconnectAttempts = 0;
      this.maxReconnectAttempts = 3;

      this.ws.onopen = () => {
        console.log("✓ WebSocket connected");
        this.reconnectAttempts = 0; // Reset on successful connection
        resolve();
      };

      this.ws.onmessage = (evt) => {
        if (evt.data instanceof ArrayBuffer) {
          this.onAudioChunk(evt.data);
        } else {
          try {
            const msg = JSON.parse(evt.data);
            this.onServerMessage(msg);
          } catch (err) {
            console.warn("Failed to parse message:", evt.data);
          }
        }
      };

      this.ws.onerror = (err) => {
        console.error("WebSocket error:", err);
        reject(new Error("WebSocket connection failed"));
      };

      this.ws.onclose = () => {
        console.log("WebSocket disconnected");
        // Only attempt reconnection if we were actively in a session
        if (
          this.state !== STATE.IDLE &&
          this.reconnectAttempts < this.maxReconnectAttempts
        ) {
          this.reconnectAttempts++;
          const delay = 1000 * Math.pow(2, this.reconnectAttempts - 1); // Exponential backoff
          console.log(
            `Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})...`,
          );
          setTimeout(() => {
            this.connectWebSocket().catch((err) => {
              console.error("Reconnection failed:", err);
              this.stopSession();
            });
          }, delay);
        } else {
          this.stopSession();
        }
      };

      // Connection timeout
      setTimeout(() => {
        if (this.ws && this.ws.readyState !== WebSocket.OPEN) {
          console.error("WebSocket connection timeout");
          reject(new Error("WebSocket connection timeout"));
        }
      }, 5000);
    });
  }

  /**
   * Handle text messages from server
   */
  onServerMessage(msg) {
    if (!msg || typeof msg !== "object") {
      console.warn("Invalid message format:", msg);
      return;
    }

    if (msg.type === "error") {
      console.error("Server error:", msg.message);
      this.showError(msg.message || "Unknown server error");
    } else if (msg.type === "transcript") {
      if (msg.text) {
        this.addTranscriptItem("student", msg.text);
        this.debug(`Transcript: ${msg.text}`);
      }
    } else if (msg.type === "response_start") {
      this.setState(STATE.PROCESSING);
      this.currentResponse = ""; // Reset accumulator
    } else if (msg.type === "response_chunk") {
      // Accumulate response for transcript display
      if (msg.text) {
        if (!this.currentResponse) {
          this.currentResponse = "";
        }
        this.currentResponse += msg.text;
        this.debug(`Response chunk: ${msg.text.substring(0, 40)}...`);
      }
    } else if (msg.type === "response_end") {
      // Display the accumulated response
      if (this.currentResponse && this.currentResponse.trim()) {
        this.addTranscriptItem("tutor", this.currentResponse);
      }
      this.currentResponse = "";
      // Return to listening state after audio finishes
      setTimeout(() => {
        if (this.state === STATE.SPEAKING) {
          this.setState(STATE.LISTENING);
        }
      }, 500);
    } else if (msg.type === "latency") {
      if (typeof msg.ms === "number") {
        this.displayLatency(msg.ms);
      }
    }
  }

  /**
   * Handle audio chunks received from server
   */
  onAudioChunk(arrayBuffer) {
    if (this.firstAudioTime === null) {
      this.firstAudioTime = Date.now();
      const latency = this.firstAudioTime - this.utteranceStartTime;
      this.displayLatency(latency);
      this.debug(`First audio received: ${latency}ms`);
    }

    // Queue audio for playback
    this.audioPlaybackQueue.push(arrayBuffer);

    if (!this.isPlaying) {
      this.playNextAudioChunk();
    }
  }

  /**
   * Play queued audio chunks sequentially
   */
  async playNextAudioChunk() {
    if (this.audioPlaybackQueue.length === 0) {
      this.isPlaying = false;
      this.firstAudioTime = null;
      return;
    }

    this.isPlaying = true;
    this.setState(STATE.SPEAKING);

    const arrayBuffer = this.audioPlaybackQueue.shift();

    try {
      const audioBuffer = await this.audioContext.decodeAudioData(arrayBuffer);
      const source = this.audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(this.audioContext.destination);

      await new Promise((resolve) => {
        source.onended = resolve;
        source.start(0);
      });

      // Play next chunk
      this.playNextAudioChunk();
    } catch (err) {
      console.error("Failed to decode audio:", err);
      this.playNextAudioChunk();
    }
  }

  /**
   * Stop the current session
   */
  async stopSession() {
    this.isRecording = false;

    // Close WebSocket cleanly
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        // Send stop message if connection is open
        this.ws.send(JSON.stringify({ type: "stop" }));
        // Give server time to process before closing
        await new Promise((resolve) => setTimeout(resolve, 100));
      } catch (err) {
        console.debug("Error sending stop message:", err);
      }
      try {
        this.ws.close();
      } catch (err) {
        console.debug("Error closing WebSocket:", err);
      }
    }
    this.ws = null;

    // Stop microphone
    if (this.mediaStream) {
      try {
        this.mediaStream.getTracks().forEach((track) => {
          try {
            track.stop();
          } catch (err) {
            console.debug("Error stopping track:", err);
          }
        });
      } catch (err) {
        console.debug("Error stopping media stream:", err);
      }
      this.mediaStream = null;
    }

    if (this.processor) {
      try {
        if (this.processor.port) {
          this.processor.port.onmessage = null;
          this.processor.port.close();
        }
      } catch (err) {
        console.debug("Error closing processor port:", err);
      }
      try {
        this.processor.disconnect();
      } catch (err) {
        console.debug("Error disconnecting processor:", err);
      }
      this.processor = null;
    }

    if (this.sourceNode) {
      try {
        this.sourceNode.disconnect();
      } catch (err) {
        console.debug("Error disconnecting source:", err);
      }
      this.sourceNode = null;
    }

    if (this.sinkNode) {
      try {
        this.sinkNode.disconnect();
      } catch (err) {
        console.debug("Error disconnecting sink:", err);
      }
      this.sinkNode = null;
    }

    // Stop audio playback
    this.audioPlaybackQueue = [];
    this.isPlaying = false;

    // Clear response accumulator
    this.currentResponse = "";

    // Update UI
    this.setState(STATE.IDLE);
    this.startBtn.textContent = "Start Session";
    this.subjectSelect.disabled = false;
    this.latencyValue.textContent = "—";
    this.firstAudioTime = null;
    this.utteranceStartTime = null;
  }

  /**
   * Update application state and UI
   */
  setState(newState) {
    this.state = newState;

    const stateConfig = {
      [STATE.IDLE]: { dot: "", text: "Ready", color: "" },
      [STATE.LISTENING]: {
        dot: "listening",
        text: "🎤 Listening",
        color: "listening",
      },
      [STATE.PROCESSING]: {
        dot: "processing",
        text: "⚙️ Processing",
        color: "processing",
      },
      [STATE.SPEAKING]: {
        dot: "speaking",
        text: "🔊 Speaking",
        color: "speaking",
      },
    };

    const config = stateConfig[newState];
    this.statusDot.className = `status-dot ${config.dot}`;
    this.statusText.textContent = config.text;
  }

  /**
   * Display latency value
   */
  displayLatency(ms) {
    this.latencyValue.textContent = `${Math.round(ms)}ms`;
    this.latencyValue.classList.add("active");
  }

  /**
   * Add a line to the transcript
   */
  addTranscriptItem(type, text) {
    if (this.transcriptList.querySelector(".welcome")) {
      this.transcriptList.innerHTML = "";
    }

    const item = document.createElement("div");
    item.className = `transcript-item ${type}`;
    item.innerHTML = `<p>${this.escapeHtml(text)}</p>`;
    this.transcriptList.appendChild(item);
    this.transcriptList.scrollTop = this.transcriptList.scrollHeight;
  }

  /**
   * Display error message
   */
  showError(message) {
    this.addTranscriptItem("error", `❌ Error: ${message}`);
    console.error(message);
  }

  /**
   * Debug logging
   */
  debug(message) {
    console.log(`[DEBUG] ${message}`);
    // Uncomment to show debug info in UI:
    // this.debugText.textContent = message;
  }

  /**
   * Escape HTML entities
   */
  escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }
}

// Initialize when DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  console.log("DOM loaded, initializing Cascade...");
  window.cascadeClient = new CascadeClient();
});
