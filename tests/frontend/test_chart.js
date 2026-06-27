const assert = require('assert');

// Mock data as would be passed from app.js to chart.js
const mockData = [
  { turn: 1, total: 500, llm: 350, tts: 150, stt: 100, perceived: 650 },
  { turn: 2, total: 400, llm: 300, tts: 90, stt: 110, perceived: null }
];

function testChartMath() {
  console.log("-- Chart Math Verification -------------------------------");
  let passed = 0;
  
  mockData.forEach((d, i) => {
    const sttVal = d.stt || 0;
    const llmVal = d.llm || 0;
    const ttsVal = d.tts || 0;
    
    // System latency is computed at render time now
    const systemVal = Math.max(0, (d.total || 0) - (llmVal + ttsVal));
    
    // Total End-to-End latency displayed
    const e2eTotal = (d.stt || 0) + (d.total || 0);
    const displayTotal = e2eTotal || sttVal + llmVal + ttsVal + systemVal;

    if (i === 0) {
      assert.strictEqual(systemVal, 0, "Turn 1 system value should be 0");
      assert.strictEqual(displayTotal, 600, "Turn 1 display total should be 600");
      passed++;
    } else if (i === 1) {
      assert.strictEqual(systemVal, 10, "Turn 2 system value should be 10 (400 - (300+90))");
      assert.strictEqual(displayTotal, 510, "Turn 2 display total should be 510");
      passed++;
    }
  });

  console.log(`  v All ${passed} tests passed!`);
}

try {
  testChartMath();
} catch (e) {
  console.error("  x Test failed:", e.message);
  process.exit(1);
}
