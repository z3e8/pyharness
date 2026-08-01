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

A name-based check alone would be best-effort only, because it leaves the two
classic gaps between *vetting* a host and *connecting* to it:

- **Re-resolution (DNS rebinding).** The guard resolves the name and the client
  resolves it again at connect, so a racing resolver can show the guard a public
  address and the socket a link-local one.
- **Re-parsing.** The guard reads the hostname out of the URL string and the
  client derives its own from the same string. They can disagree — most sharply
  on IDNA, where CPython's `getaddrinfo` applies IDNA 2003 (`faß.example.com` ->
  `fass.example.com`) and httpx applies IDNA 2008 (`xn--fa-hia.example.com`).
  Two different names, one of them never vetted.

`PinnedTransport` closes both for every request the harness makes with httpx
(the `http` capability and remote MCP): it takes the host from httpx's *own*
parse, resolves it once, and then connects to that vetted address — the URL's
host is rewritten to the IP while `Host` and the TLS `sni_hostname` keep the
original name, so virtual hosting and certificate verification are unaffected.
There is no second parse and no second resolution to race.

Two paths stay name-based and therefore best-effort: the browser (Playwright
owns Chromium's socket, so nothing here can pin it — every request is re-vetted
by the route handler instead), and any session behind an HTTP(S) proxy, where
the socket goes to the proxy rather than the target and pinning cannot apply.
Both are called out in docs/explanation/threat-model.md.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

import idna

BLOCK_PRIVATE_ENV = "PYHARNESS_BLOCK_PRIVATE_NETWORK"
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Proxy variables httpx honours from the environment. When any is set the
# request leaves for the proxy, not for the target host, so pinning the URL to a
# vetted IP would both fail to describe the real connection and break the proxy's
# own routing — `pinned_client` falls back to a plain client there.
_PROXY_ENV = ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY")


class EgressBlocked(Exception):
    """Raised when a URL's host is not a permitted outbound target."""


def ascii_host(host: str) -> str:
    """The A-label form of `host` — the name an HTTP client will actually put on
    the wire and resolve.

    This is the single-parse rule: vet the name the client will use, not a
    lookalike derived by a different code path. An IP literal and an all-ASCII
    name pass through lowercased; a non-ASCII name is IDNA-encoded with the same
    call httpx makes (`idna.encode(host.lower())`, IDNA 2008), so the two cannot
    disagree. Leaving it to the resolver instead would apply CPython's *IDNA
    2003* mapping and silently vet a different host — `faß.example.com` becomes
    `fass.example.com` there and `xn--fa-hia.example.com` in the client.

    A name that will not IDNA-encode raises `EgressBlocked` rather than being
    passed through unencoded: the client would refuse it anyway, and guessing is
    exactly the divergence this function exists to remove."""
    host = host.strip().lower()
    if host.isascii():
        return host
    try:
        return idna.encode(host).decode("ascii")
    except (idna.IDNAError, UnicodeError) as exc:
        raise EgressBlocked(
            f"host {host!r} is not a usable hostname ({exc}); refusing"
        ) from exc


def normalize_scope_hosts(entries) -> frozenset[str]:
    """Canonicalize a host-scope allowlist (e.g. `spawn(allowed_hosts=...)` or
    `Session(allowed_hosts=...)`) into the frozenset `host_in_scope` matches
    against. Accepts bare hostnames, full URLs (the hostname is extracted),
    `host:port` (the port is dropped — scope is per host), and `*.example.com`
    (normalized to `example.com`; suffix matching makes the wildcard implicit).
    Case-insensitive, trailing dots stripped. An entry that yields no hostname
    raises `ValueError` — a scope must never silently cover less than asked."""
    hosts: set[str] = set()
    for entry in entries:
        raw = str(entry).strip()
        target = raw if "://" in raw else "//" + raw
        try:
            host = urlsplit(target).hostname
        except ValueError:
            host = None
        if not host:
            raise ValueError(
                f"unusable allowed_hosts entry {raw!r} — need a hostname or URL"
            )
        host = host.lower().rstrip(".")
        if host.startswith("*."):
            host = host[2:]
        if not host:
            raise ValueError(f"unusable allowed_hosts entry {raw!r} — empty hostname")
        # Same alphabet as the hosts this scope is matched against: an entry
        # written in unicode (`café.example`) has to become the A-label the
        # request will carry, or it could never match.
        try:
            host = ascii_host(host)
        except EgressBlocked as exc:
            raise ValueError(f"unusable allowed_hosts entry {raw!r} — {exc}") from exc
        hosts.add(host)
    return frozenset(hosts)


def host_in_scope(host: str, allowed_hosts: frozenset[str]) -> bool:
    """Whether `host` is covered by a normalized scope: equal to an entry, or a
    dot-subdomain of one (`api.github.com` under `github.com`). Entries are
    authored deliberately (and shown to the approving human), so suffix
    semantics are intent, not laxity — grants stay exact-match separately."""
    host = host.lower().rstrip(".")
    return any(host == entry or host.endswith("." + entry) for entry in allowed_hosts)


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


def vet_host(
    host: str, allowed_hosts: frozenset[str] | None = None
) -> list[ipaddress._BaseAddress]:
    """Vet an already-parsed host and return the addresses it is permitted to be
    reached at, raising `EgressBlocked` otherwise.

    The shared core of the guard: host scope, then resolve, then the range
    checks. Callers that hold a URL string use `check_url`; `PinnedTransport`
    calls this directly with the host httpx parsed, and *connects to what comes
    back* — which is why the vetted addresses are returned rather than discarded.
    """
    host = ascii_host(host)
    if allowed_hosts is not None and not host_in_scope(host, allowed_hosts):
        raise EgressBlocked(
            f"host {host!r} is outside this session's allowed hosts "
            f"({', '.join(sorted(allowed_hosts))})"
        )
    strict = _strict()
    ips = _candidate_ips(host)
    for ip in ips:
        if _blocked(ip, strict):
            raise EgressBlocked(
                f"host {host!r} resolves to a blocked address ({ip}); "
                "internal/link-local targets are not permitted"
            )
    return ips


def check_url(url: str, allowed_hosts: frozenset[str] | None = None) -> str:
    """Raise `EgressBlocked` if `url` is not a permitted outbound target; return it
    unchanged otherwise. Enforces the http(s)-only + no-link-local rules above (plus
    private/loopback when PYHARNESS_BLOCK_PRIVATE_NETWORK is set).

    `allowed_hosts` (a set normalized by `normalize_scope_hosts`) additionally
    confines the target to those hosts and their subdomains — the host-scope
    check for scoped sessions/spawned children. None means unscoped; the SSRF
    rules apply either way.

    This is the *pre-flight* check: it refuses a bad target before any connection
    is opened and before a credential is attached, and it is what the browser
    route handler has. On the httpx paths `PinnedTransport` re-runs the same core
    at connect time and is the enforcement point; here the value is the early,
    specific refusal."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise EgressBlocked(
            f"scheme {scheme or '(none)'!r} is not permitted (only http/https)"
        )
    host = parts.hostname
    if not host:
        raise EgressBlocked(f"url {url!r} has no host")
    vet_host(host, allowed_hosts)
    return url


# ---------------------------------------------------------------------------
# Connection pinning
# ---------------------------------------------------------------------------


def _pin_target(ips: list[ipaddress._BaseAddress]) -> ipaddress._BaseAddress:
    """Which vetted address to connect to. IPv4 first when the host offers both:
    the same preference `llm/client.py` already settled on after an IPv6 path
    killed streaming, and the safer default on hosts with a routable-looking but
    dead IPv6 address. Pinning is deliberately to *one* address, so a host whose
    first address is down is not retried at a second — a fetch failure, traded
    for there being no unvetted address left to reach."""
    return next((ip for ip in ips if ip.version == 4), ips[0])


def proxy_configured() -> bool:
    """Whether the environment points httpx at an HTTP(S) proxy."""
    return any(
        os.environ.get(name) or os.environ.get(name.lower()) for name in _PROXY_ENV
    )


class PinnedTransport:
    """An httpx transport that vets each request's host and then connects to the
    address it vetted.

    This is the enforcement point for the `http` capability and remote MCP.
    Everything else in this module reads a URL *string*; this reads
    `request.url.raw_host`, which is httpx's own parse of the host it is about to
    connect to — so there is no second interpretation to disagree with (the IDNA
    divergence in `ascii_host`'s docstring), and because the vetted address is
    then written into the URL there is no second DNS lookup to race (rebinding).

    The rewrite is invisible to the server and to the caller: `Host` keeps the
    original name so virtual hosting still works, `sni_hostname` keeps it so TLS
    verifies the certificate against the real name and not against an IP, and
    httpx re-attaches the *original* request to the response, so redirects and
    `response.url` resolve against the name the agent asked for.

    Not a subclass of `httpx.HTTPTransport`: the inner transport is built lazily
    on first use so that constructing a client (as every one-shot `http.request`
    does) allocates no connection pool until a request is actually made."""

    def __init__(self, allowed_hosts: frozenset[str] | None = None, inner=None):
        self._allowed_hosts = allowed_hosts
        self._inner = inner

    def _transport(self):
        if self._inner is None:
            from importlib import import_module

            self._inner = import_module("httpx").HTTPTransport()
        return self._inner

    def handle_request(self, request):
        url = request.url
        scheme = url.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise EgressBlocked(
                f"scheme {scheme or '(none)'!r} is not permitted (only http/https)"
            )
        host = (url.raw_host or b"").decode("ascii")
        if not host:
            raise EgressBlocked(f"url {str(url)!r} has no host")
        ips = vet_host(host, self._allowed_hosts)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            request = self._pin(request, host, _pin_target(ips))
        return self._transport().handle_request(request)

    @staticmethod
    def _pin(request, host: str, ip: ipaddress._BaseAddress):
        """Rebuild `request` aimed at `ip` while it still speaks for `host`.

        A copy, not a mutation: httpx hands us the caller's request object and
        keeps using it afterwards (`response.request`), so rewriting it in place
        would make a relative `Location` resolve against the IP and put the IP in
        front of the per-hop scope check."""
        from importlib import import_module

        httpx = import_module("httpx")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=str(ip)),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        # httpx regenerates Host from the (now numeric) URL; put the real name
        # back so name-based virtual hosts still route.
        port = request.url.port
        pinned.headers["Host"] = f"{host}:{port}" if port is not None else host
        return pinned

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def pinned_client(*, allowed_hosts: frozenset[str] | None = None, **kwargs):
    """An `httpx.Client` whose every request is vetted and pinned by
    `PinnedTransport` — the constructor the harness uses instead of
    `httpx.Client(...)` wherever the agent can influence the target.

    Falls back to a plain client when a proxy is configured: an explicit
    transport bypasses httpx's proxy mounts, and the socket would go to the proxy
    rather than to the address we vetted, so pinning there would break routing
    without describing the real connection. Such a session keeps the name-based
    `check_url` guard and the residual rebinding exposure that comes with it (see
    docs/explanation/threat-model.md)."""
    from importlib import import_module

    httpx = import_module("httpx")
    if proxy_configured():
        return httpx.Client(**kwargs)
    return httpx.Client(transport=PinnedTransport(allowed_hosts), **kwargs)
