"""Layer 3: skill checks, delta edits, lessons, and the reflection pass."""

from __future__ import annotations

import json

import pytest

from pyharness import lessons as lessons_store
from pyharness.core.session import Session
from pyharness.llm.client import Completion
from pyharness.tools.registry import Registry
from pyharness.tools.skills import (
    edit_skill_md,
    parse_skill_md,
    read_journal,
    record_use,
    register_skill_dir,
    write_skill,
)


class ScriptedLLM:
    """Fake LLM returning canned replies in order (reflection is the last call)."""

    def __init__(self, replies=("",)):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, *, system=None, messages, tier="cheap", tools=None,
                 max_tokens=None, on_token=None, on_thinking=None, on_attempt=None,
                 cache_anchor=None, total_deadline_s=None):
        self.calls.append({"system": system, "messages": messages, "tier": tier})
        reply = self.replies.pop(0) if self.replies else ""
        return Completion(reply, [], [{"type": "text", "text": reply}])


# --- skill checks + delta edits --------------------------------------------


def test_write_skill_persists_check_and_registry_surfaces_it(tmp_path):
    d = write_skill(tmp_path, "s1", "desc", "steps", check="re-fetch the page; expect 200")
    meta, _ = parse_skill_md((d / "SKILL.md").read_text())
    assert meta["check"] == "re-fetch the page; expect 200"

    reg = Registry()
    register_skill_dir(reg, d)
    described = reg.describe("s1")
    assert "re-fetch the page; expect 200" in described


def test_edit_skill_md_applies_unique_delta_and_unverifies(tmp_path):
    write_skill(tmp_path, "s1", "desc", "click #old-button\nthen submit", check="c")
    record_use(tmp_path / "s1", "worked")
    assert read_journal(tmp_path / "s1")["verified"] is True

    edit_skill_md(tmp_path, "s1", [{"old": "#old-button", "new": "#new-button"}])
    meta, body = parse_skill_md((tmp_path / "s1" / "SKILL.md").read_text())
    assert "#new-button" in body and "#old-button" not in body
    assert meta["check"] == "c"  # frontmatter untouched
    journal = read_journal(tmp_path / "s1")
    assert journal["verified"] is False  # a revision is unproven again
    assert journal["uses"]  # but the history survives


def test_edit_skill_md_rejects_ambiguous_or_missing_old(tmp_path):
    write_skill(tmp_path, "s1", "desc", "step A\nstep A")
    with pytest.raises(ValueError, match="2 times"):
        edit_skill_md(tmp_path, "s1", [{"old": "step A", "new": "step B"}])
    with pytest.raises(ValueError, match="0 times"):
        edit_skill_md(tmp_path, "s1", [{"old": "nope", "new": "x"}])
    with pytest.raises(KeyError):
        edit_skill_md(tmp_path, "missing", [{"old": "a", "new": "b"}])


def test_edit_skill_preserves_bundled_files(tmp_path):
    write_skill(tmp_path, "s1", "desc", "use helper()", files={"helper.py": "def helper():\n    return 1\n"})
    edit_skill_md(tmp_path, "s1", [{"old": "helper()", "new": "helper() twice"}])
    assert (tmp_path / "s1" / "helper.py").exists()


# --- lessons ----------------------------------------------------------------


def test_lessons_dedupe_count_and_establish(tmp_path):
    path = tmp_path / "lessons.json"
    e1 = lessons_store.record(path, "Greenhouse boards paginate at 100.", session="a")
    assert e1["count"] == 1
    # same session re-observing: still one observation
    assert lessons_store.record(path, "Greenhouse boards paginate at 100.", session="a")["count"] == 1
    assert lessons_store.established(path) == []
    # a different session establishes it (normalization catches formatting drift)
    e2 = lessons_store.record(path, "greenhouse boards paginate at 100", session="b")
    assert e2["count"] == 2
    assert [e["text"] for e in lessons_store.established(path)] == [
        "Greenhouse boards paginate at 100."
    ]


# --- the reflection pass -----------------------------------------------------


def _session(tmp_path, replies, approver=None):
    llm = ScriptedLLM(replies)
    s = Session(tmp_path / "sess", llm=llm, skills_dir=tmp_path / "skills",
                approver=approver)
    return s, llm


def test_reflect_skips_empty_session(tmp_path):
    s, llm = _session(tmp_path, ['{"action": "lesson", "text": "x"}'])
    try:
        assert s.reflect() is None  # no messages -> no LLM call
        assert llm.calls == []
    finally:
        s.close()


def test_reflect_nothing_and_bad_json_are_quiet(tmp_path):
    for reply in ('{"action": "nothing", "reason": "routine"}', "not json at all"):
        s, _ = _session(tmp_path, [reply])
        try:
            s.messages.append({"role": "user", "content": "t"})
            s.trace.record("task", "do the thing")
            assert s.reflect() is None
        finally:
            s.close()


def test_reflect_records_lesson(tmp_path):
    reply = json.dumps({"action": "lesson", "text": "the API rate-limits at 10/min"})
    s, llm = _session(tmp_path, [reply])
    try:
        s.messages.append({"role": "user", "content": "t"})
        s.trace.record("task", "do the thing")
        summary = s.reflect()
        assert "lesson" in summary and "candidate" in summary
        (entry,) = lessons_store.load(s.lessons_path)
        assert entry["text"] == "the API rate-limits at 10/min"
        # the reflector call went to the cheap tier with the transcript
        assert llm.calls[-1]["tier"] == "cheap"
        assert "do the thing" in llm.calls[-1]["messages"][0]["content"]
    finally:
        s.close()


def test_reflect_save_skill_routes_through_approval(tmp_path):
    proposal = json.dumps({
        "action": "save_skill", "name": "apply-greenhouse",
        "description": "apply to a greenhouse job", "reason": "recurring",
        "instructions": "1. fetch the board", "check": "confirmation page shows 'submitted'",
        "keywords": ["jobs"],
    })
    # Denied: no approver
    s, _ = _session(tmp_path, [proposal])
    try:
        s.messages.append({"role": "user", "content": "t"})
        s.trace.record("task", "apply")
        summary = s.reflect()
        assert "not applied" in summary
        assert not (tmp_path / "skills" / "apply-greenhouse").exists()
    finally:
        s.close()

    # Approved: the skill lands, unverified, with its check; skills repo committed
    s2, _ = _session(tmp_path, [proposal], approver=lambda req: True)
    try:
        s2.messages.append({"role": "user", "content": "t"})
        s2.trace.record("task", "apply")
        summary = s2.reflect()
        assert "saved skill 'apply-greenhouse'" in summary
        skill_dir = tmp_path / "skills" / "apply-greenhouse"
        meta, _ = parse_skill_md((skill_dir / "SKILL.md").read_text())
        assert meta["check"] == "confirmation page shows 'submitted'"
        assert read_journal(skill_dir)["verified"] is False
        assert (tmp_path / "skills" / ".git").exists()  # archived, revertable
    finally:
        s2.close()


def test_reflect_edit_skill_applies_delta(tmp_path):
    write_skill(tmp_path / "skills", "apply-greenhouse", "d", "click #submit-v1")
    proposal = json.dumps({
        "action": "edit_skill", "name": "apply-greenhouse", "reason": "selector changed",
        "edits": [{"old": "#submit-v1", "new": "#submit-v2"}],
    })
    s, _ = _session(tmp_path, [proposal], approver=lambda req: True)
    try:
        s.messages.append({"role": "user", "content": "t"})
        s.trace.record("task", "apply")
        summary = s.reflect()
        assert "edited skill" in summary
        _, body = parse_skill_md((tmp_path / "skills" / "apply-greenhouse" / "SKILL.md").read_text())
        assert "#submit-v2" in body
    finally:
        s.close()


def test_agent_edit_skill_builtin_is_gated(tmp_path):
    """The agent-facing edit_skill op requires approval like save_skill."""
    from pyharness.broker.dispatch import PermissionDenied

    write_skill(tmp_path / "skills", "s1", "d", "old text here")
    s, _ = _session(tmp_path, [])
    try:
        with pytest.raises(PermissionDenied):
            s.broker.call("skills", "edit_skill", name="s1",
                          edits=[{"old": "old text here", "new": "new"}])
    finally:
        s.close()
