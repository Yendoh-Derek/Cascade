import copy
import threading
import numpy as np
import torch

_shared_model = None
_init_lock = threading.Lock()

def get_shared_vad_model():
    global _shared_model
    if _shared_model is None:
        with _init_lock:
            if _shared_model is None:
                _shared_model, _ = torch.hub.load(
                    "snakers4/silero-vad", "silero_vad",
                    trust_repo=True, skip_validation=True,
                )
                _shared_model.eval()
    return _shared_model

class SileroVAD:
    """
    Thin wrapper around Silero VAD for local silence detection.
    Used to fire SpeechStopped earlier than Deepgram's cloud endpointing.

    Model is ~1.8MB, runs on CPU, processes 30ms audio frames in <5ms.
    Download happens once via torch.hub on first instantiation.
    """

    CHUNK_MS = 32          # Silero requires at least 512 samples @ 16kHz (~32ms)
    SAMPLE_RATE = 16000
    SAMPLES_PER_CHUNK = 512

    def __init__(self, threshold: float = 0.5, silence_ms: int = 200, min_speech_frames: int = 3):
        """
        threshold:   VAD confidence above which audio is considered speech (0–1).
                     0.5 is Silero's recommended default.
        silence_ms:  How many consecutive ms of sub-threshold audio triggers
                     SpeechStopped. Keep at 150–250ms; lower = more false triggers.
                     This replaces Deepgram's endpointing window as the decision point.
        min_speech_frames: consecutive speech-positive frames required before
                           speech_started fires.
        """
        self.threshold = threshold
        self.silence_ms = silence_ms
        self._silence_frames_needed = max(1, round(silence_ms / self.CHUNK_MS))
        self.min_speech_frames = min_speech_frames

        # Each instance needs a separate model copy because Silero VAD contains
        # mutable recurrent state (states are modified on forward() pass).
        self.model = copy.deepcopy(get_shared_vad_model())

        self._speech_active = False
        self._silence_frame_count = 0
        self._speech_frame_count = 0
        # Pre-allocated rolling buffer avoids repeated np.concatenate allocations.
        _MAX_BUFFER_SAMPLES = self.SAMPLES_PER_CHUNK * 8  # 256ms headroom
        self._ring = np.zeros(_MAX_BUFFER_SAMPLES, dtype=np.int16)
        self._ring_write = 0  # number of valid samples in ring
        self._lock = threading.Lock()

    def feed(self, pcm16_bytes: bytes, require_extra_frames: bool = False) -> list[str]:
        """
        Feed raw PCM16 audio bytes. Returns a list of events that fired:
          "speech_started"  — first frame above threshold after silence
          "speech_stopped"  — silence_ms of sub-threshold audio after speech

        Thread-safe: Silero's recurrent state must not be updated concurrently.
        """
        with self._lock:
            return self._feed_unlocked(pcm16_bytes, require_extra_frames)

    def _feed_unlocked(self, pcm16_bytes: bytes, require_extra_frames: bool) -> list[str]:
        events: list[str] = []
        samples = np.frombuffer(pcm16_bytes, dtype=np.int16)

        # Append into pre-allocated ring buffer.  If the incoming batch is
        # larger than the remaining headroom, fall back to a fresh allocation
        # so we never silently lose samples (very rare: >8 frames at once).
        new_count = len(samples)
        available = len(self._ring) - self._ring_write
        if new_count > available:
            # Compact: move valid data to a fresh array and grow if needed.
            needed = self._ring_write + new_count
            fresh = np.empty(max(needed, len(self._ring) * 2), dtype=np.int16)
            fresh[:self._ring_write] = self._ring[:self._ring_write]
            self._ring = fresh
        self._ring[self._ring_write : self._ring_write + new_count] = samples
        self._ring_write += new_count

        while self._ring_write >= self.SAMPLES_PER_CHUNK:
            chunk = self._ring[:self.SAMPLES_PER_CHUNK].copy()
            # Shift remaining samples to the front.
            remaining = self._ring_write - self.SAMPLES_PER_CHUNK
            self._ring[:remaining] = self._ring[self.SAMPLES_PER_CHUNK : self._ring_write]
            self._ring_write = remaining

            audio_f32 = chunk.astype(np.float32) / 32768.0
            with torch.no_grad():
                confidence = self.model(
                    torch.from_numpy(audio_f32), self.SAMPLE_RATE
                ).item()

            is_speech = confidence >= self.threshold

            if is_speech:
                self._silence_frame_count = 0
                self._speech_frame_count += 1
                target_frames = self.min_speech_frames + (2 if require_extra_frames else 0)
                if not self._speech_active and self._speech_frame_count >= target_frames:
                    self._speech_active = True
                    events.append("speech_started")
            else:
                self._speech_frame_count = 0
                if self._speech_active:
                    self._silence_frame_count += 1
                    if self._silence_frame_count >= self._silence_frames_needed:
                        self._speech_active = False
                        self._silence_frame_count = 0
                        events.append("speech_stopped")

        return events

    def reset(self):
        with self._lock:
            self.model.reset_states()
            self._speech_active = False
            self._silence_frame_count = 0
            self._speech_frame_count = 0
            self._ring_write = 0
