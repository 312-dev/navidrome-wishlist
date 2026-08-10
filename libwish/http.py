"""The one HTTP path.

Providers never import `urllib` themselves. Routing every request through here is
what makes timeouts, retries, rate limiting, user agent and cookie handling
uniform, and it is what lets a cookie-authenticated provider pick up a rotated
session without knowing that sessions rotate.

Status mapping is deliberately conservative about one code. A 401 means the
credential is not accepted and becomes `AuthExpired`. A 403 does not: stores
answer 403 for a perfectly good session that simply lacks rights to one item,
and turning that into `AuthExpired` would tell a user to reconnect an account
that is working. 403 raises `PermanentError`, and only the auth layer, after
re-probing an endpoint known to work, may upgrade it.
"""

from __future__ import annotations

import gzip
import json as jsonlib
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any, Mapping

from .errors import AuthExpired, NotFound, PermanentError, RateLimited, TransientError, Unreachable
from .log import Logger, get


@dataclass(frozen=True)
class Response:
    status: int
    url: str
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, "replace")

    def json(self) -> Any:
        try:
            return jsonlib.loads(self.body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise PermanentError(f"expected JSON from {self.url}: {exc}", code="bad_json") from None


def _decompress(raw: bytes, encoding: str) -> bytes:
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        return zlib.decompress(raw)
    return raw


class HttpClient:
    """A small, retrying HTTP client over the standard library.

    `retries` counts additional attempts, so the default of 2 means up to three
    requests. Only transient conditions are retried; a permanent failure is
    raised on the first response, because repeating a request the server has
    already refused on its merits just delays the error.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: int = 30,
        cookie_jar: CookieJar | None = None,
        base_url: str = "",
        provider_id: str = "",
        log: Logger | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.provider_id = provider_id
        self.cookie_jar = cookie_jar
        self.log = log or get("http", provider=provider_id or "-")
        handlers: list[urllib.request.BaseHandler] = []
        if cookie_jar is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
        self._opener = urllib.request.build_opener(*handlers)

    def _url(self, url: str, params: Mapping[str, Any] | None) -> str:
        if self.base_url and not url.startswith(("http://", "https://")):
            url = f"{self.base_url}/{url.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(clean)
        return url

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: bytes | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        retries: int = 2,
        timeout: int | None = None,
    ) -> Response:
        full = self._url(url, params)
        hdrs = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            **(headers or {}),
        }
        if json is not None:
            data = jsonlib.dumps(json).encode()
            hdrs.setdefault("Content-Type", "application/json")

        attempt = 0
        while True:
            try:
                return self._once(method, full, data, hdrs, timeout or self.timeout)
            except (Unreachable, TransientError, RateLimited) as exc:
                attempt += 1
                if attempt > retries:
                    raise
                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    delay = min(2 ** attempt, 30)
                self.log.warning(
                    "retrying after %s", type(exc).__name__,
                    context={"url": full, "attempt": attempt, "sleep": delay},
                )
                time.sleep(delay)

    def _once(self, method: str, url: str, data: bytes | None,
              headers: Mapping[str, str], timeout: int) -> Response:
        req = urllib.request.Request(url, data=data, headers=dict(headers), method=method.upper())
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                body = _decompress(raw, (resp.headers.get("Content-Encoding") or "").lower())
                return Response(resp.status, resp.geturl(), body, dict(resp.headers))
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            body = _decompress(raw, (exc.headers.get("Content-Encoding") or "").lower()) if exc.headers else raw
            raise self._for_status(exc.code, url, body, dict(exc.headers or {})) from None
        except urllib.error.URLError as exc:
            raise Unreachable(f"{url}: {exc.reason}", code="unreachable",
                              provider_id=self.provider_id) from None
        except TimeoutError:
            raise Unreachable(f"{url}: timed out after {timeout}s", code="timeout",
                              provider_id=self.provider_id) from None

    def _for_status(self, status: int, url: str, body: bytes, headers: Mapping[str, str]):
        snippet = body[:200].decode("utf-8", "replace").strip()
        pid = self.provider_id
        if status == 429:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            return RateLimited(f"{url}: rate limited", retry_after=retry_after,
                               code="rate_limited", provider_id=pid)
        if status == 401:
            return AuthExpired(f"{url}: credential rejected", code="unauthorized", provider_id=pid)
        if status == 403:
            # Not AuthExpired. See the module docstring.
            return PermanentError(f"{url}: forbidden ({snippet})", code="forbidden", provider_id=pid)
        if status == 404:
            return NotFound(f"{url}: not found", code="not_found", provider_id=pid)
        if 500 <= status < 600:
            return TransientError(f"{url}: server error {status}", code=f"http_{status}", provider_id=pid)
        return PermanentError(f"{url}: HTTP {status} ({snippet})", code=f"http_{status}", provider_id=pid)

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Response:
        return self.request("POST", url, **kw)
