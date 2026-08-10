"""Credential storage, and the narrow handle a provider is given instead of a secret.

**What the at-rest protection is.** Secrets are stored as plaintext JSON in a
TEXT column and the database file is chmod 0600. That is all of it, and it is
worth being blunt about what it buys. It keeps a credential out of a casual
`sqlite3 library-wishlist.db 'select * from credentials'` run by another account
on the same box, and it keeps it out of a backup script running as somebody
else. It protects nothing once the database file is copied off the box, and it
protects nothing at all from code running as the application user, which has to
be able to read every secret unattended on every boot. Encrypting the column
with a key the same process can reach, in the same volume, would not change any
of those answers; it would only make the exposure harder to see. If the
database is going anywhere the user does not control, encrypt the volume or the
backup, not this column.

**Status is a table, not a chain of conditionals.** `credentials.status` has
eight values, and each has to become a provider state, a sentence the interface
prints, and one button. That mapping lives in `STATUS_UI` below so that the
interface has one branch and there is one place to change it.

**A 403 is not a verdict here.** `HttpClient` raises `PermanentError` for 403
because a store answers 403 for a live session that merely lacks rights to one
item. The store below can record that as `error` and schedule a re-probe, but
only `libwish/auth.py`, after that probe fails against an endpoint known to work
for a live credential, may move the row to `expired`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import AuthExpired
from .http import HttpClient
from .log import Logger, get
from .models import Credential, CredentialStatus

log = get("credentials")

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

ABSENT = "absent"
PENDING = "pending"
LIVE = "live"
EXPIRING = "expiring"
EXPIRED = "expired"
REVOKED = "revoked"
ERROR = "error"
UNKNOWN = "unknown"

STATUSES = frozenset({ABSENT, PENDING, LIVE, EXPIRING, EXPIRED, REVOKED, ERROR, UNKNOWN})

#: Statuses a provider may still work with. `error` is included deliberately:
#: it means the last attempt did not reach the provider, not that the
#: credential was rejected, and refusing to use it would turn a network blip
#: into an outage. `unknown` is a credential that has not been probed since
#: this process started, which is a reason to say so in the interface, not a
#: reason to stop.
USABLE = frozenset({LIVE, EXPIRING, ERROR, UNKNOWN})

ACTION_NONE = "none"
ACTION_REAUTHORIZE = "reauthorize"
ACTION_RESEED_COOKIE = "reseed_cookie"
ACTION_PASTE_TOKEN = "paste_token"

#: How the user fixes each method, so the interface can render one button
#: without matching on the text of an error message.
ACTION_BY_METHOD = {
    "cookie_jar": ACTION_RESEED_COOKIE,
    "token_paste": ACTION_PASTE_TOKEN,
}

#: Statuses where there is something for the user to do.
NEEDS_ACTION = frozenset({ABSENT, PENDING, EXPIRING, EXPIRED, REVOKED})

#: Methods that can renew themselves without the user. Everything else either
#: never expires or can only be replaced by hand.
REFRESHABLE_METHODS = frozenset({"oauth2"})

#: How long after an ambiguous 403 the single re-probe runs. Long enough that a
#: WAF's rate window has moved on, short enough that a genuinely dead session is
#: reported while the user is still at the keyboard.
REPROBE_DELAY_S = 90

#: Backoff between attempts after a failure that did not judge the credential,
#: indexed by consecutive_failures. The last value repeats.
BACKOFF_SECONDS = (60, 300, 900, 3600, 21600)


@dataclass(frozen=True)
class Presentation:
    """How one credential status reaches the screen.

    `text` is a format template taking `label` (the provider's display name)
    and `age` (how long since the last successful probe, already worded). The
    live case has no text because a working provider says nothing.
    """

    provider_state: str  # ok | config | auth_expired | error
    text: str
    banner: bool = False  # a persistent banner, not a toast
    disable_rows: bool = False  # rows whose only path is this provider lose their button
    stale: bool = False  # shown as working, but nothing has confirmed it

    def render(self, label: str, age: str = "") -> str:
        return self.text.format(label=label, age=age)


#: Roadmap C8's mapping table, as data. Published once so the interface has one
#: branch rather than seven, and so a new status cannot be added without
#: someone deciding what it looks like.
STATUS_UI: dict[str, Presentation] = {
    ABSENT: Presentation("config", "not set up"),
    PENDING: Presentation("config", "not set up"),
    LIVE: Presentation("ok", ""),
    EXPIRING: Presentation("ok", "reconnect soon", banner=True),
    # The wording is what a wishlist row shows inline beside a disabled button,
    # so it names the provider: "Qobuz session expired".
    EXPIRED: Presentation("auth_expired", "{label} session expired", banner=True, disable_rows=True),
    REVOKED: Presentation("auth_expired", "{label} session expired", banner=True, disable_rows=True),
    ERROR: Presentation("error", "cannot reach {label}"),
    UNKNOWN: Presentation("ok", "unverified for {age}", stale=True),
}


def presentation(status: str) -> Presentation:
    """The interface treatment for a status. Unknown values are not guessed at."""
    try:
        return STATUS_UI[status]
    except KeyError:
        raise ValueError(f"{status!r} is not a credential status; see STATUS_UI") from None


def action_for(method: str, status: str) -> str:
    """Which button the user needs, from the closed set the interface renders."""
    if status not in NEEDS_ACTION:
        return ACTION_NONE
    return ACTION_BY_METHOD.get(method, ACTION_REAUTHORIZE)


def _now() -> int:
    return int(time.time())


def _loads(raw: str | None, default: dict | None = None) -> dict:
    if not raw:
        return dict(default or {})
    try:
        value = json.loads(raw)
    except ValueError:
        return dict(default or {})
    return value if isinstance(value, dict) else dict(default or {})


def harden(db_path: Path | str) -> None:
    """Restrict the database and its journals to the owner.

    The WAL and shared-memory files are created by SQLite as needed and inherit
    the process umask rather than the mode of the main file, so setting the mode
    once at creation misses them. Called every time a store is opened, which is
    cheap and catches a file that appeared since the last call.
    """
    base = Path(db_path)
    for path in (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")):
        try:
            if path.exists():
                os.chmod(path, 0o600)
        except OSError as exc:
            log.warning("could not restrict permissions", context={"path": str(path), "error": str(exc)})


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class SqliteCredentialStore:
    """The only code that reads or writes `credentials.secret`.

    Takes a connection factory rather than a connection because SQLite
    connections are per-thread here and the janitor, the web request and a
    worker all reach the same rows.
    """

    def __init__(
        self,
        db: Callable[[], sqlite3.Connection],
        *,
        db_path: Path | str | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._db = db
        self.log = logger or log
        if db_path is not None:
            harden(db_path)

    # -- reads -------------------------------------------------------------

    def _row(self, provider: str) -> sqlite3.Row | None:
        return self._db().execute(
            "SELECT * FROM credentials WHERE provider = ?", (provider,)
        ).fetchone()

    def get(self, provider: str) -> Credential | None:
        row = self._row(provider)
        return None if row is None else _to_credential(row)

    def require(self, provider: str) -> Credential:
        """The credential, or an explanation of why the user has to act.

        Raises `AuthExpired` rather than returning None so that a provider
        written without a null check fails loudly at the top of a call instead
        of sending an unauthenticated request and misreading the answer.
        """
        row = self._row(provider)
        if row is None:
            raise AuthExpired(f"{provider} is not connected", code="absent", provider_id=provider)
        if row["status"] not in USABLE:
            raise AuthExpired(
                f"{provider} credential is {row['status']}"
                + (f": {row['last_error']}" if row["last_error"] else ""),
                code=row["status"],
                provider_id=provider,
            )
        return _to_credential(row)

    def providers(self) -> list[str]:
        return [r["provider"] for r in self._db().execute(
            "SELECT provider FROM credentials ORDER BY provider"
        )]

    def status(self, provider: str) -> CredentialStatus:
        """The safe projection: everything the API and the event stream may see."""
        row = self._row(provider)
        if row is None:
            return CredentialStatus(
                provider=provider, method="", status=ABSENT, account=None, detail=None,
                action=ACTION_REAUTHORIZE, expires_at=None, last_ok_at=None, refreshable=False,
            )
        meta = _loads(row["public_meta"])
        return CredentialStatus(
            provider=row["provider"],
            method=row["method"],
            status=row["status"],
            account=meta.get("account"),
            detail=row["last_error"],
            action=action_for(row["method"], row["status"]),
            expires_at=row["expires_at"],
            last_ok_at=row["last_ok_at"],
            refreshable=bool(meta.get("refreshable", row["method"] in REFRESHABLE_METHODS)),
        )

    def all_status(self) -> list[CredentialStatus]:
        return [self.status(p) for p in self.providers()]

    def due(self, *, now: int | None = None) -> list[str]:
        """Providers whose scheduled refresh or re-probe has come round."""
        return [r["provider"] for r in self._db().execute(
            "SELECT provider FROM credentials WHERE refresh_after IS NOT NULL "
            "AND refresh_after <= ? ORDER BY refresh_after",
            (now if now is not None else _now(),),
        )]

    # -- writes ------------------------------------------------------------

    def put(
        self,
        provider: str,
        method: str,
        secret: dict,
        *,
        public_meta: dict | None = None,
        expires_at: int | None = None,
        status: str = LIVE,
        refresh_after: int | None = None,
        now: int | None = None,
    ) -> None:
        """Write a whole credential. Used by a flow that just completed."""
        if status not in STATUSES:
            raise ValueError(f"{status!r} is not a credential status")
        ts = now if now is not None else _now()
        meta = dict(public_meta or {})
        meta.setdefault("refreshable", method in REFRESHABLE_METHODS)
        self._db().execute(
            """
            INSERT INTO credentials(provider, method, status, secret, public_meta,
                                    expires_at, refresh_after, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                method = excluded.method,
                status = excluded.status,
                secret = excluded.secret,
                public_meta = excluded.public_meta,
                expires_at = excluded.expires_at,
                refresh_after = excluded.refresh_after,
                last_error = NULL,
                last_error_at = NULL,
                consecutive_failures = 0,
                updated_at = excluded.updated_at
            """,
            (provider, method, status, json.dumps(secret), json.dumps(meta),
             expires_at, refresh_after, ts, ts),
        )

    def patch_secret(self, provider: str, **fields: Any) -> None:
        """Merge top-level keys into the stored secret.

        A refresh writes `token=` and nothing else. Putting a whole object
        instead would drop `app.client_secret`, which is only ever supplied
        once and cannot be recovered from the provider.
        """
        row = self._row(provider)
        if row is None:
            raise AuthExpired(f"{provider} is not connected", code="absent", provider_id=provider)
        secret = _loads(row["secret"])
        secret.update(fields)
        self._db().execute(
            "UPDATE credentials SET secret = ?, updated_at = ? WHERE provider = ?",
            (json.dumps(secret), _now(), provider),
        )

    def patch_public(self, provider: str, **fields: Any) -> None:
        """Merge into `public_meta`. Anything passed here may reach a screenshot."""
        row = self._row(provider)
        if row is None:
            return
        meta = _loads(row["public_meta"])
        meta.update(fields)
        self._db().execute(
            "UPDATE credentials SET public_meta = ?, updated_at = ? WHERE provider = ?",
            (json.dumps(meta), _now(), provider),
        )

    def set_expiry(self, provider: str, expires_at: int | None, *, refresh_after: int | None = None) -> None:
        self._db().execute(
            "UPDATE credentials SET expires_at = ?, refresh_after = ?, updated_at = ? WHERE provider = ?",
            (expires_at, refresh_after, _now(), provider),
        )

    def set_status(self, provider: str, status: str, *, detail: str | None = None,
                   now: int | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"{status!r} is not a credential status")
        ts = now if now is not None else _now()
        self._db().execute(
            "UPDATE credentials SET status = ?, last_error = COALESCE(?, last_error), "
            "updated_at = ? WHERE provider = ?",
            (status, detail, ts, provider),
        )

    def mark_ok(self, provider: str, *, now: int | None = None) -> None:
        """Record that a live call against this credential just worked.

        This is the only way back to `live`, which is what keeps the rule from
        03 section 7.3 true: a status that has never been checked against
        reality is never reported as live.
        """
        ts = now if now is not None else _now()
        self._db().execute(
            "UPDATE credentials SET status = ?, last_ok_at = ?, last_probe_at = ?, "
            "last_error = NULL, last_error_at = NULL, consecutive_failures = 0, "
            "refresh_after = CASE WHEN status = ? THEN NULL ELSE refresh_after END, "
            "updated_at = ? WHERE provider = ?",
            (LIVE, ts, ts, ERROR, ts, provider),
        )

    def mark_failed(self, provider: str, detail: str, *, fatal: bool,
                    retry_in: int | None = None, now: int | None = None) -> None:
        """Record a failure. `fatal` decides whether the credential is judged.

        `fatal=False` means the attempt failed without telling us anything
        about the credential: a timeout, a 5xx, or an ambiguous 403. The row
        goes to `error`, which the interface renders as "cannot reach X", and a
        retry is scheduled. `fatal=True` means the provider rejected the
        credential itself and only the user can fix it.
        """
        row = self._row(provider)
        if row is None:
            raise AuthExpired(f"{provider} is not connected", code="absent", provider_id=provider)
        ts = now if now is not None else _now()
        failures = (row["consecutive_failures"] or 0) + 1
        if fatal:
            status, retry_at = EXPIRED, None
        else:
            status = ERROR
            wait = retry_in if retry_in is not None else BACKOFF_SECONDS[
                min(failures - 1, len(BACKOFF_SECONDS) - 1)
            ]
            retry_at = ts + wait
        self._db().execute(
            "UPDATE credentials SET status = ?, last_error = ?, last_error_at = ?, "
            "last_probe_at = ?, consecutive_failures = ?, refresh_after = ?, updated_at = ? "
            "WHERE provider = ?",
            (status, detail, ts, ts, failures, retry_at, ts, provider),
        )

    def revoke(self, provider: str, *, detail: str = "revoked") -> None:
        """Wipe the secret but keep the row, so the interface can still explain itself.

        For a cookie provider this is not revocation at the site: if the
        browser is still logged in and the extension still enabled, the next
        heartbeat pushes the jar straight back. Telling the user that is the
        caller's job.
        """
        self._db().execute(
            "UPDATE credentials SET status = ?, secret = NULL, expires_at = NULL, "
            "refresh_after = NULL, last_error = ?, last_error_at = ?, updated_at = ? "
            "WHERE provider = ?",
            (REVOKED, detail, _now(), _now(), provider),
        )

    def delete(self, provider: str) -> None:
        """Remove the row entirely. The provider goes back to looking unconfigured."""
        self._db().execute("DELETE FROM credentials WHERE provider = ?", (provider,))

    # -- in-flight authorizations -----------------------------------------

    def pending_put(self, state: str, provider: str, mode: str, *, verifier: str = "",
                    redirect_uri: str = "", poll_interval: int | None = None,
                    ttl: int = 600, now: int | None = None) -> None:
        ts = now if now is not None else _now()
        self._db().execute(
            "INSERT OR REPLACE INTO auth_pending(state, provider, mode, verifier, "
            "redirect_uri, poll_interval, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (state, provider, mode, verifier, redirect_uri, poll_interval, ts, ts + ttl),
        )

    def pending_take(self, state: str, *, now: int | None = None) -> dict | None:
        """Read a pending row and delete it in the same breath.

        Single use is what makes a pasted callback URL safe to paste twice: the
        second attempt gets "already used" instead of a second exchange against
        a code the provider has already burned.
        """
        conn = self._db()
        row = conn.execute("SELECT * FROM auth_pending WHERE state = ?", (state,)).fetchone()
        conn.execute("DELETE FROM auth_pending WHERE state = ?", (state,))
        if row is None:
            return None
        if row["expires_at"] < (now if now is not None else _now()):
            return None
        return dict(row)

    def pending_latest(self, provider: str) -> dict | None:
        row = self._db().execute(
            "SELECT * FROM auth_pending WHERE provider = ? ORDER BY created_at DESC LIMIT 1",
            (provider,),
        ).fetchone()
        return None if row is None else dict(row)

    def pending_sweep(self, *, now: int | None = None) -> int:
        cur = self._db().execute(
            "DELETE FROM auth_pending WHERE expires_at < ?",
            (now if now is not None else _now(),),
        )
        return cur.rowcount or 0


def _to_credential(row: sqlite3.Row) -> Credential:
    return Credential(
        provider=row["provider"],
        method=row["method"],
        status=row["status"],
        secret=_loads(row["secret"]),
        public_meta=_loads(row["public_meta"]),
        expires_at=row["expires_at"],
    )


# --------------------------------------------------------------------------
# The cookie broker's jar, as an HttpClient cookie jar
# --------------------------------------------------------------------------

_jar_locks: dict[str, threading.RLock] = {}
_jar_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.RLock:
    with _jar_locks_guard:
        return _jar_locks.setdefault(path, threading.RLock())


def _cookie(name: str, value: str, domain: str) -> Cookie:
    return Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
        path="/", path_specified=True, secure=False, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    )


class BrokerCookieJar(CookieJar):
    """A cookie jar whose contents are the cookie broker's stored session.

    The broker owns the session: a browser extension seeds it, a keepalive
    thread probes it, and it absorbs rotation on its own requests. Providers
    must not read that file. Presenting it as a `CookieJar` means a provider
    makes an ordinary request through `ctx.http` and gets whatever the session
    currently is, and anything the site rotates on the way back is written
    where the broker will read it next.

    Two behaviours are deliberate. The jar is re-read before every request,
    because the keepalive thread may have rotated the session since the last
    one. And cookies are merged into the stored set rather than replacing it,
    so a write that interleaves with the broker's own loses nothing.
    """

    def __init__(self, jar_path: Path | str, domain: str, *,
                 dead_markers: Iterable[str] = ("/signin", "/login")) -> None:
        super().__init__()
        self.path = Path(jar_path)
        self.domain = domain
        self.dead_markers = tuple(dead_markers)
        self._lock = _lock_for(str(self.path))
        self.reload()

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            return {"cookies": {}, "meta": {}}
        state.setdefault("cookies", {})
        state.setdefault("meta", {})
        return state

    def _write(self, state: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def reload(self) -> None:
        state = self._read()
        self.clear()
        for name, value in (state.get("cookies") or {}).items():
            if value:
                self.set_cookie(_cookie(name, value, self.domain))

    def persist(self) -> None:
        with self._lock:
            state = self._read()
            stored = dict(state.get("cookies") or {})
            merged = {**stored, **{c.name: c.value for c in self}}
            if merged != stored:
                state["cookies"] = merged
                state.setdefault("meta", {})["last_rotation"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
                self._write(state)

    def add_cookie_header(self, request) -> None:  # type: ignore[override]
        self.reload()
        super().add_cookie_header(request)

    def extract_cookies(self, response, request) -> None:  # type: ignore[override]
        super().extract_cookies(response, request)
        # A logged-out response carries a fresh anonymous session. Absorbing it
        # would overwrite the last known good jar with a worthless one and
        # destroy the evidence of what the real session was, so the landing URL
        # is checked first. This is the broker's own rule, applied here because
        # this path does not go through the broker.
        url = request.get_full_url()
        if any(marker in url for marker in self.dead_markers):
            return
        self.persist()


def broker_jar(keeper: Any) -> BrokerCookieJar:
    """Build a jar from a `SessionKeeper`, without importing it.

    The broker lives at the repository root and is wired up by the application
    entry point, not by this package, so it is taken structurally: anything
    carrying a `cfg` with `jar_path`, `probe_url` and `dead_url_markers` works,
    which is also what makes this testable without a live site.
    """
    cfg = keeper.cfg
    domain = getattr(cfg, "cookie_domain", None) or (
        urllib.parse.urlsplit(cfg.probe_url).hostname or ""
    )
    return BrokerCookieJar(cfg.jar_path, domain,
                           dead_markers=getattr(cfg, "dead_url_markers", ("/signin", "/login")))


# --------------------------------------------------------------------------
# The handle
# --------------------------------------------------------------------------


def default_http_factory(user_agent: str, timeout: int = 30) -> Callable[..., HttpClient]:
    """The factory the runtime passes in, closing over the configured agent."""

    def make(**kw: Any) -> HttpClient:
        kw.setdefault("user_agent", user_agent)
        kw.setdefault("timeout", timeout)
        return HttpClient(**kw)

    return make


class ProviderCredentials:
    """A `CredentialHandle`: one provider's view of the credential store.

    The provider id is fixed at construction and is the only one this object
    ever passes to the store. There is no method that takes a provider name, so
    a provider cannot read or damage another provider's credential through the
    handle it was given, whatever it does with it.
    """

    def __init__(
        self,
        provider_id: str,
        store: SqliteCredentialStore,
        *,
        http_factory: Callable[..., HttpClient] | None = None,
        keeper: Any = None,
        logger: Logger | None = None,
    ) -> None:
        self._provider = provider_id
        self._store = store
        self._http_factory = http_factory or default_http_factory("library-wishlist/1.0 (+self-hosted)")
        self._keeper = keeper
        self.log = (logger or log).bind(provider=provider_id)

    @property
    def provider_id(self) -> str:
        return self._provider

    def require(self) -> Credential:
        return self._store.require(self._provider)

    def status(self) -> CredentialStatus:
        return self._store.status(self._provider)

    def http_client(self, **kw: Any) -> HttpClient:
        """An HTTP client that already carries this provider's credential.

        For a cookie provider that means a client wired to the broker's jar, so
        a rotated session is followed without the provider knowing sessions
        rotate. For everything else it is a plain client and the caller adds
        the header its API wants, because a bearer token, a query parameter and
        a signed request are not interchangeable.
        """
        kw.setdefault("provider_id", self._provider)
        cred = self._store.get(self._provider)
        if self._keeper is not None and (cred is None or cred.method == "cookie_jar"):
            kw.setdefault("cookie_jar", broker_jar(self._keeper))
            agent = getattr(self._keeper.cfg, "user_agent", None)
            if agent:
                kw.setdefault("user_agent", agent)
        return self._http_factory(**kw)

    def mark_ok(self) -> None:
        self._store.mark_ok(self._provider)

    def mark_failed(self, detail: str, *, fatal: bool) -> None:
        """Report a failure against this credential.

        A provider calls this with `fatal=False` for anything that did not
        judge the credential, which includes a 403: only the auth layer's
        re-probe may conclude that a 403 meant the login is dead.
        """
        self._store.mark_failed(self._provider, detail, fatal=fatal)
