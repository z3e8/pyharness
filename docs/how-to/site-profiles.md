# Keep the agent logged in (site profiles)

*Goal: let the agent act on an authenticated site without logging in (and passing
2FA) every session — while the cookies stay encrypted and out of the model's
reach.*

A browser context dies with the session, so without a profile every task on a
logged-in site re-authenticates. A **profile** saves that browser's login state
(cookies + localStorage), encrypted, under a name. The rule mirrors the vault:
**the agent references a profile by name; the cookie material is resolved in the
parent and never returned to agent code.** See
[Security & audit](../explanation/security-and-audit.md#site-profiles--a-login-the-agent-can-name-but-never-read)
for why.

Profiles are sealed with the **same passphrase as the vault**
(`PYHARNESS_VAULT_PASSPHRASE`) under `~/.pyharness/profiles/<name>.enc` (override
`PYHARNESS_PROFILES_DIR`). With no passphrase set, profiles are unavailable and
opening one raises — there is no plaintext fallback.

## Create a profile

**With the CLI (best for 2FA).** A headed browser opens; you log in yourself —
password, 2FA, "trust this device", all of it — then press Enter to capture and
encrypt the state:

```bash
pyharness-profiles login linkedin https://www.linkedin.com/login
# ... log in in the window, then press Enter here ...
pyharness-profiles list            # name, saved-at, cookie count, domains — never values
pyharness-profiles rm linkedin
```

**Or the agent creates it.** The agent opens a plain browser, logs in with
`fill_secret`, passes TOTP 2FA itself with `fill_totp` if the site's seed is in
the vault ([store one](use-the-vault.md#totp-seeds-2fa) as `<site>_totp`;
emailed codes it can read via the `inbox` tool, anything else you relay through
the conversation), then calls `save_profile` — which
prompts for approval, since it writes a standing credential:

```python
b = use_tool("browser")
sid = b.open_browser()
b.goto(sid, "https://www.linkedin.com/login")
b.snapshot(sid)                                 # see the fields
b.fill_secret(sid, ref="e5", secret_name="linkedin_email")
b.fill_secret(sid, ref="e6", secret_name="linkedin_password")
b.click(sid, ref="e7")                          # sign in
b.fill_totp(sid, ref="e9", secret_name="linkedin_totp")  # 2FA, if asked
b.save_profile(sid, "linkedin")                 # approval prompt; cookies encrypted
```

The same pair covers a profile whose session has expired: the agent re-logs-in
unattended (each `fill_*` prompting for approval) instead of handing the task
back to you.

## Use a profile

Every later session opens already logged in — one approval, no login:

```python
b = use_tool("browser")
b.list_profiles()                               # -> ["linkedin"]  (names only)
sid = b.open_browser(profile="linkedin")        # approval prompt: "open as linkedin"
b.goto(sid, "https://www.linkedin.com/feed")    # already authenticated
```

When the session closes, the (rotated) state re-saves automatically, so the login
keeps working as the site refreshes its cookies.

## What is and isn't protected

- Cookies are encrypted at rest and **never returned to agent code** —
  `open_browser`/`save_profile`/`list_profiles` return only a session id, counts,
  or names.
- **Opening or saving a profile needs approval.** Opening one is category OUTWARD:
  the session can then act and read *as that identity*.
- A profile session gives the agent the *powers* of the login (a free `read_text`
  can read your inbox), not the credential bytes. Grant only profiles whose
  authority you are comfortable handing to a supervised session.
- Auto-refresh saves **every** cookie the context picked up, so keep a profile
  session on its own site. A few sites store auth in `sessionStorage`, which is not
  captured — those will still ask to log in.
