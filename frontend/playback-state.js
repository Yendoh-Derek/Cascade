import { STATE } from "./state.js?v=2.0.2";

/**
 * Decide the next UI action once all scheduled audio sources have ended.
 * Extracted for unit testing.
 */
export function resolvePlaybackCompletion({
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

/**
 * Whether an inbound TTS chunk may be decoded and scheduled for playback.
 */
export function canScheduleAudioChunk(state, isAudioSourceEnded) {
  if (state === STATE.IDLE || state === STATE.CONNECTING) {
    return false;
  }
  if (state === STATE.LISTENING && isAudioSourceEnded) {
    return false;
  }
  return true;
}
