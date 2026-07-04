import { STATE } from "./state.js";

export class AudioOutputController {
  constructor(client) {
    this.client = client;
    this.audioContext = null;
    this.playbackGain = null;
    this.analyser = null;

    this.isPlaying = false;
    this.isAudioSourceEnded = false; // N2: explicit initialization
    this.nextPlaybackTime = null;
    this.speakingStartTime = null;
    this.activeSourceNodes = [];
    this.playbackTurnId = null;
    this.ttsConfig = { format: "linear16", sampleRate: 24000 };

    this._audioResumed = false;
    this._visualizationLoopId = null; // Track the animation frame loop
  }

  initContext() {
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
      console.log("✓ AudioContext and gain graph initialized");
    } catch (err) {
      console.error("AudioContext not available:", err);
    }
  }

  async resumeContext() {
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

  stopAllPlayback() {
    const now = this.audioContext ? this.audioContext.currentTime : 0;

    // Immediately disconnect old sources to prevent overlap
    if (this.playbackGain && this.audioContext) {
      this.playbackGain.gain.cancelScheduledValues(now);
      this.playbackGain.gain.setValueAtTime(0, now);
    }

    this.activeSourceNodes.forEach((source) => {
      try {
        source.stop(now);
      } catch (_) {}
      try {
        source.disconnect();
      } catch (_) {}
    });
    this.activeSourceNodes = [];
    this.nextPlaybackTime = null;
    this.isPlaying = false;

    // Cancel the visualization loop
    if (this._visualizationLoopId) {
      cancelAnimationFrame(this._visualizationLoopId);
      this._visualizationLoopId = null;
      if (this.client.ui.orb) {
        this.client.ui.orb.style.setProperty("--audio-level", "0");
      }
    }

    if (this.playbackGain && this.audioContext) {
      this.playbackGain.gain.cancelScheduledValues(now);
      this.playbackGain.gain.setValueAtTime(1, now);
    }

    console.log(
      "[AudioOutput] All playback stopped immediately (interrupt safety)",
    );
  }

  async onAudioChunk(arrayBuffer) {
    if (arrayBuffer.byteLength < 4) return;

    const view = new DataView(arrayBuffer);
    const turnId = view.getUint32(0, false);
    const audioPayload = arrayBuffer.slice(4);

    // Snapshot all guard values at the start
    const epoch = this.client.audioEpoch;
    const decodeGen = this.client.decodeGeneration;
    const activeTurnAtStart = this.client.activeTurnId;
    const stateAtStart = this.client.state;

    // Early guards - GUARD 1: Check if this turn is still active
    if (!this.client._isTurnActive(turnId)) {
      console.debug(
        `[AudioOutput] Audio chunk dropped (Guard 1): turnId=${turnId}, activeTurnId=${this.client.activeTurnId}, epoch=${epoch}, audioEpoch=${this.client.audioEpoch}`,
      );
      return;
    }
    if (
      stateAtStart === STATE.LISTENING ||
      stateAtStart === STATE.IDLE ||
      stateAtStart === STATE.CONNECTING
    ) {
      return;
    }

    if (this.audioContext && this.audioContext.state === "suspended") {
      await this.resumeContext();
    }

    // Check guards again after async resume - GUARD 2: Epoch/generation validation
    if (
      epoch !== this.client.audioEpoch ||
      decodeGen !== this.client.decodeGeneration ||
      turnId !== this.client.activeTurnId
    ) {
      console.debug(
        `[AudioOutput] Audio chunk dropped (Guard 2): turnId=${turnId}, epoch check: ${epoch} vs ${this.client.audioEpoch}, gen check: ${decodeGen} vs ${this.client.decodeGeneration}, turn check: ${turnId} vs ${this.client.activeTurnId}`,
      );
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

      // FINAL guard check before playback - GUARD 3: Pre-playback validation
      if (
        audioBuffer &&
        epoch === this.client.audioEpoch &&
        decodeGen === this.client.decodeGeneration &&
        turnId === this.client.activeTurnId &&
        this.client.state !== STATE.LISTENING &&
        this.client.state !== STATE.IDLE
      ) {
        console.debug(
          `[AudioOutput] Audio chunk APPROVED for playback: turnId=${turnId}, epoch=${epoch}, gen=${decodeGen}, bufferDuration=${audioBuffer.duration.toFixed(3)}s`,
        );
        this._schedulePlayback(audioBuffer, epoch, turnId, decodeGen);
      } else if (audioBuffer) {
        console.debug(
          `[AudioOutput] Audio chunk dropped (Guard 3): state=${this.client.state}, turnId=${turnId}`,
        );
      }
    } catch (err) {
      console.error("[AudioOutput] Audio decode failed:", err);
    }
  }

  _schedulePlayback(audioBuffer, epoch, turnId, decodeGen) {
    // GUARD 4: Final sanity check before scheduling source.
    // speakingStartTime is stamped AFTER this guard so that dropped stale
    // chunks don't reset the timer used by barge-in detection.
    if (
      epoch !== this.client.audioEpoch ||
      decodeGen !== this.client.decodeGeneration ||
      turnId !== this.client.activeTurnId ||
      this.client.state === STATE.LISTENING ||
      this.client.state === STATE.IDLE
    ) {
      console.debug(
        `[AudioOutput] Audio NOT scheduled (Guard 4): Guard check failed for turnId=${turnId}`,
      );
      return;
    }

    // Only stamp if we actually proceed to schedule playback
    this.speakingStartTime = Date.now();
    this.client.setState(STATE.SPEAKING);
    this.isPlaying = true;
    this.playbackTurnId = turnId;

    // Perceived-latency telemetry: send once per turn, on first scheduled audio.
    // Measures user-speech-end → first audio heard (true felt latency).
    //
    // Start anchor: _speechEndMs — stamped by the VAD in audio-input.js at the
    // moment client-side silence was detected (best proxy for "user stopped talking").
    // Falls back to _turnStartMs (transcript receipt) if VAD stamp is unavailable.
    //
    // End correction: AudioContext audio is buffered by the OS audio stack before
    // reaching the speaker. outputLatency (hardware buffer) and the scheduling
    // lookahead (nextPlaybackTime - currentTime) are added so the measurement
    // reflects when the user hears the first sample, not when it was queued.
    if (!this.client._firstAudioPlayed) {
      const startMs = this.client._speechEndMs ?? this.client._turnStartMs;
      if (startMs != null) {
        this.client._firstAudioPlayed = true;
        const outputLatencyMs = (this.audioContext.outputLatency || 0) * 1000;
        const schedulingOffsetMs = Math.max(
          0,
          (this.nextPlaybackTime - this.audioContext.currentTime) * 1000,
        );
        const perceivedMs = Math.round(
          performance.now() - startMs + outputLatencyMs + schedulingOffsetMs,
        );
        console.log(
          `[AudioOutput] Felt latency: ${perceivedMs}ms ` +
            `(startAnchor=${this.client._speechEndMs != null ? "VAD" : "transcript"}, ` +
            `outputLatency=${outputLatencyMs.toFixed(1)}ms, ` +
            `schedulingOffset=${schedulingOffsetMs.toFixed(1)}ms)`,
        );
        if (this.client.transport && this.client.transport.isOpen()) {
          this.client.transport.send(
            JSON.stringify({
              type: "client_latency",
              first_audio_played_ms: perceivedMs,
              turn_id: turnId,
            }),
          );
        }
      }
    }

    const currentTime = this.audioContext.currentTime;
    if (this.nextPlaybackTime === null || this.nextPlaybackTime < currentTime) {
      this.nextPlaybackTime = currentTime + 0.01;
    }

    console.log(
      `[AudioOutput] Scheduling audio: turnId=${turnId}, epoch=${epoch}, gen=${decodeGen}, buffer=${audioBuffer.duration.toFixed(3)}s, playAt=${this.nextPlaybackTime.toFixed(3)}, totalSources=${this.activeSourceNodes.length + 1}`,
    );

    const source = this.audioContext.createBufferSource();
    source.buffer = audioBuffer;
    if (this.analyser) {
      source.connect(this.analyser);
    } else if (this.playbackGain) {
      source.connect(this.playbackGain);
    } else {
      source.connect(this.audioContext.destination);
    }

    if (
      epoch !== this.client.audioEpoch ||
      decodeGen !== this.client.decodeGeneration ||
      turnId !== this.client.activeTurnId
    ) {
      return;
    }

    source.start(this.nextPlaybackTime);
    this.activeSourceNodes.push(source);

    if (this.analyser && !this._visualizationLoopId) {
      const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
      const tick = () => {
        if (this.client.state !== STATE.SPEAKING) {
          this._visualizationLoopId = null;
          return;
        }
        this.analyser.getByteFrequencyData(dataArray);
        const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
        if (this.client.ui.orb)
          this.client.ui.orb.style.setProperty("--audio-level", avg.toFixed(1));
        this._visualizationLoopId = requestAnimationFrame(tick);
      };
      this._visualizationLoopId = requestAnimationFrame(tick);
    }

    source.onended = () => {
      const index = this.activeSourceNodes.indexOf(source);
      if (index > -1) this.activeSourceNodes.splice(index, 1);
      this._checkPlaybackFinished();
    };

    this.nextPlaybackTime = this.nextPlaybackTime + audioBuffer.duration;
  }

  _checkPlaybackFinished() {
    if (this.activeSourceNodes.length === 0 && this.client.isAudioSourceEnded) {
      this.isPlaying = false;

      // Notify server that playback for this turn has truly finished over the speakers
      if (this.client.transport && this.client.transport.isOpen()) {
        this.client.transport.send(
          JSON.stringify({
            type: "playback_finished",
            turn_id: this.playbackTurnId,
          })
        );
      }

      if (this.client.state === STATE.SPEAKING) {
        this.client.setState(STATE.LISTENING);
        this.client._resetTurnState();
      } else if (this.client.state === STATE.PROCESSING) {
        this.client.setState(STATE.LISTENING);
        this.client._resetTurnState();
      }
    }
  }

  resetState() {
    this.nextPlaybackTime = null;
    this.isPlaying = false;
  }
}
