from __future__ import annotations


class LLMCapability:
    name = "llm"

    def __init__(self, llm, default_tier: str = "cheap"):
        self.llm = llm
        self.default_tier = default_tier

    def exports(self) -> dict:
        return {"llm": self.run}

    def run(self, prompt: str, tier: str | None = None, system: str | None = None) -> str:
        completion = self.llm.complete(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tier=tier or self.default_tier,
        )
        return completion.text
