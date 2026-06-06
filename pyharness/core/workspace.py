from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Workspace:
    """The session's on-disk area. `dir` is the scratch space agent code reads
    and writes; every path resolves inside it and may not escape."""

    root: Path | str

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        return self.root / "workspace"

    def path(self, path: str | Path) -> Path:
        target = (self.dir / Path(path).expanduser()).resolve()
        if not target.is_relative_to(self.dir):
            raise ValueError(f"path {path!r} escapes the workspace {self.dir}")
        return target
