"""Shared pytest fixtures.

The capability tests (`http`/`web`/`browser`) drive their capabilities against
*mocked* HTTP transports using placeholder hosts (`http://x`, `example.com`) and
never touch the network. The SSRF egress guard (`security/egress.check_url`),
however, resolves the host for real — and after the fail-closed hardening an
unresolvable placeholder is refused, which would fail those offline tests for a
reason that has nothing to do with what they test.

`_stub_egress_dns` (autouse) patches the egress resolution seam so any hostname
resolves to a fixed public IP, keeping the suite hermetic. It does *not* touch
`socket.getaddrinfo` globally (that would break real localhost servers, e.g. the
remote-MCP test), only egress's own lookup. Tests that need real egress behavior
either use IP literals (which bypass resolution entirely) or re-patch the seam
themselves — a test-body `monkeypatch` runs after this fixture and wins.
"""
from __future__ import annotations

import pytest

# A stable, non-blocked public address (TEST-NET-3, RFC 5737) so the guard's
# range checks run for real and simply pass.
_PUBLIC_IP = "203.0.113.10"


@pytest.fixture(autouse=True)
def _stub_egress_dns(monkeypatch):
    from pyharness.security import egress

    def _fake_resolve(host: str):
        return [(2, 1, 6, "", (_PUBLIC_IP, 0))]  # AF_INET, one address

    monkeypatch.setattr(egress, "_resolve_host", _fake_resolve)
