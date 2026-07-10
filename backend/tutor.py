"""
cascade/backend/tutor.py

Tutor Logic & Conversation Management.

Responsibility: Manage the tutor's identity, conversation history, and
context window across a session. Provides multi-turn coherence through
conversation history trimming.
"""

import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

# Voice-channel system prompt: lead + session prompt + tail (strongest
# constraints last). Composition mirrors a lead/session/tail pattern rather
# than one undifferentiated string, so persona and voice-formatting rules can
# evolve independently and the behavioral tail always stays last.

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
- Wrap every math expression in single dollar signs, e.g. $c^2 = a^2 + b^2$
  or $\\sqrt{a^2 + b^2}$. Use LaTeX-style notation inside tags.
- Outside of $...$ tags, avoid symbolic operators in prose. Say "greater than"
  instead of writing >, and "times" instead of writing ×.
- Examples:
  - "The Pythagorean theorem is $c^2 = a^2 + b^2$."
  - "So the length is $\\sqrt{a^2 + b^2}$."
  - "The slope is $\\frac{y_2 - y_1}{x_2 - x_1}$."
- Treat transcripts as noisy. Only correct a likely mishearing if the student
  asks or the meaning genuinely depends on it.
- Always end with something that keeps the student engaged — a check, a
  question, or a nudge to try the next step.
"""


def build_system_prompt() -> str:
    """Compose the full voice-channel system prompt."""
    session = TUTOR_SESSION_PROMPT.rstrip()

    return (
        f"{VOICE_LEAD.rstrip()}\n\n"
        f"Session Prompt:\n{session}\n\n"
        f"{VOICE_TAIL.rstrip()}"
    )


def build_messages(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Build the complete messages array for an LLM request.

    Constructs: [system_message, ...conversation_history]
    """
    system_content = build_system_prompt()
    return [{"role": "system", "content": system_content}] + history


class TutorSession:
    """
    Manages conversation history and context for a tutoring session.

    Maintains message history, handles multi-turn context, and provides
    trimming to prevent context window growth on long sessions.
    """

    def __init__(self):
        self.history: List[Dict[str, str]] = []
        logger.info("[TutorSession] Initialized")

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

    def load_history(self, history: list) -> None:
        """Pre-populate history from a client-supplied resume payload.

        Called at the start of a resumed session to restore conversational
        context. Only well-formed user/assistant message dicts are accepted;
        malformed or injected entries are silently dropped.

        Args:
            history: List of {"role": "user"|"assistant", "content": str} dicts
        """
        valid = [
            {**msg, "content": msg["content"].strip()[:10000]}
            for msg in history
            if isinstance(msg, dict)
            and msg.get("role") in {"user", "assistant"}
            and isinstance(msg.get("content"), str)
            and msg["content"].strip()
        ]
        self.history = valid[-40:]
        logger.info(
            f"[TutorSession] History loaded from client resume: {len(self.history)} messages "
            f"({len(history) - len(valid)} entries dropped as invalid)"
        )

    def get_messages(self) -> List[Dict[str, str]]:
        """Get the complete messages array for an LLM request."""
        return build_messages(self.history)

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
        max_tokens = 16000  # Conservative context budget — llama-3.3-70b-versatile supports 128k, but keeping history short keeps inference fast

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
        """Get a summary of the current session state."""
        return {
            "turns": len(self.history) // 2,
            "messages": len(self.history),
        }
