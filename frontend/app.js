/**
 * Cascade — AI Voice Tutor Frontend
 */

import { UIController } from "./ui.js";
import { AudioInputController } from "./audio-input.js";
import { AudioOutputController } from "./audio-output.js";
import { WebSocketTransport } from "./transport.js";
import { ChartRenderer } from "./chart.js";

import { STATE } from "./state.js";

class CascadeClient {
  constructor() {
    this.state = STATE.IDLE;
    
    // Turn & Guard variables
    this.sessionStartTime = null;
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
    
    this.currentResponse = "";
    this.currentStreamingBubble = null;
    this.selectedTTSEngine =
      localStorage.getItem("cascade_tts_engine") || "deepgram";

    // Setup sub-controllers
    this.ui = new UIController(this);
    this.audioOutput = new AudioOutputController(this);
    this.audioInput = new AudioInputController(this);
    this.transport = new WebSocketTransport(this);
    this.chart = new ChartRenderer(this);

    this.audioOutput.initContext();
  }

  get isMuted() {
    return false;
  }

  get isAudioSourceEnded() {
    return this.audioOutput.isAudioSourceEnded;
  }

  set isAudioSourceEnded(val) {
    this.audioOutput.isAudioSourceEnded = val;
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
      await this.audioOutput.resumeContext();
      await this.audioInput.start();
      
      this.setState(STATE.CONNECTING);
      await this.transport.connect();
      
      this.setState(STATE.LISTENING);
      this.sessionStartTime = Date.now();
      
      if (this.ui.transcriptPanel) this.ui.transcriptPanel.innerHTML = "";
      this.ui._showEmptyStateIfNeeded();
    } catch (err) {
      console.error("startSession failed:", err);
      let msg = `Failed to start: ${err.message}`;
      if (err.name === "NotAllowedError") {
        msg = "🔒 Microphone permission denied. Enable it in browser settings.";
      } else if (err.name === "NotFoundError") {
        msg = "🎤 No microphone found on this device.";
      }
      this.ui.showError(msg);
      await this.stopSession();
    }
  }

  async stopSession() {
    this.transport.intentionalDisconnect = true;
    if (this.transport.isOpen()) {
      try {
        this.transport.send("stop");
        await new Promise((r) => setTimeout(r, 100));
      } catch (_) {}
    }
    this.transport.close();
    this.audioInput.stop();
    this.audioOutput.stopAllPlayback();

    this.currentResponse = "";
    if (this.currentStreamingBubble) {
      this.currentStreamingBubble.remove();
      this.currentStreamingBubble = null;
    }
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
    this._resetTurnState();
    this.sessionStartTime = null;

    this.setState(STATE.IDLE);
  }

  _resetTurnState() {
    this.audioInput.resetTurnState();
    this.audioOutput.resetState();
  }

  _resetPlaybackOnly() {
    this.audioInput.resetPlaybackOnly();
    this.audioOutput.resetState();
  }

  _isTurnActive(turnId) {
    return (
      turnId != null &&
      this.activeTurnId != null &&
      turnId === this.activeTurnId
    );
  }

  _triggerInterruption() {
    if (this.state !== STATE.SPEAKING && this.state !== STATE.PROCESSING)
      return;
    if (this._interrupting) return;
    this._interrupting = true;

    const prevTurnId = this.activeTurnId;
    const prevEpoch = this.audioEpoch;
    const prevGen = this.decodeGeneration;

    console.log(
      `[Client] Interruption triggered! Previous turn=${prevTurnId}, epoch=${prevEpoch}, generation=${prevGen}, state=${this.state}`,
    );

    this._pendingCancelTurnId = this.activeTurnId ?? this.playbackTurnId;
    this.audioEpoch += 1;
    this.decodeGeneration += 1;
    this.activeTurnId = null;
    this.playbackTurnId = null;

    console.log(
      `[Client] Epoch incremented to ${this.audioEpoch}, generation to ${this.decodeGeneration}, activeTurnId set to null`,
    );

    this.audioOutput.stopAllPlayback();
    this.audioInput.resetPlaybackOnly();
    this.audioInput._markSpeechDetected();

    if (this.transport.isOpen()) {
      console.log("[Client] Sending cancel message to backend");
      this.transport.send(JSON.stringify({ type: "cancel" }));
    }

    this.currentResponse = "";
    if (this.currentStreamingBubble) {
      this.currentStreamingBubble.remove();
      this.currentStreamingBubble = null;
    }
    this.setState(STATE.LISTENING);
    this._resetPlaybackOnly();

    if (this._interruptTimeout) clearTimeout(this._interruptTimeout);
    this._interruptTimeout = setTimeout(() => {
      this._interrupting = false;
      this._interruptTimeout = null;
    }, 1000);
  }

  _onServerMessage(msg) {
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case "tts_config":
        this.audioOutput.ttsConfig = {
          format: msg.format,
          sampleRate: msg.sampleRate || msg.sample_rate,
        };
        console.log("[Client] Received TTS config:", this.audioOutput.ttsConfig);
        break;
      case "transcript":
        if (msg.text) {
          if (this.audioOutput.activeSourceNodes.length > 0 || this.audioOutput.isPlaying) {
            this.audioOutput.stopAllPlayback();
          }
          if (msg.turn_id != null) {
            this.activeTurnId = msg.turn_id;
            this.playbackTurnId = msg.turn_id;
          }
          this._interrupting = false;
          this._resetTurnState();
          this.ui.addTranscriptItem("student", msg.text);
          // Reset the stream buffer so previous turn's text does not bleed into
          // the next turn's streaming bubble.  If there is an in-progress bubble
          // from an interrupted turn, finalize it in place (remove the cursor
          // animation) rather than deleting it — so the chat history stays intact.
          this.currentResponse = "";
          if (this.currentStreamingBubble) {
            this.currentStreamingBubble.classList.remove("streaming");
            this.currentStreamingBubble = null;
          }
          this.totalTurns++;
          this.ui._updateStatsBar();
          this.setState(STATE.PROCESSING);
        }
        break;
      case "response_chunk":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        if (msg.text) {
          this.currentResponse = this.currentResponse
            ? `${this.currentResponse} ${msg.text}`
            : msg.text;
          
          if (!this.currentStreamingBubble) {
            this.currentStreamingBubble = this.ui.addTranscriptItem("tutor", this.currentResponse);
            if (this.currentStreamingBubble) this.currentStreamingBubble.classList.add("streaming");
          } else {
            const p = this.currentStreamingBubble.querySelector("p");
            if (p) p.innerHTML = this.ui._escapeHTML(this.currentResponse);
            if (this.ui.transcriptPanel) this.ui.transcriptPanel.scrollTop = this.ui.transcriptPanel.scrollHeight;
          }
        }
        break;
      case "response_end":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        if (this.currentStreamingBubble) {
          this.currentStreamingBubble.classList.remove("streaming");
          this.currentStreamingBubble.classList.add("message-complete");
          setTimeout(() => {
            if (this.currentStreamingBubble) this.currentStreamingBubble.classList.remove("message-complete");
            this.currentStreamingBubble = null;
          }, 1200);
        } else if (this.currentResponse && this.currentResponse.trim()) {
          const bubble = this.ui.addTranscriptItem(
            "tutor",
            this.currentResponse.trim(),
          );
          if (bubble) {
            bubble.classList.add("message-complete");
            setTimeout(() => bubble.classList.remove("message-complete"), 1200);
          }
        }
        this.currentResponse = "";
        this.currentStreamingBubble = null;
        this.audioOutput.isAudioSourceEnded = true;
        this.audioOutput._checkPlaybackFinished();
        break;
      case "turn_cancelled":
        if (msg.turn_id != null && this.activeTurnId === msg.turn_id) {
          this.activeTurnId = null;
          this.playbackTurnId = null;
          this.audioEpoch += 1;
          this.decodeGeneration += 1;
          this.audioOutput.stopAllPlayback();
          this.audioOutput.isAudioSourceEnded = true;
          this.audioOutput._checkPlaybackFinished();
          if (this.currentStreamingBubble) {
            this.currentStreamingBubble.remove();
            this.currentStreamingBubble = null;
          }
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

          const turnNum = msg.turn_id != null ? msg.turn_id : this.totalTurns;
          let entry = this.latencyHistory.find((d) => d.turn === turnNum);
          if (!entry) {
            entry = {
              turn: turnNum,
              timestamp: Date.now(),
            };
            this.latencyHistory.push(entry);
            if (this.latencyHistory.length > 10) this.latencyHistory.shift();
          }

          entry.total = msg.total_ms;
          entry.stt = msg.stt_ms || 0;
          entry.llm = msg.llm_ms || 0;
          entry.tts = msg.tts_ms || 0;
          entry.system = Math.max(0, entry.total - (entry.stt + entry.llm + entry.tts));

          entry.llm_queue = entry.llm_queue || 0;
          entry.llm_ttft = entry.llm_ttft || 0;
          entry.llm_streaming = entry.llm_streaming || 0;
          entry.tts_first_sentence = entry.tts || 0;

          this.ui._updateStatsBar();

          const panel = document.getElementById("stats-panel");
          if (panel && panel.classList.contains("open")) {
            this.chart.render();
          }
        } else if (typeof msg.ms === "number") {
          this.lastLatencyMs = msg.ms;
          this.ui._updateStatsBar();
        }
        break;
      case "llm_metrics":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        const llmTurnNum = msg.turn_id != null ? msg.turn_id : this.totalTurns;
        let llmEntry = this.latencyHistory.find((d) => d.turn === llmTurnNum);
        if (!llmEntry) {
          llmEntry = {
            turn: llmTurnNum,
            total: 0,
            stt: 0,
            llm: msg.total_ms || 0,
            tts: 0,
            system: 0,
            llm_queue: msg.queue_ms || 0,
            llm_ttft: msg.ttft_ms || 0,
            llm_streaming: msg.streaming_delay_ms || 0,
            tts_first_sentence: 0,
            timestamp: Date.now(),
          };
          this.latencyHistory.push(llmEntry);
          if (this.latencyHistory.length > 10) this.latencyHistory.shift();
        } else {
          llmEntry.llm = msg.total_ms || 0;
          llmEntry.llm_queue = msg.queue_ms || 0;
          llmEntry.llm_ttft = msg.ttft_ms || 0;
          llmEntry.llm_streaming = msg.streaming_delay_ms || 0;
          if (llmEntry.total > 0) {
            llmEntry.system = Math.max(
              0,
              llmEntry.total - (llmEntry.stt + llmEntry.llm + llmEntry.tts),
            );
          }
        }
        const panel1 = document.getElementById("stats-panel");
        if (panel1 && panel1.classList.contains("open")) {
          this.chart.render();
        }
        break;
      case "tts_metrics":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        const ttsTurnNum = msg.turn_id != null ? msg.turn_id : this.totalTurns;
        let ttsEntry = this.latencyHistory.find((d) => d.turn === ttsTurnNum);
        if (!ttsEntry) {
          ttsEntry = {
            turn: ttsTurnNum,
            total: 0,
            stt: 0,
            llm: 0,
            tts: msg.first_sentence_latency_ms || 0,
            system: 0,
            llm_queue: 0,
            llm_ttft: 0,
            llm_streaming: 0,
            tts_first_sentence: msg.first_sentence_latency_ms || 0,
            tts_engine: msg.engine || "unknown",
            timestamp: Date.now(),
          };
          this.latencyHistory.push(ttsEntry);
          if (this.latencyHistory.length > 10) this.latencyHistory.shift();
        } else {
          ttsEntry.tts = msg.first_sentence_latency_ms || 0;
          ttsEntry.tts_first_sentence = msg.first_sentence_latency_ms || 0;
          ttsEntry.tts_engine = msg.engine || "unknown";
          if (ttsEntry.total > 0) {
            ttsEntry.system = Math.max(
              0,
              ttsEntry.total - (ttsEntry.stt + ttsEntry.llm + ttsEntry.tts),
            );
          }
        }
        const panel2 = document.getElementById("stats-panel");
        if (panel2 && panel2.classList.contains("open")) {
          this.chart.render();
        }
        break;
      case "busy":
        this.ui.showToast(
          msg.message || "⏳ Still responding — please wait a moment.",
          4000,
          "info",
        );
        if (msg.reason === "capacity") {
          this.stopSession();
        }
        break;
      case "tts_error":
        this.ui.showToast(
          `⚠️ Audio synthesis failed: ${msg.message || "Unknown error"}`,
          4000,
          "warning",
        );
        break;
      case "stt_reconnecting":
        this.ui.showToast(
          `Reconnecting speech recognition (${msg.attempt}/${msg.max})…`,
          3000,
          "info",
        );
        break;
      case "stt_reconnected":
        this.ui.showToast("Speech recognition reconnected.", 2500, "info");
        break;
      case "error":
        this.ui.showError(msg.message || "Unknown server error");
        if (typeof msg.message === "string" && msg.message.includes("Unauthorized")) {
          const secret = prompt("Please enter the Cascade access secret:");
          if (secret) {
            localStorage.setItem("cascade_secret", secret.trim());
          }
          this.stopSession();
          break;
        }
        if (this.state !== STATE.IDLE) {
          const isSTTError =
            typeof msg.message === "string" && msg.message.includes("STT");
          if (isSTTError) {
            this.stopSession();
          } else {
            this.audioOutput.isPlaying = false;
            this.audioOutput.isAudioSourceEnded = true;
            this.audioOutput._checkPlaybackFinished();
          }
        }
        break;
      default:
        console.debug("Unhandled message type:", msg.type);
    }
  }

  setState(newState) {
    this.state = newState;
    this.ui.setState(newState);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  window.cascadeClient = new CascadeClient();
  window.cascadeClient.ui._updateStatsBar();
  window.cascadeClient.ui._showEmptyStateIfNeeded();
});
