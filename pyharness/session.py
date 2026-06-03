from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Session:
    """A single agent session, backed by a folder on disk.

    Everything the agent creates or runs lives under `root`. `workspace` is the
    default working directory for relative file and shell operations.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    def resolve(self, path: str | Path) -> Path:
        """Resolve `path` against the workspace (absolute paths pass through)."""
        p = Path(path)
        return p if p.is_absolute() else (self.workspace / p)
