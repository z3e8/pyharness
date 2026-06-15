from __future__ import annotations

from pathlib import Path

from ...tools.registry import Registry
from ...tools.skills import register_skill_dir, write_skill


class SkillsCapability:
    """Lets the agent author a reusable skill at runtime (design §6).

    A skill packages a repeatable procedure — markdown instructions plus optional
    bundled .py modules — under the skills root. It is written parent-side (so it
    works out-of-process), registered immediately for this session, and reloaded
    automatically in later ones. The agent finds it with `search_tools()`, reads
    it with `describe_tool()`, and loads its code with `use_tool()`."""

    name = "skills"

    def __init__(self, registry: Registry, skills_dir: str | Path):
        self.registry = registry
        self.skills_dir = Path(skills_dir)

    def exports(self) -> dict:
        return {"save_skill": self.save_skill}

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
        n = len(files or {})
        return f"saved skill {name!r} ({n} bundled file{'s' * (n != 1)}) to {skill_dir}"
