import { STATE } from "./state.js";

export class AudioInputController {
  constructor(client) {
    this.client = client;
    
    this.mediaStream = null;
    this.processor = null;
    this.sourceNode = null;
    this.sinkNode = null;
    this.isRecording = false;

    this.maxAudioLevel = 0;
    this._speechDetected = false;
    this._finalizeTimeout = null;
    this._lastFinalizeAt = 0;
    this.localFinalizeSilenceMs = 130;
    this.localFinalizeCooldownMs = 600;
    this._interruptionBuffer = [];
    this.utteranceStartTime = null;
    this.lastUtteredTime = null;

    // Tracks performance.now() of the last audio frame that contained speech.
    // Used to stamp client._speechEndMs when silence is first detected, giving
    // an accurate "user stopped talking" anchor for felt-latency measurement.
    this._lastSpeechPerfNow = null;
  }

  async start() {
    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: false,
      },
    });

    this.maxAudioLevel = 0;
    this._speechDetected = false;
    this._interruptionBuffer = [];
    
    await this._initAudioProcessing();
  }

  stop() {
    this.isRecording = false;
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

    this._clearFinalizeTimer();
    this.utteranceStartTime = null;
    this.lastUtteredTime = null;
    this._speechDetected = false;
    this._interruptionBuffer = [];
  }

  _clearFinalizeTimer() {
    if (this._finalizeTimeout) {
      clearTimeout(this._finalizeTimeout);
      this._finalizeTimeout = null;
    }
  }

  resetTurnState() {
    this._clearFinalizeTimer();
    this.utteranceStartTime = null;
    this.lastUtteredTime = null;
    this._lastSpeechPerfNow = null;
    this.maxAudioLevel = 0;
    this._speechDetected = false;
  }

  resetPlaybackOnly() {
    this._interruptionBuffer = [];
  }

  _markSpeechDetected(now = Date.now()) {
    if (!this._speechDetected) {
      this._speechDetected = true;
      this.utteranceStartTime = now;
    }
    this.lastUtteredTime = now;
    // Record performance.now() of this speech frame (for felt-latency anchor).
    // Also clear any stale _speechEndMs from the previous turn so the new
    // turn's stamp is computed fresh when silence is detected.
    this._lastSpeechPerfNow = performance.now();
    this.client._speechEndMs = null;
    this._clearFinalizeTimer();
  }

  _sendFinalize(reason = "local_vad") {
    if (!this.client.transport || !this.client.transport.isOpen()) return;
    const now = Date.now();
    if (now - this._lastFinalizeAt < this.localFinalizeCooldownMs) return;
    this._lastFinalizeAt = now;
    this._speechDetected = false;
    this.utteranceStartTime = null;
    this.lastUtteredTime = null;
    this._clearFinalizeTimer();
    this.client.transport.send(JSON.stringify({ type: "finalize", reason }));
    console.log(`[AudioInput] Sent finalize signal (${reason})`);
  }

  _scheduleFinalizeIfSilent(now = Date.now()) {
    if (
      !this._speechDetected ||
      !this.lastUtteredTime ||
      this.client.state !== STATE.LISTENING
    ) {
      return;
    }
    if (now - this.lastUtteredTime < this.localFinalizeSilenceMs) return;
    if (this._finalizeTimeout) return;

    // Stamp the felt-latency start once the silence window has passed.
    // _lastSpeechPerfNow is performance.now() of the last audio frame that
    // contained detectable speech — the best client-side proxy for
    // "user stopped talking". This gives felt_ms a start anchor that
    // pre-dates the transcript message by ~endpointing_ms, making felt_ms
    // correctly larger than pipeline total_ms.
    if (this._lastSpeechPerfNow != null && this.client._speechEndMs == null) {
      this.client._speechEndMs = this._lastSpeechPerfNow;
      console.log(
        `[AudioInput] Felt-latency anchor stamped: _speechEndMs = ${this.client._speechEndMs.toFixed(1)}ms`,
      );
    }

    this._finalizeTimeout = setTimeout(() => {
      this._finalizeTimeout = null;
      if (this.client.state === STATE.LISTENING && this._speechDetected) {
        this._sendFinalize("local_silence");
      }
    }, 60);
  }

  async _initAudioProcessing() {
    const audioContext = this.client.audioOutput.audioContext;
    if (!this.mediaStream || !audioContext) return;
    const source = audioContext.createMediaStreamSource(this.mediaStream);
    this.sourceNode = source;
    const sink = audioContext.createGain();
    sink.gain.value = 0;
    this.sinkNode = sink;

    const workletCode = this._getWorkletCode();
    try {
      const blob = new Blob([workletCode], { type: "application/javascript" });
      const url = URL.createObjectURL(blob);
      await audioContext.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);

      this.processor = new AudioWorkletNode(
        audioContext,
        "audio-processor",
      );
      this.processor.port.onmessage = (evt) => this._onAudioData(evt.data);
      source.connect(this.processor);
      this.processor.connect(sink);
      sink.connect(audioContext.destination);
      console.log("✓ AudioWorklet ready");
    } catch (_) {
      console.warn(
        "AudioWorklet unavailable — falling back to ScriptProcessor",
      );
      this.processor = audioContext.createScriptProcessor(4096, 1, 1);
      this.processor.onaudioprocess = (evt) => {
        const inputData = evt.inputBuffer.getChannelData(0);
        const ratio = audioContext.sampleRate / 16000;
        const downsampled = new Float32Array(
          Math.floor(inputData.length / ratio),
        );
        for (let i = 0; i < downsampled.length; i++) {
          let sum = 0;
          const start = Math.floor(i * ratio);
          const end = Math.min(Math.ceil((i + 1) * ratio), inputData.length);
          for (let j = start; j < end; j++) sum += inputData[j];
          downsampled[i] = sum / Math.max(1, end - start);
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
      sink.connect(audioContext.destination);
    }
    this.isRecording = true;
  }

  _getWorkletCode() {
    return `
      class AudioProcessor extends AudioWorkletProcessor {
        constructor() {
            super();
            this.bufferSize = 160; // 10ms of audio at 16kHz
            this.buffer = new Int16Array(this.bufferSize);
            this.bufferWriteIndex = 0;
        }

        process(inputs) {
          const input = inputs[0][0];
          if (input && input.length > 0) {
            const ratio = sampleRate / 16000;
            const downsampled = new Float32Array(Math.floor(input.length / ratio));
            for (let i = 0; i < downsampled.length; i++) {
              let sum = 0;
              const start = Math.floor(i * ratio);
              const end = Math.min(Math.ceil((i + 1) * ratio), input.length);
              for (let j = start; j < end; j++) sum += input[j];
              downsampled[i] = sum / Math.max(1, end - start);
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

    if (this.client.transport && this.client.transport.isOpen()) {
      this.client.transport.send(bytes);
    }

    if (
      this.client.state === STATE.LISTENING ||
      this.client.state === STATE.SPEAKING ||
      this.client.state === STATE.PROCESSING
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

    // Leaky peak detector
    this.maxAudioLevel = Math.max(0.05, this.maxAudioLevel * 0.995);
    if (rms > this.maxAudioLevel) this.maxAudioLevel = rms;
    const threshold = Math.max(0.02, this.maxAudioLevel * 0.05);

    if (this.client.ui.orb && this.client.state === STATE.LISTENING)
      this.client.ui.orb.style.setProperty("--rms", rms.toFixed(3));

    if (this.client.sessionStartTime && Date.now() - this.client.sessionStartTime < 1500)
      return;

    if (this.client.state === STATE.LISTENING) {
      if (rms >= threshold) {
        this._markSpeechDetected();
      } else {
        this._scheduleFinalizeIfSilent();
      }
    } else if (
      this.client.state === STATE.SPEAKING ||
      this.client.state === STATE.PROCESSING
    ) {
      this._detectInterruption(rms);
    }
  }

  _detectInterruption(rms) {
    if (this.client.state !== STATE.SPEAKING && this.client.state !== STATE.PROCESSING)
      return;
    if (this.client._interrupting) return;
    if (
      this.client.state === STATE.SPEAKING &&
      this.client.audioOutput.speakingStartTime &&
      Date.now() - this.client.audioOutput.speakingStartTime < 150
    ) {
      return;
    }

    if (!this._interruptionBuffer) this._interruptionBuffer = [];

    this._interruptionBuffer.push(rms);
    if (this._interruptionBuffer.length > 3) this._interruptionBuffer.shift();

    const avgRms =
      this._interruptionBuffer.reduce((a, b) => a + b, 0) /
      this._interruptionBuffer.length;
    
    const multiplier = this.client.state === STATE.SPEAKING ? 0.25 : 0.15;
    const threshold = Math.max(0.05, this.maxAudioLevel * multiplier);

    if (avgRms > threshold) {
      this.client._triggerInterruption();
    }
  }
}
