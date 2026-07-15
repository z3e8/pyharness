"""The post-session reflection pass — the learn half of the self-improvement loop.

After a session's work is done, a *separate* cheap-model pass reads the
transcript and proposes at most one durable improvement: save a new skill,
delta-edit an existing one, record a lesson, or nothing. Design constraints
(direction 5, each mapped to a documented failure mode):

- **Reflector ≠ actor.** A fresh prompt over the trace — the record of what
  actually happened — not the actor's own belief that it went well
  (self-preference bias).
- **Delta edits, never rewrites.** Skill revisions are targeted old→new
  replacements via `skills.edit_skill`; a wholesale regeneration is how
  accumulated procedure detail collapses.
- **Proposals, not authority.** Skill writes route through the broker like any
  agent call — same approval gate (supply-chain sign-off), same audit line —
  and land unverified; trust still only comes from a real run. The reflector
  has no path to journals, outcome classification, or the index (the
  reward-hacking surface): its only writes are the two gated skill ops and the
  lesson counter.
- **Repetition before belief.** Lessons enter as candidates; they surface only
  after being observed in distinct sessions (see lessons.py).
- **Keep the archive.** When the skills root is (or can become) a git repo,
  every reflection-driven skill change is committed with its evidence, so a bad
  self-edit is a revert, not a loss.

Fail-open everywhere: reflection is a bonus pass; no failure in it may break a
session or the CLI exit path.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from . import lessons as lessons_store
from .broker.capabilities.obs import _render_transcript

log = logging.getLogger("pyharness.reflect")

_TIER = "cheap"

_SYSTEM = """\
You are the reflection pass of pyharness, an autonomous Python agent that
learns skills (reusable procedures stored as markdown + optional code). You run
AFTER a session ends. Input: the session transcript (what actually happened —
trust the outputs and errors over the agent's own claims) and the current skill
library. Output: at most ONE durable improvement, as strict JSON, no other text.

Choose exactly one:

{"action": "nothing", "reason": "..."}
  The default. Right when the session taught nothing durable, went smoothly on
  existing skills, or the evidence is too thin (one odd failure is noise).

{"action": "lesson", "text": "<one sentence of operational fact>"}
  A reusable FACT, not a procedure: a site quirk, a rate limit, an auth
  gotcha. Must be evidenced in the transcript and useful beyond this task.

{"action": "edit_skill", "name": "<existing skill>", "reason": "...",
 "edits": [{"old": "<exact text from its instructions, occurring once>",
            "new": "<replacement>"}]}
  When the transcript shows a specific step of an EXISTING skill is wrong or
  missing (a changed selector, a new required field). Edits are surgical
  replacements; keep everything you are not correcting.

{"action": "save_skill", "name": "<lowercase-hyphens>", "description": "...",
 "reason": "...", "instructions": "<markdown procedure>",
 "check": "<one line: how a run confirms it worked>",
 "keywords": ["..."]}
  Only when the session completed a multi-step procedure likely to recur AND no
  existing skill covers it. Write instructions from what actually worked, at the
  level of detail the transcript supports; include concrete endpoints/selectors
  observed. The check must be verifiable from within a run (an assertion, a
  re-fetch, an expected state).

Rules: propose nothing speculative — every claim must trace to the transcript.
Prefer "nothing" over a weak proposal. Never rewrite a whole skill via edits.
JSON only."""


def reflect(session, *, apply: bool = True) -> str | None:
    """Run the reflection pass over `session`'s trace. Returns a short human
    summary of what was proposed/applied, or None when there was nothing to do.
    Never raises: any failure logs at debug and returns None."""
    try:
        return _reflect(session, apply=apply)
    except Exception:  # noqa: BLE001 — reflection is a bonus pass, never a blocker
        log.debug("reflection failed", exc_info=True)
        return None


def _reflect(session, *, apply: bool) -> str | None:
    if not session.messages:
        return None  # nothing ran
    if session.budget.remaining() <= 0:
        return None  # don't spend past an exhausted budget on a bonus pass

    transcript = _render_transcript(session.trace.path)
    skills = _skill_catalog(session)
    prompt = (
        f"CURRENT SKILL LIBRARY:\n{skills or '(empty)'}\n\n"
        f"SESSION TRANSCRIPT:\n{transcript}\n\n"
        "Respond with the single JSON action."
    )
    completion = session.llm.complete(
        system=_SYSTEM, messages=[{"role": "user", "content": prompt}], tier=_TIER
    )
    proposal = _parse_json(completion.text)
    if proposal is None:
        log.debug("reflection returned unparseable output: %r", completion.text[:200])
        return None

    action = proposal.get("action")
    session.trace.record(
        "reflection", proposal.get("reason") or "", proposal=_clip_proposal(proposal)
    )
    if action == "nothing" or action is None:
        return None
    if not apply:
        return f"reflection proposed {action} (not applied)"

    if action == "lesson":
        entry = lessons_store.record(
            session.lessons_path, proposal["text"], session=session.workspace.root.name
        )
        state = "established" if entry["count"] >= lessons_store.ESTABLISHED_AT else "candidate"
        return f"reflection recorded a lesson ({state}): {entry['text']}"

    if action == "edit_skill":
        # Through the broker: same approval gate and audit line as the agent's own
        # skill edits. A denial is a decision, not an error.
        try:
            session.broker.call(
                "skills", "edit_skill",
                name=proposal["name"], edits=proposal.get("edits") or [],
                reason=proposal.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001 — denial or bad edit: report, don't raise
            return f"reflection proposed editing skill {proposal.get('name')!r}: not applied ({exc})"
        _commit_skills(session, f"reflect: edit skill {proposal['name']}", proposal)
        return f"reflection edited skill {proposal['name']!r} — unverified until it runs"

    if action == "save_skill":
        try:
            session.broker.call(
                "skills", "save_skill",
                name=proposal["name"],
                description=proposal.get("description", ""),
                instructions=proposal.get("instructions", ""),
                keywords=tuple(proposal.get("keywords") or ()),
                check=proposal.get("check"),
            )
        except Exception as exc:  # noqa: BLE001
            return f"reflection proposed skill {proposal.get('name')!r}: not applied ({exc})"
        _commit_skills(session, f"reflect: save skill {proposal['name']}", proposal)
        return f"reflection saved skill {proposal['name']!r} — unverified until it runs"

    log.debug("reflection proposed unknown action %r", action)
    return None


def _skill_catalog(session) -> str:
    lines = []
    for info in session.registry._tools.values():
        if info.source != "learned":
            continue
        state = "verified" if info.verified else "unverified"
        lines.append(f"- {info.name} ({state}): {info.summary}")
    return "\n".join(lines)


def _parse_json(text: str) -> dict | None:
    """The first JSON object in the reply (models sometimes wrap it in a fence)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clip_proposal(proposal: dict) -> dict:
    return {k: (v[:500] if isinstance(v, str) else v) for k, v in proposal.items()}


def _commit_skills(session, message: str, proposal: dict) -> None:
    """Version the skills root so a bad self-edit is a revert, not a loss.
    Initializes a git repo there on first use; every failure is swallowed —
    versioning is a safety net, not a dependency."""
    skills_dir = Path(session.skills_dir)
    try:
        if not (skills_dir / ".git").exists():
            subprocess.run(
                ["git", "init", "-q"], cwd=skills_dir, check=True, capture_output=True
            )
        subprocess.run(["git", "add", "-A"], cwd=skills_dir, check=True, capture_output=True)
        evidence = proposal.get("reason", "")
        body = f"session: {session.workspace.root.name}\nevidence: {evidence}"
        subprocess.run(
            ["git", "-c", "user.name=pyharness-reflect",
             "-c", "user.email=reflect@pyharness.local",
             "commit", "-q", "-m", message, "-m", body],
            cwd=skills_dir, check=True, capture_output=True,
        )
    except Exception:  # noqa: BLE001
        log.debug("skills git versioning failed", exc_info=True)
