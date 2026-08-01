"""Skills: learned tools that pair markdown instructions with optional code.

A skill is a directory under the skills root:

    <skills_root>/<name>/
        SKILL.md        # frontmatter (name, description, keywords, category)
                        # + body = the procedure/instructions
        *.py            # optional bundled modules, executed on first call

It registers as one learned tool (design §6, `source="learned"`): its
description shows in `search_tools()`, `describe_tool()` reveals the full
instructions, the bundled functions, *and* the bundled source, and `use_tool()`
returns the bundled code as a module. Bundled code is lazy end to end: the
callable surface comes from parsing the source with `ast`, and nothing in a
bundled file executes until the agent actually calls one of its functions.
Both humans and the agent author skills — humans by writing the directory,
the agent via the `save_skill` builtin — and either way they reload next session.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import re
import sys
from collections.abc import Callable
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
        code=_render_sources(name, skill_dir),
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
    """Synthesize a skill's module **without executing any bundled code**.

    The public callable surface is discovered by parsing each bundled *.py with
    `ast`: every top-level public `def`/`async def` becomes a lightweight stub
    carrying the real name, signature, and docstring, so `describe`/`search`
    can list and render the functions with zero side effects. The first call
    to any stub executes all the bundled files (with the skill dir on sys.path
    so they may import one another), swaps the real functions in, and
    delegates; until then, nothing in the skill runs. A syntax error surfaces
    here (parsing needs no execution); an import-time failure surfaces on first
    call, attributed to the skill and file.

    Functions created dynamically (a lambda assignment, a conditional `def`)
    are not statically visible, so they don't list — but a module-level
    `__getattr__` (PEP 562) triggers the deferred execution on first attribute
    access, so they still resolve by name."""
    module = ModuleType(name)
    state: dict = {"loaded": False, "funcs": {}}
    for py in _skill_files(skill_dir):
        try:
            tree = ast.parse(py.read_text(), filename=str(py))
        except SyntaxError as exc:
            raise RuntimeError(
                f"skill {name!r}: bundled file {py.name} has a syntax error: {exc}"
            ) from exc
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and not node.name.startswith("_"):
                setattr(
                    module,
                    node.name,
                    _make_stub(name, skill_dir, module, state, node),
                )

    def __getattr__(attr: str):  # PEP 562: dynamic names still resolve by access
        if attr.startswith("_"):
            raise AttributeError(attr)
        _exec_skill_files(name, skill_dir, module, state)
        try:
            return module.__dict__[attr]
        except KeyError:
            raise AttributeError(f"skill {name!r} has no attribute {attr!r}") from None

    module.__getattr__ = __getattr__  # type: ignore[attr-defined]
    return module


def _make_stub(
    name: str, skill_dir: Path, module: ModuleType, state: dict, node
) -> Callable:
    """A placeholder for one bundled function: lists and renders like the real
    thing (name, signature, docstring), and on call triggers the deferred
    execution then delegates to it."""
    fname = node.name

    def stub(*args, **kwargs):
        _exec_skill_files(name, skill_dir, module, state)
        func = state["funcs"].get(fname)
        if func is None:
            raise RuntimeError(
                f"skill {name!r}: {fname}() is declared in the bundled source "
                "but was not defined when the code ran"
            )
        return func(*args, **kwargs)

    stub.__name__ = fname
    stub.__qualname__ = fname
    stub.__module__ = name  # so Registry._public_functions lists it
    stub.__doc__ = ast.get_docstring(node)
    stub.__signature__ = _signature_from_ast(node)  # type: ignore[attr-defined]
    return stub


def _exec_skill_files(
    name: str, skill_dir: Path, module: ModuleType, state: dict
) -> None:
    """The deferred half: actually import the bundled files and bind their
    public functions onto the synthesized module, replacing the stubs. Runs at
    most once per built module; a failure is raised as a clear skill-attributed
    error and leaves the module unloaded so a later call can retry."""
    if state["loaded"]:
        return
    sys.path.insert(0, str(skill_dir))
    try:
        funcs: dict[str, Callable] = {}
        for py in _skill_files(skill_dir):
            try:
                sub = _import_file(py, f"pyharness_skill_{name}_{py.stem}")
            except Exception as exc:
                raise RuntimeError(
                    f"skill {name!r}: bundled file {py.name} failed to import: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            for fname, func in inspect.getmembers(sub, inspect.isfunction):
                if not fname.startswith("_") and func.__module__ == sub.__name__:
                    func.__module__ = name  # so Registry._public_functions lists it
                    funcs[fname] = func
                    setattr(module, fname, func)
        state["funcs"] = funcs
        state["loaded"] = True
    finally:
        sys.path.remove(str(skill_dir))


def _skill_files(skill_dir: Path) -> list[Path]:
    """The bundled files that form a skill's public surface, in load order."""
    return [py for py in sorted(skill_dir.glob("*.py")) if not py.stem.startswith("_")]


def _import_file(path: Path, mod_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _SourceExpr:
    """Wraps a default or annotation's *source text* so `inspect.Signature`
    renders it verbatim — the value never exists because the code never ran."""

    __slots__ = ("_src",)

    def __init__(self, src: str):
        self._src = src

    def __repr__(self) -> str:
        return self._src


def _signature_from_ast(node) -> inspect.Signature:
    """An `inspect.Signature` reconstructed from a `def`'s AST — defaults and
    annotations render as their source text, since evaluating them would defeat
    the point of not executing the file."""
    P = inspect.Parameter
    a = node.args
    params: list[inspect.Parameter] = []
    positional = list(a.posonlyargs) + list(a.args)
    defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    for arg, default in zip(positional, defaults, strict=True):
        kind = P.POSITIONAL_ONLY if arg in a.posonlyargs else P.POSITIONAL_OR_KEYWORD
        params.append(_parameter(arg, kind, default))
    if a.vararg:
        params.append(_parameter(a.vararg, P.VAR_POSITIONAL, None))
    for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        params.append(_parameter(arg, P.KEYWORD_ONLY, default))
    if a.kwarg:
        params.append(_parameter(a.kwarg, P.VAR_KEYWORD, None))
    returns = _SourceExpr(ast.unparse(node.returns)) if node.returns else P.empty
    return inspect.Signature(params, return_annotation=returns)


def _parameter(arg, kind, default) -> inspect.Parameter:
    return inspect.Parameter(
        arg.arg,
        kind,
        default=(
            _SourceExpr(ast.unparse(default))
            if default is not None
            else inspect.Parameter.empty
        ),
        annotation=(
            _SourceExpr(ast.unparse(arg.annotation))
            if arg.annotation
            else inspect.Parameter.empty
        ),
    )


# How much bundled source `describe_tool` shows. Small files appear in full —
# seeing the actual code is what makes the agent call it instead of rewriting
# it — while a large file collapses to its def/class outline (signatures +
# docstring first lines), which is what's needed to decide to call it. The
# total across files is capped so a many-file skill can't flood the context.
_SOURCE_FULL_LIMIT = 4_000  # chars per file shown in full
_SOURCE_TOTAL_LIMIT = 10_000  # chars across files; beyond this, outlines only


def _render_sources(name: str, skill_dir: Path) -> str | None:
    """The bundled code as text for `describe_tool`, clearly delimited per file.
    Includes private (`_`-prefixed) files too — they are part of the skill even
    though they only run when a public file imports them."""
    blocks: list[str] = []
    total = 0
    for py in sorted(skill_dir.glob("*.py")):
        try:
            src = py.read_text().rstrip()
        except OSError:
            continue
        if len(src) <= _SOURCE_FULL_LIMIT and total + len(src) <= _SOURCE_TOTAL_LIMIT:
            total += len(src)
            blocks.append(f"## {py.name}\n```python\n{src}\n```")
        else:
            lines = src.count("\n") + 1
            blocks.append(
                f"## {py.name} ({lines} lines — outline only; "
                f"use_tool({name!r}) loads it)\n```python\n{_outline(src)}\n```"
            )
    if not blocks:
        return None
    head = (
        f"Bundled code (nothing runs until you call a function via use_tool({name!r})):"
    )
    return head + "\n\n" + "\n\n".join(blocks)


def _outline(src: str) -> str:
    """A large file reduced to its top-level def/class headers plus docstring
    first lines — enough to see what's callable without the body."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src[:_SOURCE_FULL_LIMIT] + "\n# ... (truncated: file has a syntax error)"
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            lines.append(f"{prefix} {node.name}{_signature_from_ast(node)}: ...")
        elif isinstance(node, ast.ClassDef):
            lines.append(f"class {node.name}: ...")
        else:
            continue
        doc = ast.get_docstring(node)
        if doc:
            lines[-1] += f"  # {doc.splitlines()[0]}"
    return "\n".join(lines) if lines else "# (no top-level functions or classes)"
