"""The session index — a derived, rebuildable SQLite view over the JSONL record.

The durable record stays canonical and append-only: `trace.jsonl` (the
orchestration — tasks, LLM calls, cells, answers), `audit.jsonl` (every
capability call, hash-chained), and each skill's `journal.json`. This module
derives one queryable SQLite file from all of it, so the agent (via the
`stats` builtin) and the human (via `pyharness-index --sql` or any sqlite
client) can ask aggregate questions — success rates, cost trends, error
taxonomy, per-skill reliability — without rereading transcripts.

Design rules:
- **The DB is a cache, never truth.** It can be deleted and rebuilt from the
  JSONL at any time (`rebuild=True`); nothing writes state that exists only
  here. That also means no migrations: a schema change bumps `_SCHEMA_VERSION`
  and stale DBs are rebuilt on next update.
- **Incremental by session dir.** A session dir whose files' (size, mtime)
  fingerprint is unchanged is skipped; a changed one has its rows replaced
  wholesale. Session files are small; per-dir re-parse beats offset bookkeeping.
- **Multi-root.** Every sessions root ever indexed is remembered in `roots`,
  so one global DB (`~/.pyharness/index.db`) spans projects and re-indexing
  covers all of them.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..util import ensure_private_dir
from .transcript import iter_jsonl, session_digest

DEFAULT_DB = "~/.pyharness/index.db"

_SCHEMA_VERSION = "4"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS roots (root TEXT PRIMARY KEY, last_scan REAL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,        -- session dir path (unique across roots)
    name TEXT,                  -- dir basename, e.g. cli-20260714-221500
    project TEXT,               -- the project the session ran in (parent of .sessions)
    started REAL, ended REAL,   -- unix timestamps (first/last trace entry)
    task TEXT,                  -- first task of the session (truncated)
    tasks INTEGER,              -- user turns
    outcome TEXT,               -- answered | stopped:max_steps | stopped:budget |
                                --   error | aborted | empty
    answer TEXT,                -- final answer (truncated)
    steps INTEGER,              -- code cells run
    errors INTEGER,
    failed_actions INTEGER,     -- broker actions that raised (action_end ok=False)
    llm_calls INTEGER,
    cost_usd REAL,
    actions INTEGER,            -- audited capability calls (outcome records)
    denials INTEGER,            -- calls denied or unapproved
    aborted_actions INTEGER,    -- audit starts with no outcome record (hard kill)
    events INTEGER,             -- capability event records (e.g. profile_saved), not calls
    fingerprint TEXT            -- size/mtime of source files, for incremental skip
);
CREATE TABLE IF NOT EXISTS llm_calls (
    session_id TEXT, ts REAL, model TEXT, tier TEXT,
    cost_usd REAL, latency_s REAL, input_tokens INTEGER, output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS cells (
    session_id TEXT, ts REAL, code TEXT, output_chars INTEGER
);
CREATE TABLE IF NOT EXISTS actions (
    session_id TEXT, ts REAL, action TEXT, phase TEXT, ok INTEGER,
    decision TEXT, approved INTEGER, error TEXT, args TEXT
);
CREATE TABLE IF NOT EXISTS skill_uses (
    session_id TEXT, ts REAL, skill TEXT, outcome TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS errors (session_id TEXT, ts REAL, text TEXT);
CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY, verified INTEGER,
    uses INTEGER, worked INTEGER, failed INTEGER,
    last_used TEXT, last_outcome TEXT, last_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);
CREATE INDEX IF NOT EXISTS idx_actions_action ON actions(action);
CREATE INDEX IF NOT EXISTS idx_skill_uses_skill ON skill_uses(skill);

CREATE VIEW IF NOT EXISTS skill_stats AS
    SELECT skill,
           COUNT(*) AS uses,
           SUM(outcome = 'worked') AS worked,
           SUM(outcome = 'failed') AS failed,
           ROUND(AVG(outcome = 'worked'), 3) AS success_rate,
           MAX(ts) AS last_used
    FROM skill_uses GROUP BY skill;

-- Cost of run N vs run 1, per skill: is repetition getting cheaper?
CREATE VIEW IF NOT EXISTS skill_run_costs AS
    SELECT su.skill, su.session_id, s.started, s.cost_usd, s.outcome,
           ROW_NUMBER() OVER (PARTITION BY su.skill ORDER BY s.started) AS run_n
    FROM (SELECT DISTINCT skill, session_id FROM skill_uses) su
    JOIN sessions s ON s.id = su.session_id;

CREATE VIEW IF NOT EXISTS error_taxonomy AS
    SELECT action, COUNT(*) AS failures, MAX(ts) AS last_seen
    FROM actions WHERE ok = 0 GROUP BY action ORDER BY failures DESC;

CREATE VIEW IF NOT EXISTS session_costs AS
    SELECT DATE(started, 'unixepoch') AS day,
           COUNT(*) AS sessions, ROUND(SUM(cost_usd), 4) AS cost_usd
    FROM sessions GROUP BY day ORDER BY day;
"""

# The schema description handed to the agent (the `stats` builtin's docstring
# embeds it) and printed by `pyharness-index --schema`. Kept by hand so it reads
# as documentation, not a dump; update it with any schema change.
SCHEMA_HELP = """\
Tables (one row per...):
  sessions(id, name, project, started, ended, task, tasks, outcome, answer,
           steps, errors, failed_actions, llm_calls, cost_usd,
           actions, denials, aborted_actions, events)             -- one session
      outcome: answered | stopped:max_steps | stopped:budget | error | aborted | empty
  llm_calls(session_id, ts, model, tier, cost_usd, latency_s,
            input_tokens, output_tokens)                          -- one LLM call
  cells(session_id, ts, code, output_chars)                       -- one code cell
  actions(session_id, ts, action, phase, ok, decision, approved, error, args)
                  -- one audit record; phase 'start' is the pre-execution intent
                  -- record, 'end' the outcome (filter phase='end' to count calls)
  skill_uses(session_id, ts, skill, outcome, note)                -- one recorded use
  errors(session_id, ts, text)                                    -- one turn error
  skills(name, verified, uses, worked, failed, last_used, last_outcome, last_note)
                                                    -- current journal state
Views:
  skill_stats(skill, uses, worked, failed, success_rate, last_used)
  skill_run_costs(skill, session_id, started, cost_usd, outcome, run_n)
      -- cost of run N vs run 1 per skill
  error_taxonomy(action, failures, last_seen)
  session_costs(day, sessions, cost_usd)
Timestamps are unix seconds: DATE(ts, 'unixepoch') gives the day."""

_TRUNC = 400  # stored text snippet cap (task/answer/code)


def _clip(text: object, limit: int = _TRUNC) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _fingerprint(session_dir: Path) -> str:
    parts = []
    for name in ("trace.jsonl", "audit.jsonl"):
        f = session_dir / name
        st = f.stat() if f.exists() else None
        parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}" if st else f"{name}:-")
    return "|".join(parts)


def _project_of(session_dir: Path) -> str:
    parent = session_dir.parent
    return str(parent.parent if parent.name == ".sessions" else parent)


def open_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    ensure_private_dir(path.parent)  # ~/.pyharness (0700): the index is not world-readable
    new_db = not path.exists()
    conn = sqlite3.connect(path)
    if new_db:
        # The index can hold session args/answers — keep it owner-only (0644 by
        # default umask would leave it world-readable). Only on create: don't
        # fight a mode a user deliberately set on an existing db.
        try:
            path.chmod(0o600)
        except OSError:
            pass
    # Parallel sessions each open this one shared index. Without these two
    # pragmas a concurrent writer hits "database is locked", the fail-open
    # caller swallows it, and that session silently never indexes. WAL lets a
    # reader and a writer coexist; busy_timeout makes a blocked writer wait for
    # the lock (up to 5s) instead of erroring immediately.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    if _has_meta(conn):
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is not None and row[0] != _SCHEMA_VERSION:
            _drop_all(conn)  # stale schema: the DB is a cache — rebuild, don't migrate
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (_SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def _has_meta(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    return row is not None


def _drop_all(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table','view','index')"
    ).fetchall()
    for kind, name in rows:
        if name.startswith("sqlite_"):
            continue
        conn.execute(f"DROP {kind.upper()} IF EXISTS {name}")
    conn.commit()


def update_index(
    db_path: str | Path,
    roots: list[str | Path] = (),
    *,
    skills_dir: str | Path | None = None,
    rebuild: bool = False,
) -> dict:
    """Bring the index up to date. `roots` are sessions directories (each holding
    session dirs) to add to the remembered set; every remembered root is
    re-scanned. Unchanged session dirs are skipped via fingerprint. Returns
    counts: {"roots": n, "sessions": scanned, "indexed": changed}."""
    conn = open_db(db_path)
    try:
        if rebuild:
            # Remembered roots survive a rebuild — they are the map of where
            # sessions live, not derived data.
            kept = conn.execute("SELECT root, last_scan FROM roots").fetchall()
            _drop_all(conn)
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            conn.executemany("INSERT OR REPLACE INTO roots VALUES (?, ?)", kept)
        now = time.time()
        for root in roots:
            root = Path(root).expanduser().resolve()
            if root.is_dir():
                conn.execute(
                    "INSERT OR REPLACE INTO roots (root, last_scan) VALUES (?, ?)",
                    (str(root), now),
                )
        all_roots = [r for (r,) in conn.execute("SELECT root FROM roots").fetchall()]

        scanned = indexed = 0
        for root in all_roots:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            for session_dir in sorted(p for p in root_path.iterdir() if p.is_dir()):
                if not (session_dir / "trace.jsonl").exists():
                    continue
                scanned += 1
                if _index_session(conn, session_dir):
                    indexed += 1
        if skills_dir is not None:
            _index_skills(conn, Path(skills_dir).expanduser())
        conn.commit()
        return {"roots": len(all_roots), "sessions": scanned, "indexed": indexed}
    finally:
        conn.close()


def _index_session(conn: sqlite3.Connection, session_dir: Path) -> bool:
    """(Re)index one session dir; returns whether it was (re)parsed."""
    sid = str(session_dir.resolve())
    fingerprint = _fingerprint(session_dir)
    row = conn.execute("SELECT fingerprint FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row is not None and row[0] == fingerprint:
        return False

    for table in ("llm_calls", "cells", "actions", "skill_uses", "errors"):
        conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    pending_cell: tuple | None = None
    for e in iter_jsonl(session_dir / "trace.jsonl"):
        ts = e.get("ts")
        kind = e.get("kind")
        if kind == "llm_call":
            conn.execute(
                "INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?)",
                (sid, ts, e.get("model"), e.get("tier"), e.get("cost_usd"),
                 e.get("latency_s"), e.get("input_tokens"), e.get("output_tokens")),
            )
        elif kind == "code":
            if pending_cell is not None:
                conn.execute("INSERT INTO cells VALUES (?,?,?,?)", pending_cell)
            pending_cell = (sid, ts, _clip(e.get("text")), None)
        elif kind == "output":
            if pending_cell is not None:
                conn.execute(
                    "INSERT INTO cells VALUES (?,?,?,?)",
                    pending_cell[:3] + (len(e.get("text") or ""),),
                )
                pending_cell = None
        elif kind == "error":
            conn.execute(
                "INSERT INTO errors VALUES (?,?,?)", (sid, ts, _clip(e.get("text")))
            )
        elif kind == "skill_use":
            conn.execute(
                "INSERT INTO skill_uses VALUES (?,?,?,?,?)",
                (sid, ts, e.get("skill"), e.get("outcome"), e.get("note")),
            )
    if pending_cell is not None:
        conn.execute("INSERT INTO cells VALUES (?,?,?,?)", pending_cell)

    for e in iter_jsonl(session_dir / "audit.jsonl"):
        ok = e.get("ok")
        approved = e.get("approved")
        conn.execute(
            "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, e.get("ts"), e.get("action"), e.get("phase"),
             None if ok is None else int(bool(ok)),
             e.get("decision"), None if approved is None else int(bool(approved)),
             _clip(e.get("error")) or None, _clip(e.get("args")) or None),
        )

    # The summary row shares its counting and outcome vocabulary with the CLI's
    # digest (`run --json` / `show`) — one scan there, one set of semantics.
    d = session_digest(session_dir)
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, session_dir.name, _project_of(session_dir), d["started"], d["ended"],
         None if d["task"] is None else _clip(d["task"]),
         d["tasks"], d["outcome"],
         None if d["answer"] is None else _clip(d["answer"]),
         d["steps"], d["errors"], d["failed_actions"], d["llm_calls"],
         d["cost_usd"], d["actions"], d["denials"], d["aborted_actions"],
         d["events"], fingerprint),
    )
    return True


def _index_skills(conn: sqlite3.Connection, skills_dir: Path) -> None:
    """Snapshot every skill journal into the `skills` table (current state,
    replacing the previous snapshot — history lives in `skill_uses`)."""
    from ..tools.skills import read_journal

    conn.execute("DELETE FROM skills")
    if not skills_dir.is_dir():
        return
    for child in sorted(skills_dir.iterdir()):
        if not (child / "SKILL.md").exists():
            continue
        journal = read_journal(child)
        uses = journal["uses"]
        last = uses[-1] if uses else {}
        conn.execute(
            "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
            (child.name, int(journal["verified"]), len(uses),
             sum(u.get("outcome") == "worked" for u in uses),
             sum(u.get("outcome") == "failed" for u in uses),
             last.get("at"), last.get("outcome"), last.get("note")),
        )


def query(
    db_path: str | Path, sql: str, *, params: tuple = (), limit: int = 200
) -> list[dict]:
    """Run read-only SQL against the index and return rows as dicts (capped at
    `limit`). The connection is opened read-only and an authorizer denies
    ATTACH/DETACH, so agent SQL can read this DB and nothing else."""
    path = Path(db_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"no session index at {path} — it is built automatically when a "
            "session closes, or run `pyharness-index`"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_readonly_authorizer)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchmany(limit)
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _readonly_authorizer(action: int, *args) -> int:
    # mode=ro + query_only already block writes; the authorizer's job is to stop
    # SQL from reaching outside this file (ATTACH can open other DBs read-only).
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK
