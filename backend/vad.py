import copy
import numpy as np
import torch

_shared_model = None

def get_shared_vad_model():
    global _shared_model
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
        self._silence_frames_needed = silence_ms // self.CHUNK_MS
        self.min_speech_frames = min_speech_frames

        # Each instance needs a separate model copy because Silero VAD contains
        # mutable recurrent state (states are modified on forward() pass).
        self.model = copy.deepcopy(get_shared_vad_model())

        self._speech_active = False
        self._silence_frame_count = 0
        self._speech_frame_count = 0
        self._buffer = np.array([], dtype=np.int16)

    def feed(self, pcm16_bytes: bytes, require_extra_frames: bool = False) -> list[str]:
        """
        Feed raw PCM16 audio bytes. Returns a list of events that fired:
          "speech_started"  — first frame above threshold after silence
          "speech_stopped"  — silence_ms of sub-threshold audio after speech
        """
        events: list[str] = []
        samples = np.frombuffer(pcm16_bytes, dtype=np.int16)
        self._buffer = np.concatenate([self._buffer, samples])

        while len(self._buffer) >= self.SAMPLES_PER_CHUNK:
            chunk = self._buffer[:self.SAMPLES_PER_CHUNK]
            self._buffer = self._buffer[self.SAMPLES_PER_CHUNK:]

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
        self.model.reset_states()
        self._speech_active = False
        self._silence_frame_count = 0
        self._speech_frame_count = 0
        self._buffer = np.array([], dtype=np.int16)
