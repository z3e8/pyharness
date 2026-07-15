"""Maintain and query the session index (see index.py).

    pyharness-index                    # update: scan ./.sessions + all known roots
    pyharness-index --rebuild          # drop everything and re-derive from JSONL
    pyharness-index --sql "SELECT..."  # read-only query, rows printed as JSON
    pyharness-index --schema           # print the tables/views reference

The index is a derived SQLite cache over the durable session record
(trace.jsonl / audit.jsonl / skill journals); deleting it loses nothing. It
lives at `~/.pyharness/index.db` (override with PYHARNESS_INDEX_DB) and is also
updated automatically when an agent session closes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .index import DEFAULT_DB, SCHEMA_HELP, query, update_index

_SKILLS_DIR = "~/.pyharness/skills"  # matches Session's default skills root


def db_path() -> Path:
    return Path(os.environ.get("PYHARNESS_INDEX_DB", DEFAULT_DB)).expanduser()


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        sys.exit(__doc__)

    if args and args[0] == "--schema":
        print(SCHEMA_HELP)
        return

    if args and args[0] == "--sql":
        if len(args) < 2:
            sys.exit('usage: pyharness-index --sql "SELECT ..."')
        try:
            rows = query(db_path(), args[1])
        except Exception as exc:
            sys.exit(f"query failed: {exc}")
        for row in rows:
            print(json.dumps(row, default=str))
        return

    rebuild = "--rebuild" in args
    roots = [Path(".sessions")] if Path(".sessions").is_dir() else []
    counts = update_index(db_path(), roots, skills_dir=_SKILLS_DIR, rebuild=rebuild)
    print(
        f"index at {db_path()}: {counts['roots']} roots, "
        f"{counts['sessions']} sessions ({counts['indexed']} re-indexed)"
    )


if __name__ == "__main__":
    main()
