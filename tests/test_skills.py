"""Skills: learned tools = markdown instructions + optional bundled code (design §6)."""

from __future__ import annotations

import inspect
import json
import os

import pytest

from pyharness.broker.capabilities.skills import SkillsCapability
from pyharness.broker.dispatch import PermissionDenied
from pyharness.broker.remote.sandbox import macos_sandbox_supported
from pyharness.core.session import Session
from pyharness.tools.registry import Registry
from pyharness.tools.skills import (
    _MAX_USES,
    load_skills,
    parse_skill_md,
    read_journal,
    record_use,
    render_files_preview,
    write_skill,
)


def test_parse_frontmatter_and_body():
    meta, body = parse_skill_md(
        "---\nname: foo\ndescription: do foo\nkeywords: a, b\n---\n\nStep 1.\nStep 2.\n"
    )
    assert meta == {"name": "foo", "description": "do foo", "keywords": "a, b"}
    assert body == "Step 1.\nStep 2."


def test_human_authored_skill_loads_from_disk(tmp_path):
    write_skill(
        tmp_path,
        "greet",
        "Greet a person",
        "Call hello(name).",
        files={
            "impl.py": "def hello(name):\n    '''Say hi.'''\n    return f'hi {name}'\n"
        },
        keywords=("welcome",),
    )
    reg = Registry()
    assert load_skills(reg, tmp_path) == ["greet"]

    # discoverable by description and by keyword, tagged as a learned tool
    assert "# greet" in reg.search("greet")
    assert "# greet" in reg.search("welcome")
    assert "learned" in reg.search("greet")

    # describe reveals the instructions AND the bundled function signature
    details = reg.describe("greet")
    assert "Call hello(name)." in details
    assert "hello(name)" in details and "Say hi." in details

    # use loads the bundled code
    assert reg.use("greet").hello("ada") == "hi ada"


def test_bundled_code_is_lazy_until_first_call(tmp_path):
    marker = tmp_path / "ran"
    write_skill(
        tmp_path / "skills",
        "lazy",
        "laziness probe",
        "Call hello().",
        files={
            "impl.py": (
                f"open({str(marker)!r}, 'w').write('imported')\n"
                "def hello(name='world'):\n"
                "    '''Say hi.'''\n"
                "    return f'hi {name}'\n"
            )
        },
    )
    reg = Registry()
    load_skills(reg, tmp_path / "skills")
    # searching, describing, and even loading the module execute nothing
    assert "# lazy" in reg.search("lazy")
    details = reg.describe("lazy")
    mod = reg.use("lazy")
    assert not marker.exists()
    # yet the AST-derived listing shows the real signature and docstring
    # (the indented line is the Functions entry, not the source block)
    assert "    hello(name='world')  # Say hi." in details
    # the first call executes the file — exactly then
    assert mod.hello("ada") == "hi ada"
    assert marker.read_text() == "imported"


def test_bundled_files_execute_once_and_import_each_other(tmp_path):
    counter = tmp_path / "count"
    write_skill(
        tmp_path / "skills",
        "pair",
        "two files",
        "b uses a",
        files={
            "aa.py": "def base():\n    return 1\n",
            "bb.py": (
                f"open({str(counter)!r}, 'a').write('x')\n"
                "import aa\n"
                "def double():\n    return aa.base() * 2\n"
            ),
        },
    )
    reg = Registry()
    load_skills(reg, tmp_path / "skills")
    mod = reg.use("pair")
    # sibling import works at deferred-exec time (skill dir goes on sys.path)
    assert mod.double() == 2
    assert mod.base() == 1
    # ...and the files ran exactly once, not once per call
    assert counter.read_text() == "x"


def test_broken_bundled_file_errors_at_call_time_not_before(tmp_path):
    write_skill(
        tmp_path,
        "boom",
        "explodes on import",
        "n/a",
        files={
            "x.py": "raise RuntimeError('imported too early')\ndef go():\n    return 1\n"
        },
    )
    reg = Registry()
    load_skills(reg, tmp_path)
    # browsing and describing must not run the bundled code, so no error yet
    assert "# boom" in reg.search("boom")
    details = reg.describe("boom")
    assert "n/a" in details and "go()" in details and "unavailable" not in details
    # the failure surfaces on the call, attributed to the skill and the file
    with pytest.raises(RuntimeError, match=r"skill 'boom': bundled file x.py"):
        reg.use("boom").go()


def test_syntax_error_is_reported_and_instructions_survive(tmp_path):
    write_skill(
        tmp_path,
        "sy",
        "won't parse",
        "the steps still show",
        files={"bad.py": "def broken(:\n"},
    )
    reg = Registry()
    load_skills(reg, tmp_path)
    details = reg.describe("sy")
    # a syntax error is detectable without executing, so describe reports it
    assert "unavailable" in details and "bad.py" in details
    assert "the steps still show" in details


def test_describe_shows_bundled_source(tmp_path):
    write_skill(
        tmp_path,
        "greet",
        "Greet a person",
        "Call hello(name).",
        files={
            "impl.py": "def hello(name):\n    '''Say hi.'''\n    return f'hi {name}'\n"
        },
    )
    reg = Registry()
    load_skills(reg, tmp_path)
    details = reg.describe("greet")
    # the code itself is part of the rendered skill, clearly delimited per file
    assert "Bundled code" in details
    assert "## impl.py" in details
    assert "return f'hi {name}'" in details


def test_large_bundled_file_shows_outline_only(tmp_path):
    filler = "    z = 1\n" * 600  # push the file past the full-source limit
    write_skill(
        tmp_path,
        "big",
        "a big module",
        "call big()",
        files={
            "huge.py": f"def big(x, y=2):\n    '''Add things.'''\n{filler}    return x\n"
        },
    )
    reg = Registry()
    load_skills(reg, tmp_path)
    details = reg.describe("big")
    # the outline keeps what's callable and drops the body
    assert "outline only" in details
    assert "def big(x, y=2): ...  # Add things." in details
    assert "z = 1" not in details


def test_dynamically_defined_function_resolves_on_access(tmp_path):
    # a function created by assignment isn't statically visible, so it doesn't
    # list — but attribute access still triggers the deferred exec and finds it
    write_skill(
        tmp_path,
        "dyn",
        "dynamic def",
        "call revealed()",
        files={
            "dyn.py": (
                "def _make():\n"
                "    def hidden():\n        return 42\n"
                "    return hidden\n"
                "revealed = _make()\n"
            )
        },
    )
    reg = Registry()
    load_skills(reg, tmp_path)
    assert reg.use("dyn").revealed() == 42
    with pytest.raises(AttributeError, match="dyn"):
        _ = reg.use("dyn").nonexistent


def test_pure_instructions_skill_has_no_functions(tmp_path):
    write_skill(tmp_path, "policy", "refund rules", "Refund within 30 days.")
    reg = Registry()
    load_skills(reg, tmp_path)
    details = reg.describe("policy")
    assert "Refund within 30 days." in details
    assert "Functions:" not in details  # nothing bundled


def test_empty_skills_dir_is_noop(tmp_path):
    assert load_skills(Registry(), tmp_path / "missing") == []


def test_save_skill_capability_registers_now_and_reloads_later(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    msg = cap.save_skill(
        "dedupe",
        "Dedupe a list",
        "Call run(items).",
        files={"impl.py": "def run(items):\n    return list(dict.fromkeys(items))\n"},
    )
    assert "saved skill 'dedupe'" in msg

    # usable immediately in this session
    assert reg.use("dedupe").run([1, 1, 2]) == [1, 2]

    # and a fresh registry rediscovers it from disk (next session)
    reloaded = Registry()
    load_skills(reloaded, tmp_path)
    assert reloaded.use("dedupe").run([3, 3]) == [3]


def test_save_skill_rejects_unsafe_name(tmp_path):
    cap = SkillsCapability(Registry(), tmp_path)
    with pytest.raises(ValueError):
        cap.save_skill("../escape", "x", "y")


def test_resave_drops_stale_bundled_code(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("t", "v1", "use old()", files={"a.py": "def old():\n    return 1\n"})
    assert reg.use("t").old() == 1

    # re-save with a renamed helper; the old file must not linger
    cap.save_skill("t", "v2", "use new()", files={"b.py": "def new():\n    return 2\n"})
    assert not (tmp_path / "t" / "a.py").exists()
    mod = Registry()  # fresh load = exactly the last save
    load_skills(mod, tmp_path)
    assert mod.use("t").new() == 2
    assert not hasattr(mod.use("t"), "old")


def test_skills_are_search_only_not_featured(tmp_path):
    write_skill(
        tmp_path, "rare", "a rarely used procedure", "do it", keywords=("widget",)
    )
    reg = Registry()
    load_skills(reg, tmp_path)
    # not in the default browse — skills don't crowd the common-tools listing
    assert "# rare" not in reg.search("")
    # but fully findable by name, description word, or keyword
    assert "# rare" in reg.search("rare")
    assert "# rare" in reg.search("procedure")
    assert "# rare" in reg.search("widget")


def test_new_skill_is_unverified(tmp_path):
    write_skill(tmp_path, "fresh", "a new procedure", "do the thing")
    reg = Registry()
    load_skills(reg, tmp_path)
    # tagged unverified in the header, and described as a hypothesis
    assert "unverified" in reg.search("fresh")
    details = reg.describe("fresh")
    assert "verified: no" in details


def test_record_use_worked_verifies_and_logs(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("greet", "greet someone", "say hi")
    assert "unverified" in reg.search("greet")

    msg = cap.record_skill_use("greet", "worked", note="clean run")
    assert "verified" in msg and "now verified" in msg

    # on disk (next session) and in this registry, it now reads as verified
    assert read_journal(tmp_path / "greet")["verified"] is True
    assert "unverified" not in reg.search("greet")
    details = reg.describe("greet")
    assert "verified: yes" in details and "clean run" in details


def test_record_use_failed_flags_last_failed(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("scrape", "scrape a site", "load the page")
    cap.record_skill_use("scrape", "failed", note="site blocks headless")

    assert "last-failed" in reg.search("scrape")
    assert "unverified" in reg.search("scrape")  # a failure does not verify
    assert "site blocks headless" in reg.describe("scrape")


def test_record_use_deviated_clears_verified_and_warns(tmp_path):
    """A run that finished only after correcting the steps must not leave the
    skill looking healthy — that is how a broken procedure hardens."""
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("libr", "read a shelf", "1. goto the page\n2. use ref e94")
    cap.record_skill_use("libr", "worked", note="clean")
    assert read_journal(tmp_path / "libr")["verified"] is True

    msg = cap.record_skill_use("libr", "deviated", note="e94 was stale; snapshotted")
    assert "now unverified" in msg
    assert read_journal(tmp_path / "libr")["verified"] is False

    assert "steps-wrong" in reg.search("libr")
    details = reg.describe("libr")
    assert "WARNING" in details and "edit_skill" in details
    # and it survives to the next session
    reloaded = Registry()
    load_skills(reloaded, tmp_path)
    assert "steps-wrong" in reloaded.search("libr")


def test_record_use_rejects_bad_outcome_and_unknown_skill(tmp_path):
    cap = SkillsCapability(Registry(), tmp_path)
    cap.save_skill("s", "d", "i")
    with pytest.raises(ValueError):
        cap.record_skill_use("s", "maybe")
    with pytest.raises(KeyError):
        cap.record_skill_use("nope", "worked")


def test_journal_is_bounded(tmp_path):
    write_skill(tmp_path, "loop", "repeated", "go")
    for i in range(_MAX_USES + 5):
        record_use(
            tmp_path / "loop",
            "worked",
            note=f"run {i}",
            now=f"2026-01-01T00:00:{i:02d}",
        )
    uses = read_journal(tmp_path / "loop")["uses"]
    assert len(uses) == _MAX_USES
    assert uses[-1]["note"] == f"run {_MAX_USES + 4}"  # newest kept, oldest dropped


def test_resave_de_verifies_but_keeps_log(tmp_path):
    reg = Registry()
    cap = SkillsCapability(reg, tmp_path)
    cap.save_skill("t", "v1", "old steps")
    cap.record_skill_use("t", "worked", note="worked once")
    assert read_journal(tmp_path / "t")["verified"] is True

    # revising the procedure makes it unproven again, but its history survives
    cap.save_skill("t", "v2", "new steps")
    journal = read_journal(tmp_path / "t")
    assert journal["verified"] is False
    assert any(u.get("note") == "worked once" for u in journal["uses"])
    # a fresh load (next session) reflects the reset
    reloaded = Registry()
    load_skills(reloaded, tmp_path)
    assert "unverified" in reloaded.search("t")


def test_save_skill_requires_approval_by_default(tmp_path):
    skills = tmp_path / "skills"
    denied = Session(
        tmp_path / "a", skills_dir=skills, unsafe_in_process=True
    )  # no approver
    with pytest.raises(PermissionDenied):
        denied.broker.namespace()["save_skill"]("x", "d", "i")
    denied.close()

    allowed = Session(
        tmp_path / "b",
        skills_dir=skills,
        approver=lambda *a: True,
        unsafe_in_process=True,
    )
    assert "saved skill 'x'" in allowed.broker.namespace()["save_skill"]("x", "d", "i")
    allowed.close()


# --- bundled code reaching capabilities (ambient builtins) --------------------


def _audit(session_root):
    return [
        json.loads(line)
        for line in (session_root / "audit.jsonl").read_text().splitlines()
    ]


def _stable(record: dict) -> dict:
    """An audit record minus its chain/clock fields, for shape comparison."""
    return {k: v for k, v in record.items() if k not in ("ts", "prev", "hash")}


def test_bundled_code_calls_a_capability_through_the_broker(tmp_path):
    """The session builtins are ambient inside bundled skill code — no import —
    and a capability call the skill makes is a full broker dispatch: it lands
    in the audit chain with exactly the record shape of a direct call."""
    session = Session(
        tmp_path / "s",
        skills_dir=tmp_path / "skills",
        approver=lambda *a: True,
        unsafe_in_process=True,
    )
    try:
        ns = session.broker.namespace()
        ns["save_skill"](
            "noter",
            "write then read a note",
            "call note(text)",
            files={
                "impl.py": (
                    "def note(text):\n"
                    "    write('note.txt', text)\n"
                    "    return read('note.txt')\n"
                )
            },
        )
        # The reuse path the agent takes: use_tool, then call the function.
        assert ns["use_tool"]("noter").note("hello") == "hello"

        skill_writes = [
            r for r in _audit(tmp_path / "s") if r.get("action") == "files.write"
        ]
        assert [r.get("phase") for r in skill_writes] == ["start", "end"]
        assert skill_writes[1]["ok"] is True

        # The same call made directly by "agent code" is indistinguishable in
        # the chain: identical record shape, field for field.
        ns["write"]("note.txt", "hello")
        direct_writes = [
            r for r in _audit(tmp_path / "s") if r.get("action") == "files.write"
        ][2:]
        assert [_stable(r) for r in direct_writes] == [_stable(r) for r in skill_writes]
    finally:
        session.close()


def test_bundled_code_reaches_capabilities_from_the_out_of_process_kernel(tmp_path):
    """The production path: agent code runs in the sandboxed child, use_tool
    hands it the skill's source as a RemoteSkillSpec, and the bundled function
    executes *in the child* — where the ambient builtins must resolve to the
    child's broker proxies and the nested capability call must still be
    audited parent-side."""
    session = Session(
        tmp_path / "s",
        skills_dir=tmp_path / "skills",
        approver=lambda *a: True,
    )
    try:
        session.broker.namespace()["save_skill"](
            "noter",
            "write then read a note",
            "call note(text)",
            files={
                "impl.py": (
                    "def note(text):\n"
                    "    write('note.txt', text)\n"
                    "    return read('note.txt')\n"
                )
            },
        )
        out = session.kernel.run("print(use_tool('noter').note('hello'))")
        assert "hello" in out
        writes = [r for r in _audit(tmp_path / "s") if r.get("action") == "files.write"]
        assert [r.get("phase") for r in writes] == ["start", "end"]
        assert writes[1]["ok"] is True
    finally:
        session.close()


def test_bundled_code_import_of_a_builtin_fails_with_guidance(tmp_path):
    """The failure suite D paid for: bundled code opening with
    `from pyharness import use_tool`. It must fail at first call with an error
    naming the skill and file and saying the builtins are already in scope."""
    session = Session(
        tmp_path / "s",
        skills_dir=tmp_path / "skills",
        approver=lambda *a: True,
        unsafe_in_process=True,
    )
    try:
        ns = session.broker.namespace()
        ns["save_skill"](
            "legacy",
            "old broken pattern",
            "call go()",
            files={
                "impl.py": "from pyharness import use_tool\ndef go():\n    return 1\n"
            },
        )
        with pytest.raises(RuntimeError, match=r"skill 'legacy': bundled file impl.py"):
            ns["use_tool"]("legacy").go()
        with pytest.raises(RuntimeError, match="already in scope"):
            ns["use_tool"]("legacy").go()
    finally:
        session.close()


def test_package_attribute_guard_names_the_builtins():
    import pyharness

    # from-import — the exact line the model wrote — carries the guidance
    with pytest.raises(ImportError, match="session builtin"):
        exec("from pyharness import use_tool")
    # plain attribute access gets the same pointer
    with pytest.raises(ImportError, match="already in scope"):
        _ = pyharness.save_skill
    # unknown names stay ordinary AttributeErrors
    with pytest.raises(AttributeError):
        _ = pyharness.definitely_not_a_thing


def test_builtin_guard_covers_every_live_op(tmp_path):
    """Drift pin: every op a full parent session injects as a kernel builtin is
    either guarded by the package `__getattr__` or shadowed by a real submodule
    (`llm`). A new builtin that is neither shows up here."""
    import pyharness

    session = Session(
        tmp_path / "drift", skills_dir=tmp_path / "skills", unsafe_in_process=True
    )
    try:
        for op in session.broker.op_names():
            try:
                obj = getattr(pyharness, op)
            except ImportError as exc:
                assert "session builtin" in str(exc)
            else:
                assert inspect.ismodule(obj), (
                    f"builtin {op!r} resolves to a package attribute that is "
                    "neither the guard nor a submodule — add it to "
                    "_KERNEL_BUILTINS in pyharness/__init__.py"
                )
    finally:
        session.close()


def test_ambient_builtins_do_not_become_skill_functions(tmp_path):
    """Seeding the builtins into a bundled module's globals must not leak them
    onto the skill's public surface — the skill exports what it defines."""
    from pyharness.tools.registry import _public_functions

    session = Session(
        tmp_path / "s",
        skills_dir=tmp_path / "skills",
        approver=lambda *a: True,
        unsafe_in_process=True,
    )
    try:
        ns = session.broker.namespace()
        ns["save_skill"](
            "tidy",
            "one function",
            "call only(text)",
            files={"impl.py": "def only(text):\n    return write('t.txt', text)\n"},
        )
        mod = session.registry.use("tidy")
        mod.only("x")  # force the deferred exec (ambient names now seeded)
        assert [fname for fname, _ in _public_functions(mod)] == ["only"]
    finally:
        session.close()


def _skill_session(tmp_path, name, files, **kwargs):
    """A session with one saved skill, ready for the out-of-process reuse path."""
    session = Session(
        tmp_path / "s",
        skills_dir=tmp_path / "skills",
        approver=lambda *a: True,
        **kwargs,
    )
    session.broker.namespace()["save_skill"](
        name, f"{name} skill", f"call into {name}", files=files
    )
    return session


def test_bundled_code_executes_inside_the_child_not_the_host(tmp_path):
    """The structural claim, independent of any OS sandbox: a bundled function
    runs in the *child* process. Parent-side execution — the old behavior — put
    agent-authored code outside every confinement the child is given, so which
    process runs it is the whole security property, and a pid is the one witness
    that cannot be faked by a proxy standing in for the real function."""
    session = _skill_session(
        tmp_path,
        "whereami",
        {"impl.py": "import os\ndef pid():\n    return os.getpid()\n"},
    )
    try:
        out = session.kernel.run(
            "import os\n"
            "print(use_tool('whereami').pid() == os.getpid(), os.getpid() != "
            f"{os.getpid()})\n"
        )
        assert out == "True True"
    finally:
        session.close()


@pytest.mark.skipif(
    not macos_sandbox_supported(), reason="OS sandbox only built for macOS"
)
def test_bundled_code_cannot_escape_the_sandbox(tmp_path):
    """The record this closes: raw socket/filesystem access inside a bundled
    function used to run unjailed in the host process. It must now be denied
    exactly as the same line in a cell is."""
    session = _skill_session(
        tmp_path,
        "escape",
        {
            "impl.py": (
                "import socket\n"
                "def net():\n"
                "    try:\n"
                "        socket.create_connection(('1.1.1.1', 80), timeout=5)\n"
                "        return 'CONNECTED'\n"
                "    except OSError:\n"
                "        return 'denied'\n"
                "def write_outside(path):\n"
                "    try:\n"
                "        open(path, 'w').write('x')\n"
                "        return 'WROTE'\n"
                "    except OSError:\n"
                "        return 'denied'\n"
            )
        },
    )
    try:
        escape = tmp_path / "escaped.txt"
        out = session.kernel.run(
            "s = use_tool('escape')\n"
            f"print(s.net(), s.write_outside({str(escape)!r}))\n"
        )
        assert out == "denied denied"
        assert not escape.exists()
    finally:
        session.close()


def test_bundled_sibling_import_resolves_in_the_child(tmp_path):
    """A skill's files may import one another. Nothing is on disk for the child
    to read (the skills root is under $HOME, inside the read jail), so the
    sibling has to resolve from the source that crossed the pipe."""
    session = _skill_session(
        tmp_path,
        "twofile",
        {
            "helper.py": "def double(n):\n    return n * 2\n",
            "main.py": "import helper\ndef run(n):\n    return helper.double(n) + 1\n",
        },
    )
    try:
        assert session.kernel.run("print(use_tool('twofile').run(20))") == "41"
    finally:
        session.close()


def test_a_bundled_file_cannot_shadow_a_stdlib_module(tmp_path):
    """The finder is appended to sys.meta_path, never prepended, so the real
    stdlib wins a name collision. The in-process loader's sys.path.insert(0) has
    the opposite precedence.

    `colorsys`, not `json`: the collision only bites a module not already in
    sys.modules, and anything the harness itself imports is cached long before
    a skill loads — which is exactly why the hazard is easy to miss."""
    session = _skill_session(
        tmp_path,
        "shadowy",
        {
            "colorsys.py": "def rgb_to_hls(*a):\n    return 'HIJACKED'\n",
            "impl.py": (
                "import colorsys\n"
                "def probe():\n"
                "    return colorsys.rgb_to_hls(1.0, 0.0, 0.0), colorsys.__file__\n"
            ),
        },
    )
    try:
        out = session.kernel.run(
            "import sys\n"
            "assert 'colorsys' not in sys.modules, 'precondition: not preimported'\n"
            "hls, origin = use_tool('shadowy').probe()\n"
            "print(hls != 'HIJACKED', origin.endswith('colorsys.py'), "
            "'<skill:' not in origin)\n"
        )
        assert out == "True True True"
    finally:
        session.close()


def test_bundled_import_failure_names_the_skill_and_file_in_the_child(tmp_path):
    """Error attribution survives the move: a failing bundled import reports the
    skill and file, in the same shape the in-process loader produces."""
    session = _skill_session(
        tmp_path,
        "broken",
        {"impl.py": "import nope_not_a_module\ndef go():\n    pass\n"},
    )
    try:
        out = session.kernel.run("use_tool('broken').go()")
        assert "skill 'broken': bundled file impl.py failed to import" in out
    finally:
        session.close()


def test_save_skill_approval_shows_the_bundled_source(tmp_path):
    """Approving a save is a supply-chain sign-off on code that runs later, so
    the prompt has to carry the code. It used to show only the skill's name."""
    seen = []
    session = Session(
        tmp_path / "s",
        skills_dir=tmp_path / "skills",
        approver=lambda request: (seen.append(request), True)[1],
        unsafe_in_process=True,
    )
    try:
        session.broker.namespace()["save_skill"](
            "audited",
            "does a thing",
            "call go()",
            files={"impl.py": "def go():\n    return bash('id')\n"},
        )
        summary = next(r.summary for r in seen if r.action == "skills.save_skill")
        assert "impl.py" in summary
        assert "return bash('id')" in summary
    finally:
        session.close()


def test_large_bundled_file_previews_as_an_outline(tmp_path):
    """A big file collapses to its def outline rather than flooding the prompt;
    the human still sees every callable it is approving."""
    body = "\n".join(f"    x{i} = {i}" for i in range(600))
    files = {"big.py": f"def huge():\n    '''Do a lot.'''\n{body}\n    return x0\n"}
    text = render_files_preview(files)
    assert "outline only" in text
    assert "def huge()" in text and "Do a lot." in text
    assert "x599 = 599" not in text
