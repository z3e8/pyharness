"""Egress guard — a defense against server-side request forgery (SSRF).

The broker runs in the *unsandboxed* parent, so every outbound request the agent
asks for (`web.fetch`, `http.request`, `browser.goto`) is made from a process that
can reach the host's own network — localhost services, a Docker socket over HTTP,
and, most dangerously, the cloud metadata endpoint (`169.254.169.254`, link-local
on every major cloud) that hands out the instance's IAM credentials. Nothing else
in the harness stops the agent from pointing a "free" GET at those.

This module gives the request path one function, `check_url`, that:

- rejects any non-http(s) scheme (no `file://`, `chrome://`, `gopher://`, ...); and
- resolves the host and blocks it when it maps to a **link-local** address
  (`169.254.0.0/16`, `fe80::/10`) — the cloud-metadata range, never a legitimate
  fetch target — so a hostname that resolves there (e.g. `metadata.google.internal`)
  is caught too, not just the bare IP.

Loopback and RFC1918/ULA private ranges stay reachable by default (local dev,
local services, and local MCP-over-http are normal), but setting
`PYHARNESS_BLOCK_PRIVATE_NETWORK=true` extends the block to them for a stricter
posture. DNS resolution *failure* fails **closed**: a hostname that will not
resolve is refused rather than waved through, so a name that resolves only
intermittently (or only for the second, unguarded lookup the client makes) can't
slip past the guard on the attempt where our lookup happens to fail.

The guard is best-effort defense-in-depth: it resolves the name here and the
client resolves it again at connect, so a deliberately racing resolver (DNS
rebinding) is not fully closed out — the guard vets the IPs *it* sees, not
necessarily the one the socket ultimately connects to. Pinning the connection to
the vetted IP is the durable fix (a custom httpx transport); it is not built in
this version and the residual TOCTOU is tracked in agents/issues.md and
docs/explanation/security-and-audit.md.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

BLOCK_PRIVATE_ENV = "PYHARNESS_BLOCK_PRIVATE_NETWORK"
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class EgressBlocked(Exception):
    """Raised when a URL's host is not a permitted outbound target."""


def _strict() -> bool:
    return os.environ.get(BLOCK_PRIVATE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _resolve_host(host: str) -> list[tuple]:
    """DNS resolution seam: return `socket.getaddrinfo` infos for `host`, or raise
    `socket.gaierror`. Factored out as the one place tests patch so the offline
    suite (placeholder hosts + mocked HTTP transports) doesn't hit real DNS, while
    the guard's own logic (literal handling, range checks, fail-closed) stays under
    test."""
    return socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)


def _candidate_ips(host: str) -> list[ipaddress._BaseAddress]:
    """Every IP the host could resolve to. An IP literal is returned as itself; a
    name is resolved via DNS.

    A resolution failure fails **closed**: it raises `EgressBlocked` rather than
    returning an empty (== "allow") list. Failing open let an unresolvable — or
    only-intermittently-resolvable — name through unchecked, which both defeats the
    guard on the lookup where our resolver happens to fail and hands a rebinding
    resolver a free pass. A name that legitimately doesn't resolve would fail the
    request anyway; refusing it here just moves the failure earlier and closes the
    hole."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = _resolve_host(host)
    except socket.gaierror as exc:
        raise EgressBlocked(
            f"host {host!r} could not be resolved ({exc}); refusing (fail-closed)"
        ) from exc
    ips: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        try:
            ips.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not ips:
        raise EgressBlocked(f"host {host!r} resolved to no usable address; refusing")
    return ips


def _blocked(ip: ipaddress._BaseAddress, strict: bool) -> bool:
    # Link-local always blocked: it is the cloud-metadata range and is never a
    # legitimate fetch target. Multicast/unspecified likewise. Loopback and
    # private/reserved ranges are blocked only in strict mode.
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    if strict and (ip.is_loopback or ip.is_private or ip.is_reserved):
        return True
    return False


def check_url(url: str) -> str:
    """Raise `EgressBlocked` if `url` is not a permitted outbound target; return it
    unchanged otherwise. Enforces the http(s)-only + no-link-local rules above (plus
    private/loopback when PYHARNESS_BLOCK_PRIVATE_NETWORK is set)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise EgressBlocked(
            f"scheme {scheme or '(none)'!r} is not permitted (only http/https)"
        )
    host = parts.hostname
    if not host:
        raise EgressBlocked(f"url {url!r} has no host")
    strict = _strict()
    for ip in _candidate_ips(host):
        if _blocked(ip, strict):
            raise EgressBlocked(
                f"host {host!r} resolves to a blocked address ({ip}); "
                "internal/link-local targets are not permitted"
            )
    return url
