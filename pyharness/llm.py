from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class Tier(str, Enum):
    """Model tier. FAST = cheap/quick, SMART = expensive/capable."""

    FAST = "fast"
    SMART = "smart"


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, system: str, messages: list[Message], tier: Tier) -> str:
        """Return the assistant's text completion."""
        ...


class AnthropicProvider:
    """LLMProvider backed by the Anthropic SDK.

    The model for each tier is read from the environment, with defaults:
        PYHARNESS_MODEL_FAST   (default: claude-haiku-4-5)
        PYHARNESS_MODEL_SMART  (default: claude-opus-4-8)
    """

    def __init__(self, *, max_tokens: int = 4096, models: dict[Tier, str] | None = None):
        import anthropic  # lazy import so the package works without the optional dep

        self._client = anthropic.Anthropic()
        self._max_tokens = max_tokens
        self._models = models or {
            Tier.FAST: os.environ.get("PYHARNESS_MODEL_FAST", "claude-haiku-4-5"),
            Tier.SMART: os.environ.get("PYHARNESS_MODEL_SMART", "claude-opus-4-8"),
        }

    def complete(self, system: str, messages: list[Message], tier: Tier) -> str:
        resp = self._client.messages.create(
            model=self._models[tier],
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        return "".join(b.text for b in resp.content if b.type == "text")


class FakeProvider:
    """Deterministic provider for tests and examples. Replays canned replies."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[tuple[str, list[Message], Tier]] = []

    def complete(self, system: str, messages: list[Message], tier: Tier) -> str:
        self.calls.append((system, list(messages), tier))
        return self._replies.pop(0) if self._replies else "Done."
