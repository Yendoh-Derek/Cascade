# Changelog

All notable changes to Cascade are documented here. Follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions.

---

## Unreleased

### Added

- **Word-level STT stability tracking** (`backend/stt.py`): `_compute_display_text()` compares each
  interim word list against the previous one to produce a `(stable, tentative)` pair. Stable words
  are confirmed across consecutive frames; the fluid trailing portion is marked tentative.
  `transcript_update` WebSocket events now carry `{stable, tentative}` instead of a single `text`
  field, eliminating transcript flicker during live speech.

- **Tentative word styling** (`frontend/style.css`, `frontend/app.js`): Interim transcripts render
  stable words in full opacity and tentative words via `<span class="tentative">` (dimmed italic),
  giving users a clear visual signal of word confidence without the previous full-opacity flicker.

- **Age-based streaming stall timeout** (`backend/pipeline.py`): `MarkdownStripper` and
  `MathAwareChunkBuffer` now accept a `stall_timeout` parameter (default 500 ms, configurable via
  `CASCADE_BUFFER_STALL_MS`). If an unclosed delimiter keeps a buffer unbalanced beyond the timeout,
  the content is force-flushed as literal text rather than silently stalling TTS output.

- **Adaptive speculative grace window** (`backend/pipeline.py`): `speculative_grace_ms` is
  automatically zeroed when the final transcript ends with `.`, `?`, or `!` — clear utterance
  boundaries that don't need an extra wait window.

- **RMS computation in AudioWorklet** (`frontend/audio-input.js`): Sum-of-squares is now
  accumulated inside the existing downsample loop before `postMessage`, so main-thread `_detectSilence`
  receives a pre-computed `rms` value and skips the redundant `DataView` loop. The main-thread path
  remains as a fallback for the ScriptProcessor path.

- **`CASCADE_BUFFER_STALL_MS` env var** (`backend/config.py`): New config field that controls
  the pipeline buffer force-flush timeout. Default: 500 ms.

- **PyTorch CPU-only Docker layer** (`Dockerfile`): Torch is now installed in its own `RUN` layer
  using `--index-url https://download.pytorch.org/whl/cpu` so the CPU wheel (not the large CUDA
  bundle) is resolved, and the layer is independently cached during rebuilds.

### Fixed

- **Math speech negative exponents** (`backend/math_speech.py`): The unbraced exponent regex
  `([A-Za-z0-9_]+)\^([A-Za-z0-9_]+)` did not include `-`, so `$x^-2$` was silently skipped.
  The character class is now `[-]?[A-Za-z0-9_]+`.

- **Math speech escaped dollar signs** (`backend/math_speech.py`): `\$` no longer opens math
  mode. `math_to_speech` now uses a negative lookbehind `(?<!\\)\$...\$` pattern.

- **LLM flush on bare hyphen** (`backend/llm.py`): A bare `-` was included in
  `ends_with_space_or_punct`, causing premature flushes mid-hyphenated-word. Removed; em-dash `—`
  retained.

- **LLM cancelled-turn partial yield** (`backend/llm.py`): `yield sentence_buffer` inside the
  `CancelledError` handler was emitting a partial, incomplete sentence into the pipeline on barge-in.
  Removed. Partial responses are still saved to history via the outer `_process_transcript` handler.

- **LLM truncation signal** (`backend/llm.py`): When Groq returns `finish_reason == "length"`,
  an ellipsis (`...`) is now appended to the last buffer so the TTS output doesn't cut off abruptly.

- **VAD silence threshold rounding** (`backend/vad.py`): `silence_ms // CHUNK_MS` used integer
  floor division. For `silence_ms=200` and `CHUNK_MS=32`, this gave 6 frames (192 ms) instead of
  the correct 6.25 → rounded to 6 frames. Now uses `max(1, round(...))` for accuracy.

- **VAD rolling buffer allocation** (`backend/vad.py`): Replaced repeated `np.concatenate` calls
  in `SileroVAD._feed_unlocked` with a pre-allocated ring buffer. Eliminates per-frame heap
  allocations at 16 kHz audio throughput.

- **Spacebar shortcut fires inside `contenteditable`** (`frontend/app.js`): The keydown handler
  now checks `e.target.isContentEditable` in addition to `INPUT`/`TEXTAREA` tags.

- **Deepgram TTS as default** (`backend/pipeline.py`): `PipelineSession(tts_engine="deepgram")`
  is now the default, matching the production intent. Edge-TTS remains available as a fallback.

- **WebSocket binary frame `turn_id` prefix** (`docs/WEBSOCKET_PROTOCOL.md`): Documented the
  4-byte big-endian `turn_id` prefix prepended to every outbound audio binary frame, which the
  client uses to discard audio from cancelled turns.

- **`transcript_update` payload** (`docs/WEBSOCKET_PROTOCOL.md`): Updated field documentation
  from `{text}` to `{stable, tentative}`.

### Changed

- `on_transcript_update` callback signature changed from `(text: str)` to
  `(stable: str, tentative: str)`. All callers in `pipeline.py` and all tests in
  `test_stt_interim.py` have been updated.
