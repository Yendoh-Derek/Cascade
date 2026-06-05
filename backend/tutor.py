"""
cascade/backend/tutor.py

Tutor Logic & Conversation Management.

Responsibility: Manage the tutor's identity, conversation history, and
context window across a session. Provides multi-turn coherence through
conversation history trimming and subject-aware system prompts.
"""

import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Cascade, an expert AI tutor. Your role is to explain \
concepts clearly, ask guiding questions to check understanding, and adapt your \
explanations to the student's level. Keep responses concise and conversational — \
two to four sentences per turn. Never lecture at length. Always engage the student."""


def build_messages(
    history: List[Dict[str, str]], subject: Optional[str] = None
) -> List[Dict[str, str]]:
    """
    Build the complete messages array for an LLM request.

    Constructs: [system_message, ...conversation_history]
    If a subject is set, it is appended to the system prompt.

    Args:
        history: Conversation history as list of {"role": "...", "content": "..."}
        subject: Optional subject area the student is studying

    Returns:
        Complete messages list ready for LLM
    """
    system_content = SYSTEM_PROMPT

    if subject:
        system_content += f"\n\nThe student is studying: {subject}."

    messages = [{"role": "system", "content": system_content}] + history

    return messages


class TutorSession:
    """
    Manages conversation history and context for a tutoring session.

    Maintains message history, handles multi-turn context, and provides
    trimming to prevent context window growth on long sessions.
    """

    def __init__(self, subject: Optional[str] = None):
        """
        Initialize a tutor session.

        Args:
            subject: Optional subject area for subject-specific tutoring
        """
        # Validate and sanitize subject
        if subject is not None:
            if not isinstance(subject, str):
                logger.warning(f"[TutorSession] Invalid subject type: {type(subject)}, ignoring")
                subject = None
            elif len(subject) > 200:
                logger.warning(f"[TutorSession] Subject too long ({len(subject)} chars), truncating")
                subject = subject[:200]
            elif not subject.strip():
                subject = None

        self.history: List[Dict[str, str]] = []
        self.subject = subject.strip() if subject else None
        logger.info(f"[TutorSession] Initialized (subject={self.subject})")

    def add_user_message(self, content: str):
        """
        Add a user message to the conversation history.

        Args:
            content: The user's message (typically from STT transcript)
        """
        if not isinstance(content, str):
            logger.warning(f"[TutorSession] Invalid user message type: {type(content)}, ignoring")
            return

        if not content.strip():
            logger.warning("[TutorSession] Empty user message, ignoring")
            return

        if len(content) > 5000:
            logger.warning(f"[TutorSession] User message too long ({len(content)} chars), truncating")
            content = content[:5000]

        self.history.append({"role": "user", "content": content.strip()})
        logger.debug(f"[TutorSession] User: {content[:60]}...")

    def add_assistant_message(self, content: str):
        """
        Add an assistant (tutor) message to the conversation history.

        Args:
            content: The tutor's response
        """
        if not isinstance(content, str):
            logger.warning(f"[TutorSession] Invalid assistant message type: {type(content)}, ignoring")
            return

        if not content.strip():
            logger.warning("[TutorSession] Empty assistant message, ignoring")
            return

        if len(content) > 10000:
            logger.warning(f"[TutorSession] Assistant message too long ({len(content)} chars), truncating")
            content = content[:10000]

        self.history.append({"role": "assistant", "content": content.strip()})
        logger.debug(f"[TutorSession] Assistant: {content[:60]}...")

    def get_messages(self) -> List[Dict[str, str]]:
        """
        Get the complete messages array for an LLM request.

        Returns:
            Messages list with system prompt and full history
        """
        return build_messages(self.history, self.subject)

    def trim_history(self, max_turns: int = 10):
        """
        Trim conversation history to the last N turn pairs.

        This prevents the context window from growing indefinitely on long
        sessions, keeping inference fast. Earlier turns are discarded; the
        most recent turns are kept.

        Args:
            max_turns: Maximum number of turn pairs to keep (default: 10)
                       One turn = one user message + one assistant message
        """
        if len(self.history) > max_turns * 2:
            # Keep only the last max_turns * 2 messages
            self.history = self.history[-(max_turns * 2) :]
            logger.info(
                f"[TutorSession] History trimmed to last {max_turns} turns "
                f"({len(self.history)} messages)"
            )

    def get_summary(self) -> Dict:
        """
        Get a summary of the current session state.

        Returns:
            Dict with turn count, subject, and history length
        """
        return {
            "subject": self.subject,
            "turns": len(self.history) // 2,
            "messages": len(self.history),
        }
