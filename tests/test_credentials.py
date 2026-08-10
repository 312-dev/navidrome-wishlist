"""Tests for credential storage, the status vocabulary and the auth methods.

No value in this file is a real credential. Everything that looks like a token,
a password or a cookie is a literal placeholder, and nothing here reaches the
network: the only site names used resolve nowhere by design.
"""

from __future__ import annotations

import email.message
import json
import os
import stat
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libwish import credentials as cr  # noqa: E402
from libwish import db as dbmod  # noqa: E402
from libwish.auth import (  # noqa: E402
    AuthManager,
    lastfm_signature,
    subsonic_auth_params,
)
from libwish.errors import AuthExpired, NotSupported  # noqa: E402
from libwish.http import Response  # noqa: E402
from libwish.models import AuthSpec  # noqa: E402

MIGRATION = REPO_ROOT / "libwish" / "migrations" / "0005_credentials.sql"

# Placeholders. None of these is, or has ever been, a credential.
FAKE_TOKEN = "placeholder-token-value"
FAKE_PASSWORD = "placeholder-password"
FAKE_COOKIE = "session=placeholder-session-value"


def spec(method: str, **kw) -> AuthSpec:
    kw.setdefault("label", "Testly")
    kw.setdefault("setup_url", None)
    kw.setdefault("setup_help", None)
    return AuthSpec(method=method, **kw)


class FakeHttp:
    """Stands in for HttpClient. Records calls and replays canned answers."""

    def __init__(self, answers=None) -> None:
        self.answers = list(answers or [])
        self.calls: list[tuple[str, str]] = []

    def _next(self, method: str, url: str):
        self.calls.append((method, url))
        if not self.answers:
            raise AssertionError(f"unexpected {method} {url}")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(200, url, json.dumps(answer).encode())

    def post(self, url, **kw):
        return self._next("POST", url)

    def get(self, url, **kw):
        return self._next("GET", url)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "wishlist.db"
        self.conn = dbmod.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.conn.executescript(MIGRATION.read_text())
        self.store = cr.SqliteCredentialStore(lambda: self.conn, db_path=self.db_path)

    def manager(self, specs, *, http=None, keepers=None) -> AuthManager:
        client = http or FakeHttp()
        return AuthManager(
            self.store, specs,
            http_factory=lambda **kw: client,
            keepers=keepers or {},
            public_url="http://wishlist.invalid:8080",
        )


class TestStore(Base):
    def test_round_trip(self):
        self.store.put("lastfm", "lastfm_web",
                       {"app": {"api_key": "test-key"}, "token": {"session_key": FAKE_TOKEN}},
                       public_meta={"account": "someone"})
        cred = self.store.get("lastfm")
        self.assertEqual(cred.provider, "lastfm")
        self.assertEqual(cred.method, "lastfm_web")
        self.assertEqual(cred.status, "live")
        self.assertEqual(cred.secret["token"]["session_key"], FAKE_TOKEN)
        self.assertEqual(cred.public_meta["account"], "someone")
        self.assertEqual(self.store.providers(), ["lastfm"])

        status = self.store.status("lastfm")
        self.assertEqual(status.account, "someone")
        self.assertFalse(status.refreshable)
        self.assertNotIn(FAKE_TOKEN, json.dumps(status.__dict__))

        self.store.delete("lastfm")
        self.assertIsNone(self.store.get("lastfm"))
        self.assertEqual(self.store.providers(), [])

    def test_database_file_is_owner_only(self):
        mode = stat.S_IMODE(os.stat(self.db_path).st_mode)
        self.assertEqual(mode, 0o600, f"expected 0600, got {oct(mode)}")

    def test_patch_secret_keeps_the_other_half(self):
        self.store.put("deezerish", "oauth2",
                       {"app": {"client_id": "test-client", "client_secret": "test-app-secret"},
                        "token": {"access_token": "first-access"}})
        self.store.patch_secret("deezerish", token={"access_token": "second-access"})
        cred = self.store.get("deezerish")
        self.assertEqual(cred.secret["app"]["client_secret"], "test-app-secret")
        self.assertEqual(cred.secret["token"]["access_token"], "second-access")

    def test_require_refuses_absent_and_expired(self):
        with self.assertRaises(AuthExpired):
            self.store.require("nobody")

        self.store.put("qobuz", "cookie_jar", {})
        self.assertEqual(self.store.require("qobuz").status, "live")

        self.store.mark_failed("qobuz", "session rejected", fatal=True)
        with self.assertRaises(AuthExpired) as caught:
            self.store.require("qobuz")
        self.assertEqual(caught.exception.code, "expired")

    def test_error_status_is_still_usable(self):
        # An unreachable provider has not told us anything about the
        # credential, so the credential is still the one we would present.
        self.store.put("qobuz", "cookie_jar", {})
        self.store.mark_failed("qobuz", "connection refused", fatal=False)
        self.assertEqual(self.store.status("qobuz").status, "error")
        self.assertEqual(self.store.require("qobuz").provider, "qobuz")

    def test_pending_row_is_single_use(self):
        self.store.pending_put("state-value", "deezerish", "same_origin",
                               verifier="verifier-value", redirect_uri="http://x.invalid/cb")
        first = self.store.pending_take("state-value")
        self.assertEqual(first["redirect_uri"], "http://x.invalid/cb")
        self.assertIsNone(self.store.pending_take("state-value"))

    def test_expired_pending_rows_are_swept(self):
        now = int(time.time())
        self.store.pending_put("old", "deezerish", "same_origin", ttl=1, now=now - 10)
        self.assertEqual(self.store.pending_sweep(now=now), 1)


class TestStatusMapping(Base):
    """Roadmap C8's table, row by row."""

    EXPECTED = {
        "absent": ("config", "not set up", False, False, False),
        "pending": ("config", "not set up", False, False, False),
        "live": ("ok", "", False, False, False),
        "expiring": ("ok", "reconnect soon", True, False, False),
        "expired": ("auth_expired", "Qobuz session expired", True, True, False),
        "revoked": ("auth_expired", "Qobuz session expired", True, True, False),
        "error": ("error", "cannot reach Qobuz", False, False, False),
        "unknown": ("ok", "unverified for 6h", False, False, True),
    }

    def test_every_status_has_a_treatment(self):
        self.assertEqual(set(self.EXPECTED), set(cr.STATUSES))
        self.assertEqual(set(cr.STATUS_UI), set(cr.STATUSES))

    def test_table(self):
        for status, (state, text, banner, disable, stale) in self.EXPECTED.items():
            with self.subTest(status=status):
                p = cr.presentation(status)
                self.assertEqual(p.provider_state, state)
                self.assertEqual(p.render(label="Qobuz", age="6h"), text)
                self.assertEqual(p.banner, banner)
                self.assertEqual(p.disable_rows, disable)
                self.assertEqual(p.stale, stale)

    def test_unknown_status_is_not_guessed_at(self):
        with self.assertRaises(ValueError):
            cr.presentation("probably_fine")

    def test_actions_are_the_closed_set(self):
        self.assertEqual(cr.action_for("cookie_jar", "expired"), "reseed_cookie")
        self.assertEqual(cr.action_for("token_paste", "absent"), "paste_token")
        self.assertEqual(cr.action_for("oauth2", "expiring"), "reauthorize")
        self.assertEqual(cr.action_for("cookie_jar", "live"), "none")
        self.assertEqual(cr.action_for("oauth2", "error"), "none")

    def test_transitions_reach_the_documented_state(self):
        cases = [
            (lambda: self.store.put("qobuz", "cookie_jar", {}, status="pending"), "pending", "config"),
            (lambda: self.store.mark_ok("qobuz"), "live", "ok"),
            (lambda: self.store.mark_failed("qobuz", "timed out", fatal=False), "error", "error"),
            (lambda: self.store.mark_failed("qobuz", "session dead", fatal=True), "expired", "auth_expired"),
            (lambda: self.store.revoke("qobuz"), "revoked", "auth_expired"),
            (lambda: self.store.set_status("qobuz", "expiring"), "expiring", "ok"),
        ]
        for act, status, state in cases:
            with self.subTest(status=status):
                act()
                got = self.store.status("qobuz")
                self.assertEqual(got.status, status)
                self.assertEqual(cr.presentation(got.status).provider_state, state)


class TestHandleIsolation(Base):
    def test_a_handle_reaches_exactly_one_provider(self):
        self.store.put("alpha", "token_paste", {"token": {"token": "alpha-placeholder"}})
        self.store.put("beta", "token_paste", {"token": {"token": "beta-placeholder"}})

        alpha = cr.ProviderCredentials("alpha", self.store)
        self.assertEqual(alpha.require().secret["token"]["token"], "alpha-placeholder")
        self.assertEqual(alpha.provider_id, "alpha")

        # There is no method on the handle that names a provider, so there is
        # no argument to pass beta's id to.
        for name in ("require", "status", "mark_ok"):
            with self.subTest(method=name):
                with self.assertRaises(TypeError):
                    getattr(alpha, name)("beta")

        alpha.mark_failed("alpha is unhappy", fatal=True)
        self.assertEqual(self.store.status("alpha").status, "expired")
        self.assertEqual(self.store.status("beta").status, "live")

    def test_marking_failed_from_a_handle_does_not_judge_by_default(self):
        self.store.put("alpha", "cookie_jar", {})
        cr.ProviderCredentials("alpha", self.store).mark_failed("403 from the edge", fatal=False)
        self.assertEqual(self.store.status("alpha").status, "error")


class TestForbiddenRule(Base):
    """The highest-value rule here: one 403 never logs the user out."""

    def setUp(self):
        super().setUp()
        self.probe_results = []
        self.probe_calls = 0

        def verify(ctx, cred):
            self.probe_calls += 1
            return self.probe_results.pop(0)

        self.spec = spec("cookie_jar", verify=verify)
        self.store.put("qobuz", "cookie_jar", {}, public_meta={"site": "qobuz"})

    def test_403_then_a_working_reprobe_leaves_the_credential_alone(self):
        mgr = self.manager({"qobuz": self.spec})
        now = int(time.time())

        mgr.on_forbidden("qobuz", "403 from /profile/downloads", now=now)
        after_403 = self.store.status("qobuz")
        self.assertEqual(after_403.status, "error")
        self.assertEqual(cr.presentation(after_403.status).render(label="Qobuz"), "cannot reach Qobuz")
        self.assertIn("qobuz", self.store.due(now=now + cr.REPROBE_DELAY_S))
        self.assertNotIn("qobuz", self.store.due(now=now + cr.REPROBE_DELAY_S - 1))

        self.probe_results = [(True, "ok")]
        outcomes = mgr.run_due(now=now + cr.REPROBE_DELAY_S)

        self.assertEqual(outcomes, {"qobuz": "live"})
        self.assertEqual(self.store.status("qobuz").status, "live")
        self.assertEqual(self.store.status("qobuz").action, "none")
        self.assertEqual(self.probe_calls, 1)

    def test_403_then_a_failing_reprobe_marks_it_expired(self):
        mgr = self.manager({"qobuz": self.spec})
        now = int(time.time())

        mgr.on_forbidden("qobuz", "403 from /profile/downloads", now=now)
        self.assertEqual(self.store.status("qobuz").status, "error")

        self.probe_results = [(False, "redirected to /signin (session expired)")]
        outcomes = mgr.run_due(now=now + cr.REPROBE_DELAY_S)

        self.assertEqual(outcomes, {"qobuz": "expired"})
        status = self.store.status("qobuz")
        self.assertEqual(status.status, "expired")
        self.assertEqual(status.action, "reseed_cookie")
        self.assertEqual(cr.presentation(status.status).render(label="Qobuz"), "Qobuz session expired")
        self.assertTrue(cr.presentation(status.status).disable_rows)
        self.assertEqual(self.probe_calls, 1)

    def test_an_unreachable_reprobe_does_not_judge_the_credential(self):
        from libwish.errors import Unreachable

        def verify(ctx, cred):
            raise Unreachable("connect timed out")

        mgr = self.manager({"qobuz": spec("cookie_jar", verify=verify)})
        now = int(time.time())
        mgr.on_forbidden("qobuz", "403", now=now)
        mgr.run_due(now=now + cr.REPROBE_DELAY_S)
        self.assertEqual(self.store.status("qobuz").status, "error")


class TestOAuth2(Base):
    SPEC = None

    def setUp(self):
        super().setUp()
        self.SPEC = spec(
            "oauth2",
            authorize_url="https://provider.invalid/authorize",
            token_url="https://provider.invalid/token",
            verify=lambda ctx, cred: (True, "ok"),
        )
        self.store.put(
            "deezerish", "oauth2",
            {"app": {"client_id": "test-client", "client_secret": "test-app-secret"},
             "token": {"access_token": "old-access", "refresh_token": "old-refresh"}},
            public_meta={"account": "someone"},
            expires_at=int(time.time()) + 60,
        )

    def test_refresh_persists_the_new_pair(self):
        http = FakeHttp([{"access_token": "new-access", "refresh_token": "new-refresh",
                          "expires_in": 3600}])
        mgr = self.manager({"deezerish": self.SPEC}, http=http)

        self.assertEqual(mgr.refresh("deezerish"), "live")

        token = self.store.get("deezerish").secret["token"]
        self.assertEqual(token["access_token"], "new-access")
        self.assertEqual(token["refresh_token"], "new-refresh")
        # The token that bought this one is kept for exactly one cycle, because
        # a response lost in flight would otherwise brick the account.
        self.assertEqual(token["prev_refresh_token"], "old-refresh")

        status = self.store.status("deezerish")
        self.assertEqual(status.status, "live")
        self.assertTrue(status.refreshable)
        self.assertEqual(status.account, "someone")
        row = self.conn.execute(
            "SELECT expires_at, refresh_after FROM credentials WHERE provider = 'deezerish'"
        ).fetchone()
        self.assertLess(row["refresh_after"], row["expires_at"])

    def test_a_rejected_refresh_expires_once_and_stops(self):
        http = FakeHttp([AuthExpired("refresh token rejected", code="unauthorized")])
        mgr = self.manager({"deezerish": self.SPEC}, http=http)

        self.assertEqual(mgr.refresh("deezerish"), "expired")
        self.assertEqual(len(http.calls), 1, "a rejected refresh must not be retried in a loop")

        status = self.store.status("deezerish")
        self.assertEqual(status.status, "expired")
        self.assertEqual(status.action, "reauthorize")
        # The credential is not scheduled again: only the user can fix it.
        self.assertEqual(self.store.due(now=int(time.time()) + 86400), [])

    def test_the_previous_refresh_token_is_tried_once_and_only_once(self):
        self.store.patch_secret("deezerish", token={
            "access_token": "old-access",
            "refresh_token": "current-refresh",
            "prev_refresh_token": "previous-refresh",
        })
        http = FakeHttp([AuthExpired("rejected", code="unauthorized"),
                         AuthExpired("rejected", code="unauthorized")])
        mgr = self.manager({"deezerish": self.SPEC}, http=http)

        self.assertEqual(mgr.refresh("deezerish"), "expired")
        self.assertEqual(len(http.calls), 2)

    def test_no_refresh_token_warns_before_it_dies(self):
        self.store.patch_secret("deezerish", token={"access_token": "only-access"})
        mgr = self.manager({"deezerish": self.SPEC}, http=FakeHttp())

        self.assertEqual(mgr.refresh("deezerish"), "expiring")
        status = self.store.status("deezerish")
        self.assertEqual(cr.presentation(status.status).render(label="Testly"), "reconnect soon")
        self.assertEqual(status.action, "reauthorize")

    def test_begin_returns_a_redirect_with_a_paste_fallback(self):
        self.store.delete("deezerish")
        mgr = self.manager({"deezerish": self.SPEC})
        step = mgr.begin("deezerish", {"client_id": "test-client"})

        self.assertEqual(step.mode, "same_origin")
        self.assertEqual(step.redirect_uri, "http://wishlist.invalid:8080/auth/deezerish/callback")
        self.assertIn("code_challenge_method=S256", step.url)
        self.assertIsNotNone(step.fallback)
        self.assertEqual(self.store.status("deezerish").status, "pending")

    def test_complete_parses_a_pasted_callback_url(self):
        self.store.delete("deezerish")
        http = FakeHttp([{"access_token": "fresh-access", "refresh_token": "fresh-refresh",
                          "expires_in": 3600}])
        mgr = self.manager({"deezerish": self.SPEC}, http=http)
        step = mgr.begin("deezerish", {"client_id": "test-client"})

        pasted = f"http://127.0.0.1:8080/auth/deezerish/callback?code=abc123&state={step.state}"
        mgr.complete("deezerish", {"callback_url": pasted})
        self.assertEqual(self.store.status("deezerish").status, "live")

        # The state is single use, so a second paste of the same URL is refused
        # rather than exchanged again.
        with self.assertRaises(Exception):
            mgr.complete("deezerish", {"callback_url": pasted})


class TestTokenPaste(Base):
    def test_whitespace_is_stripped_and_the_token_is_checked_at_paste_time(self):
        seen = {}

        def verify(ctx, cred):
            seen["token"] = cred.secret["token"]["token"]
            return True, "ok"

        mgr = self.manager({"listenbrainz": spec("token_paste", verify=verify)})
        mgr.complete("listenbrainz", {"token": f"  {FAKE_TOKEN}\n"})

        self.assertEqual(seen["token"], FAKE_TOKEN)
        self.assertEqual(self.store.status("listenbrainz").status, "live")

    def test_a_rejected_token_is_not_left_looking_connected(self):
        mgr = self.manager({"listenbrainz": spec(
            "token_paste", verify=lambda ctx, cred: (False, "invalid token"))})

        with self.assertRaises(AuthExpired):
            mgr.complete("listenbrainz", {"token": FAKE_TOKEN})
        self.assertEqual(self.store.status("listenbrainz").status, "expired")

    def test_an_unverifiable_credential_is_never_reported_live(self):
        mgr = self.manager({"listenbrainz": spec("token_paste")})
        mgr.complete("listenbrainz", {"token": FAKE_TOKEN})
        status = self.store.status("listenbrainz")
        self.assertEqual(status.status, "unknown")
        self.assertTrue(cr.presentation(status.status).stale)


class TestUserPass(Base):
    def test_subsonic_params_salt_every_request(self):
        first = subsonic_auth_params("someone", FAKE_PASSWORD)
        second = subsonic_auth_params("someone", FAKE_PASSWORD)
        self.assertEqual(first["u"], "someone")
        self.assertNotEqual(first["s"], second["s"])
        self.assertNotEqual(first["t"], second["t"])
        self.assertNotIn(FAKE_PASSWORD, json.dumps(first))

    def test_complete_stores_only_after_the_server_answers(self):
        mgr = self.manager({"navidrome": spec(
            "userpass", verify=lambda ctx, cred: (True, "ok"))})
        mgr.complete("navidrome", {"base_url": "http://navidrome.invalid:4533/",
                                   "username": "someone", "password": FAKE_PASSWORD})
        token = self.store.get("navidrome").secret["token"]
        self.assertEqual(token["base_url"], "http://navidrome.invalid:4533")
        self.assertEqual(self.store.status("navidrome").status, "live")


class TestLastfmSignature(unittest.TestCase):
    def test_format_and_callback_are_excluded(self):
        import hashlib

        params = {"method": "auth.getSession", "api_key": "test-key", "token": "test-token"}
        expected = hashlib.md5(
            ("api_keytest-keymethodauth.getSessiontokentest-token" + "test-app-secret").encode()
        ).hexdigest()
        self.assertEqual(lastfm_signature(params, "test-app-secret"), expected)
        self.assertEqual(
            lastfm_signature({**params, "format": "json", "callback": "x"}, "test-app-secret"),
            expected,
        )


class TestOAuth1a(Base):
    def test_every_entry_point_refuses_rather_than_doing_nothing(self):
        from libwish.auth import METHODS

        self.store.put("sevendigital", "oauth1a", {})
        mgr = self.manager({"sevendigital": spec("oauth1a")})

        with self.assertRaises(NotSupported):
            mgr.begin("sevendigital")
        with self.assertRaises(NotSupported):
            mgr.complete("sevendigital", {"oauth_verifier": "test-verifier"})
        with self.assertRaises(NotSupported):
            mgr.verify("sevendigital")
        with self.assertRaises(NotSupported):
            METHODS["oauth1a"].refresh(mgr.context("sevendigital"),
                                       self.store.get("sevendigital"))


class FakeSiteConfig:
    """The parts of the broker's SiteConfig this layer reads."""

    def __init__(self, name, jar_path, probe_url):
        self.name = name
        self.jar_path = str(jar_path)
        self.probe_url = probe_url
        self.cookie_domain = None
        self.dead_url_markers = ("/signin", "/login")
        self.user_agent = "Mozilla/5.0 (test)"


class FakeKeeper:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ingested = []
        self.alive = True

    def ingest(self, header):
        self.ingested.append(header)
        return {"site": self.cfg.name, "session": "live" if self.alive else "dead",
                "detail": "ok" if self.alive else "redirected to /signin"}

    def probe(self):
        return (self.alive, "ok" if self.alive else "redirected to /signin")


def seed_jar(path: Path, cookies: dict) -> None:
    path.write_text(json.dumps({"cookies": cookies, "meta": {}}))


def set_cookie_response(value: str):
    headers = email.message.Message()
    headers.add_header("Set-Cookie", value)

    class _Resp:
        def info(self):
            return headers

    return _Resp()


class TestCookieJar(Base):
    def setUp(self):
        super().setUp()
        self.jar_path = Path(self.tmp.name) / "qobuz_jar.json"
        seed_jar(self.jar_path, {"session": "placeholder-session-value"})
        self.cfg = FakeSiteConfig("qobuz", self.jar_path, "https://store.invalid/account")
        self.keeper = FakeKeeper(self.cfg)

    def test_http_client_carries_the_brokers_jar(self):
        self.store.put("qobuz", "cookie_jar", {}, public_meta={"site": "qobuz"})
        handle = cr.ProviderCredentials("qobuz", self.store, keeper=self.keeper)
        client = handle.http_client()

        self.assertIsInstance(client.cookie_jar, cr.BrokerCookieJar)
        self.assertEqual({c.name for c in client.cookie_jar}, {"session"})
        self.assertEqual(client.user_agent, "Mozilla/5.0 (test)")

        req = urllib.request.Request("https://store.invalid/account")
        client.cookie_jar.add_cookie_header(req)
        self.assertIn("placeholder-session-value", req.get_header("Cookie"))

    def test_rotation_is_written_back_where_the_broker_will_read_it(self):
        jar = cr.broker_jar(self.keeper)
        req = urllib.request.Request("https://store.invalid/account")
        jar.extract_cookies(set_cookie_response("session=rotated-placeholder; Path=/"), req)

        stored = json.loads(self.jar_path.read_text())["cookies"]
        self.assertEqual(stored["session"], "rotated-placeholder")
        self.assertEqual(stat.S_IMODE(os.stat(self.jar_path).st_mode), 0o600)

    def test_a_signin_page_does_not_overwrite_the_real_jar(self):
        jar = cr.broker_jar(self.keeper)
        req = urllib.request.Request("https://store.invalid/signin")
        jar.extract_cookies(set_cookie_response("session=anonymous-placeholder; Path=/"), req)

        stored = json.loads(self.jar_path.read_text())["cookies"]
        self.assertEqual(stored["session"], "placeholder-session-value")

    def test_a_dead_paste_is_refused_at_paste_time(self):
        self.keeper.alive = False
        mgr = self.manager({"qobuz": spec("cookie_jar")}, keepers={"qobuz": self.keeper})
        with self.assertRaises(AuthExpired):
            mgr.complete("qobuz", {"cookie": FAKE_COOKIE})
        self.assertIsNone(self.store.get("qobuz"))

    def test_a_live_paste_connects_and_stores_no_secret(self):
        mgr = self.manager({"qobuz": spec("cookie_jar")}, keepers={"qobuz": self.keeper})
        mgr.complete("qobuz", {"cookie": FAKE_COOKIE})

        self.assertEqual(self.keeper.ingested, [FAKE_COOKIE])
        cred = self.store.get("qobuz")
        self.assertEqual(cred.status, "live")
        self.assertEqual(cred.secret, {})
        row = self.conn.execute(
            "SELECT secret FROM credentials WHERE provider = 'qobuz'").fetchone()
        self.assertNotIn("placeholder", row["secret"] or "")


class TestBrokerIsUnchanged(unittest.TestCase):
    """The deployed broker is used as it is, so its jar format is the contract."""

    def test_a_real_session_keeper_backs_the_jar(self):
        try:
            from cookie_broker import SessionKeeper, SiteConfig
        except ImportError:  # pragma: no cover - only if run outside the repo
            self.skipTest("cookie_broker.py is not importable from here")

        with tempfile.TemporaryDirectory() as tmp:
            jar_path = Path(tmp) / "jar.json"
            seed_jar(jar_path, {"session": "placeholder-session-value"})
            keeper = SessionKeeper(SiteConfig(
                name="qobuz",
                jar_path=str(jar_path),
                probe_url="https://store.invalid/account",
            ))
            jar = cr.broker_jar(keeper)
            self.assertEqual({c.name for c in jar}, {"session"})
            self.assertEqual(jar.domain, "store.invalid")
            self.assertEqual(jar.dead_markers, ("/signin", "/login"))


class TestMigration(unittest.TestCase):
    def test_it_creates_both_tables_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fresh.db"
            conn = sqlite3.connect(path)
            conn.executescript(MIGRATION.read_text())
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            conn.close()
        self.assertEqual(names, {"credentials", "auth_pending"})


if __name__ == "__main__":
    unittest.main()
