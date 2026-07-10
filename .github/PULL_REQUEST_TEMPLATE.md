## What does this change?

<!-- One or two sentences. Link an issue with "Closes #123" if applicable. -->

## Why?

<!-- What problem does this solve, or what does it improve? -->

## How was this tested?

<!-- e.g. `pytest tests/ -v`, manual test against a live Deepgram/Groq session, etc. -->

- [ ] `pytest tests/ -v` passes
- [ ] `ruff check .` passes
- [ ] `mypy backend/ --ignore-missing-imports` passes
- [ ] Frontend smoke tests pass (`node tests/frontend/test_chart.js`, `test_playback_state.js`), if frontend files changed

## Latency/architecture impact

<!-- If this touches backend/pipeline.py, stt.py, tts.py, vad.py, or llm.py:
     does this change any turn-latency path (STT tail, LLM TTFB, TTS TTFA)?
     If yes, include before/after numbers if you have them. -->

## ADR needed?

<!-- If this is a significant architectural decision or refactor, add one under
     docs/adr/ per CONTRIBUTING.md and link it here. -->
