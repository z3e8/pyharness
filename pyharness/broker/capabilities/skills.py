from __future__ import annotations

from pathlib import Path

from ...security.policy import ActionCategory
from ...tools.registry import Registry
from ...tools.skills import record_use, register_skill_dir, write_skill


class SkillsCapability:
    """Lets the agent author a reusable skill at runtime (design §6).

    A skill packages a repeatable procedure — markdown instructions plus optional
    bundled .py modules — under the skills root. It is written parent-side (so it
    works out-of-process), registered immediately for this session, and reloaded
    automatically in later ones. The agent finds it with `search_tools()`, reads
    it with `describe_tool()`, and loads its code with `use_tool()`."""

    name = "skills"

    def __init__(self, registry: Registry, skills_dir: str | Path, on_event=None):
        self.registry = registry
        self.skills_dir = Path(skills_dir)
        # Optional hook the session wires to its trace log, so skill authorship
        # and outcomes land in the session record (the index reads them there).
        self._on_event = on_event or (lambda kind, text="", **extra: None)

    def exports(self) -> dict:
        return {"save_skill": self.save_skill, "record_skill_use": self.record_skill_use}

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """Both ops only write under the skills root, so they are LOCAL. Saving a
        skill's gate is a supply-chain sign-off (that code auto-loads in later
        sessions); recording a use writes only metadata, so it isn't gated."""
        name = kwargs.get("name") or (args[0] if args else "?")
        if op == "record_skill_use":
            outcome = kwargs.get("outcome") or (args[1] if len(args) >= 2 else "?")
            return ActionCategory.LOCAL, f"record use of skill {name!r}: {outcome}"
        return ActionCategory.LOCAL, f"save skill {name!r} to {self.skills_dir}"

    def save_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        files: dict[str, str] | None = None,
        keywords: tuple[str, ...] = (),
        category: str | None = None,
    ) -> str:
        """Save a reusable skill = markdown `instructions` plus optional bundled
        .py modules (`files` maps filename -> source). It reloads in later
        sessions and is usable now via search_tools(name)/use_tool(name)."""
        skill_dir = write_skill(
            self.skills_dir, name, description, instructions,
            files=files, keywords=tuple(keywords), category=category,
        )
        register_skill_dir(self.registry, skill_dir)
        self._on_event("skill_saved", name, skill=name)
        n = len(files or {})
        return (
            f"saved skill {name!r} ({n} bundled file{'s' * (n != 1)}) to {skill_dir} — "
            "unverified until you run it and record_skill_use(name, 'worked')."
        )

    def record_skill_use(self, name: str, outcome: str, note: str = "") -> str:
        """Log how a learned skill just behaved: `outcome` is 'worked' or
        'failed', with an optional `note` (a changed selector, why it broke). The
        first 'worked' marks the skill verified; the log lets you and later
        sessions see how it last behaved and catch a breaking change. Do this
        after actually running the skill — trust is earned by a real run."""
        skill_dir = self.skills_dir / name
        if not (skill_dir / "SKILL.md").exists():
            raise KeyError(f"no learned skill {name!r} to record against")
        data = record_use(skill_dir, outcome, note)
        self.registry.set_skill_usage(name, data["verified"], tuple(data["uses"]))
        self._on_event("skill_use", name, skill=name, outcome=outcome, note=note)
        state = "verified" if data["verified"] else "unverified"
        return f"recorded {outcome!r} for skill {name!r} (now {state})"
