"""Skills: learned tools = markdown instructions + optional bundled code (design §6)."""

from __future__ import annotations

import os

import pytest

from pyharness.broker.capabilities.skills import SkillsCapability
from pyharness.broker.dispatch import PermissionDenied
from pyharness.core.session import Session
from pyharness.tools.registry import Registry
from pyharness.tools.skills import (
    FEATURED_LIMIT,
    load_skills,
    parse_skill_md,
    write_skill,
)


def test_parse_frontmatter_and_body():
    meta, body = parse_skill_md(
        "---\nname: foo\ndescription: do foo\nkeywords: a, b\n---\n\nStep 1.\nStep 2.\n"
    )
    assert meta == {"name": "foo", "description": "do foo", "keywords": "a, b"}
    assert body == "Step 1.\nStep 2."


def test_human_authored_skill_loads_from_disk(tmp_path):
    write_skill(
        tmp_path, "greet", "Greet a person", "Call hello(name).",
        files={"impl.py": "def hello(name):\n    '''Say hi.'''\n    return f'hi {name}'\n"},
        keywords=("welcome",),
    )
    reg = Registry()
    assert load_skills(reg, tmp_path) == ["greet"]

    # discoverable by description and by keyword, tagged as a learned tool
    assert "# greet" in reg.search("greet")
    assert "# greet" in reg.search("welcome")
    assert "learned" in reg.search("greet")

    # describe reveals the instructions AND the bundled function signature
    details = reg.describe("greet")
    assert "Call hello(name)." in details
    assert "hello(name)" in details and "Say hi." in details

    # use loads the bundled code
    assert reg.use("greet").hello("ada") == "hi ada"


def test_search_does_not_load_skill_code(tmp_path):
    write_skill(tmp_path, "boom", "explodes on import", "n/a",
                files={"x.py": "raise RuntimeError('imported too early')\n"})
    reg = Registry()
    load_skills(reg, tmp_path)
    # browsing the catalog must not import bundled code
    assert "# boom" in reg.search("boom")
    # the failure only surfaces when the code is actually needed; instructions
    # still show because they don't require import
    details = reg.describe("boom")
    assert "n/a" in details and "unavailable" in details


def test_pure_instructions_skill_has_no_functions(tmp_path):
    write_skill(tmp_path, "policy", "refund rules", "Refund within 30 days.")
    reg = Registry()
    load_skills(reg, tmp_path)
    details = reg.describe("policy")
    assert "Refund within 30 days." in details
    assert "Functions:" not in details  # nothing bundled


def test_empty_skills_dir_is_noop(tmp_path):
    assert load_skills(Registry(), tmp_path / "missing") == []


def test_save_skill_capability_registers_now_and_reloads_later(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    msg = cap.save_skill(
        "dedupe", "Dedupe a list", "Call run(items).",
        files={"impl.py": "def run(items):\n    return list(dict.fromkeys(items))\n"},
    )
    assert "saved skill 'dedupe'" in msg

    # usable immediately in this session
    assert reg.use("dedupe").run([1, 1, 2]) == [1, 2]

    # and a fresh registry rediscovers it from disk (next session)
    reloaded = Registry()
    load_skills(reloaded, tmp_path)
    assert reloaded.use("dedupe").run([3, 3]) == [3]


def test_save_skill_rejects_unsafe_name(tmp_path):
    cap = SkillsCapability(Registry(), tmp_path)
    with pytest.raises(ValueError):
        cap.save_skill("../escape", "x", "y")


def test_resave_drops_stale_bundled_code(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("t", "v1", "use old()", files={"a.py": "def old():\n    return 1\n"})
    assert reg.use("t").old() == 1

    # re-save with a renamed helper; the old file must not linger
    cap.save_skill("t", "v2", "use new()", files={"b.py": "def new():\n    return 2\n"})
    assert not (tmp_path / "t" / "a.py").exists()
    mod = Registry(); load_skills(mod, tmp_path)  # fresh load = exactly the last save
    assert mod.use("t").new() == 2
    assert not hasattr(mod.use("t"), "old")


def test_only_recent_skills_are_featured(tmp_path):
    # author one more than the cap; stamp ascending mtimes so order is known
    for i in range(FEATURED_LIMIT + 1):
        d = write_skill(tmp_path, f"s{i:02d}", f"skill {i}", "do it")
        os.utime(d / "SKILL.md", (i, i))
    reg = Registry()
    load_skills(reg, tmp_path)

    featured = {n for n, info in reg._tools.items() if info.featured and info.source == "learned"}
    assert len(featured) == FEATURED_LIMIT
    assert "s00" not in featured  # oldest dropped from the featured set
    assert f"s{FEATURED_LIMIT:02d}" in featured  # newest kept
    # still discoverable by query even when not featured
    assert "# s00" in reg.search("s00")


def test_save_skill_requires_approval_by_default(tmp_path):
    skills = tmp_path / "skills"
    denied = Session(tmp_path / "a", skills_dir=skills)  # no approver
    with pytest.raises(PermissionDenied):
        denied.broker.namespace()["save_skill"]("x", "d", "i")
    denied.close()

    allowed = Session(tmp_path / "b", skills_dir=skills, approver=lambda *a: True)
    assert "saved skill 'x'" in allowed.broker.namespace()["save_skill"]("x", "d", "i")
    allowed.close()
