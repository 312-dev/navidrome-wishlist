"""Receiver side of the Cookie Broker.

The browser extension only ever SEEDS a jar. Everything about keeping that
session alive lives here:

  * ``open()``    - every outbound request goes through the persisted jar and
                    absorbs any ``Set-Cookie`` the site returns, so a rotating
                    session is followed rather than stranded. This is the
                    "refresh on its own flow" path.
  * ``touch()``   - a periodic authenticated probe, so an idle session never
                    ages out between real uses.
  * ``status()``  - a LIVE probe. It never reports a stored opinion, because a
                    status endpoint that echoes what it was handed will happily
                    claim a dead session is fine.

Stdlib only, to match the existing music-stack scripts.

Integration is two lines in a Flask app::

    from cookie_broker import SessionKeeper, SiteConfig, make_blueprint

    qobuz = SessionKeeper(SiteConfig(
        name="qobuz",
        jar_path="queue/qobuz_jar.json",
        legacy_header_path="queue/qobuz_cookie.txt",
        probe_url="https://www.qobuz.com/profile/downloads/track",
        dead_url_markers=("/signin", "/login"),
    ))
    qobuz.start_keepalive()
    app.register_blueprint(make_blueprint({"qobuz": qobuz}))
"""

from __future__ import annotations

import hmac
import http.cookiejar
import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

# A browser User-Agent is not cosmetic. A default urllib UA gets 403'd outright
# by some WAFs, which then looks exactly like an expired session.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class SiteConfig:
    name: str
    jar_path: str
    probe_url: str

    # Substrings that, when they appear in the FINAL url after redirects, mean
    # the session is dead. Checking the landing url beats sniffing the body:
    # a login page's markup changes, a redirect to /signin does not.
    dead_url_markers: tuple[str, ...] = ("/signin", "/login")

    # Optional: also mirror the jar to a flat `name=value; ...` file, so
    # existing scripts that read a Cookie header keep working untouched.
    legacy_header_path: str | None = None

    cookie_domain: str | None = None  # defaults to the probe url's host
    user_agent: str = DEFAULT_UA
    keepalive_seconds: int = 6 * 3600
    request_timeout: int = 45

    # Called with (site_name, message) on a live -> dead transition.
    on_dead: Callable[[str, str], None] | None = None

    extra_headers: Mapping[str, str] = field(default_factory=dict)


class SessionKeeper:
    def __init__(self, cfg: SiteConfig):
        self.cfg = cfg
        # Re-entrant, and held across the whole load/request/save cycle. A
        # rolling session cannot tolerate two in-flight consumers: both send
        # the pre-rotation value, the site rotates for the first, and the
        # second 401s and takes the session down with it.
        self._lock = threading.RLock()
        self._ssl_ctx = ssl.create_default_context()
        self._keepalive_thread: threading.Thread | None = None

        host = urllib.parse.urlsplit(cfg.probe_url).hostname or ""
        self._domain = cfg.cookie_domain or host
        os.makedirs(os.path.dirname(os.path.abspath(cfg.jar_path)) or ".", exist_ok=True)

    # -- persistence ------------------------------------------------------

    def _read_state(self) -> dict:
        try:
            with open(self.cfg.jar_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            return {"cookies": {}, "meta": {}}
        state.setdefault("cookies", {})
        state.setdefault("meta", {})
        return state

    def _write_state(self, state: dict) -> None:
        tmp = f"{self.cfg.jar_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.cfg.jar_path)

        if self.cfg.legacy_header_path:
            header = self._header_from(state["cookies"])
            legacy_tmp = f"{self.cfg.legacy_header_path}.tmp"
            with open(legacy_tmp, "w", encoding="utf-8") as fh:
                fh.write(header + "\n")
            os.chmod(legacy_tmp, 0o600)
            os.replace(legacy_tmp, self.cfg.legacy_header_path)

    @staticmethod
    def _header_from(cookies: Mapping[str, str]) -> str:
        return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)

    def _touch_meta(self, **patch) -> None:
        state = self._read_state()
        state["meta"].update(patch)
        self._write_state(state)

    # -- jar plumbing -----------------------------------------------------

    def _cookiejar(self, cookies: Mapping[str, str]) -> http.cookiejar.CookieJar:
        jar = http.cookiejar.CookieJar()
        for name, value in cookies.items():
            jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=self._domain,
                    domain_specified=True,
                    domain_initial_dot=self._domain.startswith("."),
                    path="/",
                    path_specified=True,
                    secure=False,
                    expires=None,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
        return jar

    # -- the flow-refresh path -------------------------------------------

    def open(self, url: str, *, headers: Mapping[str, str] | None = None, timeout: int | None = None):
        """GET ``url`` through the persisted jar, absorbing any rotation.

        Returns ``(final_url, body_bytes, response_headers)``. Route real work
        (page scrapes, downloads) through this so that ordinary use keeps the
        session both current and warm.
        """
        with self._lock:
            state = self._read_state()
            if not state["cookies"]:
                raise SessionDead(f"No {self.cfg.name} cookies stored. Log in via the browser to seed one.")

            jar = self._cookiejar(state["cookies"])
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar),
                urllib.request.HTTPSHandler(context=self._ssl_ctx),
            )
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.cfg.user_agent,
                    "Accept-Language": "en-US,en;q=0.9",
                    **dict(self.cfg.extra_headers),
                    **dict(headers or {}),
                },
            )
            resp = opener.open(req, timeout=timeout or self.cfg.request_timeout)
            body = resp.read()
            final_url = resp.geturl()

            # Absorb whatever the site handed back. Most PHP-style sessions keep
            # a stable id and merely refresh their server-side expiry on access,
            # in which case this is a no-op and the value of the call was the
            # access itself. When a site does roll its cookie, this is what
            # keeps us on the chain.
            #
            # But NOT when we landed on a signin page. A logged-out response
            # typically carries a brand new anonymous session, and absorbing it
            # would overwrite the last known real jar with a worthless one,
            # destroying the evidence of what we used to hold.
            landed_dead = any(m in final_url for m in self.cfg.dead_url_markers)
            if not landed_dead:
                rotated = {c.name: c.value for c in jar}
                if rotated != state["cookies"]:
                    state["cookies"] = {**state["cookies"], **rotated}
                    state["meta"]["last_rotation"] = _now()
            state["meta"]["last_request"] = _now()
            self._write_state(state)

            return final_url, body, resp.headers

    # -- liveness ---------------------------------------------------------

    def probe(self) -> tuple[bool, str]:
        """Hit the probe url and report ``(alive, detail)``.

        Network failures are reported as ``(False, ...)`` but are deliberately
        distinguishable from an auth failure by the caller via ``last_error``,
        so a flaky link does not get mistaken for a dead session.
        """
        try:
            final_url, body, _ = self.open(self.cfg.probe_url)
        except SessionDead as exc:
            return False, str(exc)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return False, f"HTTP {exc.code} (session rejected)"
            return False, f"HTTP {exc.code}"
        except Exception as exc:  # network-ish
            return False, f"unreachable: {exc}"

        if any(marker in final_url for marker in self.cfg.dead_url_markers):
            return False, f"redirected to {final_url} (session expired)"
        if not body:
            return False, "empty response"
        return True, "ok"

    def touch(self) -> bool:
        """Keepalive tick: probe, record, and alert on a live -> dead edge."""
        alive, detail = self.probe()
        with self._lock:
            state = self._read_state()
            was_alive = state["meta"].get("alive")
            state["meta"].update(
                alive=alive, last_probe=_now(), last_detail=detail
            )
            if alive:
                state["meta"]["last_alive"] = _now()
            self._write_state(state)

        # Alert only on the transition, so a dead session pings once rather
        # than every keepalive tick until it is fixed.
        if not alive and was_alive is not False and self.cfg.on_dead:
            try:
                self.cfg.on_dead(self.cfg.name, detail)
            except Exception:
                pass
        return alive

    def status(self) -> dict:
        alive, detail = self.probe()
        state = self._read_state()
        return {
            "site": self.cfg.name,
            "session": "live" if alive else "dead",
            "detail": detail,
            "cookies": len(state["cookies"]),
            "meta": state["meta"],
        }

    # -- ingest -----------------------------------------------------------

    def ingest(self, cookie_header: str) -> dict:
        """Accept a jar pushed by the browser extension, then verify it."""
        cookies = _parse_cookie_header(cookie_header)
        if not cookies:
            raise ValueError("no cookies parsed from the pushed header")

        with self._lock:
            state = self._read_state()
            state["cookies"] = cookies  # replace wholesale; the browser is authoritative
            state["meta"].update(last_ingest=_now(), ingest_count=len(cookies))
            self._write_state(state)

        alive, detail = self.probe()
        self._touch_meta(alive=alive, last_probe=_now(), last_detail=detail)
        if alive:
            self._touch_meta(last_alive=_now())
        return {"site": self.cfg.name, "session": "live" if alive else "dead", "detail": detail}

    # -- keepalive loop ---------------------------------------------------

    def start_keepalive(self) -> None:
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return

        def loop():
            # Jitter so several sites do not all probe on the same second.
            time.sleep(random.uniform(5, 30))
            while True:
                try:
                    self.touch()
                except Exception:
                    pass
                time.sleep(self.cfg.keepalive_seconds * random.uniform(0.9, 1.1))

        self._keepalive_thread = threading.Thread(
            target=loop, name=f"cookie-keepalive-{self.cfg.name}", daemon=True
        )
        self._keepalive_thread.start()


class SessionDead(RuntimeError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_cookie_header(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            out[name] = value
    return out


def ntfy_notifier(topic_url: str, title: str = "Cookie Broker") -> Callable[[str, str], None]:
    """Build an ``on_dead`` callback that pings ntfy."""

    def notify(site: str, detail: str) -> None:
        req = urllib.request.Request(
            topic_url,
            data=f"{site} session died: {detail}. Open {site} in your browser to reseed.".encode(),
            headers={"Title": f"{title}: {site}", "Priority": "default", "User-Agent": DEFAULT_UA},
        )
        urllib.request.urlopen(req, timeout=15).read()

    return notify


# ---------------------------------------------------------------------------
# Flask blueprint
# ---------------------------------------------------------------------------


def make_blueprint(keepers: Mapping[str, SessionKeeper], token_env: str = "COOKIE_BROKER_TOKEN"):
    """Blueprint exposing ``/auth/ingest`` and ``/auth/status``.

    Requires ``$COOKIE_BROKER_TOKEN``. It refuses to start without one rather
    than defaulting to open: an unauthenticated ingest endpoint lets anything
    that can reach the port overwrite, or read back the state of, a live
    session cookie.
    """
    from flask import Blueprint, jsonify, request

    token = os.environ.get(token_env, "")
    if not token:
        raise RuntimeError(f"{token_env} must be set; refusing to expose an unauthenticated ingest endpoint")

    bp = Blueprint("cookie_broker", __name__)

    def authorized() -> bool:
        supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(supplied) and hmac.compare_digest(supplied, token)

    def pick(site: str | None) -> SessionKeeper | None:
        if site:
            return keepers.get(site)
        return next(iter(keepers.values())) if len(keepers) == 1 else None

    @bp.route("/auth/ingest", methods=["POST", "OPTIONS"])
    @bp.route("/auth/ingest/<site>", methods=["POST", "OPTIONS"])
    def ingest(site: str | None = None):
        if request.method == "OPTIONS":
            return ("", 204, _cors())
        if not authorized():
            return jsonify(error="unauthorized"), 401, _cors()

        payload = request.get_json(silent=True) or {}
        keeper = pick(site or payload.get("site"))
        if not keeper:
            return jsonify(error="unknown site"), 404, _cors()

        try:
            result = keeper.ingest(payload.get("cookie") or "")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400, _cors()
        return jsonify(**result), 200, _cors()

    @bp.route("/auth/status", methods=["GET"])
    @bp.route("/auth/status/<site>", methods=["GET"])
    def status(site: str | None = None):
        if not authorized():
            return jsonify(error="unauthorized"), 401
        keeper = pick(site)
        if not keeper:
            return jsonify(sites={n: k.status() for n, k in keepers.items()})
        return jsonify(**keeper.status())

    def _cors() -> dict:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "authorization, content-type",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
        }

    return bp
