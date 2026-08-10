"""The authentication methods, and the one place a 403 may become a dead credential.

A provider does not implement authentication. It publishes an `AuthSpec` naming
a method, and the method here does the work: it renders a step for the
interface, completes the flow, refreshes what can be refreshed, and probes
what is live. That is what lets a new provider be added with a constant and a
verify function instead of a route and a template.

**Transport failure and credential failure are different things**, and the
distinction is carried by the error type rather than by a message. `HttpClient`
already draws most of the line: 401 is `AuthExpired`, 429 and 5xx and timeouts
are `TransientError`, and 403 is a `PermanentError` because a store answers 403
for a live session that merely lacks rights to one item. Only a credential
failure may move a row out of `live`; a flapping link must not tell the user
that six providers are dead.

**The 403 rule.** `on_forbidden` records the 403 as `error` and schedules a
single re-probe. `reprobe` runs the method's own live check against an endpoint
that is known to work for a good credential, and only if that also fails does
the row become `expired`. A user told to reconnect an account that is working is
the failure this exists to prevent, and it is worth the ninety-second wait.

Two methods named by `AuthSpec` are not implemented. `oauth1a` raises
`NotSupported`: its only intended consumer was 7digital, whose API access is not
obtainable by a self-hoster, so there is no live counterparty to sign requests
for. The OAuth device grant is not built either, for the same reason: none of
the providers in scope offer one publicly.
"""

from __future__ import annotations

import base64
import hashlib
import random
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .credentials import (
    ERROR,
    EXPIRED,
    EXPIRING,
    LIVE,
    PENDING,
    REPROBE_DELAY_S,
    UNKNOWN,
    ProviderCredentials,
    SqliteCredentialStore,
)
from .errors import AuthExpired, NotSupported, PermanentError, ProviderError, TransientError
from .http import HttpClient
from .log import Logger, get
from .models import AuthSpec, Credential

log = get("auth")

#: How long before a non-refreshable credential dies that the user is warned.
#: The point is to be told while the queue is still working, not after.
EXPIRING_WINDOW_S = 72 * 3600

LASTFM_API_ROOT = "https://ws.audioscrobbler.com/2.0/"


# --------------------------------------------------------------------------
# Step shapes: four, and the interface has a renderer for each
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "text"  # text | password | url
    help: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class FormStep:
    fields: tuple[Field, ...]
    instructions: str
    open_url: str | None = None  # a page to open first, linked above the form


@dataclass(frozen=True)
class RedirectStep:
    url: str
    state: str
    redirect_uri: str
    mode: str
    fallback: FormStep | None = None  # shown as "the page did not load?" beneath


@dataclass(frozen=True)
class PollStep:
    verification_uri: str
    interval_seconds: int
    expires_at: int
    user_code: str | None = None


@dataclass(frozen=True)
class ExternalStep:
    instructions: str
    status_url: str
    doc_url: str = ""


AuthStep = FormStep | RedirectStep | PollStep | ExternalStep


@dataclass
class AuthContext:
    """What a method needs to do its work. Built per call, never stored."""

    provider: str
    spec: AuthSpec
    store: SqliteCredentialStore
    http: HttpClient
    log: Logger
    public_url: str = ""
    port: int = 8080
    keeper: Any = None  # the cookie broker's SessionKeeper, taken structurally
    now: Callable[[], int] = lambda: int(time.time())


# --------------------------------------------------------------------------
# Helpers shared by more than one method
# --------------------------------------------------------------------------


def post_form(http: HttpClient, url: str, body: Mapping[str, Any]) -> dict:
    """POST an application/x-www-form-urlencoded body and read JSON back.

    Token endpoints take forms and answer JSON; this is the shape often enough
    that writing it out at each call site invites one of them to differ by
    accident.
    """
    data = urllib.parse.urlencode({k: v for k, v in body.items() if v is not None}).encode()
    resp = http.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    payload = resp.json()
    if not isinstance(payload, dict):
        raise PermanentError(f"{url}: expected a JSON object", code="bad_token_response")
    return payload


def pkce_pair() -> tuple[str, str]:
    """A code verifier and its S256 challenge.

    PKCE is what keeps an authorization code inert when it is seen by a browser
    history, a proxy log or a shoulder, which is exactly what the paste-the-URL
    fallback exposes it to.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def resolve_redirect(spec: AuthSpec, ctx: AuthContext) -> tuple[str, str]:
    """Pick the redirect mode and the exact URI to register, in preference order.

    There is no shipped client id: one instance per user means every user
    registers their own OAuth app, so the user chooses the redirect URI and
    nobody has to predict an address they cannot know. Same-origin is therefore
    the normal answer. The loopback fallback exists for a provider that refuses
    a private LAN host, because the RFC 8252 loopback exemption is honoured
    almost everywhere; the browser fails to load it and the user pastes the
    address bar back.
    """
    if "same_origin" in spec.redirect_modes and ctx.public_url:
        return "same_origin", f"{ctx.public_url.rstrip('/')}/auth/{ctx.provider}/callback"
    if "loopback_paste" in spec.redirect_modes:
        return "loopback_paste", f"http://127.0.0.1:{ctx.port}/auth/{ctx.provider}/callback"
    if "oob" in spec.redirect_modes:
        return "oob", "urn:ietf:wg:oauth:2.0:oob"
    raise PermanentError(
        f"{spec.label} needs LW_PUBLIC_URL to be set before it can be connected",
        code="no_redirect_mode", provider_id=ctx.provider,
    )


def refresh_at(expires_at: int, lifetime: int) -> int:
    """When to renew an access token: early, and not all at the same moment.

    Renewing at the last second means a clock skew or a slow token endpoint
    costs a failed request; the jitter stops every provider on the box from
    hitting its token endpoint on the same tick after a restart.
    """
    margin = max(300, lifetime // 10)
    return int(expires_at - margin * random.uniform(0.9, 1.1))


def lastfm_signature(params: Mapping[str, str], api_secret: str) -> str:
    """Last.fm's `api_sig`: md5 of the sorted key/value concatenation plus the secret.

    `format` and `callback` are excluded. Including them produces an
    invalid-signature error that reads exactly like a wrong secret, which is the
    classic hour lost to this API.
    """
    parts = "".join(f"{k}{params[k]}" for k in sorted(params)
                    if k not in ("format", "callback", "api_sig"))
    return hashlib.md5((parts + api_secret).encode("utf-8")).hexdigest()


def subsonic_auth_params(username: str, password: str) -> dict[str, str]:
    """Subsonic's `u`/`t`/`s` triple, with a fresh salt per request.

    The salted token is not a storage improvement: the salt changes every
    request, so the password itself has to be held. The legacy `p=` parameter is
    never used, in either its plain or its `enc:` hex form, because both put the
    password in the server's access log and `enc:` is hex rather than
    encryption.
    """
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {"u": username, "t": token, "s": salt}


# --------------------------------------------------------------------------
# The methods
# --------------------------------------------------------------------------


class AuthMethod:
    """One way of obtaining and keeping a credential."""

    id = ""

    def begin(self, ctx: AuthContext, submission: dict | None = None) -> AuthStep:
        raise NotSupported(f"{self.id} cannot start a flow", code="no_begin")

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        raise NotSupported(f"{self.id} cannot complete a flow", code="no_complete")

    def refresh(self, ctx: AuthContext, cred: Credential) -> Credential | None:
        """None means there is nothing to refresh, which is the common case."""
        return None

    def verify(self, ctx: AuthContext, cred: Credential) -> tuple[bool, str]:
        """A live call, never a stored opinion.

        A method with no built-in probe needs one from the spec. Reaching the
        error means a provider was registered without a way to check its own
        credential, which is a wiring bug rather than a user-visible condition.
        """
        if ctx.spec.verify is None:
            raise NotSupported(
                f"{ctx.provider} declares no verify probe, so its credential cannot be checked",
                code="no_verify", provider_id=ctx.provider,
            )
        return ctx.spec.verify(ctx, cred)

    def revoke(self, ctx: AuthContext, cred: Credential) -> None:
        """Best effort at the provider's end. The local wipe happens regardless."""
        return None


class NoneAuth(AuthMethod):
    """A provider with no login at all, so the runtime never special-cases one."""

    id = "none"

    def begin(self, ctx: AuthContext, submission: dict | None = None) -> AuthStep:
        return FormStep((), f"{ctx.spec.label} needs no credentials.")

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        ctx.store.put(ctx.provider, self.id, {}, status=LIVE)
        return ctx.store.require(ctx.provider)

    def verify(self, ctx: AuthContext, cred: Credential) -> tuple[bool, str]:
        return True, "no credential needed"


class TokenPasteAuth(AuthMethod):
    """A token the user copies out of the provider's settings page (ListenBrainz)."""

    id = "token_paste"

    def begin(self, ctx: AuthContext, submission: dict | None = None) -> AuthStep:
        return FormStep(
            (Field("token", "API token", "password",
                   help="Copy it from the settings page linked above."),),
            ctx.spec.setup_help or f"Paste your {ctx.spec.label} token.",
            open_url=ctx.spec.setup_url,
        )

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        # Whitespace is stripped before anything else looks at the value. A
        # paste that picked up a trailing newline is the single most common way
        # this goes wrong, and it otherwise surfaces at the first poll as a
        # rejected token, which sends the user reading the wrong logs.
        token = (submission.get("token") or "").strip()
        if not token:
            raise PermanentError("No token was pasted.", code="empty_token", provider_id=ctx.provider)
        ctx.store.put(ctx.provider, self.id, {"token": {"token": token}}, status=PENDING)
        _store_after_verify(self, ctx)
        return ctx.store.require(ctx.provider)


class UserPassAuth(AuthMethod):
    """Username and password against a server the user runs (Subsonic, Navidrome)."""

    id = "userpass"

    def begin(self, ctx: AuthContext, submission: dict | None = None) -> AuthStep:
        return FormStep(
            (
                Field("base_url", "Server address", "url", placeholder="http://navidrome.local:4533"),
                Field("username", "Username"),
                Field("password", "Password", "password"),
            ),
            ctx.spec.setup_help or f"Sign in to {ctx.spec.label}.",
            open_url=ctx.spec.setup_url,
        )

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        token = {
            "username": (submission.get("username") or "").strip(),
            "password": submission.get("password") or "",
            "base_url": (submission.get("base_url") or "").strip().rstrip("/"),
        }
        if not token["username"] or not token["password"]:
            raise PermanentError("A username and password are both needed.",
                                 code="incomplete", provider_id=ctx.provider)
        ctx.store.put(ctx.provider, self.id, {"token": token}, status=PENDING)
        _store_after_verify(self, ctx)
        return ctx.store.require(ctx.provider)


class LastfmWebAuth(AuthMethod):
    """Last.fm's desktop flow: a request token the user approves in a browser.

    Structurally a device grant with a token instead of a user code, which is
    why it reuses the poll step rather than needing a shape of its own. Session
    keys have no expiry by design, so there is nothing to refresh and the only
    way this dies is the user revoking the application at Last.fm.
    """

    id = "lastfm_web"

    def _root(self, ctx: AuthContext) -> str:
        return ctx.spec.token_url or LASTFM_API_ROOT

    def _call(self, ctx: AuthContext, params: dict, api_secret: str) -> dict:
        signed = {**params, "api_sig": lastfm_signature(params, api_secret), "format": "json"}
        payload = ctx.http.get(self._root(ctx), params=signed).json()
        if not isinstance(payload, dict):
            raise PermanentError("Last.fm returned something that is not an object",
                                 code="bad_response", provider_id=ctx.provider)
        return payload

    def begin(self, ctx: AuthContext, submission: dict | None = None) -> AuthStep:
        app = _app_half(ctx, submission, ("api_key", "api_secret"))
        if app is None:
            return FormStep(
                (Field("api_key", "API key"), Field("api_secret", "Shared secret", "password")),
                f"Create an API account at {ctx.spec.setup_url} and paste both values.",
                open_url=ctx.spec.setup_url,
            )
        payload = self._call(ctx, {"method": "auth.getToken", "api_key": app["api_key"]},
                             app["api_secret"])
        token = payload.get("token")
        if not token:
            raise PermanentError(f"Last.fm refused to issue a token: {payload}",
                                 code="no_token", provider_id=ctx.provider)
        now = ctx.now()
        ctx.store.put(ctx.provider, self.id, {"app": app}, status=PENDING)
        ctx.store.pending_put(token, ctx.provider, "poll", poll_interval=5, now=now)
        return PollStep(
            verification_uri=f"https://www.last.fm/api/auth/?api_key={app['api_key']}&token={token}",
            interval_seconds=5,
            expires_at=now + 600,
        )

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        cred = ctx.store.get(ctx.provider)
        app = (cred.secret.get("app") if cred else None) or {}
        if not app.get("api_key"):
            raise PermanentError("Start the Last.fm connection again; its API key is missing.",
                                 code="no_app", provider_id=ctx.provider)
        token = submission.get("token")
        if not token:
            row = ctx.store.pending_latest(ctx.provider)
            token = row["state"] if row else None
        if not token:
            raise PermanentError("This Last.fm link has expired. Start again.",
                                 code="no_pending", provider_id=ctx.provider)

        payload = self._call(ctx, {"method": "auth.getSession", "api_key": app["api_key"],
                                   "token": token}, app["api_secret"])
        code = payload.get("error")
        if code == 14:
            # Not an error: the user has not clicked Allow yet. The caller polls.
            raise TransientError("Waiting for you to approve the request at Last.fm.",
                                 code="not_authorized", provider_id=ctx.provider)
        if code == 15:
            raise PermanentError("That Last.fm link expired. Start the connection again.",
                                 code="token_expired", provider_id=ctx.provider)
        if code == 9:
            raise AuthExpired("Last.fm rejected the session key.",
                              code="invalid_session", provider_id=ctx.provider)
        session = payload.get("session") or {}
        if not session.get("key"):
            raise PermanentError(f"Last.fm did not return a session: {payload}",
                                 code="no_session", provider_id=ctx.provider)

        ctx.store.pending_take(token)
        ctx.store.put(
            ctx.provider, self.id,
            {"app": app, "token": {"session_key": session["key"]}},
            public_meta={"account": session.get("name"), "refreshable": False},
            status=LIVE,
        )
        return ctx.store.require(ctx.provider)


class CookieJarAuth(AuthMethod):
    """A browser session, owned by the cookie broker (Qobuz, Bandcamp).

    Nothing secret is stored in `credentials`. The jar lives with the broker,
    which already persists it, absorbs rotation on every request it makes,
    probes it on a timer and fires once on the live-to-dead edge. The row exists
    so the provider has the same status surface as every other one, and so the
    interface can say "reseed" rather than "reauthorize".
    """

    id = "cookie_jar"

    def _site(self, ctx: AuthContext) -> str:
        return getattr(getattr(ctx.keeper, "cfg", None), "name", ctx.provider)

    def begin(self, ctx: AuthContext, submission: dict | None = None) -> AuthStep:
        site = self._site(ctx)
        return ExternalStep(
            instructions=(
                f"Sign in to {ctx.spec.label} in your browser. Then either install the "
                "cookie extension and point it at this instance, or open the network tab, "
                "pick any request to the site, copy the whole Cookie request header and "
                "paste it below."
            ),
            status_url=f"/auth/cookie/status/{site}",
            doc_url=ctx.spec.setup_url or "",
        )

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        header = (submission.get("cookie") or "").strip()
        if not header:
            raise PermanentError("No cookie header was pasted.",
                                 code="empty_cookie", provider_id=ctx.provider)
        if ctx.keeper is None:
            raise NotSupported(f"{ctx.provider} has no cookie broker configured",
                               code="no_keeper", provider_id=ctx.provider)
        # The broker's own ingest replaces the jar wholesale and then probes it,
        # so a jar that does not work is rejected here rather than at the first
        # claim.
        result = ctx.keeper.ingest(header)
        if result.get("session") != "live":
            raise AuthExpired(
                f"That {ctx.spec.label} session did not work: {result.get('detail')}",
                code="dead_on_arrival", provider_id=ctx.provider,
            )
        ctx.store.put(ctx.provider, self.id, {},
                      public_meta={"site": self._site(ctx), "refreshable": True}, status=LIVE)
        return ctx.store.require(ctx.provider)

    def verify(self, ctx: AuthContext, cred: Credential) -> tuple[bool, str]:
        # A provider that declares its own probe knows a better endpoint than
        # the broker's generic one, which is the whole point of a store
        # checking the page it actually reads.
        if ctx.spec.verify is not None:
            return ctx.spec.verify(ctx, cred)
        if ctx.keeper is None:
            raise NotSupported(f"{ctx.provider} has no cookie broker configured",
                               code="no_keeper", provider_id=ctx.provider)
        return ctx.keeper.probe()

    def revoke(self, ctx: AuthContext, cred: Credential) -> None:
        """Deleting the jar here is a third of a revocation, and the caller must say so.

        If the browser is still signed in and the extension still enabled for
        the site, the next heartbeat pushes the jar straight back. Real
        revocation is: sign out in the browser, disable the site in the
        extension, then delete the jar.
        """
        return None


class OAuth2Auth(AuthMethod):
    """Authorization code with PKCE, and refresh (Deezer, Tidal, the general case)."""

    id = "oauth2"

    def begin(self, ctx: AuthContext, submission: dict | None = None) -> AuthStep:
        app = _app_half(ctx, submission, ("client_id",), optional=("client_secret",))
        if app is None:
            return FormStep(
                (Field("client_id", "Application ID"),
                 Field("client_secret", "Application secret", "password")),
                ctx.spec.setup_help or f"Register an app at {ctx.spec.setup_url}.",
                open_url=ctx.spec.setup_url,
            )
        if not ctx.spec.authorize_url or not ctx.spec.token_url:
            raise NotSupported(f"{ctx.provider} declares no OAuth endpoints",
                               code="no_endpoints", provider_id=ctx.provider)

        mode, redirect_uri = resolve_redirect(ctx.spec, ctx)
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair() if ctx.spec.pkce else ("", "")
        ctx.store.put(ctx.provider, self.id, {"app": app}, status=PENDING)
        ctx.store.pending_put(state, ctx.provider, mode, verifier=verifier,
                              redirect_uri=redirect_uri, now=ctx.now())

        params = {
            "response_type": "code",
            "client_id": app["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if ctx.spec.scopes:
            params["scope"] = " ".join(ctx.spec.scopes)
        if ctx.spec.pkce:
            params.update(code_challenge=challenge, code_challenge_method="S256")
        sep = "&" if "?" in ctx.spec.authorize_url else "?"
        return RedirectStep(
            url=ctx.spec.authorize_url + sep + urllib.parse.urlencode(params),
            state=state,
            redirect_uri=redirect_uri,
            mode=mode,
            fallback=FormStep(
                (Field("callback_url", "Address bar contents", "url"),),
                "The next page will fail to load. That is expected. Copy the whole "
                "address from your browser's address bar and paste it here.",
            ),
        )

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        code = submission.get("code")
        state = submission.get("state")
        if submission.get("callback_url"):
            code, state = _parse_callback(ctx, submission["callback_url"])
        if not code or not state:
            raise PermanentError("That address has no authorization code in it.",
                                 code="no_code", provider_id=ctx.provider)

        row = ctx.store.pending_take(state, now=ctx.now())
        if row is None:
            raise PermanentError("This authorization link has expired or was already used.",
                                 code="stale_state", provider_id=ctx.provider)
        if row["provider"] != ctx.provider:
            raise PermanentError("That authorization belongs to a different provider.",
                                 code="state_mismatch", provider_id=ctx.provider)

        cred = ctx.store.get(ctx.provider)
        app = (cred.secret.get("app") if cred else None) or {}
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": row["redirect_uri"],
            "client_id": app.get("client_id"),
            "client_secret": app.get("client_secret") or None,
            "code_verifier": row["verifier"] or None,
        }
        tok = post_form(ctx.http, ctx.spec.token_url or "", body)
        self._write_tokens(ctx, app, {}, tok, status=PENDING)
        _store_after_verify(self, ctx)
        return ctx.store.require(ctx.provider)

    def refresh(self, ctx: AuthContext, cred: Credential) -> Credential | None:
        token = dict(cred.secret.get("token") or {})
        current = token.get("refresh_token")
        if not current:
            # Deezer issues no refresh token at all. Nothing to do here; the
            # caller warns before the access token dies instead.
            return None
        app = cred.secret.get("app") or {}
        body = {
            "grant_type": "refresh_token",
            "refresh_token": current,
            "client_id": app.get("client_id"),
            "client_secret": app.get("client_secret") or None,
        }
        try:
            tok = post_form(ctx.http, ctx.spec.token_url or "", body)
        except AuthExpired:
            previous = token.get("prev_refresh_token")
            if not previous or previous == current:
                raise
            # A rotation response lost in flight leaves the provider holding the
            # token we threw away. One retry with the previous value recovers
            # that; a second rejection is real, and the single attempt keeps
            # this from becoming a loop.
            ctx.log.warning("retrying refresh with the previous token",
                            context={"provider": ctx.provider})
            body["refresh_token"] = previous
            tok = post_form(ctx.http, ctx.spec.token_url or "", body)
        self._write_tokens(ctx, app, token, tok, status=LIVE)
        return ctx.store.get(ctx.provider)

    def _write_tokens(self, ctx: AuthContext, app: dict, previous: dict, tok: dict,
                      *, status: str) -> None:
        """Persist a token response before anything is done with it.

        By the time this response exists the provider has already invalidated
        the refresh token that bought it. A crash between here and the first
        real call would otherwise lose the account with no way back except the
        whole flow again.
        """
        access = tok.get("access_token")
        if not access:
            raise PermanentError(f"{ctx.provider} returned no access token",
                                 code="no_access_token", provider_id=ctx.provider)
        token = {
            **previous,
            "access_token": access,
            "token_type": tok.get("token_type", "Bearer"),
            "refresh_token": tok.get("refresh_token") or previous.get("refresh_token"),
            "prev_refresh_token": previous.get("refresh_token"),
        }
        if tok.get("scope"):
            token["scope"] = tok["scope"]
        lifetime = int(tok.get("expires_in") or 0)
        expires_at = ctx.now() + lifetime if lifetime else None
        # Carry the existing public metadata forward: the account name was
        # learned by a verify probe and a refresh has no way to learn it again.
        existing = ctx.store.get(ctx.provider)
        meta = dict(existing.public_meta if existing else {})
        meta["refreshable"] = bool(token.get("refresh_token"))
        if tok.get("scope"):
            meta["scope"] = tok["scope"]
        ctx.store.put(
            ctx.provider, self.id, {"app": app, "token": token},
            public_meta=meta, expires_at=expires_at, status=status,
            refresh_after=refresh_at(expires_at, lifetime) if expires_at else None,
            now=ctx.now(),
        )


class OAuth1aAuth(AuthMethod):
    """Named by `AuthSpec`, not implemented.

    RFC 5849 signs every request rather than carrying a bearer token, so it is a
    different shape at the call site and not only at acquisition. Its one
    intended consumer was 7digital, whose API access is granted by hand and was
    found to be unobtainable for a self-hoster, which would leave a few hundred
    lines of signature-base-string encoding with no counterparty to prove it
    against. Registered here so that declaring the method fails loudly at setup
    instead of appearing to work.
    """

    id = "oauth1a"

    def _refuse(self, provider: str):
        return NotSupported(
            "OAuth 1.0a is not implemented: its only provider cannot be automated by a "
            "self-hoster, so there is nothing to sign requests for.",
            code="oauth1a_not_implemented", provider_id=provider,
        )

    def begin(self, ctx: AuthContext, submission: dict | None = None) -> AuthStep:
        raise self._refuse(ctx.provider)

    def complete(self, ctx: AuthContext, submission: dict) -> Credential:
        raise self._refuse(ctx.provider)

    def refresh(self, ctx: AuthContext, cred: Credential) -> Credential | None:
        raise self._refuse(ctx.provider)

    def verify(self, ctx: AuthContext, cred: Credential) -> tuple[bool, str]:
        raise self._refuse(ctx.provider)


METHODS: dict[str, AuthMethod] = {
    m.id: m for m in (
        NoneAuth(), TokenPasteAuth(), UserPassAuth(), LastfmWebAuth(),
        CookieJarAuth(), OAuth2Auth(), OAuth1aAuth(),
    )
}


def method_for(spec: AuthSpec) -> AuthMethod:
    try:
        return METHODS[spec.method]
    except KeyError:
        raise NotSupported(f"{spec.method!r} is not an auth method", code="unknown_method") from None


def _app_half(ctx: AuthContext, submission: dict | None, required: tuple[str, ...],
              optional: tuple[str, ...] = ()) -> dict | None:
    """The user-registered application credentials, from this form or from the row.

    Returns None when they are still missing, which is the caller's cue to ask
    for them rather than to fail: a user who has not registered an app yet has
    done nothing wrong.
    """
    stored = ctx.store.get(ctx.provider)
    app = dict((stored.secret.get("app") if stored else None) or {})
    for key in required + optional:
        value = (submission or {}).get(key)
        if value:
            app[key] = value.strip() if isinstance(value, str) else value
    if any(not app.get(key) for key in required):
        return None
    return app


def _parse_callback(ctx: AuthContext, callback_url: str) -> tuple[str, str]:
    """Pull `code` and `state` out of a URL the user pasted from the address bar."""
    query = urllib.parse.urlsplit(callback_url.strip()).query
    params = urllib.parse.parse_qs(query)
    if "error" in params:
        # The provider's own wording beats a generic failure: "the user denied
        # access" and "this app is not approved for that scope" need different
        # things done about them.
        detail = (params.get("error_description") or params.get("error"))[0]
        raise PermanentError(f"{ctx.spec.label} refused the authorization: {detail}",
                             code="authorize_denied", provider_id=ctx.provider)
    return (params.get("code", [""])[0], params.get("state", [""])[0])


def _store_after_verify(method: AuthMethod, ctx: AuthContext) -> None:
    """Probe a freshly stored credential and only then call it live.

    A half-authorized provider must never show as connected, and a status that
    has not been checked against reality is never reported as live. The row is
    written first because the probe usually needs to read it.
    """
    cred = ctx.store.get(ctx.provider)
    if cred is None:
        raise PermanentError("credential vanished during setup", code="lost_row",
                             provider_id=ctx.provider)
    try:
        ok, detail = method.verify(ctx, cred)
    except NotSupported:
        # A provider with no probe cannot be checked, and calling it live would
        # report a guess as a fact. `unknown` is the vocabulary's word for a
        # credential nothing has confirmed, and the interface says so.
        ctx.store.set_status(ctx.provider, UNKNOWN)
        return
    if not ok:
        ctx.store.mark_failed(ctx.provider, detail, fatal=True, now=ctx.now())
        raise AuthExpired(f"{ctx.spec.label} rejected that credential: {detail}",
                          code="rejected", provider_id=ctx.provider)
    ctx.store.mark_ok(ctx.provider, now=ctx.now())


# --------------------------------------------------------------------------
# The manager
# --------------------------------------------------------------------------


class AuthManager:
    """Wires specs, methods and the store together, and owns the 403 rule.

    Providers never see this. They get a `ProviderCredentials` handle from
    `handle()`, which can reach exactly one row.
    """

    def __init__(
        self,
        store: SqliteCredentialStore,
        specs: Mapping[str, AuthSpec],
        *,
        http_factory: Callable[..., HttpClient],
        keepers: Mapping[str, Any] | None = None,
        public_url: str = "",
        port: int = 8080,
        logger: Logger | None = None,
    ) -> None:
        self.store = store
        self.specs = dict(specs)
        self.keepers = dict(keepers or {})
        self._http_factory = http_factory
        self.public_url = public_url
        self.port = port
        self.log = logger or log

    # -- plumbing ----------------------------------------------------------

    def spec(self, provider: str) -> AuthSpec:
        try:
            return self.specs[provider]
        except KeyError:
            raise NotSupported(f"{provider} has no auth spec registered",
                               code="unknown_provider", provider_id=provider) from None

    def context(self, provider: str, *, now: Callable[[], int] | None = None) -> AuthContext:
        keeper = self.keepers.get(provider)
        handle = self.handle(provider)
        ctx = AuthContext(
            provider=provider,
            spec=self.spec(provider),
            store=self.store,
            http=handle.http_client(),
            log=self.log.bind(provider=provider),
            public_url=self.public_url,
            port=self.port,
            keeper=keeper,
        )
        if now is not None:
            ctx.now = now
        return ctx

    def handle(self, provider: str) -> ProviderCredentials:
        return ProviderCredentials(
            provider, self.store,
            http_factory=self._http_factory,
            keeper=self.keepers.get(provider),
            logger=self.log,
        )

    # -- flows -------------------------------------------------------------

    def begin(self, provider: str, submission: dict | None = None) -> AuthStep:
        ctx = self.context(provider)
        return method_for(ctx.spec).begin(ctx, submission)

    def complete(self, provider: str, submission: dict) -> Credential:
        ctx = self.context(provider)
        return method_for(ctx.spec).complete(ctx, submission)

    def revoke(self, provider: str) -> None:
        ctx = self.context(provider)
        cred = self.store.get(provider)
        if cred is not None:
            try:
                method_for(ctx.spec).revoke(ctx, cred)
            except ProviderError as exc:
                # The user asked for this credential to stop working here. A
                # provider that will not cooperate does not get to block that.
                ctx.log.warning("provider-side revoke failed", context={"error": str(exc)})
        self.store.revoke(provider)

    # -- health ------------------------------------------------------------

    def verify(self, provider: str, *, now: Callable[[], int] | None = None) -> tuple[bool, str]:
        """Run the live probe and record what it found. This is the Test button."""
        ctx = self.context(provider, now=now)
        cred = self.store.get(provider)
        if cred is None:
            return False, "not connected"
        try:
            ok, detail = method_for(ctx.spec).verify(ctx, cred)
        except AuthExpired as exc:
            self.store.mark_failed(provider, str(exc), fatal=True, now=ctx.now())
            return False, str(exc)
        except TransientError as exc:
            # Unreachable is not a verdict on the credential.
            self.store.mark_failed(provider, str(exc), fatal=False, now=ctx.now())
            return False, str(exc)
        if ok:
            self.store.mark_ok(provider, now=ctx.now())
        else:
            self.store.mark_failed(provider, detail, fatal=True, now=ctx.now())
        return ok, detail

    def on_forbidden(self, provider: str, detail: str, *, now: int | None = None) -> None:
        """Record a 403 without judging the credential.

        A 403 means either "your session is dead" or "the WAF disliked that
        request", and at the call site the two are identical. The row goes to
        `error`, which reads as "cannot reach X", and one re-probe is scheduled.
        Only that probe may conclude the credential is finished.
        """
        self.store.mark_failed(provider, detail, fatal=False,
                               retry_in=REPROBE_DELAY_S, now=now)

    def reprobe(self, provider: str, *, now: Callable[[], int] | None = None) -> bool:
        """The scheduled second look after an ambiguous failure.

        Returns True when the credential is still good, which puts the row back
        to `live` and leaves the user undisturbed. Only a failure here writes
        `expired`.
        """
        ok, detail = self.verify(provider, now=now)
        if not ok and self.store.status(provider).status != EXPIRED:
            # The probe could not reach the provider either, so the credential
            # is still unjudged. It stays in `error` with another attempt
            # scheduled rather than being called dead on no evidence.
            self.log.info("re-probe was inconclusive", context={"provider": provider,
                                                               "detail": detail})
        return ok

    def refresh(self, provider: str, *, now: Callable[[], int] | None = None) -> str:
        """Renew a credential that can be renewed. Returns the resulting status.

        A rejected refresh token ends the credential here rather than being
        retried on the next tick: the provider has said the answer, and a
        janitor that keeps asking turns one dead account into a rate limit.
        """
        ctx = self.context(provider, now=now)
        cred = self.store.get(provider)
        if cred is None:
            return "absent"
        try:
            renewed = method_for(ctx.spec).refresh(ctx, cred)
        except AuthExpired as exc:
            self.store.mark_failed(provider, str(exc), fatal=True, now=ctx.now())
            return EXPIRED
        except TransientError as exc:
            self.store.mark_failed(provider, str(exc), fatal=False, now=ctx.now())
            return self.store.status(provider).status
        if renewed is None:
            return self._warn_if_expiring(provider, cred, ctx.now())
        self.store.mark_ok(provider, now=ctx.now())
        return LIVE

    def _warn_if_expiring(self, provider: str, cred: Credential, now: int) -> str:
        """Say so before a non-refreshable credential dies, not after."""
        if cred.expires_at is None or cred.expires_at - now > EXPIRING_WINDOW_S:
            return cred.status
        self.store.set_status(provider, EXPIRING,
                              detail="this connection cannot renew itself and is running out")
        self.store.patch_public(provider, warned_at=now)
        return EXPIRING

    def run_due(self, *, now: int | None = None) -> dict[str, str]:
        """One janitor tick: every row whose scheduled moment has arrived.

        A row in `error` is waiting on a re-probe; anything else is waiting on a
        refresh. Both are driven from the same column so there is one schedule
        rather than two that can disagree.
        """
        at = now if now is not None else int(time.time())
        clock: Callable[[], int] = lambda: at
        outcomes: dict[str, str] = {}
        for provider in self.store.due(now=at):
            if provider not in self.specs:
                continue
            try:
                if self.store.status(provider).status == ERROR:
                    self.reprobe(provider, now=clock)
                    outcomes[provider] = self.store.status(provider).status
                else:
                    outcomes[provider] = self.refresh(provider, now=clock)
            except ProviderError as exc:
                # One provider's bad day does not end the tick.
                self.log.warning("janitor pass failed", context={"provider": provider,
                                                                "error": str(exc)})
                outcomes[provider] = self.store.status(provider).status
        self.store.pending_sweep(now=at)
        return outcomes
