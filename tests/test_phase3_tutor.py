"""
cascade/tests/test_phase3_tutor.py

Phase 3 integration test — verify TutorSession and multi-turn coherence.

Tests:
1. TutorSession creation and message building
2. History accumulation across multiple turns
3. System prompt injection with subject context
4. History trimming at max_turns boundary

Usage:
    python tests/test_phase3_tutor.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.tutor import TutorSession, build_messages


def test_tutor_session_creation():
    """Test basic TutorSession creation."""
    print("  [1/6] TutorSession creation...", end=" ")
    session = TutorSession(subject="Biology")
    assert session.subject == "Biology"
    assert session.history == []
    print("✓")


def test_message_building():
    """Test message building with subject context."""
    print("  [2/6] Message building with subject...", end=" ")
    history = [{"role": "user", "content": "What is photosynthesis?"}]
    messages = build_messages(history, subject="Biology")

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "Biology" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    print("✓")


def test_single_turn():
    """Test a single turn: user message + assistant message."""
    print("  [3/6] Single turn (user + assistant)...", end=" ")
    session = TutorSession(subject="Mathematics")

    session.add_user_message("What is 2+2?")
    session.add_assistant_message("2+2 equals 4.")

    assert len(session.history) == 2
    assert session.history[0]["role"] == "user"
    assert session.history[1]["role"] == "assistant"
    print("✓")


def test_multi_turn_coherence():
    """Test multi-turn conversation maintains context."""
    print("  [4/6] Multi-turn coherence...", end=" ")
    session = TutorSession(subject="Physics")

    # Turn 1
    session.add_user_message("What is gravity?")
    session.add_assistant_message("Gravity is the force that pulls objects toward each other.")

    # Turn 2 — should have context from Turn 1
    session.add_user_message("How does it affect satellites?")
    session.add_assistant_message("Satellites orbit because gravity keeps them in orbit.")

    # Turn 3
    session.add_user_message("Can you give an analogy?")
    session.add_assistant_message("Think of gravity like a rope pulling the satellite toward Earth.")

    assert len(session.history) == 6  # 3 turns × 2 messages each
    messages = session.get_messages()
    assert len(messages) == 7  # system + 6 history

    # Verify system prompt includes subject
    assert "Physics" in messages[0]["content"]

    # Verify all turns are present
    assert "gravity" in messages[1]["content"]
    assert "satellite" in messages[3]["content"]
    print("✓")


def test_history_trimming():
    """Test that history trimming preserves recent turns."""
    print("  [5/6] History trimming (max 3 turns)...", end=" ")
    session = TutorSession()

    # Add 5 turns (10 messages)
    for turn in range(1, 6):
        session.add_user_message(f"Question {turn}")
        session.add_assistant_message(f"Answer {turn}")

    assert len(session.history) == 10

    # Trim to max 3 turns
    session.trim_history(max_turns=3)

    # Should now have only 6 messages (3 turns × 2 messages)
    assert len(session.history) == 6

    # Verify the most recent turns are kept
    assert "Question 3" in session.history[0]["content"]
    assert "Answer 5" in session.history[-1]["content"]
    print("✓")


def test_session_summary():
    """Test session summary reporting."""
    print("  [6/6] Session summary...", end=" ")
    session = TutorSession(subject="Chemistry")

    session.add_user_message("Q1")
    session.add_assistant_message("A1")
    session.add_user_message("Q2")
    session.add_assistant_message("A2")

    summary = session.get_summary()
    assert summary["subject"] == "Chemistry"
    assert summary["turns"] == 2
    assert summary["messages"] == 4
    print("✓")


def main():
    print("\n" + "=" * 56)
    print("  CASCADE — Phase 3 TutorSession Integration Tests")
    print("=" * 56)
    print()

    try:
        test_tutor_session_creation()
        test_message_building()
        test_single_turn()
        test_multi_turn_coherence()
        test_history_trimming()
        test_session_summary()

        print()
        print("=" * 56)
        print("  ✓  All tests passed!")
        print("=" * 56)
        print()
        return True

    except AssertionError as e:
        print(f"✗ FAILED")
        print(f"\n  Error: {e}\n")
        return False
    except Exception as e:
        print(f"✗ ERROR")
        print(f"\n  Unexpected error: {e}\n")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
