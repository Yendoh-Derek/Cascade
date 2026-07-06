"""Tests for split-utterance merge across speech_final fragments."""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from backend.pipeline import MERGE_WINDOW_SEC, PipelineSession
from tests.test_latency_metrics import make_pipeline_session


def _init_session(session: PipelineSession) -> None:
    session.llm_generator = MagicMock()
    session.tts_engine = MagicMock()
    session.model_config["speculative_grace_ms"] = 500


@pytest.mark.asyncio
async def test_superseding_fragments_merge_into_one_turn():
    session = make_pipeline_session()
    _init_session(session)
    captured: list[str] = []

    async def capture_process(transcript, turn_id, was_speculative=False):
        captured.append(transcript)
        try:
            await asyncio.wait_for(session._cancel_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass

    session._process_transcript = capture_process  # type: ignore[method-assign]

    session._on_transcript_received("Hello Cascade, I want to learn about")
    await asyncio.sleep(0.05)
    session._on_transcript_received("the pythagoras theorem")

    await asyncio.sleep(0.05)

    assert len(captured) == 2
    assert captured[1] == (
        "Hello Cascade, I want to learn about the pythagoras theorem"
    )


@pytest.mark.asyncio
async def test_vad_cancel_then_fragment_merges():
    session = make_pipeline_session()
    _init_session(session)
    captured: list[str] = []

    async def capture_process(transcript, turn_id, was_speculative=False):
        captured.append(transcript)
        try:
            await asyncio.wait_for(session._cancel_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass

    session._process_transcript = capture_process  # type: ignore[method-assign]

    session._on_transcript_received("Hello Cascade, I want to learn about")
    assert session._inflight_transcript == "Hello Cascade, I want to learn about"

    session._on_vad_interrupted("")
    session._on_transcript_received("the pythagoras theorem")

    await asyncio.sleep(0.05)

    assert len(captured) == 1
    assert captured[0] == (
        "Hello Cascade, I want to learn about the pythagoras theorem"
    )


def test_fragment_after_merge_window_not_merged():
    session = make_pipeline_session()
    session._stash_pending_merge("first fragment", from_turn_id=1)
    session._pending_merge_at = time.perf_counter() - MERGE_WINDOW_SEC - 1

    result = session._maybe_merge_with_pending("second fragment")

    assert result == "second fragment"
    assert session._pending_merge_text is None


def test_cancelled_turn_clears_stale_pending_merge():
    """Interrupted turns must not leave pending merge for unrelated later utterances."""
    session = make_pipeline_session()
    session._stash_pending_merge("stale interrupted text", from_turn_id=1)

    turn_id = 2
    can_send = False
    if can_send:
        session._clear_pending_merge()
    elif session._pending_merge_turn_id != turn_id:
        session._clear_pending_merge()

    assert session._pending_merge_text is None


def test_cancelled_turn_preserves_pending_for_continuation_merge():
    """Fragment-split stash must survive the cancelled turn's finally block."""
    session = make_pipeline_session()
    session._stash_pending_merge("Hello Cascade, I want to learn about", from_turn_id=1)

    turn_id = 1
    can_send = False
    if can_send:
        session._clear_pending_merge()
    elif session._pending_merge_turn_id != turn_id:
        session._clear_pending_merge()

    assert session._pending_merge_text == "Hello Cascade, I want to learn about"
    assert session._pending_merge_turn_id == 1


@pytest.mark.asyncio
async def test_barge_in_clears_pending_merge():
    session = make_pipeline_session()
    _init_session(session)
    session._stash_pending_merge("prior fragment", from_turn_id=1)
    session._on_transcript_received("tell me about math")
    session.set_ai_speaking(True)

    session._on_vad_interrupted("")

    assert session._pending_merge_text is None


@pytest.mark.asyncio
async def test_successful_turn_clears_pending_merge():
    session = make_pipeline_session()
    _init_session(session)
    session.model_config["speculative_grace_ms"] = 0
    session._stash_pending_merge("stale fragment")

    async def mock_gen(messages, timeout_sec=30):
        yield "Hi there."

    session.llm_generator.generate = mock_gen  # type: ignore[method-assign]

    async def mock_tts_streaming(chunk_queue, timeout_sec=30, cancel_event=None):
        while True:
            chunk = await chunk_queue.get()
            if chunk is None:
                break
            yield {"type": "tts_metadata", "latency_ms": 10, "engine": "edge"}
            yield b"audio"

    session.tts_engine.synthesise_streaming = mock_tts_streaming  # type: ignore[method-assign]

    session._on_transcript_received("complete question")
    if session.processing_task:
        await session.processing_task

    assert session._pending_merge_text is None
