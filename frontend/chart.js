export class ChartRenderer {
  constructor(client) {
    this.client = client;
    window.addEventListener("resize", () => {
      // Re-render chart on resize if it has data
      if (this.client.latencyHistory && this.client.latencyHistory.length > 0) {
        this.render();
      }
    });
  }

  _chartTickStep(maxMs) {
    const targetTicks = 5;
    const raw = maxMs / targetTicks;
    const mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1))));
    const norm = raw / mag;
    let nice;
    if (norm <= 1.5) nice = 1;
    else if (norm <= 3) nice = 2;
    else if (norm <= 7) nice = 5;
    else nice = 10;
    return nice * mag;
  }

  _chartNiceMax(peak) {
    const step = this._chartTickStep(peak);
    return Math.max(Math.ceil(peak / step) * step, step * 2);
  }

  /** Keep bar labels below the chart ceiling; draw inside the bar when needed. */
  _barLabelY(yTop, chartBottom) {
    const LINE_H = 11;
    const TOP_MARGIN = 8;
    let labelY = yTop - 6;

    if (labelY - LINE_H >= TOP_MARGIN) {
      return { labelY, inside: false };
    }

    labelY = Math.min(yTop + 14, chartBottom - 4);
    return { labelY, inside: true };
  }

  _drawBarLabel(ctx, text, x, y, inside) {
    if (inside) {
      ctx.save();
      ctx.shadowColor = "rgba(0,0,0,0.55)";
      ctx.shadowBlur = 3;
      ctx.fillStyle = "rgba(255,255,255,0.92)";
      ctx.fillText(text, x, y);
      ctx.restore();
      return;
    }
    ctx.fillText(text, x, y);
  }

  render() {
    const canvas = document.getElementById("latency-chart");
    if (!canvas) return;

    const data = this.client.latencyHistory;
    const dpr = window.devicePixelRatio || 1;

    // Dynamically calculate width from parent container padding/margin
    const container = canvas.parentElement;
    const containerWidth = container ? container.clientWidth : 600;
    const W = Math.max(280, Math.min(600, containerWidth - 32)); // Leave margins, clamp range
    const H = 300;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = `${W}px`;
    canvas.style.height = `${H}px`;

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const PAD = { top: 20, right: 20, bottom: 40, left: 56 };
    const chartW = W - PAD.left - PAD.right;
    const chartH = H - PAD.top - PAD.bottom;
    const chartBottom = PAD.top + chartH;

    const colors = {
      endpointing: "#60a5fa", // Blue
      stt_tail: "#818cf8", // Indigo
      llm: "#c084fc", // Purple
      tts: "#34d399", // Emerald
      system: "#fb923c", // Warm Orange
    };

    ctx.clearRect(0, 0, W, H);

    if (data.length === 0) {
      ctx.fillStyle = "rgba(255,255,255,0.2)";
      ctx.font = "13px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No data yet — start a conversation", W / 2, H / 2);
      return;
    }

    const peakTotal = Math.max(
      ...data.map((d) => {
        // endpointing + stt_tail are outside total_ms (measured before utterance_end_time).
        // Bar height = endpointing + stt_tail + total since these are additive phases.
        const endpointingVal = d.endpointing || 0;
        const sttTailVal = d.stt_tail || 0;
        const llmVal = d.llm || 0;
        const ttsVal = d.tts || 0;
        const systemVal = Math.max(0, (d.total || 0) - (llmVal + ttsVal));
        return Math.max(
          endpointingVal + sttTailVal + (d.total || 0), // true e2e
          endpointingVal + sttTailVal + llmVal + ttsVal + systemVal,
        );
      }),
      100,
    );
    const tickStep = this._chartTickStep(peakTotal);
    const maxMs = this._chartNiceMax(peakTotal);
    const scaleY = (v) => PAD.top + chartH - (v / maxMs) * chartH;
    const scaleX = (i) => PAD.left + ((i + 0.5) / data.length) * chartW;

    // Y-axis label
    ctx.save();
    ctx.translate(14, PAD.top + chartH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = "rgba(255,255,255,0.25)";
    ctx.font = "10px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Latency (ms)", 0, 0);
    ctx.restore();

    // Y gridlines
    for (let v = 0; v <= maxMs; v += tickStep) {
      const y = scaleY(v);
      ctx.beginPath();
      ctx.strokeStyle = "rgba(255,255,255,0.06)";
      ctx.lineWidth = 1;
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(PAD.left + chartW, y);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,0.25)";
      ctx.font = "10px JetBrains Mono, monospace";
      ctx.textAlign = "right";
      const label =
        v >= 1000 ? `${(v / 1000).toFixed(v % 1000 === 0 ? 0 : 1)}k` : `${v}`;
      ctx.fillText(label, PAD.left - 8, y + 4);
    }

    // Stacked bars: Draw Endpointing -> STT Tail -> LLM -> TTS -> System
    const BAR_W = Math.min(36, (chartW / data.length) * 0.55);

    data.forEach((d, i) => {
      const x = scaleX(i) - BAR_W / 2;
      let yBase = scaleY(0);
      let yTop = yBase;

      // 1. Endpointing bar (bottom of the stack)
      const endpointingVal = d.endpointing || 0;
      if (endpointingVal > 0) {
        const barH = (endpointingVal / maxMs) * chartH;
        yBase -= barH;
        yTop = Math.min(yTop, yBase);
        ctx.fillStyle = colors.endpointing;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x, yBase, BAR_W, barH);
        ctx.globalAlpha = 1;
      }

      // 2. STT Tail bar
      const sttTailVal = d.stt_tail || 0;
      if (sttTailVal > 0) {
        const barH = (sttTailVal / maxMs) * chartH;
        yBase -= barH;
        yTop = Math.min(yTop, yBase);
        ctx.fillStyle = colors.stt_tail;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x, yBase, BAR_W, barH);
        ctx.globalAlpha = 1;
      }

      // 3. LLM bar
      const llmVal = d.llm || 0;
      if (llmVal > 0) {
        const barH = (llmVal / maxMs) * chartH;
        yBase -= barH;
        yTop = Math.min(yTop, yBase);
        ctx.fillStyle = colors.llm;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x, yBase, BAR_W, barH);
        ctx.globalAlpha = 1;
      }

      // 4. TTS bar
      const ttsVal = d.tts || 0;
      if (ttsVal > 0) {
        const barH = (ttsVal / maxMs) * chartH;
        yBase -= barH;
        yTop = Math.min(yTop, yBase);
        ctx.fillStyle = colors.tts;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x, yBase, BAR_W, barH);
        ctx.globalAlpha = 1;
      }

      // 5. System bar (top of the stack — pipeline overhead inside total_ms)
      const systemVal = Math.max(0, (d.total || 0) - (llmVal + ttsVal));
      if (systemVal > 0) {
        const barH = (systemVal / maxMs) * chartH;
        yBase -= barH;
        yTop = Math.min(yTop, yBase);
        ctx.fillStyle = colors.system;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x, yBase, BAR_W, barH);
        ctx.globalAlpha = 1;
      }

      // Label shows full end-to-end latency.
      const e2eTotal = endpointingVal + sttTailVal + (d.total || 0);
      const displayTotal =
        e2eTotal || endpointingVal + sttTailVal + llmVal + ttsVal + systemVal;
      ctx.font = "9px JetBrains Mono, monospace";
      ctx.textAlign = "center";

      const { labelY, inside } = this._barLabelY(yTop, chartBottom);
      ctx.fillStyle = inside ? "rgba(255,255,255,0.92)" : "rgba(255,255,255,0.55)";
      this._drawBarLabel(ctx, `${displayTotal}ms`, scaleX(i), labelY, inside);

      // Turn label
      ctx.fillStyle = "rgba(255,255,255,0.25)";
      ctx.font = "9px Inter, sans-serif";
      ctx.fillText(`T${d.turn}`, scaleX(i), H - PAD.bottom + 14);
    });
  }
}
