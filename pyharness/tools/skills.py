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
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from ..util import ensure_private_dir
from .registry import Registry

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_skill_name(name: str) -> str:
    """A skill name becomes a directory under the skills root, so it must be a
    simple slug — no dots or slashes that could traverse out of it. The single
    check every write/edit/record path runs before `name` touches the filesystem."""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"skill name {name!r} must match [A-Za-z0-9_-]+ (no dots or slashes)"
        )
    return name


# A skill's trust state lives in a sidecar next to SKILL.md, not in the
# procedure itself: whether it has ever run successfully (`verified`) and a
# bounded log of recent outcomes. Trust is earned by a real run — a freshly
# saved or revised skill is a hypothesis until it works against the live surface.
_JOURNAL = "journal.json"
_MAX_USES = 10
_EMPTY_JOURNAL = {"verified": False, "uses": []}


def read_journal(skill_dir: str | Path) -> dict:
    """A skill's trust state — `{"verified": bool, "uses": [...]}` — or the empty
    default (unverified, no uses) when it has none yet or the file is unreadable."""
    path = Path(skill_dir) / _JOURNAL
    if not path.exists():
        return dict(_EMPTY_JOURNAL, uses=[])
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(_EMPTY_JOURNAL, uses=[])
    return {
        "verified": bool(data.get("verified", False)),
        "uses": list(data.get("uses", [])),
    }


def _write_journal(skill_dir: Path, data: dict) -> None:
    (skill_dir / _JOURNAL).write_text(json.dumps(data, indent=2) + "\n")


def record_use(
    skill_dir: str | Path, outcome: str, note: str = "", *, now: str | None = None
) -> dict:
    """Append a use outcome to a skill's journal and return the updated state.
    A 'worked' outcome marks the skill verified (trust is earned by a real run,
    never asserted); the log is bounded to the most recent `_MAX_USES` entries so
    a later session can see how it last behaved and catch a breaking change."""
    if outcome not in ("worked", "failed"):
        raise ValueError("outcome must be 'worked' or 'failed'")
    skill_dir = Path(skill_dir)
    data = read_journal(skill_dir)
    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    entry = {"at": stamp, "outcome": outcome}
    if note:
        entry["note"] = note
    data["uses"] = (data["uses"] + [entry])[-_MAX_USES:]
    if outcome == "worked":
        data["verified"] = True
    _write_journal(skill_dir, data)
    return data


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
    keywords = tuple(
        k.strip() for k in meta.get("keywords", "").split(",") if k.strip()
    )
    journal = read_journal(skill_dir)
    registry.add_skill(
        name,
        meta.get("description", ""),
        body,
        loader=lambda: _build_skill_module(name, skill_dir),
        keywords=keywords,
        category=meta.get("category") or None,
        verified=journal["verified"],
        uses=tuple(journal["uses"]),
        check=meta.get("check") or None,
    )
    return name


def edit_skill_md(skills_dir: str | Path, name: str, edits: list) -> Path:
    """Apply delta edits to a skill's SKILL.md body — never a wholesale rewrite
    (a full regeneration is how accumulated procedure detail gets destroyed).
    Each edit is `{"old": ..., "new": ...}` (or an (old, new) pair) and `old`
    must appear exactly once in the current body. Frontmatter and bundled files
    are untouched; the revised procedure is unproven, so `verified` clears (the
    use log survives). Returns the skill dir."""
    validate_skill_name(name)
    skill_dir = Path(skills_dir) / name
    md = skill_dir / "SKILL.md"
    if not md.exists():
        raise KeyError(f"no skill {name!r} to edit")
    text = md.read_text()
    meta, body = parse_skill_md(text)
    if not edits:
        raise ValueError("no edits given")
    for i, edit in enumerate(edits):
        old, new = (edit["old"], edit["new"]) if isinstance(edit, dict) else edit
        if not old:
            raise ValueError(f"edit {i}: empty 'old' text")
        count = body.count(old)
        if count != 1:
            raise ValueError(
                f"edit {i}: 'old' text occurs {count} times in {name}'s instructions"
                " (must be exactly 1)"
            )
        body = body.replace(old, new, 1)

    front = text.split("---", 2)[1] if text.lstrip().startswith("---") else "\n"
    md.write_text("---" + front + "---\n\n" + body.strip() + "\n")
    journal = read_journal(skill_dir)
    journal["verified"] = False
    _write_journal(skill_dir, journal)
    return skill_dir


def write_skill(
    skills_dir: str | Path,
    name: str,
    description: str,
    instructions: str,
    *,
    files: dict[str, str] | None = None,
    keywords: tuple[str, ...] = (),
    category: str | None = None,
    check: str | None = None,
) -> Path:
    """Persist a skill to disk as SKILL.md + bundled .py files. Returns its dir.
    `check` is the skill's own success test — how a run knows it worked (an
    assertion, a re-fetch, an expected state) — kept in the frontmatter so trust
    can rest on something verifiable instead of the runner's impression."""
    validate_skill_name(name)
    skill_dir = Path(skills_dir) / name
    ensure_private_dir(skill_dir)  # under ~/.pyharness/skills (0700)
    # A skill is exactly its last save: drop bundled .py from a prior version so a
    # renamed/removed helper can't linger and get imported.
    for stale in skill_dir.glob("*.py"):
        stale.unlink()

    front = [f"name: {name}", f"description: {description}"]
    if keywords:
        front.append("keywords: " + ", ".join(keywords))
    if category:
        front.append(f"category: {category}")
    if check:
        front.append(f"check: {' '.join(str(check).split())}")  # one line
    md = "---\n" + "\n".join(front) + "\n---\n\n" + instructions.strip() + "\n"
    (skill_dir / "SKILL.md").write_text(md)

    for fname, source in (files or {}).items():
        if not fname.endswith(".py") or "/" in fname or fname.startswith("_"):
            raise ValueError(f"bundled file {fname!r} must be a simple *.py name")
        (skill_dir / fname).write_text(source)

    # A (re)written procedure is unproven until it runs successfully again: clear
    # the verified flag but keep the use log, so a revision can still see how the
    # last version broke. A brand-new skill starts from the empty (unverified) log.
    journal = read_journal(skill_dir)
    journal["verified"] = False
    _write_journal(skill_dir, journal)
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
