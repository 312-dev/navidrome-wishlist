"""Last.fm loved tracks.

`user.getlovedtracks` reads a public profile, so an API key and a username are
the whole of the configuration and there is no session to keep alive. The
response is newest first, paginated, and carries a per-love `date.uts` that is
the second the user pressed the heart.

Two things about this API drive the shape of the code below.

Failures arrive as HTTP 200 with an error code in the body. A provider that
looked only at the status would report a suspended API key as a successful poll
that happened to find nothing, and the source would sit quietly returning zero
loves for as long as the key stayed dead. Every response is checked for `error`
before it is parsed.

Loves imported into Last.fm in bulk share a single second, so a window can be
wider than one page and can straddle the cursor. The walk pages until it sees a
love older than the cursor, and the cursor comparison is inclusive, which
re-delivers the boundary second rather than dropping the rest of it.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, ClassVar

from ..errors import AuthExpired, PermanentError, RateLimited, TransientError
from ..models import (
    AuthSpec,
    ConfigSpec,
    LovedTrack,
    ProviderInfo,
    SourcePage,
    TrackIds,
)
from . import (
    DEFAULT_POLL,
    Mode,
    SourceBase,
    bounded_page,
    clip_raw,
    cursor_after,
    register,
)

API_URL = "https://ws.audioscrobbler.com/2.0/"

#: Page sizes. An incremental poll expects a handful of new loves and a backfill
#: is walking a decade of them, so they pay different amounts per request.
PAGE_INCREMENTAL = 50
PAGE_BACKFILL = 100

#: A ceiling on how far one walk will page, high enough that no real account
#: reaches it. Hitting it is an error rather than a short answer: the walk runs
#: newest first, so stopping early leaves a gap at the old end, and a cursor
#: that says "everything at or after this second is delivered" cannot describe
#: that gap. Returning the window anyway would strand every love below it.
PAGE_LIMIT = 200

#: Documented error codes, grouped by what the caller should do about them.
#: A key that Last.fm has rejected is `AuthExpired` rather than `ConfigError`
#: because a provider may only raise `ProviderError`, and because both cases end
#: the same way: polling stops and a human has to go and fix the credential.
AUTH_CODES = frozenset({4, 9, 10, 13, 26})
TRANSIENT_CODES = frozenset({8, 11, 16})
RATE_LIMIT_CODE = 29

LASTFM_AUTH = AuthSpec(
    method="none",
    label="Last.fm",
    setup_url="https://www.last.fm/api/account/create",
    setup_help=(
        "Create an API account and paste the API key. Loved tracks are public, "
        "so there is nothing to authorize and nothing that expires."
    ),
)


def _as_list(value: Any) -> list[dict]:
    """Last.fm returns a bare object rather than a list of one."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _uts(record: dict) -> int | None:
    """The second the love happened, from `date.uts`."""
    date = record.get("date")
    return _int(date.get("uts")) if isinstance(date, dict) else None


def _clean(value: Any) -> str | None:
    """A present-but-empty Last.fm field means absent, and absent means None.

    Half of the loves on a real account carry `mbid: ""`. Passing that through
    as an identifier would have the matcher score against an empty string.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


@register
class LastfmSource(SourceBase):
    info: ClassVar[ProviderInfo] = ProviderInfo(
        id="lastfm",
        kind="source",
        display_name="Last.fm",
        homepage="https://www.last.fm",
        auth=LASTFM_AUTH,
        config=(
            ConfigSpec("api_key", required=True, secret=True,
                       help="API key from last.fm/api/account/create"),
            ConfigSpec("username", required=True,
                       help="The profile whose loved tracks to read"),
        ),
        capabilities=frozenset({"backfill", "cursor"}),
        poll=DEFAULT_POLL,
    )

    # --- health ----------------------------------------------------------
    def check(self) -> None:
        """One cheap call, so the settings screen can say more than "saved"."""
        self._call(limit=1, page=1)

    # --- fetch -----------------------------------------------------------
    def poll(self, cursor: dict | None, *, mode: Mode = "incremental",
             max_items: int = 500) -> SourcePage:
        if mode == "seed":
            return self._seed()

        after = cursor_after(cursor)
        per_page = PAGE_BACKFILL if mode == "backfill" else PAGE_INCREMENTAL
        collected: list[LovedTrack] = []
        skipped = 0
        total: int | None = None
        page_no = 1

        while True:
            body = self._call(limit=per_page, page=page_no)
            tracks = _as_list(body.get("track"))
            attr = body.get("@attr") if isinstance(body.get("@attr"), dict) else {}
            total = _int(attr.get("total")) if total is None else total
            total_pages = _int(attr.get("totalPages")) or 1

            hit_boundary = False
            for record in tracks:
                loved_at = _uts(record)
                if after is not None and loved_at is not None and loved_at < after:
                    # Newest first, so everything below this point is older than
                    # the cursor and already accounted for.
                    hit_boundary = True
                    break
                item = self._to_track(record, loved_at)
                if item is None:
                    skipped += 1
                    continue
                collected.append(item)

            if hit_boundary or not tracks or page_no >= total_pages:
                break
            page_no += 1
            if page_no > PAGE_LIMIT:
                raise PermanentError(
                    f"stopped after {PAGE_LIMIT} pages with {total_pages} to read; "
                    "this history is larger than one poll can walk",
                    code="too_many_pages", provider_id=self.info.id,
                )

        return bounded_page(cursor, collected, skipped=skipped, total=total,
                            max_items=max_items)

    def _seed(self) -> SourcePage:
        """Record where the account stands without queueing any of its history.

        The mark is the newest love's own timestamp rather than the wall clock,
        because our clock and Last.fm's are not the same clock and the gap
        between them would be a window of loves nobody ever fetches. With the
        inclusive boundary that newest second is re-delivered by the first real
        poll, which is one already-known love rather than a decade of them.
        """
        body = self._call(limit=1, page=1)
        tracks = _as_list(body.get("track"))
        attr = body.get("@attr") if isinstance(body.get("@attr"), dict) else {}
        newest = max((_uts(record) or 0 for record in tracks), default=0)
        return SourcePage(
            items=(),
            cursor={"after": newest or int(time.time())},
            total=_int(attr.get("total")),
        )

    # --- mapping ---------------------------------------------------------
    def _to_track(self, record: dict, loved_at: int | None) -> LovedTrack | None:
        artist_block = record.get("artist") if isinstance(record.get("artist"), dict) else {}
        artist = _clean(artist_block.get("name"))
        title = _clean(record.get("name"))
        if not artist or not title:
            return None

        url = _clean(record.get("url"))
        mbid = _clean(record.get("mbid"))
        artist_mbid = _clean(artist_block.get("mbid"))
        return LovedTrack(
            source_id=self.info.id,
            source_item_id=url or mbid or self._synthetic_id(artist, title),
            loved_at=loved_at,
            artist=artist,
            title=title,
            ids=TrackIds(
                recording_mbid=mbid,
                artist_mbids=(artist_mbid,) if artist_mbid else (),
                native_url=url,
            ),
            raw=clip_raw(record),
        )

    @staticmethod
    def _synthetic_id(artist: str, title: str) -> str:
        """The idempotency key when Last.fm gives us nothing to key on.

        There is no love-event id on this endpoint, so an un-love followed by a
        re-love reuses the key either way and will not re-queue. A hash of the
        two strings is stable across polls, which is the only property the key
        has to have.
        """
        digest = hashlib.sha1(f"{artist}\t{title}".encode()).hexdigest()
        return f"sha1:{digest}"

    # --- transport -------------------------------------------------------
    def _call(self, *, limit: int, page: int) -> dict:
        resp = self.http.get(
            API_URL,
            params={
                "method": "user.getlovedtracks",
                "user": self._required("username"),
                "api_key": self._required("api_key"),
                "format": "json",
                "limit": limit,
                "page": page,
            },
        )
        body = resp.json()
        if not isinstance(body, dict):
            raise PermanentError("expected a JSON object from Last.fm",
                                 code="schema", provider_id=self.info.id)
        if "error" in body:
            raise self._classify(body)
        loved = body.get("lovedtracks")
        if not isinstance(loved, dict):
            raise PermanentError("response carried no lovedtracks block",
                                 code="schema", provider_id=self.info.id)
        return loved

    def _classify(self, body: dict):
        """Turn a 200-with-an-error-body into the right exception.

        Status alone is not enough here and never has been: Last.fm answers 200
        for an invalid API key, a suspended key and a throttled one alike.
        """
        code = _int(body.get("error")) or 0
        message = str(body.get("message") or "").strip() or f"error {code}"
        pid = self.info.id
        if code in AUTH_CODES:
            return AuthExpired(f"Last.fm rejected the API key: {message}",
                               code="auth_expired", provider_id=pid)
        if code == RATE_LIMIT_CODE:
            return RateLimited(f"Last.fm is throttling: {message}",
                               code="rate_limited", provider_id=pid)
        if code in TRANSIENT_CODES:
            return TransientError(f"Last.fm is unwell: {message}",
                                  code="transient", provider_id=pid)
        return PermanentError(f"Last.fm refused the request: {message}",
                              code=f"lastfm_{code}", provider_id=pid)
