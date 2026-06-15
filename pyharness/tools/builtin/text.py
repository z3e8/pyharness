"""Plain-text statistics."""

from __future__ import annotations

__category__ = "text"
__keywords__ = ("count", "words", "characters", "lines")


def counts(text: str) -> dict:
    """Return character, word, and line counts for a block of text."""
    return {
        "chars": len(text),
        "words": len(text.split()),
        "lines": text.count("\n") + 1 if text else 0,
    }
