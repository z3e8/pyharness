from __future__ import annotations

from dataclasses import dataclass

# Wire protocol between the parent (kernel/broker) and the child (agent userland).
#
# parent -> child:
#   ("run", code:str)        execute one cell against the persistent namespace
#   ("shutdown",)            stop the serve loop and exit
#
# child -> parent (while a cell runs):
#   ("call", op:str, args:tuple, kwargs:dict)   a capability call to dispatch
# parent -> child reply:
#   ("ok", value)            the (picklable) result
#   ("err", exception)       re-raised inside the child so the cell sees it
#
# child -> parent (cell finished):
#   ("done", output:str)     captured stdout/stderr/traceback for the orchestrator


@dataclass(frozen=True)
class RemoteToolSpec:
    """A picklable stand-in for a tool module. A live module cannot cross the IPC
    boundary (and executing its functions in the child would bypass the broker),
    so `use_tool` returns this; the child rebuilds a proxy module from it whose
    function calls route back through the broker as `tools.invoke`."""

    name: str
    functions: tuple[str, ...]
