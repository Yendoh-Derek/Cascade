"""
cascade/backend/tutor.py

Tutor Logic & Conversation Management.

Responsibility: Manage the tutor's identity, conversation history, and
context window across a session. Provides multi-turn coherence through
conversation history trimming and subject-aware system prompts.
"""

import logging
import re
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Voice-channel system prompt: lead + session prompt + tail (strongest
# constraints last). Composition mirrors a lead/session/tail pattern rather
# than one undifferentiated string, so persona and voice-formatting rules can
# evolve independently and the behavioral tail always survives dynamic
# context (e.g. subject) being injected without losing its "last" position.

VOICE_LEAD = """\
You are in a spoken conversation. The user speaks and hears you.
The session prompt below defines your persona and goals. These voice rules
control only how you format and pace spoken output.
"""

TUTOR_SESSION_PROMPT = """\
You are Cascade, an expert AI tutor. Explain concepts clearly, ask guiding
questions to check understanding, and adapt explanations to the student's level.
"""

VOICE_TAIL = """\
## Voice Rules
- Default to one or two spoken sentences. Go longer only when the concept
  genuinely needs it or the student asks for more depth.
- Speak naturally. No markdown, bullets, headers, or action/emote text like
  *chuckles* — this is read aloud exactly as written.
- Treat transcripts as noisy. Only correct a likely mishearing if the student
  asks or the meaning genuinely depends on it.
- Always end with something that keeps the student engaged — a check, a
  question, or a nudge to try the next step.
"""


def build_system_prompt(subject: Optional[str] = None) -> str:
    """
    Compose the full voice-channel system prompt.

    Order is: lead (channel framing) -> session prompt (persona + optional
    subject context) -> tail (voice/behavior rules, strongest constraints
    last).

    Args:
        subject: Deprecated/ignored parameter for subject context compatibility.

    Returns:
    """
    session = TUTOR_SESSION_PROMPT.rstrip()

    return (
        f"{VOICE_LEAD.rstrip()}\n\n"
        f"Session Prompt:\n{session}\n\n"
        f"{VOICE_TAIL.rstrip()}"
    )


def build_messages(
    history: List[Dict[str, str]], subject: Optional[str] = None
) -> List[Dict[str, str]]:
    """
    Build the complete messages array for an LLM request.

    Constructs: [system_message, ...conversation_history]

    Args:
        history: Conversation history as list of {"role": "...", "content": "..."}
        subject: Optional subject area the student is studying

    Returns:
        Complete messages list ready for LLM
    """
    system_content = build_system_prompt(subject)

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
            else:
                # Remove characters that are not alphanumeric, space, hyphens, or underscores
                # to prevent prompt injection, and limit length to 100 characters.
                subject_clean = re.sub(r"[\r\n]", "", subject)
                subject_clean = re.sub(r"[^a-zA-Z0-9 \-_]", "", subject_clean).strip()
                if len(subject_clean) > 100:
                    logger.warning(f"[TutorSession] Subject too long ({len(subject_clean)} chars), truncating")
                    subject_clean = subject_clean[:100]
                subject = subject_clean or None

        self.history: List[Dict[str, str]] = []
        self.subject = subject
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
        Trim conversation history to keep it manageable.

        This prevents the context window from growing indefinitely on long
        sessions, keeping inference fast. Earlier turns are discarded; the
        most recent turns are kept.

        Also implements token-based trimming for better accuracy.

        Args:
            max_turns: Maximum number of turn pairs to keep (default: 10)
                       One turn = one user message + one assistant message
        """
        # First pass: trim by turn count
        if len(self.history) > max_turns * 2:
            self.history = self.history[-(max_turns * 2) :]
            logger.info(
                f"[TutorSession] History trimmed to last {max_turns} turns "
                f"({len(self.history)} messages)"
            )

        # Second pass: estimate token count and trim if needed
        # Rough estimate: 1 token ≈ 4 characters for English text
        total_tokens = self._estimate_tokens()
        max_tokens = 16000  # Increased limit for Groq Llama 3.3 70B

        if total_tokens > max_tokens:
            # Remove oldest message pair (user + assistant) together to maintain
            # role ordering. Popping single messages can leave an orphaned
            # assistant message at history[0], which confuses some LLMs.
            while len(self.history) >= 2 and total_tokens > max_tokens:
                self.history.pop(0)  # user message
                self.history.pop(0)  # assistant message
                total_tokens = self._estimate_tokens()

            logger.info(
                f"[TutorSession] Trimmed by token count to {len(self.history)} "
                f"messages (~{total_tokens} tokens)"
            )

    def _estimate_tokens(self) -> int:
        """
        Estimate total tokens in conversation history.

        Uses rough heuristic: 1 token ≈ 4 characters (for English).
        System prompt overhead is estimated at 50 tokens.

        Returns:
            Estimated token count
        """
        total_chars = sum(len(msg.get("content", "")) for msg in self.history)
        system_overhead = 50
        return (total_chars // 4) + system_overhead

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