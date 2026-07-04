import { STATE } from "./state.js?v=2.0.2";

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

    this._initUIListeners();
    this._restoreTTSSelection();

    // Passively update relative timestamps every 30s
    setInterval(() => this._updateTimestamps(), 30000);
  }

  _restoreTTSSelection() {
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
          btn.addEventListener("click", () => this.client.toggleSession());
        } else if (btn.id === "btn-clear-transcript") {
          btn.addEventListener("click", async () => {
            if (this.client.state !== STATE.IDLE) {
              await this.client.stopSession();
            }
            this.clearTranscript();
            this.client._resetTurnState();
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
            "Switching voice engine — restarting session…",
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

  setState(newState) {
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

    if (this.statusText) {
      const statusLabels = {
        [STATE.IDLE]: "",
        [STATE.CONNECTING]: "connecting",
        [STATE.LISTENING]: "listening",
        [STATE.PROCESSING]: "thinking",
        [STATE.SPEAKING]: "speaking",
      };
      this.statusText.textContent = statusLabels[newState] ?? "";
      this.statusText.className = `status-text state-${newState.toLowerCase()}`;
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
        if (label) label.textContent = "Begin";
        // Restore play icon
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
}
