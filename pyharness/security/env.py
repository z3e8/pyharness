"""Default-deny environment for subprocesses reachable by agent code.

Three spawn surfaces run code the agent controls or third parties wrote — the
out-of-process child kernel, `shell.bash`, and a local (stdio) MCP server. Each
starts from `minimal_environ()`: a small allowlist of vars any program needs to
behave normally, instead of "the whole parent environment minus known secrets."
A denylist can only strip what it knows about; whatever else a user drops into
`.env` (`AWS_SECRET_ACCESS_KEY`, `DATABASE_URL`, a custom `*_TOKEN`) would pass
straight through to every subprocess. Default-deny matches the rest of the
harness's posture: the workspace is the sandbox; the broker guards the
perimeter.

`PYHARNESS_ENV_PASSTHROUGH="FOO,BAR"` is the escape hatch for vars a workflow
genuinely needs to reach a subprocess (a proxy setting, a tool's config). It
can never resurrect the harness's own secret-bearing variables.
"""

from __future__ import annotations

import os

from ..llm.client import PROVIDER_SECRET_ENV
from .vault import DEFAULT_ENV_PREFIX, PASSPHRASE_ENV

PASSTHROUGH_ENV = "PYHARNESS_ENV_PASSTHROUGH"

# What any well-behaved program needs: identity/paths, terminal, locale, time,
# plus the TLS-trust and proxy vars common tooling reads. Deliberately small —
# grow it only for vars whose absence breaks *common* workflows; anything
# workflow-specific belongs in the passthrough list.
_ALLOW_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TZ",
        "LANG",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_ALLOW_PREFIXES = ("LC_",)

# The harness's own secret-bearing variables: env-backed vault secrets, the
# file-vault passphrase, and the provider API keys the parent holds to call the
# LLM. Never passed through — not even when named in PYHARNESS_ENV_PASSTHROUGH.
_SECRET_NAMES = frozenset({PASSPHRASE_ENV, *PROVIDER_SECRET_ENV})


def _is_secret(key: str) -> bool:
    return key in _SECRET_NAMES or key.startswith(DEFAULT_ENV_PREFIX)


def minimal_environ() -> dict[str, str]:
    """The minimal environment for a subprocess run on the agent's behalf:
    the default allowlist plus the user's PYHARNESS_ENV_PASSTHROUGH names,
    minus anything secret-bearing regardless of list."""
    passthrough = {
        name.strip()
        for name in os.environ.get(PASSTHROUGH_ENV, "").split(",")
        if name.strip()
    }
    allowed = _ALLOW_NAMES | passthrough
    return {
        key: value
        for key, value in os.environ.items()
        if (key in allowed or key.startswith(_ALLOW_PREFIXES)) and not _is_secret(key)
    }


def reduce_environ() -> None:
    """Shrink *this* process's environment to `minimal_environ()`. Called in the
    child kernel before any agent code runs, so neither `os.environ` nor any
    subprocess the child spawns can read a var the parent legitimately holds."""
    keep = minimal_environ()
    os.environ.clear()
    os.environ.update(keep)
