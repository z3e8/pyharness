from __future__ import annotations

import json
from dataclasses import dataclass


class RemoteError(Exception):
    """A broker error normalized for the trip back to the child.

    Broker exceptions are re-raised inside the failing cell, but they cross the
    pickle channel first, and `BaseException.__reduce__` reconstructs an
    exception as `type(exc)(*exc.args)`. Any exception whose `__init__` needs
    more than its positional `args` — e.g. `anthropic.APIStatusError`, with
    required keyword-only `response`/`body` — therefore raises `TypeError` when
    the child unpickles it, masking the real failure. Such exceptions are
    reconstructed parent-side as a `RemoteError` (single `str` arg, so it always
    round-trips) carrying the original type name and message. See
    `host._safe_exc`."""


# Wire protocol between the parent (kernel/broker) and the child (agent userland).
#
# parent -> child:
#   ("run", code:str)        execute one cell against the persistent namespace
#   ("shutdown",)            stop the serve loop and exit
#
# child -> parent (while a cell runs):
#   ("call", op:str, args:list, kwargs:dict)   a capability call to dispatch
# parent -> child reply:
#   ("ok", value)            the (picklable) result
#   ("err", exception)       re-raised inside the child so the cell sees it
#
# child -> parent (cell finished):
#   ("done", output:str)     captured stdout/stderr/traceback for the orchestrator
#
# Trust boundary (see docs/explanation/security-and-audit.md). The parent is privileged (vault,
# broker, LLM client) and runs *unsandboxed*; the child runs LLM-authored code
# and is the thing we don't trust. The two directions are therefore encoded
# differently:
#
#   child -> parent  is UNTRUSTED. It is framed as JSON (`send_json`/`recv_json`)
#       over the connection's raw byte channel. JSON has no code-execution hook,
#       so nothing the child sends can run in the parent — closing the pickle
#       deserialization RCE that an arbitrary `recv()` would expose. The cost is
#       that capability *arguments* must be JSON values (str/number/bool/None/
#       list/dict); tuples arrive as lists. This is what real call arguments
#       (paths, URLs, code) already are.
#
#   parent -> child  is TRUSTED. It stays on the connection's pickle channel
#       (`send`/`recv`), so capability return values and exception types reach
#       the child intact. The child unpickling parent data is not an escalation:
#       the child is the sandbox, and a compromised parent already owns it.
#
# `send_bytes`/`recv_bytes` (JSON) and `send`/`recv` (pickle) share one framing
# format on a Connection, so using one per direction on the same pipe is safe.


def send_json(conn, msg: tuple) -> None:
    """Send an untrusted-direction message (child -> parent) as a JSON frame.

    Raises TypeError if `msg` contains a non-JSON value — callers turn that into
    a clear error for the agent rather than letting it cross the wire."""
    conn.send_bytes(json.dumps(msg).encode("utf-8"))


def recv_json(conn):
    """Receive a JSON frame from the untrusted direction (parent reading child).

    Decodes with `json.loads`, never `pickle`, so a hostile child cannot execute
    code in the parent by crafting the payload."""
    return json.loads(conn.recv_bytes())


@dataclass(frozen=True)
class RemoteToolSpec:
    """A picklable stand-in for a tool module. A live module cannot cross the IPC
    boundary (and executing its functions in the child would bypass the broker),
    so `use_tool` returns this; the child rebuilds a proxy module from it whose
    function calls route back through the broker as `tools.invoke`."""

    name: str
    functions: tuple[str, ...]


@dataclass(frozen=True)
class RemoteSkillSpec:
    """A picklable learned skill: its bundled **source**, not a proxy.

    The deliberate exception to `RemoteToolSpec`'s rule. An installed module or
    an MCP wrapper *is* capability code — it opens its own sockets, so running
    it in the child would bypass the broker outright. A learned skill is not:
    it is agent-authored Python whose only reach outside the process is the
    session builtins seeded into its globals, and in the child those builtins
    are the same broker proxies a cell gets. So shipping the source and
    executing it child-side loses no mediation and gains the thing parent-side
    execution threw away — the OS sandbox. Raw `socket`/`open('~/.ssh/...')`
    inside a bundled function is jailed exactly as it is inside a cell.

    `sources` carries every bundled file (private ones too, for sibling
    imports); `load_order` names the public ones in execution order."""

    name: str
    sources: tuple[tuple[str, str], ...]
    load_order: tuple[str, ...]
