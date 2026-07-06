"""Unit tests for streaming markdown stripping."""

from backend.pipeline import MarkdownStripper, strip_markdown


class TestStripMarkdown:
    def test_bold_single_chunk(self):
        assert strip_markdown("This is **bold** text") == "This is bold text"

    def test_inline_code_single_chunk(self):
        assert strip_markdown("Use `print()` here") == "Use print() here"

    def test_link_single_chunk(self):
        assert strip_markdown("See [docs](https://example.com) now") == "See docs now"


class TestMarkdownStripper:
    def _combined(self, stripper: MarkdownStripper, *chunks: str) -> str:
        parts = [stripper.feed(chunk) for chunk in chunks]
        parts.append(stripper.flush())
        return " ".join(part for part in parts if part)

    def test_split_bold_across_chunks(self):
        stripper = MarkdownStripper()
        result = self._combined(stripper, "This is **bol", "d** text")
        assert result == "This is bold text"
        assert "*" not in result

    def test_split_inline_code_across_chunks(self):
        stripper = MarkdownStripper()
        result = self._combined(stripper, "Run `prin", "t()` now")
        assert result == "Run print() now"
        assert "`" not in result

    def test_split_link_across_chunks(self):
        stripper = MarkdownStripper()
        result = self._combined(stripper, "See [doc", "s](https://example.com) here")
        assert result == "See docs here"

    def test_flush_drains_remaining_buffer(self):
        stripper = MarkdownStripper()
        assert stripper.feed("plain text") == "plain text"
        assert stripper.flush() == ""

    def test_multiple_chunks_no_markdown(self):
        stripper = MarkdownStripper()
        assert stripper.feed("Hello ") == "Hello"
        assert stripper.feed("world") == "world"
        assert stripper.flush() == ""
