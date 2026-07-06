"""Tests for STT interim → is_final UI update behavior."""

import pytest

from backend.stt import STTHandler


@pytest.mark.asyncio
async def test_is_final_emits_transcript_update_before_speech_final():
    """is_final corrections must reach the UI before speech_final flush."""
    updates: list[str] = []
    finals: list[str] = []

    handler = STTHandler(
        api_key="test",
        on_transcript=lambda text: finals.append(text),
        on_transcript_update=lambda text: updates.append(text),
    )

    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "Think"}]},
        "is_final": False,
        "speech_final": False,
    })
    assert updates == ["Think"]
    assert not finals

    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "Exactly"}]},
        "is_final": True,
        "speech_final": False,
    })
    assert updates == ["Think", "Exactly"]
    assert not finals

    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": ""}]},
        "is_final": False,
        "speech_final": True,
    })
    assert finals == ["Exactly"]


@pytest.mark.asyncio
async def test_is_final_and_speech_final_in_one_message():
    """Deepgram often bundles is_final and speech_final in a single Results message."""
    updates: list[str] = []
    finals: list[str] = []

    handler = STTHandler(
        api_key="test",
        on_transcript=lambda text: finals.append(text),
        on_transcript_update=lambda text: updates.append(text),
    )

    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "Think"}]},
        "is_final": False,
        "speech_final": False,
    })

    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "Exactly"}]},
        "is_final": True,
        "speech_final": True,
    })

    assert updates == ["Think", "Exactly"]
    assert finals == ["Exactly"]
