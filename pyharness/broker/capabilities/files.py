from __future__ import annotations

from ...core.workspace import Workspace


class FilesCapability:
    name = "files"

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def exports(self) -> dict:
        return {"read": self.read, "write": self.write, "edit": self.edit}

    def read(self, path: str, offset: int = 0, limit: int | None = None) -> str:
        """Read a workspace file whole, or a line-window of it. `offset` skips that
        many leading lines and `limit` caps how many are returned — page a long
        file instead of pulling it all into context. The content is never
        truncated: with no window it comes back complete, in a kernel variable to
        process as the agent sees fit."""
        text = self.ws.path(path).read_text()
        if offset or limit is not None:
            lines = text.splitlines(keepends=True)
            end = offset + limit if limit is not None else None
            text = "".join(lines[offset:end])
        return text

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
