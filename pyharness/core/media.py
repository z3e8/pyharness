from __future__ import annotations

import base64

# Per-image and per-cell ceilings. Images ride in message history untouched by
# compaction (see G8), so every attachment costs context on every later turn —
# the caps keep one careless cell from flooding the window. A JPEG screenshot of
# a full page is well under 4 MB at quality 80.
MAX_IMAGE_BYTES = 4_000_000
MAX_IMAGES_PER_CELL = 2


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
        self, *, max_bytes: int = MAX_IMAGE_BYTES, max_items: int = MAX_IMAGES_PER_CELL
    ):
        self._max_bytes = max_bytes
        self._max_items = max_items
        self._items: list[tuple[str, bytes]] = []

    def attach(self, *, media_type: str, data: bytes) -> None:
        """Stage one image for delivery to the model. Raises past the per-cell or
        per-image cap so a runaway cell can't flood the context window."""
        if len(self._items) >= self._max_items:
            raise ValueError(
                f"at most {self._max_items} images per cell — take fewer look()s"
            )
        if len(data) > self._max_bytes:
            raise ValueError(
                f"image is {len(data)} bytes, over the {self._max_bytes}-byte cap"
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
