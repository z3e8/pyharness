from __future__ import annotations

import re
import subprocess
import urllib.parse
import urllib.request

from .llm import LLMProvider, Message, Tier
from .permissions import RulePolicy
from .session import Session

MAX_OUTPUT = 10_000


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


class Toolbox:
    """The capability API exposed to agent-written code.

    Every method routes through the permission policy before acting, so the
    policy is the single chokepoint for what the agent can do.
    """

    def __init__(self, session: Session, policy: RulePolicy, llm: LLMProvider):
        self._session = session
        self._policy = policy
        self._llm = llm

    # --- shell -----------------------------------------------------------
    def bash(self, cmd: str, timeout: int = 60) -> str:
        self._policy.check("bash", cmd)
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=self._session.workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return truncate(proc.stdout + proc.stderr)

    # --- files -----------------------------------------------------------
    def read(self, path: str) -> str:
        p = self._session.resolve(path)
        self._policy.check("file.read", str(p))
        return truncate(p.read_text())

    def write(self, path: str, content: str) -> str:
        p = self._session.resolve(path)
        self._policy.check("file.write", str(p))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {p}"

    def edit(self, path: str, old: str, new: str) -> str:
        p = self._session.resolve(path)
        self._policy.check("file.edit", str(p))
        text = p.read_text()
        count = text.count(old)
        if count != 1:
            raise ValueError(f"`old` matched {count} times in {p}; must match exactly once")
        p.write_text(text.replace(old, new))
        return f"edited {p}"

    # --- search ----------------------------------------------------------
    def search(self, pattern: str, path: str = ".") -> str:
        self._policy.check("search", pattern)
        root = self._session.resolve(path)
        rx = re.compile(pattern)
        hits: list[str] = []
        files = [root] if root.is_file() else root.rglob("*")
        for f in files:
            if not f.is_file():
                continue
            try:
                for i, line in enumerate(f.read_text().splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f}:{i}:{line}")
            except (UnicodeDecodeError, OSError):
                continue
        return truncate("\n".join(hits)) or "(no matches)"

    # --- http ------------------------------------------------------------
    def http_get(self, url: str) -> str:
        self._policy.check("http.get", url)
        with urllib.request.urlopen(url, timeout=30) as r:
            return truncate(r.read().decode())

    def http_post(self, url: str, data: dict | str) -> str:
        self._policy.check("http.post", url)
        body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data.encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30) as r:
            return truncate(r.read().decode())

    # --- nested llm calls ------------------------------------------------
    def llm(self, prompt: str, *, system: str = "", tier: Tier = Tier.FAST) -> str:
        """Let agent code make its own LLM calls (e.g. to spawn sub-agents)."""
        self._policy.check("llm", tier.value)
        return self._llm.complete(system, [Message("user", prompt)], tier)
