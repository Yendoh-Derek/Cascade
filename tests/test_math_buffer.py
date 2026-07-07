from backend.pipeline import MathAwareChunkBuffer


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

