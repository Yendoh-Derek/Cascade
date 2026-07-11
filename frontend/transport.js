import { STATE } from "./state.js?v=2.1.1";

export class WebSocketTransport {
  constructor(client) {
    this.client = client;
    this.ws = null;
    this.intentionalDisconnect = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 3;

    this.WS_HOST = window.location.hostname || "localhost";
    this.WS_PORT = window.location.port || "8000";
  }

  _teardownWs() {
    if (!this.ws) return;
    const ws = this.ws;
    this.ws = null;
    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
    if (
      ws.readyState === WebSocket.CONNECTING ||
      ws.readyState === WebSocket.OPEN
    ) {
      try {
        ws.close();
      } catch (_) {
        /* ignore */
      }
    }
  }

  connect() {
    return new Promise((resolve, reject) => {
      this.intentionalDisconnect = false;
      let settled = false;

      const settle = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(connectTimeout);
        clearTimeout(authGraceTimeout);
        fn(value);
      };

      const wsProtocol =
        window.location.protocol === "https:" ? "wss:" : "ws:";
      const secret = sessionStorage.getItem("cascade_secret") || "";

      const wsUrl = `${wsProtocol}//${this.WS_HOST}:${this.WS_PORT}/ws?tts_engine=${encodeURIComponent(this.client.selectedTTSEngine)}`;
      console.log(`[Transport] Connecting to ${wsUrl}`);
      this.ws = new WebSocket(wsUrl);
      this.ws.binaryType = "arraybuffer";

      let awaitingAuth = false;
      let authGraceTimeout = null;

      const connectTimeout = setTimeout(() => {
        if (this.ws && this.ws.readyState !== WebSocket.OPEN) {
          this._teardownWs();
          settle(reject, new Error("WebSocket connection timed out"));
        }
      }, 5000);

          const finishConnect = () => {
        if (!settled) {
          // Send identify message for quota system
          let testerId = localStorage.getItem("cascade_tester_id");
          if (!testerId) {
            testerId = crypto.randomUUID();
            localStorage.setItem("cascade_tester_id", testerId);
          }
          this.send(JSON.stringify({ type: "identify", tester_id: testerId }));
          
          settle(resolve);
        }
      };

      const scheduleAuthGrace = () => {
        clearTimeout(authGraceTimeout);
        authGraceTimeout = setTimeout(() => {
          if (!awaitingAuth) finishConnect();
        }, 750);
      };

      this.ws.onopen = () => {
        if (settled) {
          this._teardownWs();
          return;
        }
        clearTimeout(connectTimeout);
        this.reconnectAttempts = 0;
        console.log("[ok] [Transport] WebSocket connected");
        scheduleAuthGrace();
      };

      this.ws.onmessage = async (evt) => {
        if (evt.data instanceof ArrayBuffer) {
          try {
            await this.client.audioOutput.onAudioChunk(evt.data);
          } catch (err) {
            console.error("[Transport] Audio chunk handling failed:", err);
          }
          return;
        }

        try {
          const msg = JSON.parse(evt.data);

          if (msg.type === "challenge") {
            awaitingAuth = true;
            clearTimeout(authGraceTimeout);
            if (!secret) {
              settle(
                reject,
                new Error("Unauthorized: Invalid or missing auth secret"),
              );
              return;
            }

            try {
              const enc = new TextEncoder();
              const key = await crypto.subtle.importKey(
                "raw",
                enc.encode(secret),
                { name: "HMAC", hash: "SHA-256" },
                false,
                ["sign"],
              );
              const signature = await crypto.subtle.sign(
                "HMAC",
                key,
                enc.encode(msg.nonce),
              );
              const response = Array.from(new Uint8Array(signature))
                .map((b) => b.toString(16).padStart(2, "0"))
                .join("");
              this.send(JSON.stringify({ type: "auth", response }));
            } catch (err) {
              settle(reject, new Error("Authentication failed"));
            }
            return;
          }

          if (msg.type === "auth_ok") {
            awaitingAuth = false;
            finishConnect();
            return;
          }

          if (
            msg.type === "error" &&
            typeof msg.message === "string" &&
            msg.message.includes("Unauthorized")
          ) {
            settle(reject, new Error(msg.message));
            return;
          }

          if (msg.type === "ping") {
            this.send(JSON.stringify({ type: "pong" }));
            return;
          }

          if (!settled) finishConnect();
          try {
            await this.client._onServerMessage(msg);
          } catch (err) {
            console.error("[Transport] Server message handler failed:", err);
          }
        } catch (err) {
          console.warn("[Transport] Unparseable server message:", evt.data);
        }
      };

      this.ws.onerror = () => {
        if (settled) return;
        this._teardownWs();
        settle(reject, new Error("WebSocket error"));
      };

      this.ws.onclose = () => {
        clearTimeout(connectTimeout);
        clearTimeout(authGraceTimeout);
        if (settled) return;

        if (
          !this.intentionalDisconnect &&
          this.client.state !== STATE.IDLE &&
          this.reconnectAttempts < this.maxReconnectAttempts
        ) {
          this.reconnectAttempts++;
          this.client.resetTurnAndEpochState();
          if (this.client.state !== STATE.IDLE) {
            this.client.setState(STATE.LISTENING);
          }
          const delay = 1000 * Math.pow(2, this.reconnectAttempts - 1);
          console.log(
            `[Transport] Reconnect attempt ${this.reconnectAttempts} (delay ${delay})`,
          );
          setTimeout(() => {
            this.connect().catch((err) => {
              console.error("[Transport] Reconnect failed:", err);
            });
          }, delay);
        } else if (
          !this.intentionalDisconnect &&
          this.client.state !== STATE.IDLE
        ) {
          this.client.ui.showError(
            "Connection lost. Please start a new session.",
          );
          void this.client.stopSession({ force: true }).catch((err) => {
            console.error("[Transport] stopSession failed:", err);
          });
        }
      };
    });
  }

  send(data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(data);
    }
  }

  close() {
    this.intentionalDisconnect = true;
    this._teardownWs();
  }

  isOpen() {
    return this.ws && this.ws.readyState === WebSocket.OPEN;
  }
}
