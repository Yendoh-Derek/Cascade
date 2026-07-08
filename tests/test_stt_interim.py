"""Tests for STT interim → is_final UI update behavior.

Updated to reflect the word-level stability tracking introduced in the STT
flicker fix: on_transcript_update now receives (stable: str, tentative: str)
instead of a single text string.
"""

import pytest

from backend.stt import STTHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler(updates, finals):
    """Construct an STTHandler wired to the provided capture lists."""
    return STTHandler(
        api_key="test",
        on_transcript=lambda text: finals.append(text),
        # New two-arg signature: (stable, tentative)
        on_transcript_update=lambda stable, tentative: updates.append((stable, tentative)),
    )


# ---------------------------------------------------------------------------
# Existing flow tests — updated for new (stable, tentative) payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_is_final_emits_transcript_update_before_speech_final():
    """is_final corrections must reach the UI before speech_final flush."""
    updates: list[tuple[str, str]] = []
    finals: list[str] = []

    handler = _make_handler(updates, finals)

    # First interim: "Think" — no prior words, so stable="" tentative="Think"
    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "Think"}]},
        "is_final": False,
        "speech_final": False,
    })
    assert len(updates) == 1
    stable, tentative = updates[0]
    # No prior words yet — entire token is tentative
    assert tentative == "Think"
    assert not finals

    # is_final=True: transcript confirmed, emitted as (buffer, "")
    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "Exactly"}]},
        "is_final": True,
        "speech_final": False,
    })
    # is_final fires a stable update with empty tentative
    assert len(updates) == 2
    stable2, tentative2 = updates[1]
    assert "Exactly" in stable2
    assert tentative2 == ""
    assert not finals

    # speech_final flushes the buffer → on_transcript fires
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
    updates: list[tuple[str, str]] = []
    finals: list[str] = []

    handler = _make_handler(updates, finals)

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

    # Two updates: one for interim "Think", one for is_final "Exactly"
    assert len(updates) == 2
    # Final update should be stable with empty tentative
    stable_final, tentative_final = updates[1]
    assert "Exactly" in stable_final
    assert tentative_final == ""
    assert finals == ["Exactly"]


# ---------------------------------------------------------------------------
# New tests: word-level stability logic (_compute_display_text)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stability_first_call_everything_is_tentative():
    """On the very first interim, all words except the last are stable, last is tentative."""
    handler = STTHandler(api_key="test", on_transcript=lambda _: None)

    stable, tentative = handler._compute_display_text("hello world")
    # First call: no prior state; "hello" → stable prefix, "world" → tentative tail
    assert tentative == "world"
    assert stable == "hello"


@pytest.mark.asyncio
async def test_stability_consistent_prefix_is_stable():
    """Words that haven't changed between interims become stable."""
    handler = STTHandler(api_key="test", on_transcript=lambda _: None)

    # First interim: "the quick"
    handler._compute_display_text("the quick")
    # Second interim with same prefix — "the quick" stays stable, "brown" is tentative
    stable, tentative = handler._compute_display_text("the quick brown")
    assert stable == "the quick"
    assert tentative == "brown"


@pytest.mark.asyncio
async def test_stability_word_swap_breaks_stability():
    """A changed word resets the stable boundary at that position."""
    handler = STTHandler(api_key="test", on_transcript=lambda _: None)

    handler._compute_display_text("the quick")
    # "quick" swaps to "slow" — nothing after "the" is stable
    stable, tentative = handler._compute_display_text("the slow fox")
    assert stable == "the"
    assert "slow" in tentative
    assert "fox" in tentative


@pytest.mark.asyncio
async def test_stability_resets_on_is_final():
    """After an is_final message, _last_interim_words should be cleared."""
    updates: list[tuple[str, str]] = []
    finals: list[str] = []
    handler = _make_handler(updates, finals)

    # Build up some interim state
    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "hello world"}]},
        "is_final": False,
        "speech_final": False,
    })

    # is_final resets interim word state
    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "hello world"}]},
        "is_final": True,
        "speech_final": False,
    })
    assert handler._last_interim_words == []

    # Next interim after reset should treat everything as fresh
    stable, tentative = handler._compute_display_text("new sentence")
    # After reset, first call again — last word tentative
    assert tentative == "sentence"


@pytest.mark.asyncio
async def test_stability_empty_transcript_returns_empty():
    """Empty transcript should return empty stable and tentative."""
    handler = STTHandler(api_key="test", on_transcript=lambda _: None)
    stable, tentative = handler._compute_display_text("")
    assert stable == ""
    assert tentative == ""


@pytest.mark.asyncio
async def test_stability_single_word_all_tentative():
    """A single-word interim should be fully tentative."""
    handler = STTHandler(api_key="test", on_transcript=lambda _: None)
    stable, tentative = handler._compute_display_text("hello")
    assert stable == ""
    assert tentative == "hello"


@pytest.mark.asyncio
async def test_live_stable_includes_buffer():
    """live_stable prepends the confirmed transcript_buffer before stable interim words."""
    updates: list[tuple[str, str]] = []
    finals: list[str] = []
    handler = _make_handler(updates, finals)

    # Simulate a prior is_final having confirmed "First sentence."
    handler.transcript_buffer = "First sentence."

    await handler._handle_message({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": "Second thought"}]},
        "is_final": False,
        "speech_final": False,
    })

    assert len(updates) == 1
    stable, tentative = updates[0]
    # stable should include the confirmed buffer
    assert "First sentence." in stable
