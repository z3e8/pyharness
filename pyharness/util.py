from __future__ import annotations

MAX_OUTPUT = 10_000


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    """Cap text length, leaving an honest marker of what was dropped."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def summarize_args(args: tuple, kwargs: dict, limit: int = 200) -> str:
    """A short, log-safe rendering of a capability call's arguments."""
    parts = [truncate(repr(a), limit) for a in args]
    parts += [f"{k}={truncate(repr(v), limit)}" for k, v in kwargs.items()]
    return ", ".join(parts)
