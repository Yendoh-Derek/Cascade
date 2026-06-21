import { STATE } from "./state.js";

export class WebSocketTransport {
  constructor(client) {
    this.client = client;
    this.ws = null;
    this.intentionalDisconnect = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 3;
    
    // Config host configuration
    this.WS_HOST = window.location.hostname || "localhost";
    this.WS_PORT = window.location.port || "8000";
  }

  connect() {
    return new Promise((resolve, reject) => {
      this.intentionalDisconnect = false;
      const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      
      const urlParams = new URLSearchParams(window.location.search);
      const secret = urlParams.get("secret") || localStorage.getItem("cascade_secret") || "";
      const secretParam = secret ? `&secret=${encodeURIComponent(secret)}` : "";
      
      const wsUrl = `${wsProtocol}//${this.WS_HOST}:${this.WS_PORT}/ws?tts_engine=${encodeURIComponent(this.client.selectedTTSEngine)}${secretParam}`;
      console.log(`[Transport] Connecting to ${wsUrl}`);
      this.ws = new WebSocket(wsUrl);
      this.ws.binaryType = "arraybuffer";

      const timeout = setTimeout(() => {
        if (this.ws && this.ws.readyState !== WebSocket.OPEN) {
          reject(new Error("WebSocket connection timed out"));
        }
      }, 5000);

      this.ws.onopen = () => {
        clearTimeout(timeout);
        this.reconnectAttempts = 0;
        console.log("✓ [Transport] WebSocket connected");
        resolve();
      };

      this.ws.onmessage = (evt) => {
        if (evt.data instanceof ArrayBuffer) {
          this.client.audioOutput.onAudioChunk(evt.data);
        } else {
          try {
            this.client._onServerMessage(JSON.parse(evt.data));
          } catch (_) {
            console.warn("[Transport] Unparseable server message:", evt.data);
          }
        }
      };

      this.ws.onerror = (err) => {
        clearTimeout(timeout);
        reject(new Error("WebSocket error"));
      };

      this.ws.onclose = () => {
        clearTimeout(timeout);
        if (
          !this.intentionalDisconnect &&
          this.client.state !== STATE.IDLE &&
          this.reconnectAttempts < this.maxReconnectAttempts
        ) {
          this.reconnectAttempts++;
          const delay = 1000 * Math.pow(2, this.reconnectAttempts - 1);
          console.log(
            `[Transport] Reconnect attempt ${this.reconnectAttempts} (delay ${delay})`,
          );
          setTimeout(() => {
            this.connect().catch(() => {});
          }, delay);
        } else if (!this.intentionalDisconnect && this.client.state !== STATE.IDLE) {
          this.client.ui.showError("Connection lost. Please start a new session.");
          this.client.stopSession();
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
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.close();
      } catch (_) {}
    }
    this.ws = null;
  }

  isOpen() {
    return this.ws && this.ws.readyState === WebSocket.OPEN;
  }
}
