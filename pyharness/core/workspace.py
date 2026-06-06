from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Workspace:
    """The session's on-disk area. `dir` is the scratch space agent code reads
    and writes; relative paths resolve inside it."""

    root: Path | str

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        return self.root / "workspace"

    def path(self, path: str | Path) -> Path:
        target = Path(path).expanduser()
        return target.resolve() if target.is_absolute() else (self.dir / target).resolve()
