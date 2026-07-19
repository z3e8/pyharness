"""The read-only inbox capability, against a fake in-memory IMAP server.

The fake mimics just enough of `imaplib.IMAP4_SSL` (login, readonly select,
UID SEARCH/FETCH, logout) to exercise the real parsing, extraction, and
security paths: PEEK-only fetches, vault-side credentials, redaction, and
attachment spill-to-workspace.
"""

from __future__ import annotations

import imaplib
from email.message import EmailMessage

import pytest

from pyharness.broker.capabilities.inbox import InboxCapability
from pyharness.core.workspace import Workspace
from pyharness.security.vault import Vault

PASSWORD = "app-pw-123"


def _message(
    subject: str, body: str, *, date: str, html: str | None = None
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Shop <noreply@shop.test>"
    msg["To"] = "agent@example.test"
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


class _FakeIMAP:
    """One instance per `_connection()`; canned mailbox set on the class."""

    instances: list = []
    # folder -> ordered {uid: {"flags": str, "raw": bytes, "bodystructure": str}}
    mailbox: dict[str, dict[str, dict]] = {}

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.logins: list[tuple[str, str]] = []
        self.selects: list[tuple[str, bool]] = []
        self.commands: list[tuple[str, tuple]] = []
        self.logged_out = False
        self._folder: dict[str, dict] | None = None
        _FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.logins.append((user, password))
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self.selects.append((mailbox, readonly))
        name = mailbox.strip('"')
        if name not in self.mailbox:
            return "NO", [b"no such folder"]
        self._folder = self.mailbox[name]
        return "OK", [str(len(self._folder)).encode()]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]

    def uid(self, command, *args):
        self.commands.append((command, args))
        if command == "SEARCH":
            uids = [
                uid
                for uid, m in self._folder.items()
                if args != ("UNSEEN",) or "\\Seen" not in m["flags"]
            ]
            return "OK", [" ".join(uids).encode()]
        assert command == "FETCH"
        uid_set, spec = args
        if "HEADER.FIELDS" in spec:
            items: list = []
            for uid in uid_set.split(","):
                m = self._folder.get(uid)
                if m is None:
                    continue
                headers = m["raw"].split(b"\n\n", 1)[0] + b"\n"
                meta = (
                    f"1 (UID {uid} FLAGS ({m['flags']}) BODYSTRUCTURE {m['bodystructure']} "
                    f"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {{{len(headers)}}}"
                ).encode()
                items += [(meta, headers), b")"]
            return "OK", items
        m = self._folder.get(uid_set)
        if m is None:
            return "OK", [None]
        meta = f"1 (UID {uid_set} BODY[] {{{len(m['raw'])}}}".encode()
        return "OK", [(meta, m["raw"]), b")"]


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    plain = _message(
        "Your verification code",
        "Your code is 424242.\nConfirm: https://shop.test/verify?t=abc.\nThanks!",
        date="Mon, 13 Jul 2026 10:00:00 +0000",
    )
    rich = _message(
        "Sign in to Site",
        "Use the magic link: https://site.test/magic?k=1",
        date="Tue, 14 Jul 2026 09:00:00 +0000",
        html=(
            "<html><body><p>Welcome back.</p>"
            '<a href="https://site.test/magic?k=1">Sign in</a>'
            '<a href="https://site.test/help">Help</a></body></html>'
        ),
    )
    rich.add_attachment(
        b"%PDF-1.7 fake", maintype="application", subtype="pdf", filename="report.pdf"
    )
    rich.add_attachment(f"col\nleak {PASSWORD} here\n", filename="../../evil.csv")
    old = _message("Old news", "nothing to see", date="Sun, 01 Feb 2026 08:00:00 +0000")

    _FakeIMAP.instances = []
    _FakeIMAP.mailbox = {
        "INBOX": {
            "3": {
                "flags": "\\Seen",
                "raw": old.as_bytes(),
                "bodystructure": '("text" "plain" NIL NIL NIL "7bit" 14 1)',
            },
            "7": {
                "flags": "",
                "raw": plain.as_bytes(),
                "bodystructure": '("text" "plain" NIL NIL NIL "7bit" 64 3)',
            },
            "9": {
                "flags": "",
                "raw": rich.as_bytes(),
                "bodystructure": '(("text" "plain") ("application" "pdf" ("name" "report.pdf") '
                'NIL NIL "base64" 13) "mixed" ("attachment" ("filename" "report.pdf")))',
            },
        }
    }
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)
    monkeypatch.setenv("PYHARNESS_IMAP_HOST", "imap.test")
    monkeypatch.setenv("PYHARNESS_IMAP_USER", "agent@example.test")
    return InboxCapability(Workspace(tmp_path), vault=Vault({"imap": PASSWORD}))


def test_list_returns_metadata_newest_first(inbox):
    rows = inbox.list()
    assert [r["id"] for r in rows] == ["9", "7", "3"]
    code = rows[1]
    assert code["from"] == "Shop <noreply@shop.test>"
    assert code["subject"] == "Your verification code"
    assert code["seen"] is False and rows[2]["seen"] is True
    assert rows[0]["has_attachments"] is True and code["has_attachments"] is False
    assert all("text" not in r and "body" not in r for r in rows)  # metadata only


def test_list_respects_limit_and_unseen(inbox):
    assert [r["id"] for r in inbox.list(limit=1)] == ["9"]
    unseen = inbox.list(unseen_only=True)
    assert [r["id"] for r in unseen] == ["9", "7"]
    imap = _FakeIMAP.instances[-1]
    assert ("SEARCH", ("UNSEEN",)) in imap.commands


def test_connection_is_readonly_peek_and_vault_backed(inbox):
    inbox.list()
    inbox.read("7")
    for imap in _FakeIMAP.instances:
        assert imap.logins == [("agent@example.test", PASSWORD)]
        assert all(readonly for _, readonly in imap.selects)
        assert imap.selects[0][0] == '"INBOX"'
        for command, args in imap.commands:
            if command == "FETCH":
                assert "BODY.PEEK[" in args[1]  # never a \Seen-flipping fetch
        assert imap.logged_out


def test_search_builds_imap_criteria(inbox):
    inbox.search(
        query="invoice", from_="noreply@shop.test", subject="July", since="2026-07-05"
    )
    command, args = next(
        c for c in _FakeIMAP.instances[-1].commands if c[0] == "SEARCH"
    )
    assert args == (
        "TEXT",
        '"invoice"',
        "FROM",
        '"noreply@shop.test"',
        "SUBJECT",
        '"July"',
        "SINCE",
        "05-Jul-2026",
    )


def test_read_plain_body_with_links(inbox):
    msg = inbox.read("7")
    assert msg["headers"]["subject"] == "Your verification code"
    assert "Your code is 424242." in msg["text"]
    assert {"text": "", "href": "https://shop.test/verify?t=abc"} in msg["links"]
    assert msg["attachments"] == []


def test_read_harvests_html_anchors_and_dedupes(inbox):
    msg = inbox.read("9")
    assert "magic link" in msg["text"]  # text prefers the plain part
    hrefs = [link["href"] for link in msg["links"]]
    assert hrefs.count("https://site.test/magic?k=1") == 1
    assert "https://site.test/help" in hrefs
    by_href = {link["href"]: link for link in msg["links"]}
    assert by_href["https://site.test/magic?k=1"]["text"] == "Sign in"


def test_read_html_only_message_reduces_to_text(inbox):
    only_html = EmailMessage()
    only_html["From"] = "a@b.test"
    only_html["Subject"] = "HTML only"
    only_html["Date"] = "Tue, 14 Jul 2026 11:00:00 +0000"
    only_html.set_content(
        "<html><body><h1>Welcome</h1><p>Verify at "
        '<a href="https://x.test/v?t=1">this link</a>.</p></body></html>',
        subtype="html",
    )
    _FakeIMAP.mailbox["INBOX"]["11"] = {
        "flags": "",
        "raw": only_html.as_bytes(),
        "bodystructure": '("text" "html")',
    }
    msg = inbox.read("11")
    assert "Welcome" in msg["text"] and "<h1>" not in msg["text"]
    assert [link["href"] for link in msg["links"]] == ["https://x.test/v?t=1"]


def test_read_spills_attachments_to_workspace(inbox, tmp_path):
    msg = inbox.read("9")
    ws_dir = tmp_path / "workspace"
    by_name = {a["filename"]: a for a in msg["attachments"]}
    pdf = by_name["report.pdf"]
    assert pdf["path"] == "inbox/INBOX/9/report.pdf"
    assert (ws_dir / pdf["path"]).read_bytes() == b"%PDF-1.7 fake"
    assert pdf["content_type"] == "application/pdf" and pdf["bytes"] == 13
    # A crafted traversal filename is flattened to its basename, and a secret
    # echoed into an attachment body is masked before it touches disk.
    evil = by_name["evil.csv"]
    assert evil["path"] == "inbox/INBOX/9/evil.csv"
    assert PASSWORD not in (ws_dir / evil["path"]).read_text()


def test_password_never_surfaces_in_results(inbox):
    leaky = _message(
        "psst",
        f"the password is {PASSWORD}, sshh",
        date="Tue, 14 Jul 2026 12:00:00 +0000",
    )
    _FakeIMAP.mailbox["INBOX"]["12"] = {
        "flags": "",
        "raw": leaky.as_bytes(),
        "bodystructure": '("text" "plain")',
    }
    msg = inbox.read("12")
    assert PASSWORD not in msg["text"] and "***" in msg["text"]


def test_read_missing_message_raises(inbox):
    with pytest.raises(KeyError):
        inbox.read("999")


def test_unconfigured_env_fails_with_pointer(inbox, monkeypatch):
    monkeypatch.delenv("PYHARNESS_IMAP_HOST")
    with pytest.raises(RuntimeError, match="PYHARNESS_IMAP_HOST"):
        inbox.list()


def test_missing_vault_secret_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)
    monkeypatch.setenv("PYHARNESS_IMAP_HOST", "imap.test")
    monkeypatch.setenv("PYHARNESS_IMAP_USER", "agent@example.test")
    empty = InboxCapability(Workspace(tmp_path), vault=Vault({}))
    with pytest.raises(KeyError, match="imap"):
        empty.list()
