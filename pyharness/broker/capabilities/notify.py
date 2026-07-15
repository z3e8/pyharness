from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from typing import Callable

# Strip C0 control characters (keeping tab and newline) from agent-authored notify
# text before it is rendered. Left unfiltered, an ESC (\x1b) lets the agent emit
# ANSI cursor/color sequences that repaint the terminal — clearing the screen or
# coloring text to mimic the harness's own `⚠ approval required` prompt. The one
# inbound channel (the approval prompt) is still the only thing that accepts input,
# but the display must not be forgeable.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_LEVELS = ("info", "attention", "done")
# Desktop notifications carry a fixed title; the agent's text goes only in the
# body, so nothing the agent writes can present itself as the harness (or any
# other sender) speaking.
_TITLE = "pyharness agent"
_BODY_LIMIT = 240


def _desktop_notify(message: str) -> None:
    """Show a native desktop notification. Best-effort by design: an unsupported
    platform or a failing helper is silently ignored — the session event and the
    audit record are the reliable channels."""
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(message)} with title {json.dumps(_TITLE)}"
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", _TITLE, message], capture_output=True, timeout=5)


class NotifyCapability:
    """The agent's one outbound channel to the human — strictly output-only.

    A notification is shown live (the session event stream), lands in the
    hash-chained audit like every capability call, and is best-effort mirrored
    as a desktop notification so a human not watching the terminal still sees
    it. It carries no interactivity anywhere: nothing to click, confirm, or
    reply to — the approval prompt remains the only channel that accepts human
    input, which is what keeps an agent-authored message from ever functioning
    as one."""

    name = "notify"

    def __init__(
        self,
        on_event: Callable | None = None,
        desktop: Callable[[str], None] | None = _desktop_notify,
    ):
        self.on_event = on_event or (lambda kind, text, **extra: None)
        self.desktop = desktop

    def exports(self) -> dict:
        return {"notify": self.notify}

    def notify(self, message: str, level: str = "info") -> str:
        """Send a one-way note to the user, shown immediately (and best-effort as
        a desktop notification). `level` is "info" (a checkpoint worth knowing),
        "attention" (blocked or needs a human soon), or "done" (long work
        finished). Use it sparingly — for checkpoints and attention-worthy
        events, not narration; the plain-text reply remains the answer channel.
        Returns "delivered"."""
        if level not in _LEVELS:
            raise ValueError(f"level must be one of {_LEVELS}, got {level!r}")
        message = _CONTROL_RE.sub("", str(message))
        self.on_event("notify", message, level=level)
        if self.desktop is not None:
            try:
                self.desktop(message[:_BODY_LIMIT])
            except Exception:
                pass  # best-effort: never let a display helper break the agent's call
        return "delivered"
