from __future__ import annotations

from pathlib import Path

from ...security.policy import ActionCategory
from ...tools.registry import Registry
from ...tools.skills import (
    edit_skill_md,
    record_use,
    register_skill_dir,
    validate_skill_name,
    write_skill,
)


class SkillsCapability:
    """Lets the agent author a reusable skill at runtime (design §6).

    A skill packages a repeatable procedure — markdown instructions plus optional
    bundled .py modules — under the skills root. It is written parent-side (so it
    works out-of-process), registered immediately for this session, and reloaded
    automatically in later ones. The agent finds it with `search_tools()`, reads
    it (instructions + bundled source) with `describe_tool()`, and loads its code
    with `use_tool()` — nothing in a bundled file executes before that.

    Bundled code is agent-authored, so it executes where the agent's own code
    does: inside the sandboxed child, with the session builtins ambient (see
    `RemoteSkillSpec`)."""

    name = "skills"

    def __init__(
        self, registry: Registry, skills_dir: str | Path, on_event=None, namespace=None
    ):
        self.registry = registry
        self.skills_dir = Path(skills_dir)
        # Optional hook the session wires to its trace log, so skill authorship
        # and outcomes land in the session record (the index reads them there).
        self._on_event = on_event or (lambda kind, text="", **extra: None)
        # Zero-arg provider of the session's builtin namespace, threaded into the
        # skill loader so bundled code runs with the same broker-gated builtins
        # agent cells get (see tools/skills.py). Deferred: evaluated on first
        # call of a bundled function, never here.
        self._namespace = namespace

    def exports(self) -> dict:
        return {
            "save_skill": self.save_skill,
            "edit_skill": self.edit_skill,
            "record_skill_use": self.record_skill_use,
        }

    def _guard_shadow(self, name: str) -> None:
        """A skill may overwrite another *learned* skill (that is how re-save and
        edit work), but must never take a name a core capability or an
        installed/MCP tool already owns. Shadowing e.g. `http` would reroute every
        call made under that trusted name through agent-authored code while the
        approval summaries still read as the real capability's. Mirrors the same
        guard `tools.add_mcp_server` applies to server names."""
        existing = self.registry.info(name)
        if existing is not None and existing.source != "learned":
            raise ValueError(
                f"cannot use skill name {name!r}: a {existing.source} tool already "
                "owns it; pick a different name"
            )

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """All ops only write under the skills root, so they are LOCAL. Saving or
        editing a skill gates on a supply-chain sign-off (that content auto-loads
        in later sessions); recording a use writes only metadata, so it isn't
        gated."""
        name = kwargs.get("name") or (args[0] if args else "?")
        if op == "record_skill_use":
            outcome = kwargs.get("outcome") or (args[1] if len(args) >= 2 else "?")
            return ActionCategory.LOCAL, f"record use of skill {name!r}: {outcome}"
        if op == "edit_skill":
            edits = kwargs.get("edits") or (args[1] if len(args) >= 2 else [])
            return ActionCategory.LOCAL, f"apply {len(edits)} edit(s) to skill {name!r}"
        return ActionCategory.LOCAL, f"save skill {name!r} to {self.skills_dir}"

    def save_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        files: dict[str, str] | None = None,
        keywords: tuple[str, ...] = (),
        category: str | None = None,
        check: str | None = None,
    ) -> str:
        """Save a reusable skill = markdown `instructions` plus bundled .py
        modules (`files` maps filename -> source). It reloads in later sessions
        and is usable now via search_tools(name)/use_tool(name).

        **Put the code in `files`, not in the markdown.** Anything you would
        otherwise re-type on the next run — a fetch, a parse, a login sequence,
        an output formatter — belongs in `files` as an importable function, so
        the next run does `use_tool(name).summarize(...)` instead of rewriting
        it from the instructions. `instructions` is for the procedure *around*
        the code: when to use it, what to check, what can go wrong. A skill
        whose markdown contains steps you could have written as a function has
        saved the reader nothing. Bundled files are lazy — nothing in them
        executes until a function is actually called — and their source is
        shown by describe_tool, so future runs can see what is callable.

        **Your builtins are in scope inside bundled code**, exactly as in your
        cells: call use_tool/search_tools/read/llm/… directly and reach
        external capabilities the usual way (e.g. `web = use_tool("web")` then
        `web.fetch(url)`). Never `import pyharness` — the builtins are not
        package exports, and that import fails. Bundled code runs in the same
        sandboxed process your cells do and under the same limits: every call it
        makes is policy/audit/budget-gated the same as your own, and raw
        filesystem or network access is jailed the same way too.

        `check` is the skill's success test — one line saying how a run confirms
        it worked (an assertion, a re-fetch, an expected state) — so
        record_skill_use rests on evidence, not impression."""
        validate_skill_name(name)
        self._guard_shadow(name)
        skill_dir = write_skill(
            self.skills_dir,
            name,
            description,
            instructions,
            files=files,
            keywords=tuple(keywords),
            category=category,
            check=check,
        )
        register_skill_dir(self.registry, skill_dir, namespace=self._namespace)
        self._on_event("skill_saved", name, skill=name)
        n = len(files or {})
        return (
            f"saved skill {name!r} ({n} bundled file{'s' * (n != 1)}) to {skill_dir} — "
            "unverified until you run it and record_skill_use(name, 'worked')."
        )

    def edit_skill(self, name: str, edits: list, reason: str = "") -> str:
        """Revise a skill's instructions with targeted delta edits — each edit is
        {"old": <exact text appearing once>, "new": <replacement>} — instead of
        re-saving the whole procedure (a full rewrite is how accumulated detail
        gets lost). Frontmatter and bundled files are untouched. The revised
        skill is unproven again: verified clears until a real run works."""
        validate_skill_name(name)
        self._guard_shadow(name)
        skill_dir = edit_skill_md(self.skills_dir, name, edits)
        register_skill_dir(self.registry, skill_dir, namespace=self._namespace)
        self._on_event(
            "skill_edited", name, skill=name, edits=len(edits), reason=reason
        )
        return (
            f"applied {len(edits)} edit(s) to skill {name!r} — unverified until a "
            "run works and you record_skill_use(name, 'worked')."
        )

    def record_skill_use(self, name: str, outcome: str, note: str = "") -> str:
        """Log how a learned skill just behaved: `outcome` is 'worked' or
        'failed', with an optional `note` (a changed selector, why it broke). The
        first 'worked' marks the skill verified; the log lets you and later
        sessions see how it last behaved and catch a breaking change. Do this
        after actually running the skill — trust is earned by a real run."""
        validate_skill_name(name)
        skill_dir = self.skills_dir / name
        if not (skill_dir / "SKILL.md").exists():
            raise KeyError(f"no learned skill {name!r} to record against")
        data = record_use(skill_dir, outcome, note)
        self.registry.set_skill_usage(name, data["verified"], tuple(data["uses"]))
        self._on_event("skill_use", name, skill=name, outcome=outcome, note=note)
        state = "verified" if data["verified"] else "unverified"
        return f"recorded {outcome!r} for skill {name!r} (now {state})"
