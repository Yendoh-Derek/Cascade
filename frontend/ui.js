import { STATE } from "./state.js?v=2.1.1";

function renderMathSpans(escapedText) {
  if (typeof escapedText !== "string" || escapedText.length === 0) {
    return escapedText;
  }

  let result = "";
  let i = 0;

  while (i < escapedText.length) {
    const ch = escapedText[i];

    // Keep escaped dollars as literal text and do not treat them as delimiters.
    if (ch === "\\" && i + 1 < escapedText.length && escapedText[i + 1] === "$") {
      result += "\\$";
      i += 2;
      continue;
    }

    if (ch !== "$") {
      result += ch;
      i += 1;
      continue;
    }

    // Find the next unescaped dollar to close this math span.
    let j = i + 1;
    while (j < escapedText.length) {
      if (escapedText[j] === "\\" && j + 1 < escapedText.length && escapedText[j + 1] === "$") {
        j += 2;
        continue;
      }
      if (escapedText[j] === "$") {
        break;
      }
      j += 1;
    }

    if (j >= escapedText.length) {
      // Unclosed math delimiter; keep the rest as-is.
      result += escapedText.slice(i);
      break;
    }

    const expr = escapedText.slice(i + 1, j);
    try {
      if (typeof katex === "undefined" || !katex?.renderToString) {
        result += `$${expr}$`;
      } else {
        result += katex.renderToString(expr, { throwOnError: false, output: "html" });
      }
    } catch (_) {
      result += `$${expr}$`;
    }
    i = j + 1;
  }

  return result;
}

export class UIController {
  constructor(client) {
    this.client = client;
    this.orb = document.getElementById("orb");
    this.transcriptPanel = document.getElementById("transcript-panel");
    this.statusText = document.getElementById("status-text");
    this.btnToggleSession = document.getElementById("btn-toggle-session");
    this.btnClearTranscript = document.getElementById("btn-clear-transcript");
    this.btnStats = document.getElementById("btn-stats");
    this.transcriptEmpty = document.getElementById("transcript-empty");
    this.quotaTimer = document.getElementById("quota-timer");
    this.quotaTimerText = document.getElementById("quota-timer-text");

    this._initUIListeners();
    this._restoreTTSSelection();

    // Passively update relative timestamps every 30s
    setInterval(() => this._updateTimestamps(), 30000);
  }

  _restoreTTSSelection() {
    /*
     * ADR-005: TTS selector is hidden from the UI. This method is kept intact
     * so it can be re-enabled by un-commenting the TTS selector in index.html.
     * While hidden, querySelectorAll returns an empty NodeList — no-op.
     */
    const activeBtn = document.querySelector(
      `.tts-toggle-btn[data-engine="${this.client.selectedTTSEngine}"]`,
    );
    document.querySelectorAll(".tts-toggle-btn").forEach((btn) => {
      const isActive =
        btn.getAttribute("data-engine") === this.client.selectedTTSEngine;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
    if (activeBtn) activeBtn.classList.add("active");
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
        this.client.toggleSession();
      });
      this.orb.addEventListener("keydown", (evt) => {
        if (evt.key === " " || evt.key === "Enter") {
          evt.preventDefault();
          if (
            this.client.state === STATE.SPEAKING ||
            this.client.state === STATE.PROCESSING
          ) {
            this.client._triggerInterruption();
          } else {
            this.client.toggleSession();
          }
        }
      });
    }

    const animatedBtns = document.querySelectorAll("button, .menu-btn");
    animatedBtns.forEach(
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
          btn.addEventListener("click", () => this.client.toggleSession());
        } else if (btn.id === "btn-clear-transcript") {
          btn.addEventListener("click", async () => {
            await this.client.resetConversation();
          });
        } else if (btn.id === "btn-stats") {
          btn.addEventListener("click", () => this._openStatsPanel());
        }
      },
    );

    /*
     * ADR-005: TTS Engine Selector Toggles — DISABLED (selector hidden from UI).
     * Deepgram Aura 2 is the hardcoded default (set in app.js via localStorage fallback).
     *
     * To re-enable:
     *   1. Un-comment the TTS selector markup in frontend/index.html (.menu-bar-left block).
     *   2. Remove the .menu-bar--centered class from the <footer> in index.html.
     *   3. Un-comment the block below.
     *   4. See docs/adr/ADR-005-tts-selector-removed.md for the full guide.
     *
    // Custom TTS Engine Selector Toggles
    document.querySelectorAll(".tts-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const engine = btn.getAttribute("data-engine");
        this.client.selectedTTSEngine = engine;
        localStorage.setItem("cascade_tts_engine", engine);
        document
          .querySelectorAll(".tts-toggle-btn")
          .forEach((b) => {
            b.classList.remove("active");
            b.setAttribute(
              "aria-pressed",
              b.getAttribute("data-engine") === engine ? "true" : "false",
            );
          });
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
        console.log(`[Client] TTS Engine changed to: ${engine}`);
        if (this.client.state !== STATE.IDLE) {
          this.showToast(
            "Switching voice engine \u2014 restarting session\u2026",
            3000,
            "info",
          );
          this.client
            .stopSession()
            .then(() => this.client.startSession())
            .catch((err) => {
              console.error("[UI] TTS engine switch failed:", err);
            });
        }
      });
    });
    */

    // Close stats panel listeners
    const btnCloseStats = document.getElementById("btn-close-stats");
    if (btnCloseStats) {
      btnCloseStats.addEventListener("click", () => this._closeStatsPanel());
    }
    const statsBackdrop = document.getElementById("stats-backdrop");
    if (statsBackdrop) {
      statsBackdrop.addEventListener("click", () => this._closeStatsPanel());
    }

    // ── Survey / Rating slider ──
    const surveyBackdrop = document.getElementById("survey-modal-backdrop");
    const surveySubmit = document.getElementById("btn-submit-survey");
    const btnSkipSurvey = document.getElementById("btn-skip-survey");
    const surveySlider = document.getElementById("survey-rating");
    const ratingValDisplay = document.getElementById("rating-val-display");
    const ratingTrackFill = document.getElementById("rating-track-fill");
    const btnCloseSurvey = document.getElementById("btn-close-survey");

    const _updateRatingUI = (value) => {
      const pct = ((value - 1) / 4) * 100;
      if (ratingTrackFill) ratingTrackFill.style.width = `${pct}%`;
      if (ratingValDisplay) ratingValDisplay.textContent = `${value} / 5`;
      if (surveySlider) {
        surveySlider.value = value;
        surveySlider.setAttribute("aria-valuenow", value);
      }
      document.querySelectorAll(".rating-pip").forEach((pip) => {
        pip.classList.toggle("active", parseInt(pip.dataset.value, 10) === value);
      });
    };

    // Initial render
    _updateRatingUI(3);

    if (surveySlider) {
      surveySlider.addEventListener("input", (e) => {
        _updateRatingUI(parseInt(e.target.value, 10));
      });
    }

    document.querySelectorAll(".rating-pip").forEach((pip) => {
      pip.addEventListener("click", () => {
        _updateRatingUI(parseInt(pip.dataset.value, 10));
      });
    });

    if (btnCloseSurvey) {
      btnCloseSurvey.addEventListener("click", () => window.location.reload());
    }
    if (btnSkipSurvey) {
      btnSkipSurvey.addEventListener("click", () => window.location.reload());
    }

    const btnCloseExpired = document.getElementById("btn-close-expired");
    if (btnCloseExpired) {
      btnCloseExpired.addEventListener("click", () => window.location.reload());
    }

    const btnCloseCapacity = document.getElementById("btn-close-capacity");
    if (btnCloseCapacity) {
      btnCloseCapacity.addEventListener("click", () => window.location.reload());
    }

    const btnCloseIpLimit = document.getElementById("btn-close-ip-limit");
    if (btnCloseIpLimit) {
      btnCloseIpLimit.addEventListener("click", () => window.location.reload());
    }

    if (surveySubmit) {
      surveySubmit.addEventListener("click", async () => {
        const comment = document.getElementById("survey-comment")?.value || "";
        const testerId = localStorage.getItem("cascade_tester_id");
        const selectedRating = surveySlider ? parseInt(surveySlider.value, 10) : 3;

        if (!testerId) return;

        surveySubmit.disabled = true;
        const origLabel = surveySubmit.querySelector(".btn-label");
        if (origLabel) origLabel.textContent = "Submitting...";
        else surveySubmit.textContent = "Submitting...";

        try {
          const res = await fetch("/quota/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tester_id: testerId, rating: selectedRating, comment }),
          });
          if (!res.ok) throw new Error("Submission failed");

          // Show success state
          const formContent = document.getElementById("survey-form-content");
          const surveyFooter = document.getElementById("survey-footer");
          const surveySuccess = document.getElementById("survey-success");
          const modalHeader = document.getElementById("survey-modal-header");

          if (formContent) formContent.style.display = "none";
          if (surveyFooter) surveyFooter.style.display = "none";
          if (modalHeader) modalHeader.style.display = "none";
          if (surveySuccess) surveySuccess.style.display = "flex";
        } catch (e) {
          console.error("Survey submission error:", e);
          this.showToast("Couldn't send feedback right now — thanks anyway!", 3500, "warning");
          surveySubmit.disabled = false;
          if (origLabel) origLabel.textContent = "Submit Feedback";
        }
      });
    }
  }

  _openStatsPanel() {
    const panel = document.getElementById("stats-panel");
    const backdrop = document.getElementById("stats-backdrop");
    if (panel) {
      panel.classList.add("open");
      panel.setAttribute("aria-hidden", "false");
    }
    if (backdrop) backdrop.classList.add("open");
    this.client.chart.render();
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

  resetOrbSpeakVars() {
    if (!this.orb) return;
    this.orb.style.setProperty("--audio-level", "0");
    this.orb.style.setProperty("--audio-level-norm", "0");
    for (let i = 1; i <= 4; i++) {
      this.orb.style.setProperty(`--speak-bar-${i}`, "0");
    }
  }

  setState(newState, prevState = this.client.state) {
    if (!this.orb) return;
    if (
      prevState === STATE.SPEAKING ||
      prevState === STATE.WINDING_DOWN
    ) {
      if (newState !== STATE.SPEAKING) {
        this.resetOrbSpeakVars();
      }
    }

    this.orb.setAttribute(
      "data-prev-state",
      prevState ? prevState.toLowerCase().replace(/_/g, "-") : "none",
    );

    this.orb.classList.remove(
      "state-idle",
      "state-connecting",
      "state-listening",
      "state-winding-down",
      "state-processing",
      "state-speaking",
    );
    const classMap = {
      [STATE.IDLE]: "state-idle",
      [STATE.CONNECTING]: "state-connecting",
      [STATE.LISTENING]: "state-listening",
      [STATE.WINDING_DOWN]: "state-winding-down",
      [STATE.PROCESSING]: "state-processing",
      [STATE.SPEAKING]: "state-speaking",
    };
    if (classMap[newState]) this.orb.classList.add(classMap[newState]);

    if (this.statusText) {
      const statusLabels = {
        [STATE.IDLE]: "",
        [STATE.CONNECTING]: "connecting",
        [STATE.LISTENING]: "listening",
        [STATE.WINDING_DOWN]: "listening",
        [STATE.PROCESSING]: "thinking",
        [STATE.SPEAKING]: "speaking",
      };
      this.statusText.textContent = statusLabels[newState] ?? "";
      const statusClass =
        newState === STATE.WINDING_DOWN ? "listening" : newState.toLowerCase();
      this.statusText.className = `status-text state-${statusClass}`;
    }

    if (this.btnToggleSession) {
      const label = this.btnToggleSession.querySelector(".btn-label");
      const icon = this.btnToggleSession.querySelector(".btn-icon");
      this.btnToggleSession.disabled = newState === STATE.CONNECTING;
      if (this.orb) {
        this.orb.style.pointerEvents =
          newState === STATE.CONNECTING ? "none" : "";
      }
      const ariaLabels = {
        [STATE.IDLE]: "Tap to start voice session",
        [STATE.CONNECTING]: "Connecting session",
        [STATE.LISTENING]: "Listening — tap to stop session",
        [STATE.WINDING_DOWN]: "Finishing response — listening soon",
        [STATE.PROCESSING]: "Thinking — tap to stop session",
        [STATE.SPEAKING]: "Speaking — tap to stop or press Space to interrupt",
      };
      if (this.orb) {
        this.orb.setAttribute(
          "aria-label",
          ariaLabels[newState] || ariaLabels[STATE.IDLE],
        );
      }
      if (newState === STATE.IDLE) {
        this.btnToggleSession.classList.remove("active");
        const hasHistory = !!(this.client.conversationHistory && this.client.conversationHistory.length > 0);
        if (label) label.textContent = hasHistory ? "Continue" : "Begin";
        // Restore play icon (same for both Begin and Continue)
        if (icon) {
          icon.innerHTML = `<polygon points="5 3 19 12 5 21 5 3"/>`;
          icon.setAttribute("fill", "currentColor");
          icon.setAttribute("stroke", "none");
        }
      } else {
        this.btnToggleSession.classList.add("active");
        if (label) label.textContent = "Stop";
        // Switch to stop square icon
        if (icon) {
          icon.innerHTML = `<rect x="4" y="4" width="16" height="16" rx="2"/>`;
          icon.setAttribute("fill", "currentColor");
          icon.setAttribute("stroke", "none");
        }
      }
    }
  }


  /** @deprecated stats-bar element removed — kept as no-op to avoid call-site errors */
  _updateStatsBar() {}

  _ensureEmptyState() {
    if (!this.transcriptPanel) return;
    let empty = this.transcriptPanel.querySelector("#transcript-empty");
    if (!empty) {
      empty = document.createElement("p");
      empty.className = "transcript-empty";
      empty.id = "transcript-empty";
      empty.textContent = "Your conversation will appear here...";
      this.transcriptPanel.prepend(empty);
    }
    this.transcriptEmpty = empty;
  }

  clearTranscript() {
    if (!this.transcriptPanel) return;
    this.transcriptPanel
      .querySelectorAll(".message")
      .forEach((el) => el.remove());
    this._ensureEmptyState();
    this._showEmptyStateIfNeeded();
  }

  maybeShowFirstRunHint() {
    if (localStorage.getItem("cascade_hints_seen")) return;
    this.showToast(
      "Tip: Use headphones for best results. Tap the orb to speak. Press Space to interrupt while Cascade is talking.",
      8000,
      "neutral",
    );
    localStorage.setItem("cascade_hints_seen", "1");
  }

  _showEmptyStateIfNeeded() {
    if (!this.transcriptPanel) return;
    this._ensureEmptyState();
    if (!this.transcriptEmpty) return;
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
      msg.innerHTML = `<p>${this.renderTutorHTML(text)}</p><span class="message-timestamp">just now</span>`;
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

  renderTutorHTML(text) {
    return renderMathSpans(this._escapeHTML(text));
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

  openSecretModal() {
    const backdrop = document.getElementById("secret-modal-backdrop");
    const input = document.getElementById("secret-input");
    if (backdrop) {
      backdrop.setAttribute("aria-hidden", "false");
      if (input) {
        input.value = "";
        input.focus();
      }
    }
  }

  closeSecretModal() {
    const backdrop = document.getElementById("secret-modal-backdrop");
    if (backdrop) {
      backdrop.setAttribute("aria-hidden", "true");
    }
  }

  updateQuotaWarning(secondsRemaining) {
    if (this.quotaTimer && this.quotaTimerText) {
      this.quotaTimer.setAttribute("aria-hidden", "false");
      this._quotaSecondsRemaining = secondsRemaining;

      if (this._quotaCountdownInterval) {
        clearInterval(this._quotaCountdownInterval);
        this._quotaCountdownInterval = null;
      }
      this._renderQuotaTime(secondsRemaining);
    }
  }

  _renderQuotaTime(secondsRemaining) {
    if (!this.quotaTimerText) return;
    const m = Math.floor(secondsRemaining / 60);
    const s = Math.floor(secondsRemaining % 60);
    this.quotaTimerText.textContent = `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    if (this.quotaTimer) {
      if (secondsRemaining <= 10) {
        this.quotaTimer.classList.add("danger");
      } else {
        this.quotaTimer.classList.remove("danger");
      }
    }
  }

  showGracePeriod() {
    if (this.quotaTimer && this.quotaTimerText) {
      this.quotaTimer.classList.remove("danger");
      this.quotaTimer.classList.add("grace-period");
      this.quotaTimerText.innerHTML = `<span style="font-size: 11px; letter-spacing: 0.05em; text-transform: uppercase;">Wrapping up...</span>`;
    }
  }

  // BUG-2 fix: hide the quota timer and stop any running countdown when the
  // session ends so it doesn't linger across stops/resets/new sessions.
  hideQuotaTimer() {
    if (this._quotaCountdownInterval) {
      clearInterval(this._quotaCountdownInterval);
      this._quotaCountdownInterval = null;
    }
    this._quotaSecondsRemaining = null;
    if (this.quotaTimer) {
      this.quotaTimer.setAttribute("aria-hidden", "true");
      this.quotaTimer.classList.remove("danger", "grace-period");
      // Restore timer text for next session
      if (this.quotaTimerText) this.quotaTimerText.textContent = "00:00";
    }
  }

  showSurvey(reason) {
    const backdrop = document.getElementById("survey-modal-backdrop");
    if (backdrop) {
      backdrop.setAttribute("aria-hidden", "false");
    }
  }

  showSessionExpired() {
    const backdrop = document.getElementById("expired-modal-backdrop");
    if (backdrop) {
      backdrop.setAttribute("aria-hidden", "false");
    }
  }

  showCapacityReached(message) {
    const backdrop = document.getElementById("capacity-modal-backdrop");
    const msgElement = document.getElementById("capacity-modal-message");
    if (msgElement && message) {
      msgElement.textContent = message;
    }
    if (backdrop) {
      backdrop.setAttribute("aria-hidden", "false");
    }
  }

  showIpRateLimited(message) {
    const backdrop = document.getElementById("ip-limit-modal-backdrop");
    const msgElement = document.getElementById("ip-limit-modal-message");
    if (msgElement && message) {
      msgElement.textContent = message;
    }
    if (backdrop) {
      backdrop.setAttribute("aria-hidden", "false");
    }
  }
}
