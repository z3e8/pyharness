from __future__ import annotations

import mimetypes
from uuid import uuid4

from ...core.workspace import Workspace

# The variable boundary. A fetched body that is textual and fits this ceiling
# rides back inline as an ordinary string the agent parses in the kernel; binary
# bodies, oversized text, and any explicit `save=` spill to a workspace file so
# the *full* data lands intact without pushing an unbounded string across the
# child pipe. Generous on purpose: ordinary pages and API responses stay inline;
# only the rare huge or binary payload takes the disk path.
#
# This is NOT the context boundary. What the agent chooses to `print()` is still
# capped by the kernel's display guardrail (`util.MAX_OUTPUT`). Keeping the two
# separate is the whole point of G1: data reaches the variable whole, and the
# agent decides how much of it to surface.
INLINE_TEXT_LIMIT = 5_000_000  # 5 MB

# When a body spills to disk, how much of its head to hand back inline so the
# agent can orient (detect a schema, confirm the right page) before it reads or
# parses the file.
PREVIEW_CHARS = 2_000

_DOWNLOAD_DIR = "downloads"


def is_textual(content_type: str) -> bool:
    """Whether a response body is text the agent can read directly (vs. binary
    that only makes sense on disk). Decided by content-type, since `resp.text` of
    a PDF or image is mojibake regardless of length."""
    ct = (content_type or "").strip().lower()
    if not ct or ct.startswith("text/"):
        return True  # a body with no declared type reads as text (the common case)
    return any(t in ct for t in ("json", "xml", "javascript", "csv", "urlencoded"))


def _download_path(content_type: str, default_name: str) -> str:
    """A collision-free workspace-relative path for a spilled body. Keeps the
    source name when it carries an extension; otherwise appends one guessed from
    the content-type so downstream tooling (a PDF reader, an image lib) sees a
    sensible suffix."""
    stem = default_name or "body"
    if "." in stem:
        ext = ""
    else:
        ext = (
            mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
        )
    return f"{_DOWNLOAD_DIR}/{uuid4().hex}-{stem}{ext}"


def deliver(
    *,
    ws: Workspace,
    content_type: str,
    text: str,
    content: bytes | None = None,
    save: str | None = None,
    default_name: str = "body",
    inline_limit: int | None = None,
) -> dict:
    """Route a fetched body to a kernel variable or a workspace file, and return
    the body-shaped fields of a capability result.

    `text` is the decoded body (already secret-redacted by the caller); `content`
    is the raw bytes, required only for a binary body. The caller owns redaction
    because it holds the sink — a secret echoed in the body must be masked *before*
    it is written to disk, not just in the returned dict.

    Returns exactly one of two shapes, always with the same keys:
      inline  -> {"text": <full text>, "path": None,   "bytes": None, "preview": None, "saved": False}
      on disk -> {"text": None,        "path": <rel>,  "bytes": N,    "preview": <head>, "saved": True}
    """
    # Read the ceiling at call time (not as a bound default) so it stays a single
    # tunable knob — patchable in tests, and later config-driven.
    limit = INLINE_TEXT_LIMIT if inline_limit is None else inline_limit
    textual = is_textual(content_type)
    if save is None and textual and len(text) <= limit:
        return {
            "text": text,
            "path": None,
            "bytes": None,
            "preview": None,
            "saved": False,
        }

    data = text.encode("utf-8", "replace") if textual else (content or b"")
    path = save or _download_path(content_type, default_name)
    target = ws.path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {
        "text": None,
        "path": path,
        "bytes": len(data),
        "preview": text[:PREVIEW_CHARS] if textual else None,
        "saved": True,
    }
