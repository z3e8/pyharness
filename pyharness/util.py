from __future__ import annotations

MAX_OUTPUT = 10_000


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    """Cap text length for display, keeping the head *and* tail so the ending
    survives — a log's error, a command's summary line, the close of a document.
    Head-only truncation silently drops exactly the part that usually matters.

    This is the *display* guardrail (what the agent prints back into its own
    context); it does not bound what a capability puts in a kernel variable. Full
    data reaches the variable intact — see `broker.capabilities.payload`."""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    head = text[: limit * 3 // 4]
    tail = text[-(limit // 4):]
    return f"{head}\n... [truncated {dropped} chars] ...\n{tail}"


def summarize_args(args: tuple, kwargs: dict, limit: int = 200) -> str:
    """A short, log-safe rendering of a capability call's arguments."""
    parts = [truncate(repr(a), limit) for a in args]
    parts += [f"{k}={truncate(repr(v), limit)}" for k, v in kwargs.items()]
    return ", ".join(parts)
