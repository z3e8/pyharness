"""Capture live pages into the corpus, so scoring replays from disk.

**Why replay exists here.** Not benchmark reproducibility — nobody is going to
re-run this suite to audit the number, and designing for that would buy a corpus
tiering scheme nobody needs. The reason is narrower and actually load-bearing for
the person maintaining this: when the score moves, you have to be able to say
whether *your code* moved it or whether a site was redesigned overnight. Pinning
the pages removes the second explanation. That is the whole justification, and it
is why this is one flag and one directory rather than a subsystem.

Drift measurement, if it is ever wanted, is free and needs nothing built: capture
again later and diff against what is committed.

**Capture goes through the harness, not around it.** This drives the broker's own
gated path — policy, audit, egress guard and all — rather than reaching for
`httpx` directly. A corpus fetched by a side channel would be a corpus the
harness never vetted, which is a strange foundation for a suite about what the
harness vets.

**But through `http.request`, not `web.fetch`.** The plan named `web.fetch(save=…)`
on the strength of its docstring ("the full content lands in the workspace").
Measured, that is not what lands: `web.fetch` sets `extract_text=True`, so what
reaches disk is the *reduced markdown*, not the page. On this suite's own hostile
invoice it is 1497 bytes against the response's real 2988 — tables flattened,
comments and attributes gone, links and forms peeled off into a structure that is
not saved at all. Replaying that would serve markdown as if it were HTML and
re-extract it a second time, so the corpus would not reproduce the page it was
captured from. `http.request` with extraction off writes the response bytes
verbatim, which is the only thing a replay corpus can be built out of.
`web.fetch` is itself a thin wrapper over `http.request`, so this is the same
broker path with one argument different — not a way around the gate.

A page that captures as an empty or near-empty body is *signal*, not a failure:
it means the content is JavaScript-rendered and that task needs the browser
capability rather than `web.fetch`. The caller is told the byte count so the
distinction is visible rather than silently baked into the corpus.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .server import CORPUS


@dataclass(frozen=True)
class Captured:
    name: str
    url: str
    bytes: int
    path: Path

    @property
    def looks_javascript_rendered(self) -> bool:
        """A body too small to be the page a human sees. Worth surfacing: the
        remedy is a different capability, not a retry."""
        return self.bytes < 512


def capture(sources: dict[str, str], *, into: Path = CORPUS) -> list[Captured]:
    """Fetch each `{corpus_name: url}` through the harness and save it into `into`.

    Runs no model: this drives the `web` capability directly through the broker,
    the same object the agent's `use_tool("web")` returns. So it needs no API key
    and costs nothing.

    The budget is left unlimited rather than pinned at zero. `Budget.check()`
    gates *every* brokered action on cumulative spend, not just LLM calls, so a
    zero limit refuses the very first fetch — `spent >= limit` is already true at
    0. A limit of zero is therefore "do nothing", not "spend nothing".
    """
    from pyharness.core.session import Session

    into.mkdir(parents=True, exist_ok=True)
    captured: list[Captured] = []
    root = Path(tempfile.mkdtemp(prefix="pyharness-capture-"))
    try:
        session = Session(
            root / "session",
            # Capture is a deliberate operator action against URLs the operator
            # typed, so the prompts a headless deny would produce are noise.
            # Nothing here is scored; the scoring runs replay against disk.
            approver=lambda request: _allow(),
            skills_dir=root / "skills",
            index_db=None,
            mcp_config=None,
        )
        try:
            http = session.broker.as_tool_module("http")
            for name, url in sources.items():
                # `extract_text` left off (its default): the response body is
                # written verbatim. See the module docstring for why the
                # extracted form is unusable as a replay corpus.
                http.request(None, "GET", url, save=name)
                saved = Path(session.workspace.path(name))
                target = into / name
                shutil.copyfile(saved, target)
                captured.append(
                    Captured(
                        name=name,
                        url=url,
                        bytes=target.stat().st_size,
                        path=target,
                    )
                )
        finally:
            session.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return captured


def _allow():
    from pyharness.broker.dispatch import ApprovalOutcome

    return ApprovalOutcome.ONCE
