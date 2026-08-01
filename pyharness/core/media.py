from __future__ import annotations

import base64

# Per-image and per-cell ceilings. Images ride in message history untouched by
# compaction (see G8), so every attachment costs context on every later turn —
# the caps keep one careless cell from flooding the window. A JPEG screenshot of
# a full page is well under 4 MB at quality 80.
MAX_IMAGE_BYTES = 4_000_000
MAX_IMAGES_PER_CELL = 2

# The provider's hard per-edge dimension ceiling (8000x8000 px). An image past it
# is refused with an API 400 — and that refusal is *permanent*, because the block
# stays in the message history and every later call re-sends it. So the ceiling is
# enforced here, at the only door into the model's context, rather than trusted to
# whatever produced the bytes.
MAX_IMAGE_EDGE_PX = 8000

# JPEG start-of-frame markers, whose payload carries the frame dimensions. The
# gaps (0xC4, 0xC8, 0xCC) are not frame headers: they are the Huffman-table,
# JPEG-extension and arithmetic-coding-conditioning segments.
_JPEG_SOF_MARKERS = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def image_size(media_type: str, data: bytes) -> tuple[int, int] | None:
    """`(width, height)` in pixels read straight from the file header, or None
    when the bytes carry no header we can parse (an unsupported format, or a
    truncated/corrupt one).

    Header-only by design: it is a few dozen bytes of parsing against a cap that
    only needs the dimensions, which is cheaper than an image library and keeps
    one out of the dependency set."""
    if media_type == "image/png":
        # IHDR is fixed at offset 8 (magic) + 8 (length + "IHDR"); width and
        # height are the first two big-endian uint32s of its payload.
        if len(data) >= 24 and data.startswith(_PNG_MAGIC):
            return (
                int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"),
            )
        return None
    if media_type == "image/jpeg":
        return _jpeg_size(data)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Walk a JPEG's segment chain to its start-of-frame header and read the
    dimensions out of it. Unlike PNG there is no fixed offset: the frame header
    sits after a variable run of application/quantization segments."""
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            return None  # out of sync with the segment chain; don't guess
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte, legal padding before a marker
            i += 1
            continue
        if marker == 0xD8 or 0xD0 <= marker <= 0xD9:  # standalone, no payload
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2:
            return None  # malformed: a zero-length segment would never advance
        if marker in _JPEG_SOF_MARKERS:
            # SOF payload: precision (1 byte), height (2), width (2).
            return (
                int.from_bytes(data[i + 7 : i + 9], "big"),
                int.from_bytes(data[i + 5 : i + 7], "big"),
            )
        i += 2 + length
    return None


def strip_image_blocks(messages: list[dict]) -> int:
    """Replace every image block in `messages` with a text note, in place, and
    return how many were replaced.

    The escape hatch for a poisoned history. An image the API refuses does not
    just fail its own turn: it stays in `messages`, so every subsequent call
    re-sends it and fails identically, and the session cannot recover. Dropping
    the images downgrades that to a *degraded* session — the model loses what it
    saw, not the conversation."""
    return _strip_blocks(messages)


def _strip_blocks(blocks: list) -> int:
    """Replace image blocks in a content list, recursing into nested content
    (a tool_result carries its own block list, which is where `look` images
    land)."""
    removed = 0
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            blocks[index] = {
                "type": "text",
                "text": (
                    "(image dropped: the API refused it, and leaving it in the "
                    "history would fail every later call)"
                ),
            }
            removed += 1
        elif isinstance(block.get("content"), list):
            removed += _strip_blocks(block["content"])
    return removed


class MediaOutbox:
    """A parent-side staging area for non-text results the model must *see*.

    The kernel returns only text to the orchestrator loop, and image bytes must
    never cross the child pipe. So a capability that runs parent-side (today only
    ``browser.look``) drops screenshot bytes here during a cell's execution, and
    the :class:`~pyharness.core.agent.Agent` drains them immediately after
    ``kernel.run`` to build the tool_result's image content blocks. One outbox is
    shared by the session's capability and its agent; draining per cell keeps each
    image attached to the call that produced it."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_IMAGE_BYTES,
        max_items: int = MAX_IMAGES_PER_CELL,
        max_edge_px: int = MAX_IMAGE_EDGE_PX,
    ):
        self._max_bytes = max_bytes
        self._max_items = max_items
        self._max_edge_px = max_edge_px
        self._items: list[tuple[str, bytes]] = []

    def attach(self, *, media_type: str, data: bytes) -> None:
        """Stage one image for delivery to the model. Raises past the per-cell,
        per-image or per-edge cap so a runaway cell can't flood the context
        window — and so an oversized image fails *here*, where the caller still
        sees an error it can act on, rather than in the message history where it
        would kill every later call."""
        if len(self._items) >= self._max_items:
            raise ValueError(
                f"at most {self._max_items} images per cell — take fewer look()s"
            )
        if len(data) > self._max_bytes:
            raise ValueError(
                f"image is {len(data)} bytes, over the {self._max_bytes}-byte cap"
            )
        size = image_size(media_type, data)
        if size is not None and max(size) > self._max_edge_px:
            raise ValueError(
                f"image is {size[0]}x{size[1]}px, over the {self._max_edge_px}px "
                "per-edge limit the API enforces — capture a smaller region "
                "(look() takes the viewport by default; on a long page, scroll "
                "and look again rather than asking for the whole thing at once)"
            )
        self._items.append((media_type, data))

    def drain(self) -> list[dict]:
        """Return the staged images as Anthropic base64 image blocks and clear the
        buffer. Called once per cell by the agent loop."""
        blocks = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(data).decode(),
                },
            }
            for media_type, data in self._items
        ]
        self._items.clear()
        return blocks
