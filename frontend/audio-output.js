import { STATE } from "./state.js?v=2.0.2";
import {
  canScheduleAudioChunk,
  resolvePlaybackCompletion,
} from "./playback-state.js?v=2.0.2";

const PLAYBACK_TAIL_TIMEOUT_MS = 3000;

export class AudioOutputController {
  constructor(client) {
    this.client = client;
    this.audioContext = null;
    this.playbackGain = null;
    this.analyser = null;

    this.isPlaying = false;
    this.isAudioSourceEnded = false;
    this.nextPlaybackTime = null;
    this.speakingStartTime = null;
    this.activeSourceNodes = [];
    this.playbackTurnId = null;
    this.ttsConfig = { format: "linear16", sampleRate: 24000 };

    this._audioResumed = false;
    this._visualizationLoopId = null;
    this._playbackTailTimeout = null;
  }

  initContext() {
    if (this.audioContext) return true;
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
      return true;
    } catch (err) {
      console.error("AudioContext not available:", err);
      this.audioContext = null;
      return false;
    }
  }

  ensurePlaybackReady() {
    return this.initContext();
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

  _clearPlaybackTailTimeout() {
    if (this._playbackTailTimeout) {
      clearTimeout(this._playbackTailTimeout);
      this._playbackTailTimeout = null;
    }
  }

  _startPlaybackTailTimeout() {
    this._clearPlaybackTailTimeout();
    this._playbackTailTimeout = setTimeout(() => {
      this._playbackTailTimeout = null;
      if (
        this.activeSourceNodes.length === 0 &&
        !this.isAudioSourceEnded &&
        (this.client.state === STATE.WINDING_DOWN ||
          this.client.state === STATE.SPEAKING)
      ) {
        console.warn(
          "[AudioOutput] Playback tail timeout — forcing LISTENING recovery",
        );
        this.isAudioSourceEnded = true;
        this._checkPlaybackFinished();
      }
    }, PLAYBACK_TAIL_TIMEOUT_MS);
  }

  _cleanupSpeakingVisuals() {
    if (this._visualizationLoopId) {
      cancelAnimationFrame(this._visualizationLoopId);
      this._visualizationLoopId = null;
    }
    if (this.client.ui?.orb) {
      this.client.ui.resetOrbSpeakVars();
    }
  }

  stopAllPlayback() {
    const now = this.audioContext ? this.audioContext.currentTime : 0;

    this._clearPlaybackTailTimeout();

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
    this._cleanupSpeakingVisuals();

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

    const epoch = this.client.audioEpoch;
    const decodeGen = this.client.decodeGeneration;
    const stateAtStart = this.client.state;

    if (!this.client._isTurnActive(turnId)) {
      console.debug(
        `[AudioOutput] Audio chunk dropped (Guard 1): turnId=${turnId}, activeTurnId=${this.client.activeTurnId}, epoch=${epoch}, audioEpoch=${this.client.audioEpoch}`,
      );
      return;
    }
    if (!canScheduleAudioChunk(stateAtStart, this.isAudioSourceEnded)) {
      return;
    }

    if (this.audioContext && this.audioContext.state === "suspended") {
      await this.resumeContext();
    }

    if (!this.audioContext) return;
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

      if (
        audioBuffer &&
        epoch === this.client.audioEpoch &&
        decodeGen === this.client.decodeGeneration &&
        turnId === this.client.activeTurnId &&
        canScheduleAudioChunk(this.client.state, this.isAudioSourceEnded)
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
    if (
      epoch !== this.client.audioEpoch ||
      decodeGen !== this.client.decodeGeneration ||
      turnId !== this.client.activeTurnId ||
      !canScheduleAudioChunk(this.client.state, this.isAudioSourceEnded)
    ) {
      console.debug(
        `[AudioOutput] Audio NOT scheduled (Guard 4): Guard check failed for turnId=${turnId}`,
      );
      return;
    }

    this._clearPlaybackTailTimeout();
    this.isPlaying = true;
    this.playbackTurnId = turnId;

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

    const isFirstScheduledSource =
      this.activeSourceNodes.length === 1 &&
      !this.client._firstAudioPlayed &&
      (this.client.state === STATE.PROCESSING ||
        this.client.state === STATE.WINDING_DOWN);

    if (isFirstScheduledSource) {
      this.speakingStartTime = Date.now();
    }

    if (
      this.client.state === STATE.PROCESSING ||
      this.client.state === STATE.WINDING_DOWN
    ) {
      this.client.setState(STATE.SPEAKING);
    }

    if (isFirstScheduledSource) {
      this._sendPerceivedLatency(turnId);
    }

    if (this.analyser && !this._visualizationLoopId) {
      const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
      const barCount = 4;
      const tick = () => {
        if (this.client.state !== STATE.SPEAKING) {
          this._visualizationLoopId = null;
          return;
        }
        this.analyser.getByteFrequencyData(dataArray);
        const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
        const normalizedAudio = Math.min(1, avg / 60);
        const sliceSize = Math.max(1, Math.floor(dataArray.length / barCount));
        const t = performance.now() / 200;

        if (this.client.ui.orb) {
          this.client.ui.orb.style.setProperty("--audio-level", avg.toFixed(1));
          this.client.ui.orb.style.setProperty(
            "--audio-level-norm",
            normalizedAudio.toFixed(3),
          );
          for (let i = 0; i < barCount; i++) {
            const slice = dataArray.subarray(
              i * sliceSize,
              (i + 1) * sliceSize,
            );
            const sliceAvg =
              slice.reduce((a, b) => a + b, 0) / Math.max(1, slice.length);
            const sliceNorm = Math.min(1, sliceAvg / 60);
            const wobble = 0.85 + 0.15 * Math.sin(t + i * 0.9);
            this.client.ui.orb.style.setProperty(
              `--speak-bar-${i + 1}`,
              Math.min(1, sliceNorm * wobble).toFixed(3),
            );
          }
        }
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

  _sendPerceivedLatency(turnId) {
    const startMs = this.client._speechEndMs ?? this.client._turnStartMs;
    if (startMs == null) return;

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

  _checkPlaybackFinished() {
    const result = resolvePlaybackCompletion({
      activeSourceCount: this.activeSourceNodes.length,
      isAudioSourceEnded: this.isAudioSourceEnded,
      currentState: this.client.state,
    });

    if (result.action === "none") {
      return;
    }

    if (result.action === "wind_down") {
      this.isPlaying = false;
      this._cleanupSpeakingVisuals();
      this.client.audioInput.pauseVadForWindDown();
      this._startPlaybackTailTimeout();
      this.client.setState(STATE.WINDING_DOWN);
      return;
    }

    this._clearPlaybackTailTimeout();
    this.isPlaying = false;
    this._cleanupSpeakingVisuals();

    if (this.client.transport && this.client.transport.isOpen()) {
      this.client.transport.send(
        JSON.stringify({
          type: "playback_finished",
          turn_id: this.playbackTurnId,
        }),
      );
    }

    if (result.nextState) {
      this.client.setState(result.nextState);
      this.client._resetTurnState();
    }
  }

  resetState() {
    this._clearPlaybackTailTimeout();
    this.nextPlaybackTime = null;
    this.isPlaying = false;
    this.isAudioSourceEnded = false;
  }
}
