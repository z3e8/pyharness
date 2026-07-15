"""Manage the MCP servers the agent mounts as Python tools.

    pyharness-mcp add NAME --command CMD [--arg=A]...    # local server
        (use the --arg=-y form for values that start with a dash)
    pyharness-mcp add NAME --url URL                     # remote server
        common options: --env K=V / --header K=V (values should be vault refs
        like secret:NAME — a cleartext credential is refused), --summary TEXT,
        --keyword K (repeatable), --category C
    pyharness-mcp list
    pyharness-mcp rm NAME

Edits `.mcp.json` in the current directory (override with PYHARNESS_MCP_CONFIG),
the same file the `pyharness` session mounts at startup. Servers connect
lazily, so adding one here never contacts it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SECRET_PREFIX = "secret:"


def _config_path() -> Path:
    return Path(os.environ.get("PYHARNESS_MCP_CONFIG", ".mcp.json"))


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        sys.exit(f"could not parse {path}: {exc}")


def _save(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, indent=2) + "\n")


def _pairs(items: list[str], flag: str) -> dict:
    mapping = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            sys.exit(f"{flag} expects K=V, got {item!r}")
        mapping[key] = value
    return mapping


def _check_secret_refs(mapping: dict, field: str) -> None:
    for key, value in mapping.items():
        if not value.startswith(_SECRET_PREFIX):
            sys.exit(
                f"--{field} {key} is not a '{_SECRET_PREFIX}NAME' vault reference; "
                "refusing to write a possible cleartext credential to the config. "
                f"Store it with pyharness-vault, then pass {key}={_SECRET_PREFIX}NAME."
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyharness-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="add a server to the config")
    add.add_argument("name")
    add.add_argument("--command", help="local server: the executable to run")
    add.add_argument("--arg", action="append", default=[], dest="args",
                     help="local server: one argument (repeatable; use --arg=-y for dash values)")
    add.add_argument("--url", help="remote server: the Streamable HTTP endpoint")
    add.add_argument("--env", action="append", default=[], help="K=V (V a secret: ref)")
    add.add_argument("--header", action="append", default=[], help="K=V (V a secret: ref)")
    add.add_argument("--summary", help="one-line description shown by search_tools")
    add.add_argument("--keyword", action="append", default=[], dest="keywords",
                     help="discovery keyword (repeatable)")
    add.add_argument("--category", help="discovery category (e.g. vcs, chat)")

    sub.add_parser("list", help="list configured servers")

    rm = sub.add_parser("rm", help="remove a server from the config")
    rm.add_argument("name")
    return parser


def main() -> None:
    opts = _parser().parse_args()
    path = _config_path()
    config = _load(path)
    servers = config.setdefault("mcpServers", {})

    if opts.cmd == "list":
        if not servers:
            print(f"no servers in {path}")
            return
        for name, spec in servers.items():
            target = spec.get("command") or spec.get("url") or "?"
            summary = f"  # {spec['summary']}" if spec.get("summary") else ""
            print(f"{name}: {target}{summary}")
        return

    if opts.cmd == "rm":
        if opts.name not in servers:
            sys.exit(f"no server named {opts.name!r} in {path}")
        del servers[opts.name]
        _save(path, config)
        print(f"removed {opts.name!r} ({len(servers)} server(s) remaining in {path})")
        return

    # add
    if bool(opts.command) == bool(opts.url):
        sys.exit("specify exactly one of --command (local) or --url (remote)")
    if opts.name in servers:
        sys.exit(f"a server named {opts.name!r} already exists in {path}; rm it first")
    env = _pairs(opts.env, "--env")
    headers = _pairs(opts.header, "--header")
    _check_secret_refs(env, "env")
    _check_secret_refs(headers, "header")
    spec = {
        key: value
        for key, value in {
            "command": opts.command,
            "args": opts.args,
            "url": opts.url,
            "env": env,
            "headers": headers,
            "summary": opts.summary,
            "keywords": opts.keywords,
            "category": opts.category,
        }.items()
        if value
    }
    servers[opts.name] = spec
    _save(path, config)
    print(f"added {opts.name!r} to {path} (mounts lazily on the next session)")


if __name__ == "__main__":
    main()
