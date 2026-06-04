from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol


@dataclass(frozen=True)
class Message:
    role: str
    content: str


class LLM(Protocol):
    def complete(self, system: str, messages: list[Message]) -> str:
        ...


class AnthropicLLM:
    def __init__(self, model: str | None = None, max_tokens: int = 4096):
        anthropic = import_module("anthropic")
        self._client = anthropic.Anthropic()
        self._model = model or os.environ.get("PYHARNESS_MODEL", "claude-opus-4-8")
        self._max_tokens = max_tokens

    def complete(self, system: str, messages: list[Message]) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        return "".join(b.text for b in resp.content if b.type == "text")
