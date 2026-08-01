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

# The *conditional* limit the 8000px ceiling does not cover. Once a single
# request carries more than 20 image blocks, the API drops to a far stricter
# per-image dimension limit (~2000px) and refuses anything past it with an
# `invalid_request_error` naming "many-image requests". Every screenshot already
# in history — all well inside 8000px, all previously fine — would become
# oversized the moment the 21st arrived. Rather than clip every image to 2000px
# (below the model's own 2576px high-resolution long edge, so it would degrade
# what the model can read on every screenshot to defend a case most sessions
# never reach), the session's image *count* is bounded and the oldest images are
# dropped. See `enforce_image_budget`.
MAX_IMAGES_IN_HISTORY = 20

# JPEG start-of-frame markers, whose payload carries the frame dimensions. The
# gaps (0xC4, 0xC8, 0xCC) are not frame headers: they are the Huffman-table,
# JPEG-extension and arithmetic-coding-conditioning segments.
_JPEG_SOF_MARKERS = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Every format the API accepts: JPEG, PNG, GIF, WebP. The parser covers all four
# so that "we cannot read this header" and "the API would not accept this image"
# mean the same thing — which is what lets `attach` fail closed on an unreadable
# header without refusing images the API would have taken.
SUPPORTED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def image_size(media_type: str, data: bytes) -> tuple[int, int] | None:
    """`(width, height)` in pixels read straight from the file header, or None
    when the bytes carry no header we can parse (a format the API does not
    accept, or a truncated/corrupt one).

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
    if media_type == "image/gif":
        # The logical screen descriptor follows the 6-byte signature: width then
        # height, each a little-endian uint16.
        if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
            return (
                int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"),
            )
        return None
    if media_type == "image/webp":
        return _webp_size(data)
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


def _webp_size(data: bytes) -> tuple[int, int] | None:
    """Read the canvas size out of a WebP's RIFF container.

    Three encodings share the container and each stores the dimensions
    differently, so all three are handled: `VP8 ` (lossy), `VP8L` (lossless) and
    `VP8X` (extended — animation, alpha, metadata). An unrecognized chunk is a
    None rather than a guess."""
    if len(data) < 16 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        # Lossy: a 3-byte frame tag, the 3-byte start code, then 14-bit width
        # and height (the top 2 bits of each uint16 are the scaling factor).
        if len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        return None
    if chunk == b"VP8L":
        # Lossless: a signature byte, then width-1 and height-1 packed as two
        # 14-bit fields into one little-endian uint32.
        if len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        return None
    if chunk == b"VP8X":
        # Extended: a flags word, then canvas width-1 and height-1 as 24-bit
        # little-endian fields.
        if len(data) >= 30:
            return (
                int.from_bytes(data[24:27], "little") + 1,
                int.from_bytes(data[27:30], "little") + 1,
            )
        return None
    return None


_REFUSED_NOTE = (
    "(image dropped: the API refused it, and leaving it in the "
    "history would fail every later call)"
)
_BUDGET_NOTE = (
    f"(image dropped: a request carrying more than {MAX_IMAGES_IN_HISTORY} images "
    "falls to a much stricter per-image size limit, so the oldest views make way "
    "for the newest — look() again if you still need this one)"
)


def strip_image_blocks(messages: list[dict]) -> int:
    """Replace every image block in `messages` with a text note, in place, and
    return how many were replaced.

    The escape hatch for a poisoned history. An image the API refuses does not
    just fail its own turn: it stays in `messages`, so every subsequent call
    re-sends it and fails identically, and the session cannot recover. Dropping
    the images downgrades that to a *degraded* session — the model loses what it
    saw, not the conversation."""
    return _replace_images(messages, _REFUSED_NOTE, None)


def count_image_blocks(messages: list[dict]) -> int:
    """How many image blocks `messages` currently carries — counting the ones
    nested inside a tool_result, which is where `look`'s images live."""
    total = 0
    for block in messages:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            total += 1
        elif isinstance(block.get("content"), list):
            total += count_image_blocks(block["content"])
    return total


def enforce_image_budget(
    messages: list[dict], limit: int = MAX_IMAGES_IN_HISTORY
) -> int:
    """Drop the *oldest* image blocks until at most `limit` remain, in place, and
    return how many went.

    The per-cell cap bounds one cell; this bounds the session. `MAX_IMAGES_PER_CELL`
    is 2, so eleven cells that each `look()` twice cross the threshold — and
    crossing it retroactively invalidates every screenshot already in history,
    all of which were inside the unconditional 8000px ceiling and previously
    fine. Dropping the oldest keeps the request under the threshold so the rest
    stay valid, which costs the model its earliest views rather than all of them
    (what the API-400 escape in `strip_image_blocks` would cost instead).

    Kept out of :class:`MediaOutbox`, which stages one cell and deliberately
    knows nothing about history. This is a history-shaping pass like
    ``strip_image_blocks``, and it belongs beside it — the agent loop runs it
    before each call, so the bound holds for every request regardless of how the
    images got there."""
    excess = count_image_blocks(messages) - limit
    if excess <= 0:
        return 0
    return _replace_images(messages, _BUDGET_NOTE, excess)


def _replace_images(blocks: list, note: str, budget: int | None) -> int:
    """Replace image blocks in a content list with `note`, recursing into nested
    content (a tool_result carries its own block list, which is where `look`
    images land). `budget` caps how many are replaced — None means all of them.

    Walks in document order, so a budget replaces the oldest images first."""
    replaced = 0
    for index, block in enumerate(blocks):
        if budget is not None and replaced >= budget:
            break
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            blocks[index] = {"type": "text", "text": note}
            replaced += 1
        elif isinstance(block.get("content"), list):
            remaining = None if budget is None else budget - replaced
            replaced += _replace_images(block["content"], note, remaining)
    return replaced


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
        if size is None:
            # Fail closed. An image whose dimensions can't be read can't be
            # checked against the per-edge cap, and letting it through on trust
            # is the exact failure this cap exists to prevent: a block the API
            # refuses, in the history, poisoning every later call. Since the
            # parser covers all four formats the API accepts, unreadable here
            # means the API would almost certainly refuse it too.
            raise ValueError(
                f"cannot read the dimensions of this {media_type} image — the "
                "header is unreadable or the format is not one the API accepts "
                f"({', '.join(SUPPORTED_MEDIA_TYPES)}). Not attaching it: an "
                "image whose size can't be checked could be one the API refuses, "
                "and that failure would be permanent"
            )
        if max(size) > self._max_edge_px:
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
