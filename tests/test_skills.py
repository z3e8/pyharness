"""Skills: learned tools = markdown instructions + optional bundled code (design §6)."""

from __future__ import annotations

import pytest

from pyharness.broker.capabilities.skills import SkillsCapability
from pyharness.broker.dispatch import PermissionDenied
from pyharness.core.session import Session
from pyharness.tools.registry import Registry
from pyharness.tools.skills import (
    _MAX_USES,
    load_skills,
    parse_skill_md,
    read_journal,
    record_use,
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


def test_skills_are_search_only_not_featured(tmp_path):
    write_skill(tmp_path, "rare", "a rarely used procedure", "do it", keywords=("widget",))
    reg = Registry()
    load_skills(reg, tmp_path)
    # not in the default browse — skills don't crowd the common-tools listing
    assert "# rare" not in reg.search("")
    # but fully findable by name, description word, or keyword
    assert "# rare" in reg.search("rare")
    assert "# rare" in reg.search("procedure")
    assert "# rare" in reg.search("widget")


def test_new_skill_is_unverified(tmp_path):
    write_skill(tmp_path, "fresh", "a new procedure", "do the thing")
    reg = Registry()
    load_skills(reg, tmp_path)
    # tagged unverified in the header, and described as a hypothesis
    assert "unverified" in reg.search("fresh")
    details = reg.describe("fresh")
    assert "verified: no" in details


def test_record_use_worked_verifies_and_logs(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("greet", "greet someone", "say hi")
    assert "unverified" in reg.search("greet")

    msg = cap.record_skill_use("greet", "worked", note="clean run")
    assert "verified" in msg and "now verified" in msg

    # on disk (next session) and in this registry, it now reads as verified
    assert read_journal(tmp_path / "greet")["verified"] is True
    assert "unverified" not in reg.search("greet")
    details = reg.describe("greet")
    assert "verified: yes" in details and "clean run" in details


def test_record_use_failed_flags_last_failed(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("scrape", "scrape a site", "load the page")
    cap.record_skill_use("scrape", "failed", note="site blocks headless")

    assert "last-failed" in reg.search("scrape")
    assert "unverified" in reg.search("scrape")  # a failure does not verify
    assert "site blocks headless" in reg.describe("scrape")


def test_record_use_rejects_bad_outcome_and_unknown_skill(tmp_path):
    cap = SkillsCapability(Registry(), tmp_path)
    cap.save_skill("s", "d", "i")
    with pytest.raises(ValueError):
        cap.record_skill_use("s", "maybe")
    with pytest.raises(KeyError):
        cap.record_skill_use("nope", "worked")


def test_journal_is_bounded(tmp_path):
    write_skill(tmp_path, "loop", "repeated", "go")
    for i in range(_MAX_USES + 5):
        record_use(tmp_path / "loop", "worked", note=f"run {i}", now=f"2026-01-01T00:00:{i:02d}")
    uses = read_journal(tmp_path / "loop")["uses"]
    assert len(uses) == _MAX_USES
    assert uses[-1]["note"] == f"run {_MAX_USES + 4}"  # newest kept, oldest dropped


def test_resave_de_verifies_but_keeps_log(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("t", "v1", "old steps")
    cap.record_skill_use("t", "worked", note="worked once")
    assert read_journal(tmp_path / "t")["verified"] is True

    # revising the procedure makes it unproven again, but its history survives
    cap.save_skill("t", "v2", "new steps")
    journal = read_journal(tmp_path / "t")
    assert journal["verified"] is False
    assert any(u.get("note") == "worked once" for u in journal["uses"])
    # a fresh load (next session) reflects the reset
    reloaded = Registry()
    load_skills(reloaded, tmp_path)
    assert "unverified" in reloaded.search("t")


def test_save_skill_requires_approval_by_default(tmp_path):
    skills = tmp_path / "skills"
    denied = Session(tmp_path / "a", skills_dir=skills, unsafe_in_process=True)  # no approver
    with pytest.raises(PermissionDenied):
        denied.broker.namespace()["save_skill"]("x", "d", "i")
    denied.close()

    allowed = Session(tmp_path / "b", skills_dir=skills, approver=lambda *a: True,
                      unsafe_in_process=True)
    assert "saved skill 'x'" in allowed.broker.namespace()["save_skill"]("x", "d", "i")
    allowed.close()
