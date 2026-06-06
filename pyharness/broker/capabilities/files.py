from __future__ import annotations

from ...core.workspace import Workspace
from ...util import truncate


class FilesCapability:
    name = "files"

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def exports(self) -> dict:
        return {"read": self.read, "write": self.write, "edit": self.edit}

    def read(self, path: str) -> str:
        return truncate(self.ws.path(path).read_text())

    def write(self, path: str, content: str) -> str:
        target = self.ws.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content)} chars to {target}"

    def edit(self, path: str, old: str, new: str) -> str:
        target = self.ws.path(path)
        text = target.read_text()
        count = text.count(old)
        if count != 1:
            raise ValueError(f"`old` matched {count} times in {target}; must match exactly once")
        target.write_text(text.replace(old, new))
        return f"edited {target}"
