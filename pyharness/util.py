from __future__ import annotations

import io
from collections import deque
from pathlib import Path

MAX_OUTPUT = 10_000


def ensure_private_dir(path: str | Path) -> Path:
    """`mkdir -p path`, then tighten it to 0700 (owner-only).

    `~/.pyharness` and its subdirs hold private material — the encrypted vault,
    browser login profiles, the cross-project session index, saved skills — none
    of it world-readable. A bare `mkdir` inherits the process umask (commonly
    0755, world-readable), so the mode is set explicitly. Re-asserted on every
    call, so a directory another subsystem (or an older version) created
    world-readable converges to private the next time it's touched. Best-effort:
    a chmod the OS refuses (odd filesystem) must not break the caller."""
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    try:
        p.chmod(0o700)
    except OSError:
        pass
    return p

# Hard ceiling on how much a single cell's captured stdout/stderr may hold in
# memory before `truncate` ever runs. A runaway `print` (megabytes to gigabytes
# in one cell) would otherwise fill an unbounded StringIO entirely, OOMing the
# process from one line of agent code. Set well above MAX_OUTPUT so it never
# perturbs normal display truncation — it only caps pathological output.
CAPTURE_CEILING = 1_000_000


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


class CaptureBuffer(io.TextIOBase):
    """A stdout/stderr sink that never holds more than ~`ceiling` characters.

    A drop-in for `io.StringIO` as the target of `redirect_stdout`, but bounded:
    it keeps the first half and a rolling last half of the ceiling and drops the
    middle of a runaway stream, tracking how much it dropped. This keeps the same
    head+tail-with-marker shape `truncate` produces while guaranteeing memory
    stays bounded no matter how much a cell prints. `getvalue()` returns the
    captured text; callers still pass it through `truncate` for display."""

    def __init__(self, ceiling: int = CAPTURE_CEILING):
        self._half = max(1, ceiling // 2)
        self._head: list[str] = []
        self._head_len = 0
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._total = 0

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        n = len(s)
        self._total += n
        if self._head_len < self._half:
            room = self._half - self._head_len
            self._head.append(s[:room])
            self._head_len += min(room, n)
            s = s[room:]
        if s:
            # One huge write must not blow the tail budget on its own.
            if len(s) > self._half:
                s = s[-self._half:]
                self._tail.clear()
                self._tail_len = 0
            self._tail.append(s)
            self._tail_len += len(s)
            while len(self._tail) > 1 and self._tail_len - len(self._tail[0]) >= self._half:
                self._tail_len -= len(self._tail.popleft())
        return n

    def getvalue(self) -> str:
        head = "".join(self._head)
        tail = "".join(self._tail)
        dropped = self._total - len(head) - len(tail)
        if dropped <= 0:
            return head + tail
        return f"{head}\n... [output capped: dropped {dropped} chars] ...\n{tail}"


def summarize_args(args: tuple, kwargs: dict, limit: int = 200) -> str:
    """A short, log-safe rendering of a capability call's arguments."""
    parts = [truncate(repr(a), limit) for a in args]
    parts += [f"{k}={truncate(repr(v), limit)}" for k, v in kwargs.items()]
    return ", ".join(parts)
