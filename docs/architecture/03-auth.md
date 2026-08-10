# 03 - Credentials and OAuth

Agent 3 deliverable. Covers how any provider (source or store) obtains, stores,
refreshes, verifies and revokes a credential, and how a dead credential reaches
the user.

Written 2026-08-09. Companion documents: `01-sources.md`, `02-stores.md`,
`04-identity.md`, `05-runtime.md`.

---

## Decisions

1. **One `AuthMethod` abstraction, six concrete methods.** The brief names five
   patterns; there are actually six, because 7digital is OAuth **1.0a**, not
   OAuth 2, and 1.0a needs per-request HMAC signing rather than a bearer header.
   The methods are `oauth2`, `oauth1a`, `lastfm_web`, `token_paste`, `userpass`,
   `cookie_jar`. A provider declares one; it does not implement auth itself.

2. **Every flow ends at the same place**: a `Credential` written through
   `CredentialStore.put()`. The differences between the six live entirely in
   `begin()` / `complete()`, which the UI renders from a closed set of four step
   shapes (`RedirectStep`, `FormStep`, `PollStep`, `ExternalStep`).

3. **The OAuth redirect problem is solved by a ladder, not a single mechanism**,
   tried in this order per provider:
   1. **Device authorization grant** (RFC 8628) when the provider offers it. No
      redirect exists, so nothing can be wrong. None of Deezer / Tidal public /
      7digital offer it today, so this is a hook, not a current path.
   2. **Same-origin redirect** to `${PUBLIC_URL}/auth/<provider>/callback`. This
      is the default and it covers most cases, for a reason that is easy to miss:
      **there is no shipped client_id.** One instance per user means each user
      registers their own OAuth app, so each user *chooses* the redirect URI, and
      can choose their own instance address. Deezer's own developer guidance
      tells headless users to register `http://<ip>:<port>/auth`.
   3. **Paste the callback URL** when the provider rejects a private or
      `http://` host. Register the loopback URI `http://127.0.0.1:<port>/auth/<p>/callback`
      (RFC 8252 loopback exemption, honoured almost universally), let the browser
      fail to load it, and have the user copy the address bar into a text box.
      One paste per provider per lifetime. Works from any browser on any machine,
      needs no third party and no extra listener.
   4. **Manual `code` paste** for providers that still support
      `urn:ietf:wg:oauth:2.0:oob` (Trakt-style) or that display a PIN.

4. **No relay is built or operated.** A public redirect page (the
   `my.home-assistant.io/redirect/oauth` model) would be a central server, which
   locked decision 1 rules out, and it is a permanent operational commitment: if
   the DNS name lapses, every install's authorization breaks at once. The design
   is nonetheless relay-*ready*, because step 3.2 is only "a URL from config":
   a user who wants one sets `OAUTH_REDIRECT_URL` and hosts the 40-line static
   page in the appendix themselves. That page is provided, with its caveats.

5. **PKCE (S256) is mandatory** on every OAuth 2 provider that supports it, and
   `state` is a 32-byte random bound to a single-use `auth_pending` row with a
   10-minute TTL. This is what makes the paste-the-URL fallback and any
   user-hosted relay safe: an authorization code seen by a log, a shoulder, or a
   proxy is inert without the verifier, which never leaves the instance.

6. **Secrets are encrypted at rest with AES-256-GCM** in a `credentials.secret_enc`
   BLOB, keyed from `LW_SECRET_KEY` (env, preferred) or `/config/secret.key`
   (auto-generated, `0600`). This adds `cryptography` as a second runtime
   dependency, and that is the right trade: the alternative is hand-rolling a
   cipher out of `hmac`, which is worse than storing plaintext because it looks
   safe. **Section 6 is explicit about what this does and does not buy**, and the
   short version is: it protects a database that leaves the box, and it protects
   nothing at all from anyone who can run code as the app user.

7. **The cookie broker is integrated, not redesigned.** `SessionKeeper` already
   solves session persistence, rotation absorption, keepalive, live-probe status
   and dead-edge alerting, and it is proven against live Qobuz. The `cookie_jar`
   auth method is a thin adapter over it. One small **additive** upstream change
   is requested (optional `load_state` / `save_state` hooks on `SiteConfig`, so
   the jar can share the same encryption as everything else); the default
   behaviour is unchanged if the hooks are absent.

8. **`/auth/ingest` and `/auth/status` keep working** and stay as permanent
   aliases so deployed extension installs do not break. The canonical mount moves
   to `/auth/cookie/*` to keep the `/auth/<provider>/*` namespace clean.

9. **Transport failure and credential failure are different types**, enforced by
   an exception split (`TransportError` vs `AuthError`). Only `AuthError` may
   move a credential out of `live`. This is the single rule that stops a flaky
   link from marking every provider dead and paging the user at 3am.

10. **Refresh happens in a janitor job, never in a request path.** A request that
    meets a 401 gets exactly one inline refresh-and-retry, under a per-provider
    re-entrant lock, for the same reason `SessionKeeper` holds its lock across
    the whole load/request/save cycle: two concurrent consumers of a rotating
    credential both present the old value and the loser kills the session.

11. **Refresh-token rotation persists before it is used.** Order is exchange,
    fsync the new tokens, then make the first real call. The previous refresh
    token is retained for one cycle, because a response lost in flight after a
    successful server-side rotation otherwise bricks the account permanently.

12. **A dead credential is a first-class UI object, not an error toast.**
    `credentials.status` is the single source of truth, it is pushed over SSE as
    a `credential` event carrying a closed-set `action`
    (`reauthorize | reseed_cookie | paste_token | none`), and affected wishlist
    rows disable Claim with the specific reason inline. Status also has an
    `expiring` value so a non-refreshable credential (Deezer issues no refresh
    token) warns before it dies rather than after.

13. **No endpoint ever returns secret material.** Not for debugging, not behind a
    flag. `/api/credentials` returns status, account label, expiry and last error.
    The extension already follows this rule (`diagnose()` logs cookie *names*
    only); the server side matches it.

---

## 1. Data model

### 1.1 `credentials`

One row per provider. Single-user instance, so no owner column.

```sql
CREATE TABLE credentials (
  provider              TEXT PRIMARY KEY,   -- "lastfm", "qobuz", "deezer", "navidrome"
  method                TEXT NOT NULL,      -- oauth2|oauth1a|lastfm_web|token_paste|userpass|cookie_jar
  status                TEXT NOT NULL DEFAULT 'absent',
                        -- absent|pending|live|expiring|expired|revoked|error
  secret_enc            BLOB,               -- AESGCM(nonce || ciphertext || tag) over a JSON object
  key_version           INTEGER NOT NULL DEFAULT 1,
  public_meta           TEXT NOT NULL DEFAULT '{}',  -- JSON, non-secret only
  expires_at            INTEGER,            -- unix seconds; NULL = no known expiry
  refresh_after         INTEGER,            -- unix seconds; when the janitor should act
  last_ok_at            INTEGER,
  last_probe_at         INTEGER,
  last_error            TEXT,
  last_error_at         INTEGER,
  consecutive_failures  INTEGER NOT NULL DEFAULT 0,
  created_at            INTEGER NOT NULL,
  updated_at            INTEGER NOT NULL
);
CREATE INDEX credentials_refresh ON credentials(refresh_after)
  WHERE refresh_after IS NOT NULL;
```

`expires_at` and `refresh_after` are deliberately outside the encrypted blob. The
janitor must schedule without decrypting anything, and an expiry timestamp is not
a secret. `public_meta` is the same bargain: the account display name, the granted
scopes, a `client_id` prefix for support, `refreshable: true|false`. If a value
would embarrass the user in a screenshot, it belongs in `secret_enc`.

### 1.2 Secret blob shape

The plaintext under `secret_enc` is a JSON object with namespaced keys, so one row
holds both the user-registered app credentials and the acquired user tokens:

```json
{
  "app":   { "client_id": "...", "client_secret": "..." },
  "token": { "access_token": "...", "refresh_token": "...",
             "prev_refresh_token": "...", "token_type": "Bearer",
             "scope": "basic_access,manage_library" }
}
```

Per method:

| Method | `app` | `token` |
|---|---|---|
| `oauth2` | client_id, client_secret (may be absent for public clients) | access, refresh, prev_refresh |
| `oauth1a` | consumer_key, consumer_secret | oauth_token, oauth_token_secret |
| `lastfm_web` | api_key, api_secret | session_key |
| `token_paste` | - | token |
| `userpass` | - | username, password |
| `cookie_jar` | - | (empty; the jar lives with the broker, see 5.6) |

### 1.3 `auth_pending`

In-flight authorizations. Short-lived, single use, and swept by the janitor.

```sql
CREATE TABLE auth_pending (
  state         TEXT PRIMARY KEY,   -- 32 random bytes, urlsafe-b64
  provider      TEXT NOT NULL,
  mode          TEXT NOT NULL,      -- same_origin|loopback_paste|relay|oob|device
  verifier_enc  BLOB,               -- PKCE code_verifier, or the OAuth1 request-token secret
  redirect_uri  TEXT NOT NULL,      -- replayed verbatim at token exchange; providers compare it
  device_code_enc BLOB,             -- RFC 8628 only
  poll_interval INTEGER,            -- RFC 8628 only
  created_at    INTEGER NOT NULL,
  expires_at    INTEGER NOT NULL    -- created_at + 600, or the device code's own expiry
);
```

`redirect_uri` is stored rather than recomputed. Providers compare the value sent
at `/token` against the value sent at `/authorize` byte for byte, and recomputing
it from config lets a `PUBLIC_URL` edit mid-flow produce an error message that
blames the provider.

### 1.4 Migration from today

There is nothing to migrate. Today's credentials are `queue/qobuz_cookie.txt`,
`queue/qobuz_jar.json` and env vars. The jar keeps its own file (section 5.6);
the env vars become the `app` half of a credential row on first boot (section 7.3).

---

## 2. Interfaces

### 2.1 `CredentialStore`

The only code that touches `secret_enc`. Everything else deals in dataclasses.

```python
@dataclass(frozen=True)
class Credential:
    provider: str
    method: str
    status: str
    secret: dict            # decrypted; never logged, never serialized to a response
    public_meta: dict
    expires_at: int | None

@dataclass(frozen=True)
class CredentialStatus:     # the safe projection, what the API and SSE emit
    provider: str
    method: str
    status: str
    account: str | None
    detail: str | None
    action: str             # none|reauthorize|reseed_cookie|paste_token
    expires_at: int | None
    last_ok_at: int | None
    refreshable: bool

class CredentialStore:
    def get(self, provider: str) -> Credential | None: ...
    def require(self, provider: str) -> Credential: ...        # raises AuthError if absent/expired
    def put(self, provider: str, method: str, secret: dict, *,
            public_meta: dict | None = None,
            expires_at: int | None = None,
            status: str = "live") -> None: ...
    def patch_secret(self, provider: str, **fields) -> None:   # shallow-merge, for refresh
        ...
    def mark_ok(self, provider: str) -> None: ...
    def mark_failed(self, provider: str, detail: str, *, fatal: bool) -> None: ...
    def revoke(self, provider: str) -> None: ...               # provider revoke + wipe row
    def status(self, provider: str) -> CredentialStatus: ...
    def all_status(self) -> list[CredentialStatus]: ...
```

`patch_secret` exists so a refresh writes only `token.access_token` and
`token.refresh_token` and cannot accidentally drop `app.client_secret` by putting
a partial object.

### 2.2 `AuthMethod`

```python
class AuthMethod(Protocol):
    id: str

    def describe(self, spec: AuthSpec) -> AuthDescriptor:
        """Static UI copy: what the user is about to do and what they need first
        (a registered app, a token from a settings page, an installed extension)."""

    def begin(self, ctx: AuthContext) -> AuthStep:
        """Start a flow. May write auth_pending. Must not write credentials."""

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        """Finish it. Consumes the auth_pending row. Writes the credential only
        after verify() passes, so a half-authorized provider never shows live."""

    def refresh(self, ctx: AuthContext, cred: Credential) -> Credential | None:
        """None means 'nothing to refresh' (Last.fm session keys, Subsonic).
        Raises AuthError(fatal=True) when the refresh token itself is rejected."""

    def verify(self, ctx: AuthContext, cred: Credential) -> tuple[bool, str]:
        """A LIVE call against the provider. Never a stored opinion."""

    def revoke(self, ctx: AuthContext, cred: Credential) -> None:
        """Best effort provider-side revocation; the local wipe happens regardless."""
```

`AuthContext` carries the store, an HTTP client with the browser UA, the resolved
redirect URI, a logger bound to `provider=`, and the provider's `AuthSpec`.

### 2.3 What a provider declares

A provider does not implement any of the above. It declares a spec:

```python
@dataclass(frozen=True)
class AuthSpec:
    method: str                       # picks the AuthMethod
    label: str                        # "Deezer"
    setup_url: str | None             # where the user registers an app
    setup_help: str | None            # one paragraph, rendered above the form
    # oauth2 / oauth1a
    authorize_url: str | None = None
    token_url: str | None = None
    revoke_url: str | None = None
    device_url: str | None = None     # presence enables rung 1 of the ladder
    scopes: tuple[str, ...] = ()
    pkce: bool = True
    redirect_modes: tuple[str, ...] = ("same_origin", "loopback_paste")
    # everything
    verify: Callable[[AuthContext, Credential], tuple[bool, str]] | None = None
```

Example, complete:

```python
DEEZER_AUTH = AuthSpec(
    method="oauth2",
    label="Deezer",
    setup_url="https://developers.deezer.com/myapps",
    setup_help=(
        "Create an app at Deezer, then paste its Application ID and Secret "
        "below. Deezer allows exactly ONE redirect URL per app, so set it to "
        "the value shown after you paste the ID."
    ),
    authorize_url="https://connect.deezer.com/oauth/auth.php",
    token_url="https://connect.deezer.com/oauth/access_token.php",
    scopes=("basic_access", "offline_access"),
    pkce=False,                       # Deezer does not implement PKCE
    redirect_modes=("same_origin",),  # Deezer accepts a private IP; no fallback needed
)
```

### 2.4 Step shapes

Four, and the UI has a renderer for each. Adding a provider never adds a fifth.

```python
@dataclass
class RedirectStep:      # send the browser to the provider
    url: str
    state: str
    fallback: "FormStep | None"   # shown as "the page did not load?" beneath

@dataclass
class FormStep:          # render fields, POST to /auth/<p>/complete
    fields: list[Field]  # Field(name, label, type=text|password|url, help, placeholder)
    instructions: str
    open_url: str | None # a link to open first, e.g. the Last.fm allow page

@dataclass
class PollStep:          # RFC 8628, or Last.fm's token flow which is the same shape
    user_code: str | None
    verification_uri: str
    interval_seconds: int
    expires_at: int

@dataclass
class ExternalStep:      # the work happens outside this app: the cookie broker
    instructions: str
    status_url: str      # /auth/cookie/status/<site>
    doc_url: str
```

Note `PollStep` is what makes Last.fm cheap: its desktop flow (`auth.getToken`,
send the user to `last.fm/api/auth`, then `auth.getSession`) is structurally a
device grant with a token instead of a user code, so it reuses the poll renderer
rather than needing bespoke UI.

---

## 3. The OAuth redirect problem

This is the hard part of the brief, so the reasoning is set out before the answer.

### 3.1 Why it is usually hard, and why it is less hard here

The standard framing is: a hosted app has one client_id, one registered redirect
URI, and thousands of installs at addresses it cannot know. That is genuinely
hard, and it is why Home Assistant runs a redirect service.

**That is not our situation.** Locked decision 1 says one instance per user and no
central server, which means there is no shipped client_id, which means every user
registers their own OAuth app. A user registering their own app gets to type their
own redirect URI into the provider's form. The unpredictable address is only a
problem for whoever has to predict it, and here nobody does.

So the residual problem is narrower and more honest: **which providers refuse to
accept a private, non-TLS redirect URI even when the app's own owner is asking?**

Evidence gathered:

| Provider | Verdict |
|---|---|
| Deezer | Accepts it, and documents it. Deezer's own developer FAQ tells headless users to register a local IP with a port, e.g. `http://<ip>:8765/auth`, or `http://127.0.0.1:8765/auth`. One redirect URI per app, no wildcards. |
| Tidal | Auth code + PKCE with a registered redirect URI, developer-portal registered. Device grant exists (RFC 8628) but is documented as internal-apps-only, so treat it as unavailable. Whether a plain `http://` private host is accepted at registration is unverified. |
| 7digital | OAuth **1.0a**, not 2. Callback is set on the app or passed as `oauth_callback`; the family supports `oob`. Getting API access at all requires contacting them. |
| Last.fm | Not OAuth. The desktop flow needs no callback URL whatsoever. |
| ListenBrainz | Pasted token. No redirect. |
| Subsonic / Navidrome | Username plus salted token. No redirect. |
| Qobuz, Bandcamp | Cookie broker. No redirect. Bandcamp's API is gated to labels and fulfilment partners by manual approval, so it is not an alternative. |

Of the entire provider set, **exactly two** could plausibly need a redirect
workaround, and one of them (Deezer) explicitly does not.

### 3.2 The four candidate mechanisms

**A. Same-origin redirect from an explicitly configured public URL.**
`PUBLIC_URL` is a required env var; the callback is `${PUBLIC_URL}/auth/<p>/callback`.
The app prints it in the UI with a copy button so the user pastes it into the
provider's form rather than typing it.
*For:* the browser is already talking to this origin, so it demonstrably resolves;
no third party; no extra listener; refresh works forever after; identical code path
for `http://192.168.1.42:8080`, a tailnet name, and a Caddy-fronted HTTPS host.
*Against:* dies on a provider that requires https or rejects private hosts; a
`PUBLIC_URL` that does not match what the user actually types produces a redirect
mismatch whose error text names the provider, not the config.

**B. Loopback listener (RFC 8252).**
Register `http://127.0.0.1:PORT/callback` and have the app receive it directly.
*Against:* only works when the browser and the app are on the same machine. The
target deployment is a container beside Navidrome on a NAS or a Mac Mini, with the
browser on a laptop. **This mechanism is unavailable for the primary deployment
shape**, and it is worth saying so plainly rather than listing it as an option.

**C. Loopback registration plus paste of the callback URL.**
The half of B that survives. Register `http://127.0.0.1:8080/auth/<p>/callback`,
because the loopback exemption in RFC 8252 is honoured almost everywhere and
providers that reject private LAN IPs still accept 127.0.0.1. The browser is
redirected there, fails to connect, and leaves the complete URL in the address bar
including `?code=...&state=...`. The user copies the address bar into a single
text field. The app parses it, matches `state` against `auth_pending`, and
exchanges with the recorded `redirect_uri`.
*For:* universal; no relay; no listener; no same-machine requirement; one paste per
provider, ever, since refresh tokens carry it afterwards.
*Against:* it looks like a failure to a user who does not read the instruction, so
the copy has to be blunt ("The next page will fail to load. That is expected. Copy
the whole address from your browser's address bar and paste it here."). A code in
a browser history is a minor exposure, bounded by PKCE, single-use state and a
600-second TTL.

**D. Device authorization grant (RFC 8628).**
The user is shown a short code, enters it at the provider's verification URI on any
device, and the app polls the token endpoint. No redirect URI participates.
*For:* the correct answer, and the one Trakt-style ecosystems settled on for
exactly this class of app.
*Against:* our providers do not offer it publicly today. Build the hook, do not
build the expectation.

**E. A public relay page (the Home Assistant model), for completeness.**
`my.home-assistant.io/redirect/oauth` is registered as the provider's redirect
URI; the page reads the user's own instance URL from browser `localStorage` and
forwards the query string on. The instance URL is never sent to the service.
*For:* one registered URI works for every install; genuinely good UX.
*Against, and these are decisive here:*
- It is a central server, which locked decision 1 forbids.
- The authorization code and state transit a third-party origin's request line.
  Even as a static page, that is a TLS-terminating host and a CDN access log. HA
  has taken real user pushback on precisely this
  (core issue #104488, "forces user to share OAuth redirect URL with
  home-assistant.io"), and there is an existing feature request to support local
  redirect endpoints instead.
- It is a forever commitment. Every install's re-authorization depends on one DNS
  name continuing to resolve and one page continuing to be served, indefinitely,
  by a hobby project.
- If the instance URL is ever taken from `state` rather than from `localStorage`,
  the page becomes an open redirector that will forward authorization codes to any
  origin an attacker names.

### 3.3 Recommendation

**Do not build a relay. Ship the ladder: device grant if offered, otherwise
same-origin, otherwise paste-the-callback-URL.** In practice that means
same-origin for Deezer, same-origin-with-paste-fallback for Tidal, and nothing at
all for the majority of providers, which do not use OAuth 2.

Concretely, per provider the spec declares `redirect_modes`, and
`begin()` resolves in order:

```python
def resolve_redirect(spec: AuthSpec, cfg: Config) -> tuple[str, str]:
    """Returns (mode, redirect_uri)."""
    if cfg.oauth_redirect_url:                       # user-hosted relay escape hatch
        return "relay", cfg.oauth_redirect_url
    if spec.device_url:
        return "device", ""
    if "same_origin" in spec.redirect_modes and cfg.public_url:
        return "same_origin", f"{cfg.public_url}/auth/{spec.provider}/callback"
    if "loopback_paste" in spec.redirect_modes:
        return "loopback_paste", f"http://127.0.0.1:{cfg.port}/auth/{spec.provider}/callback"
    if "oob" in spec.redirect_modes:
        return "oob", "urn:ietf:wg:oauth:2.0:oob"
    raise ConfigError(f"{spec.label} needs PUBLIC_URL to be set")
```

The UI always shows the resolved `redirect_uri` verbatim, with a copy button and
the sentence "paste this into the provider's app settings exactly". A mismatch
between what the user registered and what the app sends is the single most common
failure in this whole area, and the fix is to never make the user retype it.

`OAUTH_REDIRECT_URL` is the escape hatch. Setting it puts the flow in `relay` mode
and the app treats the value as the registered redirect URI, expecting the code to
arrive back at `/auth/<p>/callback` by whatever means the user arranged. The
appendix has the static page. It is documented as user-hosted, and the risk note
travels with it.

### 3.4 The exchange, once the code is in hand

All four modes converge here, which is why the ladder costs so little:

```python
def exchange(ctx, state: str, code: str) -> Credential:
    row = ctx.pending.take(state)                 # single-use; deletes on read
    if row is None:
        raise AuthError("This authorization link has expired or was already used.")
    if row.provider != ctx.spec.provider:
        raise AuthError("state/provider mismatch")   # should be unreachable
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": row.redirect_uri,          # verbatim, from the row
        "client_id": ctx.secret["app"]["client_id"],
    }
    if ctx.spec.pkce:
        body["code_verifier"] = ctx.decrypt(row.verifier_enc)
    if secret := ctx.secret["app"].get("client_secret"):
        body["client_secret"] = secret
    tok = ctx.http.post_form(ctx.spec.token_url, body)   # raises AuthError on 400/401
    return ctx.store_token(tok)
```

`take()` deleting on read is what makes replay of a pasted URL harmless: paste the
same URL twice and the second attempt gets a clean "already used" rather than a
second exchange against a code the provider has already burned.

---

## 4. Encryption at rest, and what it is actually worth

### 4.1 Mechanism

- Cipher: AES-256-GCM via `cryptography`'s `AESGCM`. Nonce is 12 random bytes,
  fresh per write, stored as the blob prefix. AAD is
  `f"{provider}|{key_version}"`, so a blob cannot be moved between rows.
- Key: 32 bytes. Sourced in this order:
  1. `LW_SECRET_KEY`, base64 of 32 bytes. **Preferred.**
  2. `/config/secret.key`, generated on first boot with `os.urandom(32)`, written
     `0600` and owned by `PUID:PGID`.
  If neither exists and `/config` is not writable, the app refuses to start rather
  than falling back to plaintext. Same posture as `make_blueprint` refusing to run
  without `COOKIE_BROKER_TOKEN`.
- Rotation: `LW_SECRET_KEY_OLD` may be set for one boot. Startup re-wraps every row
  it cannot open with the current key, bumps `key_version`, and logs a count. The
  janitor logs a warning every hour while `LW_SECRET_KEY_OLD` is still set, so it
  does not become permanent.
- Adding `cryptography` is a real cost against the current one-dependency purity.
  It is accepted: manylinux and musllinux wheels exist for both amd64 and arm64, so
  the multi-arch image does not need a compiler. The rejected alternative was a
  hand-built HMAC-SHA256 keystream, which would be worse than plaintext because it
  would look like protection.

### 4.2 What it protects against

These are the realistic loss paths for a self-hosted app, and encryption covers
all of them:

- **The database leaving the box.** `/config` gets backed up to a NAS, synced to
  a cloud folder, snapshotted by the hypervisor, `docker cp`'d somewhere, or
  attached to a GitHub issue when the user asks for help. This is common, and it
  is the case that matters. It is covered **only if the key is not in the same
  place**, which is exactly why `LW_SECRET_KEY` in compose is the recommendation
  and `/config/secret.key` is the fallback.
- **Read access to the DB file without read access to the key.** Different modes,
  different owners, a bind mount shared read-only with another container.
- **Casual inspection.** `sqlite3 /config/wishlist.db 'select * from credentials'`
  is something a curious user or an unrelated script does by accident. Returning a
  BLOB instead of a Qobuz session cookie is worth something on its own.

### 4.3 What it does not protect against, stated plainly

- **Anything running as the app user.** The app must decrypt unattended on every
  boot, with no human present. That means the key is reachable by the process,
  which means it is reachable by anyone who can run code as that process. A
  malicious dependency, a template injection, an RCE in a route: all of them get
  the plaintext. Encryption at rest moves nothing here.
- **Root, or anyone with the host filesystem or the container image.**
- **A `/config` volume taken whole**, when the key lives in `/config/secret.key`.
  Key and ciphertext travel together and the encryption buys exactly zero. This is
  the default configuration, so the default configuration is the weak one. Say so
  in the README, not only here.
- **Anything running in the browser.** The cookie broker's threat model is
  unchanged and is structurally an infostealer by design; encrypting the jar at
  rest does not change what the extension can read.
- **A determined attacker of any kind.** This is not a security boundary.

The honest one-line summary, and the one that should appear in user-facing docs:
**this is envelope hygiene for a database that travels, not a defence against
anyone who is already on the box.** It is worth doing because it costs a
dependency and forty lines, not because it makes the box safe.

### 4.4 Blast radius, which is the more useful lever

Given 4.3, the design spends more effort on limiting what a stolen credential can
do than on making it hard to steal:

- Request the narrowest scopes that work, per provider, and record the granted set
  in `public_meta` so the UI can show what was actually granted versus asked.
- Never request write scopes on a source. Reading loves needs no write access
  anywhere.
- Keep the store credentials read-and-download only. Nothing in this app should be
  able to spend money; purchase happens in the user's own browser, at the store,
  by decision.
- `DELETE /auth/<provider>` calls the provider's revoke endpoint where one exists,
  so a user who suspects a leak has a working button rather than a support article.

---

## 5. The six methods

Each subsection states what is stored, whether it expires, how it refreshes, the
verify probe, and how it dies.

### 5.1 `oauth2` (Deezer, Tidal, and the general case)

- **Stored:** `app.client_id`, `app.client_secret`, `token.access_token`,
  `token.refresh_token`, `token.prev_refresh_token`.
- **begin():** resolve the redirect mode (3.3), create `auth_pending`, return a
  `RedirectStep` whose `fallback` is a `FormStep` with one field, `callback_url`.
- **complete():** either the `/callback` route or the pasted URL, both funnelling
  into `exchange()` (3.4). If a pasted URL contains `error=access_denied`, surface
  the provider's `error_description` verbatim rather than a generic failure.
- **Expiry:** from `expires_in`. `refresh_after = expires_at - max(300, lifetime // 10)`,
  jittered plus or minus 10 percent.
- **Refresh:** standard `grant_type=refresh_token`. **Deezer is the awkward one:
  it issues no refresh token at all**, so `refresh()` returns `None` and
  `public_meta.refreshable` is `false`. For those, the janitor moves the status to
  `expiring` at `expires_at - 72h` and emits a `credential` SSE with
  `action=reauthorize`, so the user is warned before the queue stops working
  rather than after.
- **Verify:** the provider's cheapest identity call (`GET /user/me` for Deezer,
  `GET /users/me` for Tidal). Success also refreshes `public_meta.account`.
- **Death:** a 401 from the token endpoint on refresh is `AuthError(fatal=True)`
  and moves straight to `expired`. A 401 on a resource call triggers one inline
  refresh-and-retry first.

### 5.2 `oauth1a` (7digital)

Called out separately because it is genuinely different and the brief's grouping
hides that. There is no bearer token; **every request is signed**, so the
credential is used differently at the call site, not just acquired differently.

- **Stored:** `app.consumer_key`, `app.consumer_secret`, `token.oauth_token`,
  `token.oauth_token_secret`.
- **begin():** POST to the request-token endpoint with `oauth_callback` set to the
  resolved redirect URI, or to `oob` if the mode resolved that way. Persist the
  request token secret in `auth_pending.verifier_enc`; it is required to sign the
  access-token exchange and is lost otherwise.
- **complete():** exchange request token plus `oauth_verifier` (from the callback,
  the pasted URL, or a PIN the user types) for the access token.
- **Expiry:** none, typically. `refresh()` returns `None`.
- **Verify:** a signed call to the account endpoint.
- **Signing:** HMAC-SHA1 over the normalized request, per RFC 5849. This is
  fiddly and easy to get subtly wrong, particularly percent-encoding of the
  signature base string. It belongs in one place, `auth/oauth1.py`, with its own
  tests against the RFC's worked example, and nowhere else.
- **Caveat:** 7digital API access requires contacting them for approval, so this
  method may end up implemented against no live account. Prototype it against a
  local RFC-5849 fixture rather than blocking on their approval.

### 5.3 `lastfm_web` (Last.fm)

Not OAuth, and pleasantly, needs no callback at all.

- **Stored:** `app.api_key`, `app.api_secret`, `token.session_key`.
- **begin():** call `auth.getToken` for an unauthorized request token, then return
  a `PollStep` pointing at
  `https://www.last.fm/api/auth/?api_key=<k>&token=<t>`. There is no user code to
  display; the token is already embedded in the link.
- **complete():** poll `auth.getSession` with the signed token until it stops
  returning error 14 (token not authorized) or 15 (token expired). Error 15 restarts
  the flow rather than reporting failure.
- **Signing:** the `api_sig` is `md5(sorted param concatenation + api_secret)`.
  Note it must exclude `format` and `callback`; including them is the classic
  cause of a signature-invalid error that reads like a wrong secret.
- **Expiry:** **none. Session keys have infinite lifetime by design.** So
  `refresh()` returns `None`, `expires_at` is NULL, and the only way this dies is
  the user revoking the application at Last.fm.
- **Verify:** `user.getInfo` with the session key.
- **Death:** error 9 (invalid session key) is `AuthError(fatal=True)`, straight to
  `revoked`, `action=reauthorize`.

### 5.4 `token_paste` (ListenBrainz)

- **Stored:** `token.token`.
- **begin():** a `FormStep` with one password field and `open_url` pointing at
  `https://listenbrainz.org/settings/`. The instruction names the exact page and
  the exact field label, because "paste your token" without that is where most
  users stall.
- **complete():** validate before storing. ListenBrainz has
  `GET /1/validate-token`, which returns the username, so store the username in
  `public_meta.account` and show it back as confirmation. **Never store an
  unvalidated token**: a whitespace-padded paste that fails only at the first real
  poll produces a bug report about the poller.
- **Expiry:** none. `refresh()` returns `None`.
- **Verify:** `/1/validate-token`.
- **Death:** a 401 on any call, `action=paste_token`.

### 5.5 `userpass` (Subsonic / Navidrome, Airsonic, Gonic)

- **Stored:** `token.username`, `token.password`, plus `token.base_url` if it is
  not already in provider config.
- **Wire format:** Subsonic's `u`, `t=md5(password + salt)`, `s=<salt>` with a
  fresh random salt per request. **The salted token is not a storage improvement**:
  the salt is per-request, so the app must hold the password itself. Storing a
  password rather than a derived value is unavoidable here, which is a point in
  favour of section 4 existing at all.
- **Never use** the legacy `p=` plaintext or `p=enc:<hex>` parameter. `enc:` is
  hex, not encryption, and it puts the password in the server's access log.
- **begin()/complete():** a `FormStep` with url, username, password. `complete()`
  calls `ping.view` and stores only on `status="ok"`.
- **Expiry:** none. `refresh()` returns `None`.
- **Verify:** `ping.view`. Distinguish carefully: HTTP 200 with
  `<error code="40">` is `AuthError`; a connection refused is `TransportError`.
  Navidrome being restarted must not mark the credential dead.
- **Death:** error 40 (wrong credentials) or 41 (token auth not supported by
  server), `action=reauthorize`.

### 5.6 `cookie_jar` (Qobuz, Bandcamp)

This method is an adapter, not an implementation. `SessionKeeper` already owns
persistence, rotation absorption, keepalive, live probing and dead-edge alerting,
it is deployed, and it was proven end to end against live Qobuz on 2026-08-02.
Nothing here re-solves any of that.

- **Stored in `credentials`:** nothing secret. The row exists so the provider has a
  uniform status, with `method='cookie_jar'` and `public_meta.site='qobuz'`. The
  jar stays in `SessionKeeper`'s own file.
- **begin():** an `ExternalStep`. Two tiers, matching locked decision 7:
  - *Baseline, no extension:* instructions to copy the `Cookie:` request header
    from DevTools (Network tab, any authenticated request, "Copy value") and paste
    it into a textarea. This posts to `/auth/cookie/ingest/<site>` with the app's
    own session, and is exactly the same input `SessionKeeper.ingest()` already
    takes. **This is the documented default**, because an extension that reads
    `HttpOnly` cookies is structurally an infostealer and shipping it as the
    primary path is a liability.
  - *Upgrade tier:* install the unpacked extension, set the ingest URL and token
    in its options. From then on it reseeds automatically whenever the user visits
    the site.
- **Refresh:** `SessionKeeper.touch()` on its own keepalive thread, plus absorption
  on every real request routed through `SessionKeeper.open()`. The wishlist app
  should route store reads through `open()` rather than reading the legacy flat
  header file, so ordinary use keeps the session warm and the lock prevents the
  Claim-lands-during-keepalive rotation race.
- **Verify:** `SessionKeeper.status()`, which performs a real request every call.
- **Death:** `SiteConfig.on_dead` fires once on the live-to-dead edge. Wire it to a
  callback that calls `store.mark_failed(provider, detail, fatal=True)` and, if
  `NTFY_URL` is set, the existing `ntfy_notifier`. `action=reseed_cookie`.
- **Revoke gotcha, worth documenting for users:** deleting the jar server-side is
  not revocation. If the browser is still logged in and the extension is still
  enabled for that site, the next heartbeat (15 minutes) pushes the jar straight
  back. Real revocation is: log out in the browser, disable the site in the
  extension options, then delete the jar. `DELETE /auth/qobuz` should say this on
  screen rather than silently doing a third of it.

#### The one upstream change requested

`SessionKeeper` writes its jar as plaintext JSON at `0600`, plus an optional
plaintext `legacy_header_path` mirror. To bring it under section 4 without
touching any of its logic, add two optional hooks to `SiteConfig`:

```python
# in SiteConfig
load_state: Callable[[str], dict] | None = None   # path -> state dict
save_state: Callable[[str, dict], None] | None = None
```

and have `_read_state` / `_write_state` delegate when they are set, keeping today's
file behaviour as the default. The wishlist app passes codecs that AES-GCM the blob
with the same key as `credentials`. When encryption is enabled, `legacy_header_path`
must be left unset, since a flat `name=value` mirror is plaintext by definition;
that is fine, because the migration hinge it exists for is only needed while
`qobuz_fetch.py` is unmodified, and routing through `open()` retires it.

This is additive and default-preserving. It is the only change asked of the broker.

---

## 6. Refresh scheduling and failure handling

### 6.1 The exception split

```python
class TransportError(Exception):
    """The network or the site is unhappy. The credential is UNJUDGED."""

class AuthError(Exception):
    def __init__(self, detail: str, *, fatal: bool = False):
        self.fatal = fatal   # True = the user must act; False = try a refresh first
```

Classification rules, which every provider must follow:

| Signal | Type |
|---|---|
| HTTP 401 | `AuthError` |
| HTTP 403 | `AuthError`, but see 6.4 |
| Landed on a `/signin` or `/login` URL after redirects | `AuthError(fatal=True)` |
| HTTP 429 | `TransportError` (and honour `Retry-After`) |
| HTTP 5xx, 404, timeout, DNS failure, connection refused | `TransportError` |
| Provider error body with a known auth code (Last.fm 9, Subsonic 40) | `AuthError(fatal=True)` |

**Only `AuthError` may move a credential out of `live`.** `TransportError`
increments `consecutive_failures` and sets `status='error'` at most, which the UI
renders as "cannot reach Qobuz" rather than "log in again". This is the rule that
prevents a flapping WAN link from telling the user all six providers are dead.

### 6.2 The janitor

One job in the scheduler (see `05-runtime.md`), tick 60s, single-threaded:

1. `SELECT * FROM credentials WHERE refresh_after <= now AND status IN ('live','expiring')`
2. For each, take the per-provider lock and call `AuthMethod.refresh()`.
3. On `None`, if `expires_at` is within 72h set `status='expiring'` and emit the
   SSE event once (`public_meta.warned_at` guards repeats).
4. On success, `patch_secret`, recompute `refresh_after`, `mark_ok`.
5. On `AuthError(fatal=True)`, `mark_failed(fatal=True)`, emit, notify.
6. On `TransportError`, back off: 1m, 5m, 15m, 1h, 6h, capped at 6h, jittered.
7. Sweep `auth_pending WHERE expires_at < now`.
8. Every hour, `verify()` each `live` credential whose `last_probe_at` is older
   than 6h, so a credential revoked at the provider's end is discovered before the
   user needs it. Jitter across providers, exactly as `start_keepalive` does.

### 6.3 Rotation safety

```python
def refresh(ctx, cred):
    with ctx.lock(cred.provider):                      # re-entrant, per provider
        tok = ctx.http.post_form(ctx.spec.token_url, {
            "grant_type": "refresh_token",
            "refresh_token": cred.secret["token"]["refresh_token"],
            "client_id": cred.secret["app"]["client_id"],
        })
        # PERSIST BEFORE USE. The provider has already invalidated the old refresh
        # token by the time this response exists; a crash here loses the account.
        ctx.store.patch_secret(cred.provider, token={
            **cred.secret["token"],
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token", cred.secret["token"]["refresh_token"]),
            "prev_refresh_token": cred.secret["token"]["refresh_token"],
        })
        return ctx.store.get(cred.provider)
```

`prev_refresh_token` is retained for one cycle. If a rotation response is lost in
flight, the provider may have rotated server-side while we kept the old value; on
the next attempt, a rejection of the current token is retried once with
`prev_refresh_token` before declaring the credential dead. Some providers reject
this (one-time use is enforced strictly); the retry is cheap and the failure mode
it covers is unrecoverable.

The re-entrant per-provider lock, held across read/request/write, is the same
shape and for the same reason as `SessionKeeper._lock`.

### 6.4 The ambiguous 403

A 403 can mean "credential rejected" or "WAF disliked your request". The cookie
broker already carries the scar: a default urllib User-Agent gets 403'd outright
by some WAFs, which is indistinguishable from an expired session at the call site,
which is why `DEFAULT_UA` exists.

Rule: a 403 sets `status='error'` and schedules **one** re-probe after 90 seconds.
Only if the re-probe also fails, and the landing URL check agrees, does it become
`expired`. Providers that can disambiguate (a landing URL, a JSON error code)
should do so and raise `AuthError(fatal=True)` directly.

---

## 7. Surfacing state

### 7.1 Routes

```
GET    /auth                          provider list with CredentialStatus each
POST   /auth/<provider>/begin         start a flow -> AuthStep JSON
GET    /auth/<provider>/callback      OAuth landing (same_origin and relay modes)
POST   /auth/<provider>/complete      pasted URL / token / userpass / PIN
POST   /auth/<provider>/verify        force a live probe
DELETE /auth/<provider>               provider revoke + local wipe

POST   /auth/cookie/ingest/<site>     cookie broker, canonical mount
GET    /auth/cookie/status/<site>     cookie broker, canonical mount
POST   /auth/ingest                   permanent alias, deployed extensions
GET    /auth/status                   permanent alias, deployed extensions
```

The broker blueprint is registered twice: once at `url_prefix="/auth/cookie"` and
once bare. The extension's ingest URL is runtime-configurable in its options page
(`secrets.ingestUrl`), so moving installs to the canonical path is a one-field
change, but the alias stays regardless. Keeping `/auth/status` as a literal beside
`/auth/<provider>` works in Flask (static rules outrank converters) but is fragile
to read, which is the real reason for the canonical mount.

**Cross-agent dependency, flagged:** these routes must not be reachable
unauthenticated. The broker endpoints gate on `COOKIE_BROKER_TOKEN`; the new
`/auth/<provider>/*` routes have no equivalent yet, and the app today binds
`0.0.0.0` with the Flask dev server. Until app-level auth exists (`05-runtime.md`),
either bind `127.0.0.1` or require the same bearer token on the new routes.
An unauthenticated `POST /auth/deezer/begin` on a LAN-reachable port lets a
passer-by start a flow; an unauthenticated `DELETE` lets them wipe credentials.

### 7.2 SSE event

Emitted on every status transition, and once per `expiring` warning:

```json
{ "type": "credential",
  "provider": "qobuz",
  "label": "Qobuz",
  "status": "expired",
  "detail": "redirected to https://www.qobuz.com/signin (session expired)",
  "action": "reseed_cookie",
  "since": 1786000000 }
```

`action` is closed-set so the UI can map it to one button without string matching
on `detail`: `reauthorize` opens the flow, `reseed_cookie` opens the cookie
instructions, `paste_token` opens the token form, `none` is informational.

UI treatment: a persistent banner per non-`live` provider, dismissible only by
fixing it, plus per-row consequences. A wishlist row whose only buy path is Qobuz
shows Claim disabled with "Qobuz session expired" inline, not a generic error after
the user clicks. Rows unaffected by the dead provider are untouched.

### 7.3 Status freshness

`/auth/cookie/status` performs a real request on every call, deliberately, and that
is right for a human pressing Test. It is wrong for a page that polls: six
providers times a live probe per poll is a self-inflicted rate limit and a way to
get an IP flagged.

So: `GET /auth` serves **cached** status with `age_seconds`, refreshed by the
janitor's 6-hourly verify sweep and by any real call the app makes.
`POST /auth/<p>/verify` is the live probe, and it is what the Test button calls.
The rule from the broker README still holds and is not being softened: a status
value that has never been checked against reality is never reported as `live`. A
credential that has not been probed since the process started reports
`status='unknown'` until the first probe completes.

### 7.4 Config surface

Locked decision 8 says config is entirely via env. Credentials acquired
interactively cannot be, so the split is: **env configures, the database holds what
was acquired.** Anything a user could paste is also accepted from env, so a fully
declarative compose file is possible:

| Var | Meaning |
|---|---|
| `PUBLIC_URL` | the origin the user types in the browser. Required for `same_origin`. |
| `OAUTH_REDIRECT_URL` | escape hatch; forces `relay` mode against a user-hosted page. |
| `LW_SECRET_KEY` | base64 of 32 bytes. Preferred over the keyfile. |
| `LW_SECRET_KEY_OLD` | one-boot rotation. |
| `COOKIE_BROKER_TOKEN` | unchanged, required by the broker blueprint. |
| `NTFY_URL` | dead-credential notifications, unchanged. |
| `LW_<PROVIDER>_CLIENT_ID` / `_CLIENT_SECRET` | seeds the `app` half of an oauth2/oauth1a row. |
| `LW_<PROVIDER>_TOKEN` | seeds a `token_paste` row. |
| `LW_<PROVIDER>_USERNAME` / `_PASSWORD` | seeds a `userpass` row. |

Env wins over the database and the UI renders those fields read-only with "set by
environment", so a user does not edit a value that will be overwritten on restart.
Env-seeded secrets are still written encrypted into the row on boot, so the runtime
path is uniform.

---

## 8. Worked example: adding a new provider's auth

A hypothetical store, "Presto", OAuth 2 with PKCE, refresh tokens, and a provider
that rejects private LAN addresses at registration.

```python
# providers/stores/presto.py
from wishlist.auth import AuthSpec, AuthContext, Credential, AuthError

PRESTO_AUTH = AuthSpec(
    method="oauth2",
    label="Presto Music",
    setup_url="https://www.prestomusic.com/developers/apps",
    setup_help=(
        "Create an app, then copy the redirect URL shown below into its "
        "'Callback URL' field. Presto does not accept LAN addresses, so the app "
        "will use a loopback URL and ask you to paste the result back."
    ),
    authorize_url="https://api.prestomusic.com/oauth/authorize",
    token_url="https://api.prestomusic.com/oauth/token",
    revoke_url="https://api.prestomusic.com/oauth/revoke",
    scopes=("library.read", "downloads"),
    pkce=True,
    redirect_modes=("loopback_paste",),      # skip same_origin; they reject it
    verify=lambda ctx, cred: _verify(ctx, cred),
)

def _verify(ctx: AuthContext, cred: Credential) -> tuple[bool, str]:
    try:
        me = ctx.http.get_json(
            "https://api.prestomusic.com/v1/me",
            headers={"Authorization": f"Bearer {cred.secret['token']['access_token']}"},
        )
    except AuthError as exc:
        return False, str(exc)
    ctx.store.patch_public(cred.provider, account=me["email"])
    return True, "ok"

class PrestoStore:
    id = "presto"
    label = "Presto Music"
    auth = PRESTO_AUTH
    # ... the store contract from 02-stores.md; it calls
    # ctx.credentials.require("presto") and never touches auth otherwise.
```

That is the whole auth surface for a new OAuth 2 provider: a spec and a verify
probe. No route, no template, no UI, no scheduler entry. The generic `oauth2`
`AuthMethod` supplies begin, complete, refresh and revoke; the runtime's registry
(see `05-runtime.md`) picks the spec up when the provider registers.

For a provider needing a genuinely new pattern, the checklist is:

1. Implement `AuthMethod` in `wishlist/auth/methods/<name>.py`.
2. Map its failures onto `AuthError` / `TransportError` using the 6.1 table. Write
   the test that proves `verify()` returns `False` for a deliberately broken
   credential **before** the test that proves it returns `True`. The broker's test
   matrix does exactly this ("bogus session -> dead, proves the check can fail"),
   and the audit that reproduced the wrong-track bug is the standing reminder that
   a check which cannot fail is not a check.
3. Reuse an existing `AuthStep` shape. If none fits, that is a design discussion,
   not a fifth dataclass added quietly.
4. Add the method to the table in this document.

---

## 9. Appendix: the user-hosted relay page

Provided because `OAUTH_REDIRECT_URL` exists and someone will want this. It is not
hosted by the project, and the risks in 3.2E apply in full.

```html
<!doctype html>
<meta charset="utf-8">
<title>Wishlist OAuth relay</title>
<body>
<p id="msg">Forwarding your authorization back to your Library Wishlist...</p>
<script>
// The instance URL is stored in THIS browser only. It is never derived from the
// query string: doing that would turn this page into an open redirector that
// forwards authorization codes to any origin an attacker names.
const KEY = "wishlist-instance";
let base = localStorage.getItem(KEY);
if (!base) {
  base = prompt("Full URL of your Library Wishlist, e.g. http://192.168.1.42:8080");
  if (base) localStorage.setItem(KEY, base.replace(/\/+$/, ""));
}
if (base) {
  const q = location.search || ("?" + location.hash.slice(1));
  // Confirm the destination on screen before bouncing, so a tampered
  // localStorage value is visible rather than silent.
  document.getElementById("msg").textContent = "Forwarding to " + base;
  location.replace(base + "/auth/callback" + q);
}
</script>
</body>
```

Deploy it to any static host on a domain the user controls, register that URL with
the provider, and set `OAUTH_REDIRECT_URL` to it. Note the single generic
`/auth/callback` path: in relay mode the provider is identified from `state`, since
one registered URL has to serve every provider.

Known costs, restated so they travel with the code: the authorization code appears
in the static host's access log; the page must keep resolving for as long as any
re-authorization might be needed; PKCE is what keeps a logged code inert, so this
page must never be used with a provider that does not support PKCE.

---

## Open questions / risks

1. **Tidal's registration rules are unverified.** Whether their developer portal
   accepts a plain `http://` loopback or LAN redirect URI was not confirmed, only
   inferred. If it rejects loopback too, Tidal has no working rung on the ladder
   short of a relay, and that would be the one case that reopens the relay
   question. Worth ten minutes of checking against a real registration before
   committing to `redirect_modes` for Tidal.

2. **7digital access may be unobtainable.** API access is by request, and the
   brief's own framing is that 7digital exists "as a third to prove the
   abstraction". If approval does not come, `oauth1a` gets implemented and tested
   against RFC 5849 fixtures with no live counterparty, which proves the shape but
   not the integration. Acceptable, but it should be a known state, not a surprise.

3. **`cryptography` versus the one-dependency aesthetic.** This is the round's
   first real dependency addition beyond Flask, and the current codebase's
   stdlib-only discipline is a genuine asset. The alternative I would accept is
   "no encryption, `0600` files, documented plainly", which is honest and cheap.
   What I would not accept is hand-rolled crypto. Flagging the choice rather than
   assuming it.

4. **The default key location undermines the default threat model.** With
   `/config/secret.key`, a backup of `/config` carries key and ciphertext together.
   The documentation must lead with `LW_SECRET_KEY` in the compose file, and the
   first-boot log line should say so out loud. There is a case for refusing to
   auto-generate the keyfile at all and forcing the env var; I did not take it,
   because a first-run wall is how self-hosted apps lose users, but it is arguable.

5. **Objection to a locked decision, per the output contract.** Locked decision 7
   makes manual cookie paste the baseline and the extension an upgrade tier, which
   I agree with. But the DevTools "copy request headers" path is genuinely hard for
   a non-technical user and it is the *default* path for the two stores that matter
   most (Qobuz and Bandcamp). I am not proposing to relitigate it; I am flagging
   that the quality of that one paragraph of documentation, with a screenshot per
   browser, largely determines whether the product works for anyone but its author.

6. **Unverified in the broker itself, carried forward from its README:** whether
   Qobuz rotates `qobuz-session` on authenticated requests, and the real idle TTL
   behind the 6h keepalive guess. Neither changes this design, but a shorter real
   TTL would make `reseed_cookie` a frequent event rather than a rare one, which
   would change how prominent the reseed UI needs to be.

7. **Multiple accounts per provider are not modelled.** `credentials.provider` is
   the primary key, so one Deezer account, one Qobuz session. That matches
   single-user, single-instance. If a household ever wants two Last.fm accounts
   feeding one library, the key becomes `(provider, account_id)` and every
   `store.get(provider)` call site changes. Cheap to do now, expensive later; I
   left it out because YAGNI, but it is the schema change most likely to be
   regretted.

8. **`prev_refresh_token` retry may violate some providers' one-time-use policy**
   and could, in the worst case, trip an abuse heuristic. It is guarded to a single
   retry, but it is a behaviour worth watching in logs after the first provider
   ships rather than assuming benign.

## Sources consulted

- [Deezer FAQs for developers](https://support.deezer.com/hc/en-gb/articles/360011538897-Deezer-FAQs-For-Developers) - local IP redirect URLs for headless devices, single redirect URI
- [Deezer getting started](https://developers.deezer.com/guidelines/getting_started)
- [TIDAL developer authorization](https://developer.tidal.com/documentation/api-sdk/api-sdk-authorization) and [tidal-sdk-android auth](https://tidal-music.github.io/tidal-sdk-android/auth/index.html) - device login restricted to internal apps
- [Last.fm desktop application auth how-to](https://www.last.fm/api/desktopauth) and [auth.getSession](https://www.last.fm/api/show/auth.getSession) - no callback URL, infinite session keys
- [My Home Assistant](https://www.home-assistant.io/integrations/my/) and [OAuth2 authorize callback](https://my.home-assistant.io/redirect/oauth/) - the relay model, instance URL in localStorage
- [HA core issue #104488](https://github.com/home-assistant/core/issues/104488) and [Application Credentials: support local oauth redirect endpoints](https://community.home-assistant.io/t/application-credentials-support-local-oauth-redirect-endpoints/490533) - user pushback on the relay
- [Trakt authentication](https://docs.trakt.tv/reference/auth) and [Trakt PIN how-to](https://forums.trakt.tv/t/how-do-i-connect-my-media-center-add-ons-or-apps-using-a-pin-code/22120) - device and oob precedent for self-hosted apps
- [7digital API client](https://github.com/7digital/7digital-api) and [oauth reference](https://github.com/7digital/oauth-reference-page) - OAuth 1.0a
- [Does Bandcamp have an API?](https://get.bandcamp.help/en/articles/15263422-does-bandcamp-have-an-api) - labels and fulfilment partners only, by request
- [OAuth 2.0 for native apps guidance](https://developers.google.com/identity/protocols/oauth2/native-app) - loopback exemption practice
