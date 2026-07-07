/**
 * Cascade — AI Voice Tutor Frontend
 */

import { UIController } from "./ui.js?v=2.1.0";
import { AudioInputController } from "./audio-input.js?v=2.1.0";
import { AudioOutputController } from "./audio-output.js?v=2.1.0";
import { WebSocketTransport } from "./transport.js?v=2.1.0";
import { ChartRenderer } from "./chart.js?v=2.1.0";

import { STATE } from "./state.js?v=2.1.0";

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

    // Perceived-latency tracking
    this._turnStartMs = null; // stamped at transcript receipt (fallback anchor)
    this._speechEndMs = null; // stamped by VAD at speech end (primary anchor)
    this._firstAudioPlayed = false;

    // Conversation history — persists across pause/resume cycles.
    // Each entry: {role: "user"|"assistant", content: string}.
    // Sent to the server on resume via load_history message.
    this.conversationHistory = [];
    this._pendingUserMessage = null; // user utterance awaiting a completed AI response

    this.currentResponse = "";
    this.currentStreamingBubble = null;
    this.pendingSubtitleUpdate = false;
    this.subtitleThrottleMs = 100;
    this._subtitleTimer = null;
    this._messageCompleteTimer = null;
    this._sessionBusy = false;
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

  get isAudioSourceEnded() {
    return this.audioOutput.isAudioSourceEnded;
  }

  set isAudioSourceEnded(val) {
    this.audioOutput.isAudioSourceEnded = val;
  }

  async toggleSession() {
    if (this._sessionBusy) return;
    if (this.state === STATE.IDLE) {
      await this.startSession();
    } else {
      await this.stopSession();
    }
  }

  _cancelUiTimers() {
    if (this._subtitleTimer) {
      clearTimeout(this._subtitleTimer);
      this._subtitleTimer = null;
    }
    if (this._messageCompleteTimer) {
      clearTimeout(this._messageCompleteTimer);
      this._messageCompleteTimer = null;
    }
    this.pendingSubtitleUpdate = false;
  }

  async startSession() {
    if (this._sessionBusy || this.state !== STATE.IDLE) return;
    this._sessionBusy = true;
    try {
      if (!this.audioOutput.ensurePlaybackReady()) {
        throw new Error(
          "Audio playback is not available. Try another browser or check your sound settings.",
        );
      }
      await this.audioOutput.resumeContext();

      this.setState(STATE.CONNECTING);
      await this.transport.connect();

      // Resume: send saved history to the server before any audio arrives.
      // This must happen right after connection so the server can pre-populate
      // TutorSession.history before the first speech_final event is processed.
      if (this.conversationHistory.length > 0) {
        this.transport.send(
          JSON.stringify({ type: "load_history", history: this.conversationHistory })
        );
        console.log(
          `[Client] Resuming session — sent ${this.conversationHistory.length} history messages to server`
        );
      }

      // Start microphone capture only after the websocket is connected and any
      // resume history has been sent, so the backend can restore context before
      // processing new user audio.
      await this.audioInput.start();

      this.setState(STATE.LISTENING);
      this.currentStreamingBubble = null;
      this.currentStudentBubble = null;
      this.sessionStartTime = Date.now();

      // Only clear the transcript on a fresh session, not on resume.
      if (this.conversationHistory.length === 0) {
        this.ui.clearTranscript();
      }
      this.ui.maybeShowFirstRunHint();
    } catch (err) {
      console.error("startSession failed:", err);
      let msg = `Failed to start: ${err.message}`;
      if (err.name === "NotAllowedError") {
        msg = "🔒 Microphone permission denied. Enable it in browser settings.";
      } else if (err.name === "NotFoundError") {
        msg = "🎤 No microphone found on this device.";
      }
      this.ui.showError(msg);
      if (
        typeof err.message === "string" &&
        err.message.includes("Unauthorized")
      ) {
        this.ui.openSecretModal();
        this._initSecretModal();
      }
      await this._teardownSession();
    } finally {
      this._sessionBusy = false;
    }
  }

  async stopSession({ force = false } = {}) {
    if (!force) {
      if (this._sessionBusy) return;
      if (this.state === STATE.IDLE && !this.transport.isOpen()) return;
    }
    if (this._sessionBusy && !force) return;
    this._sessionBusy = true;
    try {
      await this._teardownSession();
    } finally {
      this._sessionBusy = false;
    }
  }

  async _teardownSession() {
    this._cancelUiTimers();
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

    // Discard any in-flight (incomplete) turn — it won't have an assistant
    // response so it shouldn't be saved to history.
    this._pendingUserMessage = null;

    this.currentResponse = "";
    if (this.currentStreamingBubble) {
      // Remove the streaming bubble — an interrupted partial response
      // should not be left dangling in the transcript on pause.
      this.currentStreamingBubble.remove();
      this.currentStreamingBubble = null;
    }
    this.resetTurnAndEpochState();
    this.sessionStartTime = null;

    // Keep transcript and conversationHistory intact — the user can resume.
    this.setState(STATE.IDLE);
  }

  resetTurnAndEpochState() {
    this._cancelUiTimers();
    this.activeTurnId = null;
    this.playbackTurnId = null;
    this.audioEpoch += 1;
    this.decodeGeneration += 1;
    this._interrupting = false;
    this._pendingCancelTurnId = null;
    this._pendingUserMessage = null;

    if (this._interruptTimeout) {
      clearTimeout(this._interruptTimeout);
      this._interruptTimeout = null;
    }
    this._resetTurnState();
    // Clear felt-latency anchors so a new session always starts clean.
    this._speechEndMs = null;
    this._turnStartMs = null;
  }

  /**
   * Hard reset — clears the conversation transcript, wipes all saved history,
   * and returns to a completely fresh state. Bound to the Reset button.
   */
  async resetConversation() {
    if (this.state !== STATE.IDLE) {
      await this.stopSession();
    }
    this.conversationHistory = [];
    this._pendingUserMessage = null;
    this.ui.clearTranscript();
    this._resetTurnState();
    // Refresh button label — no history means "Begin" rather than "Continue".
    this.ui.setState(STATE.IDLE, STATE.IDLE);
    console.log("[Client] Conversation reset — history cleared");
  }

  _resetTurnState() {
    this.audioInput.resetTurnState();
    this.audioOutput.resetState();
    this._firstAudioPlayed = false;
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

  _renderStreamingBubble() {
    if (!this.currentStreamingBubble || !this.currentResponse) return;
    const p = this.currentStreamingBubble.querySelector("p");
    if (p) p.innerHTML = this.ui.renderTutorHTML(this.currentResponse);
    if (this.ui.transcriptPanel)
      this.ui.transcriptPanel.scrollTop =
        this.ui.transcriptPanel.scrollHeight;
  }

  _scheduleSubtitleUpdate() {
    if (this._subtitleTimer) clearTimeout(this._subtitleTimer);
    this.pendingSubtitleUpdate = true;
    this._subtitleTimer = setTimeout(() => {
      this._subtitleTimer = null;
      this.pendingSubtitleUpdate = false;
      this._renderStreamingBubble();
    }, this.subtitleThrottleMs);
  }

  _finalizeTutorBubble() {
    if (!this.currentStreamingBubble) return;
    if (this._subtitleTimer) {
      clearTimeout(this._subtitleTimer);
      this._subtitleTimer = null;
    }
    this.pendingSubtitleUpdate = false;
    if (this.currentResponse) this._renderStreamingBubble();
    this.currentStreamingBubble.classList.remove("streaming");
    this.currentStreamingBubble = null;
    this.currentResponse = "";
  }

  _updateStudentTranscript(text, { final = false } = {}) {
    if (!this.currentStudentBubble) {
      this.currentStudentBubble = this.ui.addTranscriptItem("student", text);
      if (!final && this.currentStudentBubble)
        this.currentStudentBubble.classList.add("streaming");
    } else {
      const p = this.currentStudentBubble.querySelector("p");
      if (p) p.textContent = text;
      if (final) {
        this.currentStudentBubble.classList.remove("streaming");
      } else {
        this.currentStudentBubble.classList.add("streaming");
      }
    }
  }

  _triggerInterruption() {
    if (this.state !== STATE.SPEAKING && this.state !== STATE.PROCESSING)
      return;
    if (this._interrupting) return;
    this._interrupting = true;
    this._cancelUiTimers();

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

    this._finalizeTutorBubble();
    this.setState(STATE.LISTENING);
    this._resetPlaybackOnly();

    if (this._interruptTimeout) clearTimeout(this._interruptTimeout);
    this._interruptTimeout = setTimeout(() => {
      this._interrupting = false;
      this._interruptTimeout = null;
    }, 1000);
  }

  async _onServerMessage(msg) {
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case "tts_config":
        this.audioOutput.ttsConfig = {
          format: msg.format,
          sampleRate: msg.sampleRate || msg.sample_rate,
        };
        console.log(
          "[Client] Received TTS config:",
          this.audioOutput.ttsConfig,
        );
        break;
      case "transcript_update":
        if (msg.text) {
          if (
            this.audioOutput.activeSourceNodes.length > 0 ||
            this.audioOutput.isPlaying
          ) {
            this.audioOutput.stopAllPlayback();
            // Force state out of SPEAKING if we just interrupted it via STT update
            if (
              this.state === STATE.SPEAKING ||
              this.state === STATE.PROCESSING ||
              this.state === STATE.WINDING_DOWN
            ) {
              this.setState(STATE.LISTENING);
            }
          }
          if (!this.currentStudentBubble) {
            this._resetTurnState();
            this._finalizeTutorBubble();
            this._updateStudentTranscript(msg.text);
            this.currentResponse = "";
          } else {
            this._updateStudentTranscript(msg.text);
          }
        }
        break;
      case "transcript":
        if (msg.text) {
          if (
            this.audioOutput.activeSourceNodes.length > 0 ||
            this.audioOutput.isPlaying
          ) {
            this.audioOutput.stopAllPlayback();
            // Force state out of SPEAKING/PROCESSING if interrupted via STT final
            if (
              this.state === STATE.SPEAKING ||
              this.state === STATE.PROCESSING ||
              this.state === STATE.WINDING_DOWN
            ) {
              this.setState(STATE.LISTENING);
            }
          }
          let is_update = false;
          if (msg.turn_id != null) {
            if (this.activeTurnId === msg.turn_id) {
               is_update = true;
            }
            this.activeTurnId = msg.turn_id;
            this.playbackTurnId = msg.turn_id;
          }

          // Save the user's utterance — it will be committed to conversationHistory
          // once the AI response completes successfully (at response_end).
          this._pendingUserMessage = msg.text;

          if (this._speechEndMs == null) {
            this._speechEndMs = performance.now() - 300;
          }
          this._turnStartMs = performance.now();
          this._firstAudioPlayed = false;
          this._interrupting = false;
          
          if (!is_update && !this.currentStudentBubble) {
            this._resetTurnState();
            this._finalizeTutorBubble();
            this._updateStudentTranscript(msg.text, { final: true });
            this.currentResponse = "";
            this.totalTurns++;
            this.ui._updateStatsBar();
          } else {
            this._updateStudentTranscript(msg.text, { final: true });
            this.currentResponse = "";
            this._finalizeTutorBubble();
            if (!is_update) {
              this.totalTurns++;
              this.ui._updateStatsBar();
            }
          }
          this.setState(STATE.PROCESSING);
        }
        break;
      case "response_chunk":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        if (msg.text) {
          this.currentResponse = this.currentResponse
            ? this.currentResponse + msg.text
            : msg.text;

          if (!this.currentStreamingBubble) {
            this.currentStreamingBubble = this.ui.addTranscriptItem(
              "tutor",
              this.currentResponse,
            );
            this.currentStudentBubble = null;
            if (this.currentStreamingBubble)
              this.currentStreamingBubble.classList.add("streaming");
          } else {
            this._scheduleSubtitleUpdate();
          }
        }
        break;
      case "response_end":
        if (msg.turn_id != null && !this._isTurnActive(msg.turn_id)) break;
        if (this._subtitleTimer) {
          clearTimeout(this._subtitleTimer);
          this._subtitleTimer = null;
        }
        this.pendingSubtitleUpdate = false;
        this._renderStreamingBubble();
        if (this.currentStreamingBubble) {
          const completedBubble = this.currentStreamingBubble;
          completedBubble.classList.remove("streaming");
          completedBubble.classList.add("message-complete");
          this._messageCompleteTimer = setTimeout(() => {
            this._messageCompleteTimer = null;
            completedBubble.classList.remove("message-complete");
          }, 1200);
        } else if (this.currentResponse && this.currentResponse.trim()) {
          const bubble = this.ui.addTranscriptItem(
            "tutor",
            this.currentResponse.trim(),
          );
          if (bubble) {
            bubble.classList.add("message-complete");
            this._messageCompleteTimer = setTimeout(() => {
              this._messageCompleteTimer = null;
              bubble.classList.remove("message-complete");
            }, 1200);
          }
        }

        // Commit the completed turn to conversation history so it can be
        // replayed to the server on a future resume.
        if (this._pendingUserMessage && this.currentResponse && this.currentResponse.trim()) {
          this.conversationHistory.push({ role: "user", content: this._pendingUserMessage });
          this.conversationHistory.push({ role: "assistant", content: this.currentResponse.trim() });
          console.log(`[Client] Turn saved to history (${this.conversationHistory.length / 2} turns total)`);
        }
        this._pendingUserMessage = null;

        this.currentResponse = "";
        this.currentStreamingBubble = null;
        // Mark the audio source as ended so _checkPlaybackFinished() can
        // transition to LISTENING once all scheduled audio chunks finish playing.
        // Do NOT call _checkPlaybackFinished() here unconditionally — active source nodes may
        // still have buffered audio scheduled ahead; let their onended callbacks
        // fire naturally to avoid a premature LISTENING state.
        this.audioOutput.isAudioSourceEnded = true;

        // If no audio was ever scheduled (e.g. TTS error or empty text),
        // we must manually trigger the transition back to LISTENING, otherwise
        // the UI will be permanently stuck in PROCESSING state.
        if (this.audioOutput.activeSourceNodes.length === 0) {
          this.audioOutput._checkPlaybackFinished();
        }
        break;
      case "turn_cancelled":
        // Discard the pending user message — the turn never produced an AI response.
        this._pendingUserMessage = null;
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
          this.setState(STATE.LISTENING);
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
          this.lastLatencyMs =
            (msg.stt_tail_ms || 0) + (msg.endpointing_ms || 0) + msg.total_ms;

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
          entry.stt_tail = msg.stt_tail_ms || 0;
          entry.endpointing = msg.endpointing_ms || 0;
          entry.llm = msg.llm_ms || 0;
          entry.tts = msg.tts_ms || 0;

          entry.llm_queue = entry.llm_queue || 0;
          entry.llm_ttft = entry.llm_ttft || 0;
          entry.llm_streaming = entry.llm_streaming || 0;
          entry.tts_first_chunk = entry.tts_first_chunk || 0;

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
            stt_tail: 0,
            endpointing: 0,
            llm: msg.total_ms || 0,
            tts: 0,
            system: 0,
            llm_queue: msg.queue_ms || 0,
            llm_ttft: msg.ttft_ms || 0,
            llm_streaming: msg.streaming_delay_ms || 0,
            llm_retry: msg.retry_ms || 0,
            tts_first_chunk: 0,
            timestamp: Date.now(),
          };
          this.latencyHistory.push(llmEntry);
          if (this.latencyHistory.length > 10) this.latencyHistory.shift();
        } else {
          llmEntry.llm = msg.total_ms || 0;
          llmEntry.llm_queue = msg.queue_ms || 0;
          llmEntry.llm_ttft = msg.ttft_ms || 0;
          llmEntry.llm_streaming = msg.streaming_delay_ms || 0;
          llmEntry.llm_retry = msg.retry_ms || 0;
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
            tts: msg.first_chunk_latency_ms || 0,
            system: 0,
            llm_queue: 0,
            llm_ttft: 0,
            llm_streaming: 0,
            tts_first_chunk: msg.first_chunk_latency_ms || 0,
            tts_engine: msg.engine || "unknown",
            timestamp: Date.now(),
          };
          this.latencyHistory.push(ttsEntry);
          if (this.latencyHistory.length > 10) this.latencyHistory.shift();
        } else {
          ttsEntry.tts = msg.first_chunk_latency_ms || 0;
          ttsEntry.tts_first_chunk = msg.first_chunk_latency_ms || 0;
          ttsEntry.tts_engine = msg.engine || "unknown";
        }
        const panel2 = document.getElementById("stats-panel");
        if (panel2 && panel2.classList.contains("open")) {
          this.chart.render();
        }
        break;
      case "perceived_latency":
        // Reported by the server after receiving a client_latency message.
        // Displays true end-to-end latency (transcript received → first audio played on speaker).
        if (typeof msg.perceived_ms === "number") {
          const turnNum = msg.turn_id != null ? msg.turn_id : this.totalTurns;
          let entry = this.latencyHistory.find((d) => d.turn === turnNum);
          if (entry) {
            entry.perceived = msg.perceived_ms;
          }
          const panel = document.getElementById("stats-panel");
          if (panel && panel.classList.contains("open")) {
            this.chart.render();
          }
        }
        break;
      case "rate_limited":
        this.ui.showToast(
          msg.message || "Speaking too fast — audio was throttled.",
          3000,
          "warning",
        );
        break;
      case "busy":
        this.ui.showToast(
          msg.message || "⏳ Still responding — please wait a moment.",
          4000,
          "info",
        );
        if (msg.reason === "capacity") {
          await this.stopSession();
        }
        break;
      case "tts_error":
        this.ui.showToast(
          `⚠️ Audio synthesis failed: ${msg.message || "Unknown error"}`,
          4000,
          "warning",
        );
        this.audioOutput.isAudioSourceEnded = true;
        this.audioOutput._checkPlaybackFinished();
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
        if (
          typeof msg.message === "string" &&
          msg.message.includes("Unauthorized")
        ) {
          this.ui.openSecretModal();
          this._initSecretModal();
          break;
        }
        if (this.state !== STATE.IDLE) {
          const isSTTError =
            typeof msg.message === "string" && msg.message.includes("STT");
          if (isSTTError) {
            await this.stopSession();
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
    const prevState = this.state;
    this.state = newState;
    this.ui.setState(newState, prevState);
  }

  _initSecretModal() {
    if (this._secretModalInitialized) return;
    this._secretModalInitialized = true;
    const submitBtn = document.getElementById("btn-submit-secret");
    const cancelBtn = document.getElementById("btn-cancel-secret");
    const input = document.getElementById("secret-input");

    if (submitBtn) {
      submitBtn.addEventListener("click", async () => {
        if (input && input.value.trim()) {
          sessionStorage.setItem("cascade_secret", input.value.trim());
        }
        this.ui.closeSecretModal();
        await this.stopSession();
        await this.startSession();
      });
    }

    if (cancelBtn) {
      cancelBtn.addEventListener("click", async () => {
        this.ui.closeSecretModal();
        await this.stopSession();
      });
    }

    if (input) {
      input.addEventListener("keydown", async (e) => {
        if (e.key === "Enter") {
          if (input.value.trim()) {
            sessionStorage.setItem("cascade_secret", input.value.trim());
          }
          this.ui.closeSecretModal();
          await this.stopSession();
          await this.startSession();
        } else if (e.key === "Escape") {
          this.ui.closeSecretModal();
          await this.stopSession();
        }
      });
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  window.cascadeClient = new CascadeClient();
  window.cascadeClient.ui._updateStatsBar();
  window.cascadeClient.ui._showEmptyStateIfNeeded();

  // Add spacebar shortcut to toggle microphone
  document.addEventListener("keydown", (e) => {
    // Ignore if user is typing in an input field (like the secret modal)
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
      return;
    }

    // Spacebar toggles the session
    if (e.code === "Space") {
      e.preventDefault(); // Prevent page scrolling

      // If we are currently responding, interrupt instead of stopping
      if (
        window.cascadeClient.state === STATE.SPEAKING ||
        window.cascadeClient.state === STATE.PROCESSING
      ) {
        window.cascadeClient._triggerInterruption();
      } else {
        window.cascadeClient.toggleSession();
      }
    }
  });
});
