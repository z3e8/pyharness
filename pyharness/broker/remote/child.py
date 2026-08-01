from __future__ import annotations

import importlib.abc
import importlib.util
import inspect
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from types import ModuleType

from ...security.env import reduce_environ
from ...util import CaptureBuffer, truncate
from .protocol import RemoteSkillSpec, RemoteToolSpec, send_json
from .sandbox import apply_resource_limits


def child_main(
    conn,
    op_names: list[str],
    venv_site_packages: str | None = None,
    workdir: str | None = None,
    self_confine: bool = False,
) -> None:
    """Entry point for the child process (spawned by the host).

    The child is the agent's userland: it holds the persistent namespace and a
    proxy for each capability, but no broker, vault, LLM client, or capability
    code. Every side effect is a proxy call that crosses the pipe to the parent;
    pure computation (loops, data wrangling, the agent's own variables) stays
    here, unseen by the orchestrator."""
    # First, before any agent code can run: shrink the environment the child
    # inherited from the parent to the minimal allowlist (security/env.py). The
    # vault's contract is that cleartext never enters the child, and default-deny
    # also keeps every *unknown* secret a user put in .env out of os.environ.
    reduce_environ()
    apply_resource_limits()
    if workdir:
        # The workspace is the agent's home: chdir into it so raw Python (open,
        # os.listdir, a library's savefig) and the `files` builtins agree on what a
        # relative path means. The spawn bootstrap's own imports already ran, so
        # this can't disturb them; later imports resolve via sys.path (absolute).
        import os as _os

        try:
            _os.chdir(workdir)
        except OSError:
            pass
    if venv_site_packages:
        import sys as _sys

        if venv_site_packages not in _sys.path:
            _sys.path.insert(0, venv_site_packages)
    if self_confine:
        # Linux confines the child here rather than by wrapping its exec (macOS
        # does the latter, so it never reaches this branch). Last of the startup
        # steps and before the first proxy exists, so no agent code has run — but
        # after chdir and the venv sys.path splice, because both must be settled
        # before the filesystem allowlist is computed from them.
        #
        # Landlock is an allowlist: a path missing from the roots below does not
        # weaken confinement, it breaks an import at runtime. The session venv's
        # site-packages is the one that is easy to miss — it lives under the
        # host's sbdir, deliberately outside the workspace.
        from pathlib import Path as _Path

        from .linux_sandbox import apply_linux_sandbox

        extra = (venv_site_packages,) if venv_site_packages else ()
        apply_linux_sandbox(_Path(workdir) if workdir else None, extra)
    # Built empty first so the proxies can close over the finished dict: a
    # learned skill loaded later is executed with this same namespace ambient in
    # its globals, exactly as a cell has it (see `_build_local_skill`).
    namespace: dict = {}
    namespace.update({op: _make_proxy(conn, op, namespace) for op in op_names})
    while True:
        try:
            msg = conn.recv()
        except KeyboardInterrupt:
            # A forwarded Ctrl-C (see host._resync_after_abort) that lost the
            # race with cell completion: the cell already finished, so there is
            # nothing to abort. Stay alive and wait for the next command.
            continue
        if msg[0] == "shutdown":
            return
        if msg[0] == "run":
            output = _run_cell(conn, namespace, msg[1])
            with _PIPE_LOCK:  # never interleave with a straggler thread's call frame
                send_json(conn, ("done", output))


def _run_cell(conn, namespace: dict, code: str) -> str:
    """Exec one cell against the persistent namespace, mirroring the in-process
    Kernel: only captured stdout/stderr (and tracebacks) return to the parent;
    every other value stays live in the namespace for later cells."""
    # CaptureBuffer, not io.StringIO: a runaway `print` of gigabytes would fill
    # an unbounded buffer entirely before `truncate` runs, OOMing the child from
    # one line of agent code. CaptureBuffer bounds capture to CAPTURE_CEILING.
    out = CaptureBuffer()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            exec(compile(code, "<cell>", "exec"), namespace)
    except (Exception, KeyboardInterrupt):
        # KeyboardInterrupt is the parent forwarding a Ctrl-C mid-cell (see
        # host._resync_after_abort): abort the cell like an in-process kernel
        # would — traceback into the output, namespace preserved for later cells.
        out.write(traceback.format_exc(limit=5))
    return truncate(out.getvalue().rstrip()) or "(no output)"


# One pipe, one request/reply in flight at a time. Agent code that calls
# capabilities from threads would otherwise interleave frames and cross
# replies — the lock serializes such calls instead of corrupting the protocol.
# (Parallelism belongs to spawn()/wait(), which fan out parent-side.)
_PIPE_LOCK = threading.Lock()


def _call(conn, op: str, args, kwargs, ambient: dict):
    """Send a capability call to the parent and return its result. Arguments
    must be JSON-transferable (the parent never unpickles child data); a
    non-transferable one fails here with a clear error instead of crossing.

    `ambient` is the child's builtin namespace, needed only when the reply is a
    learned skill's source — that code is executed here with these builtins in
    scope."""
    with _PIPE_LOCK:
        try:
            send_json(conn, ("call", op, args, kwargs))
        except TypeError as exc:
            raise TypeError(
                f"argument to {op!r} is not transferable to the broker: {exc}"
            ) from None
        return _recv_result(conn, ambient)


def _make_proxy(conn, op: str, ambient: dict):
    def proxy(*args, **kwargs):
        return _call(conn, op, args, kwargs, ambient)

    proxy.__name__ = op
    return proxy


def _recv_result(conn, ambient: dict):
    tag, payload = conn.recv()
    if tag == "err":
        raise payload
    if isinstance(payload, RemoteSkillSpec):
        return _build_local_skill(payload, ambient)
    if isinstance(payload, RemoteToolSpec):
        return _RemoteModule(conn, payload, ambient)
    return payload


def _build_local_skill(spec: RemoteSkillSpec, ambient: dict) -> ModuleType:
    """Execute a learned skill's bundled source *here*, in the sandboxed child,
    and return it as a module of ordinary local functions.

    This is the whole point of `RemoteSkillSpec`: the code the agent saved runs
    under the same Seatbelt/Landlock confinement as one of its cells, instead of
    unjailed in the host process. Each file executes with `ambient` — the
    child's broker proxies — seeded into its globals, so `write(...)`/
    `use_tool(...)` inside a skill still cross the pipe and take the full
    policy → audit → budget path. A file's own definitions overwrite a seeded
    name, so nothing a skill declares is shadowed.

    Unlike the in-process loader (`tools/skills.py`), this is eager: `use_tool`
    is already the documented moment a skill's code runs, and the laziness there
    exists so `search`/`describe` — both parent-side, never reaching here — can
    render a skill without executing it."""
    module = ModuleType(spec.name)
    sources = dict(spec.sources)
    finder = _SkillSourceFinder(sources, ambient)
    sys.meta_path.append(finder)
    try:
        for filename in spec.load_order:
            mod_name = f"pyharness_skill_{spec.name}_{filename[:-3]}"
            try:
                sub = _exec_source(mod_name, filename, sources[filename], ambient)
            except Exception as exc:
                sys.modules.pop(mod_name, None)
                raise RuntimeError(
                    f"skill {spec.name!r}: bundled file {filename} failed to import: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            for fname, func in inspect.getmembers(sub, inspect.isfunction):
                if not fname.startswith("_") and func.__module__ == mod_name:
                    func.__module__ = spec.name
                    setattr(module, fname, func)
    finally:
        sys.meta_path.remove(finder)
    return module


def _exec_source(
    mod_name: str, filename: str, source: str, ambient: dict
) -> ModuleType:
    """Execute one bundled file as a module, with the session builtins seeded
    into its globals first. Registered in `sys.modules` before exec so a sibling
    that imports it mid-load gets this module rather than a second copy."""
    module = ModuleType(mod_name)
    module.__file__ = f"<skill:{filename}>"
    module.__dict__.update(ambient)
    sys.modules[mod_name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


class _SkillSourceFinder:
    """Resolves a skill's bundled siblings from source held in memory, so a
    bundled `helpers.py` can `import` a bundled `parse.py` with no file on disk
    (the child's read jail covers the skills root, which lives under `$HOME`).

    **Appended** to `sys.meta_path`, never prepended: the stdlib and
    site-packages finders run first, so a bundled file named `json.py` cannot
    shadow the module it is named after. The in-process loader's
    `sys.path.insert(0, ...)` has the opposite precedence and that hazard.
    Installed only while the skill's files execute, matching the in-process
    loader's window."""

    def __init__(self, sources: dict[str, str], ambient: dict):
        self._sources = sources
        self._ambient = ambient

    def find_spec(self, fullname: str, path=None, target=None):
        if path is not None or "." in fullname:
            return None  # submodule of a real package: not ours
        filename = f"{fullname}.py"
        if filename not in self._sources:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _SkillSourceLoader(filename, self._sources[filename], self._ambient),
            origin=f"<skill:{filename}>",
        )


class _SkillSourceLoader(importlib.abc.Loader):
    """Loads one bundled sibling, seeding the session builtins the same way the
    top-level files get them — a helper module calls capabilities too."""

    def __init__(self, filename: str, source: str, ambient: dict):
        self._filename, self._source, self._ambient = filename, source, ambient

    def exec_module(self, module: ModuleType) -> None:
        module.__file__ = f"<skill:{self._filename}>"
        module.__dict__.update(self._ambient)
        exec(compile(self._source, module.__file__, "exec"), module.__dict__)


class _RemoteModule:
    """A proxy for a tool module. Attribute access yields callables that route
    each invocation back to the parent's broker (`tools.invoke`), so a tool's
    functions are policy/audit/budget-gated exactly like any other capability."""

    def __init__(self, conn, spec: RemoteToolSpec, ambient: dict):
        self._conn = conn
        self._name = spec.name
        self._functions = frozenset(spec.functions)
        self._ambient = ambient

    def __getattr__(self, attr: str):
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._functions:
            raise AttributeError(
                f"tool {self._name!r} has no function {attr!r}; "
                f"available: {', '.join(sorted(self._functions))}"
            )

        def call(*args, **kwargs):
            return _call(
                self._conn,
                "invoke",
                (self._name, attr, args, kwargs),
                {},
                self._ambient,
            )

        call.__name__ = attr
        return call

    def __dir__(self):
        return sorted(self._functions)
