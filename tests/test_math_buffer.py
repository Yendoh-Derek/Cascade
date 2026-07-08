import time
from backend.pipeline import MathAwareChunkBuffer
from backend.pipeline import MarkdownStripper


def test_split_math_across_chunks():
    buffer = MathAwareChunkBuffer()
    assert buffer.feed("The formula is ").rstrip() == "The formula is"
    assert buffer.feed("$c^2") == ""
    assert buffer.feed(" = a^2 + b^2$") == "c squared equals a squared plus b squared"
    assert buffer.feed(" is famous.").strip() == "is famous."


def test_flush_strips_dangling_dollar():
    buffer = MathAwareChunkBuffer()
    assert buffer.feed("$x^2") == ""
    assert buffer.flush() == "x^2"


def test_escaped_dollar_does_not_open_math_mode():
    buffer = MathAwareChunkBuffer()
    assert buffer.feed(r"Price is \$5 and ").rstrip() == r"Price is \$5 and"
    assert buffer.feed("$x^2$") == "x squared"


def test_math_buffer_stall_timeout():
    """Regression: unmatched $ should force-flush after stall_timeout seconds."""
    buf = MathAwareChunkBuffer(stall_timeout=0.05)  # 50ms for fast tests
    assert buf.feed("$x^2") == ""  # Unbalanced — held
    time.sleep(0.06)  # Exceed stall timeout
    result = buf.feed(" more text")  # Next feed triggers force flush
    # Force-flushed content should be emitted (raw, not converted)
    assert result != ""


def test_markdown_stripper_stall_timeout():
    """Regression: unclosed markdown should force-flush after stall_timeout seconds."""
    stripper = MarkdownStripper(stall_timeout=0.05)  # 50ms for fast tests
    assert stripper.feed("*italic text") == ""  # Unclosed — held
    time.sleep(0.06)  # Exceed stall timeout
    result = stripper.feed(" more text")  # Next feed triggers force flush
    assert result != ""
