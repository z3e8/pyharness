---
name: harness-loop
description: Test-drive the pyharness agent from the outside — run a probe task headlessly, inspect what happened with low context cost, fix the harness, re-run. Use when a change needs a real end-to-end run to verify (not just make test), when debugging why a session failed, or when asked to "try a run and see what happens".
---

# The harness loop: run → observe → fix → re-run

You are the coding agent working ON the harness; the pyharness agent runs
INSIDE it. This loop lets you exercise a real session and read the results
without flooding your context.

## 1. Probe

```bash
uv run pyharness run "<small task that exercises the change>" --json
```

- Needs `ANTHROPIC_API_KEY` in `.env` (a real run costs real money —
  `make test` does not and stays the default verification). Cap spend with
  `--budget 1` for cheap probes.
- Each run gets a fresh `.sessions/run-<timestamp>` dir; the digest lands on
  stdout as one JSON object, everything else on stderr.
- **Approvals are denied by default** (no stdin). If the probe legitimately
  needs gated actions (skill writes, package installs, mutating HTTP), add
  `--approve-all` — it approves per-call and never mints grants.
- Exit code = outcome: 0 `answered`, 2 `stopped:max_steps`, 3 `stopped:budget`,
  4 `error`, 5 `aborted`/`empty`.

## 2. Observe — cheapest view first

```bash
uv run pyharness show                    # digest of the latest session (no API key)
uv run pyharness show <name> --transcript  # what it actually did, flattened
uv run pyharness-index --sql "SELECT name, outcome, cost_usd FROM sessions ORDER BY started DESC LIMIT 10"
```

The digest answers "did it work, what did it cost, was anything denied". The
transcript answers "what did it do" — task, agent text, code, outputs, errors,
skill uses, answer. **Never `cat trace.jsonl`**: every `llm_call` entry embeds
the full prompt snapshot, so the raw file is orders of magnitude bigger than
the session; if the transcript isn't enough, filter one event kind, e.g.
`jq -c 'select(.kind=="action_start")' <dir>/trace.jsonl`. The audit trail
(`history` of capability calls, denials) is `<dir>/audit.jsonl`, and
`make verify-audit DIR=<dir>` checks its hash chain.

For watching a probe live: the run prints a `[watch]` URL on stderr
(`http://127.0.0.1:6061`), and `make watch` tails `.sessions/` from outside.

## 3. Fix and re-run

Fix the harness code, run `make test` until green, then repeat the probe.
Delete throwaway `.sessions/run-*` dirs when done if they clutter (they are
gitignored either way).

## Boundary — do not widen it

You (the coding agent) may modify the whole harness. The in-session agent may
not: its self-improvement surface is skills/lessons only (`~/.pyharness/skills`
behind an approval gate), its file writes are jailed to the session workspace,
and the OS sandbox blocks writes to this repo. Keep it that way — never "fix" a
probe failure by loosening policy defaults, the workspace jail, or the sandbox.
A denial in the digest is usually the harness working as designed.
