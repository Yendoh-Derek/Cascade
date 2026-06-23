export class ChartRenderer {
  constructor(client) {
    this.client = client;
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

  render() {
    const canvas = document.getElementById("latency-chart");
    if (!canvas) return;

    const data = this.client.latencyHistory;
    const dpr = window.devicePixelRatio || 1;
    const W = 600;
    const H = 300;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = `${W}px`;
    canvas.style.height = `${H}px`;

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const PAD = { top: 36, right: 20, bottom: 40, left: 56 };
    const chartW = W - PAD.left - PAD.right;
    const chartH = H - PAD.top - PAD.bottom;
    const TARGET_MS = 600;

    const colors = {
      stt: "#818cf8",     // Indigo
      llm: "#c084fc",     // Purple
      tts: "#34d399",     // Emerald
      system: "#fb923c"   // Warm Orange
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
        const sttVal = d.stt || 0;
        const llmVal = d.llm || 0;
        const ttsVal = d.tts || 0;
        const systemVal = d.system != null ? d.system : (d.total > 0 ? Math.max(0, d.total - (sttVal + llmVal + ttsVal)) : 0);
        return Math.max(d.total || 0, sttVal + llmVal + ttsVal + systemVal);
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

    // In-chart legend (top-left)
    let legendX = PAD.left;
    const legendY = 12;
    ctx.font = "10px Inter, sans-serif";
    ctx.textAlign = "left";

    const legendItems = [
      { key: "stt", label: "STT" },
      { key: "llm", label: "LLM" },
      { key: "tts", label: "TTS" },
      { key: "system", label: "System" }
    ];

    legendItems.forEach(({ key, label }) => {
      ctx.fillStyle = colors[key];
      ctx.beginPath();
      ctx.arc(legendX + 4, legendY + 4, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.45)";
      ctx.fillText(label, legendX + 12, legendY + 8);
      legendX += ctx.measureText(label).width + 28;
    });

    ctx.strokeStyle = "rgba(255,255,255,0.3)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(legendX, legendY + 4);
    ctx.lineTo(legendX + 16, legendY + 4);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,255,255,0.45)";
    ctx.fillText("600ms target", legendX + 20, legendY + 8);

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

    // Target line at 600ms
    if (TARGET_MS <= maxMs) {
      const targetY = scaleY(TARGET_MS);
      ctx.beginPath();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(255,255,255,0.3)";
      ctx.lineWidth = 1;
      ctx.moveTo(PAD.left, targetY);
      ctx.lineTo(PAD.left + chartW, targetY);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Stacked bars: Draw STT -> LLM -> TTS -> System
    const BAR_W = Math.min(36, (chartW / data.length) * 0.55);

    data.forEach((d, i) => {
      const x = scaleX(i) - BAR_W / 2;
      let yBase = scaleY(0);
      let yTop = yBase;

      // 1. STT bar (bottom of the stack)
      const sttVal = d.stt || 0;
      if (sttVal > 0) {
        const barH = (sttVal / maxMs) * chartH;
        yBase -= barH;
        yTop = Math.min(yTop, yBase);
        ctx.fillStyle = colors.stt;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x, yBase, BAR_W, barH);
        ctx.globalAlpha = 1;
      }

      // 2. LLM bar
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

      // 3. TTS bar
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

      // 4. System bar (top of the stack)
      const systemVal = d.system != null ? d.system : (d.total > 0 ? Math.max(0, d.total - (sttVal + llmVal + ttsVal)) : 0);
      if (systemVal > 0) {
        const barH = (systemVal / maxMs) * chartH;
        yBase -= barH;
        yTop = Math.min(yTop, yBase);
        ctx.fillStyle = colors.system;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x, yBase, BAR_W, barH);
        ctx.globalAlpha = 1;
      }

      // Total label above bar
      const displayTotal = d.total || (sttVal + llmVal + ttsVal + systemVal);
      ctx.fillStyle = "rgba(255,255,255,0.55)";
      ctx.font = "9px JetBrains Mono, monospace";
      ctx.textAlign = "center";
      ctx.fillText(`${displayTotal}ms`, scaleX(i), yTop - 6);

      // Turn label
      ctx.fillStyle = "rgba(255,255,255,0.25)";
      ctx.font = "9px Inter, sans-serif";
      ctx.fillText(`T${d.turn}`, scaleX(i), H - PAD.bottom + 14);
    });
  }
}
