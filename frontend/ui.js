import { STATE } from "./app.js";

export class UIController {
  constructor(client) {
    this.client = client;
    this.orb = document.getElementById("orb");
    this.transcriptPanel = document.getElementById("transcript-panel");
    this.statusText = document.getElementById("status-text");
    this.btnToggleSession = document.getElementById("btn-toggle-session");
    this.btnClearTranscript = document.getElementById("btn-clear-transcript");
    this.btnStats = document.getElementById("btn-stats");
    this.statsBar = document.getElementById("stats-bar");
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
        this.client.toggleSession();
      });
      this.orb.addEventListener("keydown", (evt) => {
        if (evt.key === " " || evt.key === "Enter") {
          evt.preventDefault();
          this.client.toggleSession();
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
          btn.addEventListener("click", () => {
            if (this.transcriptPanel) this.transcriptPanel.innerHTML = "";
            this.client._resetTurnState();
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
        this.client.selectedTTSEngine = engine;
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
    this.statsBar.textContent = this.client.lastLatencyMs
      ? `${this.client.lastLatencyMs}ms`
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
}
