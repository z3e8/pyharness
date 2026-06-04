from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 10_000


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


@dataclass
class Workspace:
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

    def bash(self, cmd: str, timeout: int = 60) -> str:
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=self.dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            return truncate(output + f"\n[timed out after {timeout}s]")

        return truncate(proc.stdout + proc.stderr)

    def read(self, path: str | Path) -> str:
        return truncate(self.path(path).read_text())

    def write(self, path: str | Path, content: str) -> str:
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content)} chars to {target}"

    def edit(self, path: str | Path, old: str, new: str) -> str:
        target = self.path(path)
        text = target.read_text()
        count = text.count(old)
        if count != 1:
            raise ValueError(f"`old` matched {count} times in {target}; must match exactly once")
        target.write_text(text.replace(old, new))
        return f"edited {target}"

    def search(self, pattern: str, path: str | Path = ".") -> str:
        root = self.path(path)
        regex = re.compile(pattern)
        files = [root] if root.is_file() else root.rglob("*")
        hits: list[str] = []

        for file in files:
            if not file.is_file():
                continue
            try:
                for line_number, line in enumerate(file.read_text().splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{file}:{line_number}:{line}")
            except (OSError, UnicodeDecodeError):
                continue

        return truncate("\n".join(hits)) or "(no matches)"
