from __future__ import annotations

import re

from ...core.workspace import Workspace


class SearchCapability:
    name = "search"

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def exports(self) -> dict:
        return {"search": self.search}

    def search(self, pattern: str, path: str = ".") -> str:
        root = self.ws.path(path)
        regex = re.compile(pattern)
        files = [root] if root.is_file() else root.rglob("*")
        hits: list[str] = []
        for file in files:
            if not file.is_file():
                continue
            try:
                for n, line in enumerate(file.read_text().splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{file}:{n}:{line}")
            except (OSError, UnicodeDecodeError):
                continue
        return "\n".join(hits) or "(no matches)"
