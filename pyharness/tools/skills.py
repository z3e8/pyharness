"""Skills: learned tools that pair markdown instructions with optional code.

A skill is a directory under the skills root:

    <skills_root>/<name>/
        SKILL.md        # frontmatter (name, description, keywords, category)
                        # + body = the procedure/instructions
        *.py            # optional bundled modules, imported on first use

It registers as one learned tool (design §6, `source="learned"`): its
description shows in `search_tools()`, `describe_tool()` reveals the full
instructions plus any bundled functions, and `use_tool()` loads the bundled
code. Both humans and the agent author skills — humans by writing the directory,
the agent via the `save_skill` builtin — and either way they reload next session.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType

from .registry import Registry

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into its `---` frontmatter (key: value lines) and body."""
    meta: dict[str, str] = {}
    body = text
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, front, body = parts
            for line in front.strip().splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip().lower()] = value.strip()
    return meta, body.strip()


def load_skills(registry: Registry, skills_dir: str | Path) -> list[str]:
    """Register every skill directory under `skills_dir` (none if it's absent)."""
    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        return []
    loaded = []
    for child in sorted(skills_dir.iterdir()):
        if child.is_dir() and (name := register_skill_dir(registry, child)):
            loaded.append(name)
    return loaded


def register_skill_dir(registry: Registry, skill_dir: Path) -> str | None:
    """Register one skill directory; return its name (None if it has no SKILL.md)."""
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return None
    meta, body = parse_skill_md(md.read_text())
    name = meta.get("name") or skill_dir.name
    keywords = tuple(k.strip() for k in meta.get("keywords", "").split(",") if k.strip())
    registry.add_skill(
        name,
        meta.get("description", ""),
        body,
        loader=lambda: _build_skill_module(name, skill_dir),
        keywords=keywords,
        category=meta.get("category") or None,
    )
    return name


def write_skill(
    skills_dir: str | Path,
    name: str,
    description: str,
    instructions: str,
    *,
    files: dict[str, str] | None = None,
    keywords: tuple[str, ...] = (),
    category: str | None = None,
) -> Path:
    """Persist a skill to disk as SKILL.md + bundled .py files. Returns its dir."""
    if not _NAME_RE.match(name):
        raise ValueError(f"skill name {name!r} must match [A-Za-z0-9_-]+")
    skill_dir = Path(skills_dir) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    # A skill is exactly its last save: drop bundled .py from a prior version so a
    # renamed/removed helper can't linger and get imported.
    for stale in skill_dir.glob("*.py"):
        stale.unlink()

    front = [f"name: {name}", f"description: {description}"]
    if keywords:
        front.append("keywords: " + ", ".join(keywords))
    if category:
        front.append(f"category: {category}")
    md = "---\n" + "\n".join(front) + "\n---\n\n" + instructions.strip() + "\n"
    (skill_dir / "SKILL.md").write_text(md)

    for fname, source in (files or {}).items():
        if not fname.endswith(".py") or "/" in fname or fname.startswith("_"):
            raise ValueError(f"bundled file {fname!r} must be a simple *.py name")
        (skill_dir / fname).write_text(source)
    return skill_dir


def _build_skill_module(name: str, skill_dir: Path) -> ModuleType:
    """Import a skill's bundled *.py and expose their public functions as one
    module named after the skill. The skill dir is on sys.path during import so
    its files may import one another."""
    module = ModuleType(name)
    sys.path.insert(0, str(skill_dir))
    try:
        for py in sorted(skill_dir.glob("*.py")):
            if py.stem.startswith("_"):
                continue
            sub = _import_file(py, f"pyharness_skill_{name}_{py.stem}")
            for fname, func in inspect.getmembers(sub, inspect.isfunction):
                if not fname.startswith("_") and func.__module__ == sub.__name__:
                    func.__module__ = name  # so Registry._public_functions lists it
                    setattr(module, fname, func)
    finally:
        sys.path.remove(str(skill_dir))
    return module


def _import_file(path: Path, mod_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
