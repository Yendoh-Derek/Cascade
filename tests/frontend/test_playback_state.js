const assert = require("assert");

// Mirror frontend module without ESM in Node
const STATE = {
  IDLE: "IDLE",
  CONNECTING: "CONNECTING",
  LISTENING: "LISTENING",
  WINDING_DOWN: "WINDING_DOWN",
  PROCESSING: "PROCESSING",
  SPEAKING: "SPEAKING",
};

function resolvePlaybackCompletion({
  activeSourceCount,
  isAudioSourceEnded,
  currentState,
}) {
  if (activeSourceCount > 0) {
    return { action: "none" };
  }

  if (isAudioSourceEnded) {
    if (
      currentState === STATE.SPEAKING ||
      currentState === STATE.PROCESSING ||
      currentState === STATE.WINDING_DOWN
    ) {
      return { action: "complete", nextState: STATE.LISTENING };
    }
    return { action: "none" };
  }

  if (currentState === STATE.SPEAKING) {
    return { action: "wind_down", nextState: STATE.WINDING_DOWN };
  }

  return { action: "none" };
}

function canScheduleAudioChunk(state, isAudioSourceEnded) {
  if (state === STATE.IDLE || state === STATE.CONNECTING) {
    return false;
  }
  if (state === STATE.LISTENING && isAudioSourceEnded) {
    return false;
  }
  return true;
}

function testResolvePlaybackCompletion() {
  console.log("-- Playback Completion State Machine ---------------------");
  let passed = 0;

  assert.deepStrictEqual(
    resolvePlaybackCompletion({
      activeSourceCount: 2,
      isAudioSourceEnded: false,
      currentState: STATE.SPEAKING,
    }),
    { action: "none" },
  );
  passed++;

  assert.deepStrictEqual(
    resolvePlaybackCompletion({
      activeSourceCount: 0,
      isAudioSourceEnded: false,
      currentState: STATE.SPEAKING,
    }),
    { action: "wind_down", nextState: STATE.WINDING_DOWN },
  );
  passed++;

  assert.deepStrictEqual(
    resolvePlaybackCompletion({
      activeSourceCount: 0,
      isAudioSourceEnded: true,
      currentState: STATE.WINDING_DOWN,
    }),
    { action: "complete", nextState: STATE.LISTENING },
  );
  passed++;

  assert.deepStrictEqual(
    resolvePlaybackCompletion({
      activeSourceCount: 0,
      isAudioSourceEnded: true,
      currentState: STATE.PROCESSING,
    }),
    { action: "complete", nextState: STATE.LISTENING },
  );
  passed++;

  assert.deepStrictEqual(
    resolvePlaybackCompletion({
      activeSourceCount: 0,
      isAudioSourceEnded: true,
      currentState: STATE.LISTENING,
    }),
    { action: "none" },
  );
  passed++;

  console.log(`  v All ${passed} playback completion tests passed!`);
}

function testCanScheduleAudioChunk() {
  console.log("-- Audio Scheduling Guards -------------------------------");
  let passed = 0;

  assert.strictEqual(canScheduleAudioChunk(STATE.IDLE, false), false);
  assert.strictEqual(canScheduleAudioChunk(STATE.CONNECTING, false), false);
  assert.strictEqual(canScheduleAudioChunk(STATE.LISTENING, true), false);
  assert.strictEqual(canScheduleAudioChunk(STATE.LISTENING, false), true);
  assert.strictEqual(canScheduleAudioChunk(STATE.WINDING_DOWN, false), true);
  assert.strictEqual(canScheduleAudioChunk(STATE.SPEAKING, false), true);
  passed += 6;

  console.log(`  v All ${passed} scheduling guard tests passed!`);
}

try {
  testResolvePlaybackCompletion();
  testCanScheduleAudioChunk();
} catch (e) {
  console.error("  x Test failed:", e.message);
  process.exit(1);
}
