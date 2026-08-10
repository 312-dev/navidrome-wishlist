"""ListenBrainz feedback with a positive score.

`/1/feedback/user/<user>/get-feedback?score=1` is readable without a token for a
public profile, so the token is optional configuration and is sent only when the
user supplied one. Results come back newest first, offset paginated, with a
per-item `created` timestamp.

This is the richest of the two shipping sources and the least reliable one. With
`metadata=true` it returns recording, release and artist MBIDs, which is what
makes it the source whose records win a metadata merge, and it is also the
variant that produced two dozen read timeouts in the live ingest log. The
mitigation is the timeout and page size below: a longer deadline than the shared
default, and a small page on the incremental path where a handful of new loves
is the expectation.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar
from urllib.parse import quote  # escaping a path segment, not a second HTTP path

from ..errors import PermanentError
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

API_BASE = "https://api.listenbrainz.org"

#: `metadata=true` is expensive server-side and this endpoint times out under
#: the shared 30 second default often enough to have filled the old log.
TIMEOUT_S = 45

#: An incremental poll is looking for the few loves since the last one, so it
#: asks for a small page of the expensive metadata variant. A backfill is
#: reading a whole history and pays for the larger page.
PAGE_INCREMENTAL = 25
PAGE_BACKFILL = 100

#: A ceiling on how far one walk will page, high enough that no real account
#: reaches it. Hitting it is an error rather than a short answer: the walk runs
#: newest first, so stopping early leaves a gap at the old end, and a cursor
#: that says "everything at or after this second is delivered" cannot describe
#: that gap. Returning the window anyway would strand every love below it.
PAGE_LIMIT = 200

#: Cover Art Archive addressing. The API hands back the pieces (`caa_id` and the
#: release it belongs to) rather than a URL, and this is the documented way to
#: assemble one. Nothing is guessed: without both pieces there is no cover.
CAA_URL = "https://archive.org/download/mbid-{release}/mbid-{release}-{caa_id}_thumb250.jpg"

LISTENBRAINZ_AUTH = AuthSpec(
    method="none",
    label="ListenBrainz",
    setup_url="https://listenbrainz.org/settings/",
    setup_help=(
        "A username is enough for a public profile. Add the user token from "
        "your ListenBrainz settings only if your feedback is private."
    ),
)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


@register
class ListenBrainzSource(SourceBase):
    info: ClassVar[ProviderInfo] = ProviderInfo(
        id="listenbrainz",
        kind="source",
        display_name="ListenBrainz",
        homepage="https://listenbrainz.org",
        auth=LISTENBRAINZ_AUTH,
        config=(
            ConfigSpec("username", required=True,
                       help="The ListenBrainz account whose feedback to read"),
            ConfigSpec("token", secret=True,
                       help="User token, needed only for a private profile"),
        ),
        capabilities=frozenset({"backfill", "cursor"}),
        poll=DEFAULT_POLL,
    )

    # --- health ----------------------------------------------------------
    def check(self) -> None:
        self._call(count=1, offset=0, metadata=False)

    # --- fetch -----------------------------------------------------------
    def poll(self, cursor: dict | None, *, mode: Mode = "incremental",
             max_items: int = 500) -> SourcePage:
        if mode == "seed":
            return self._seed()

        after = cursor_after(cursor)
        count = PAGE_BACKFILL if mode == "backfill" else PAGE_INCREMENTAL
        collected: list[LovedTrack] = []
        skipped = 0
        total: int | None = None
        offset = 0
        pages = 0

        while True:
            pages += 1
            if pages > PAGE_LIMIT:
                raise PermanentError(
                    f"stopped after {PAGE_LIMIT} requests with {total} loves to read; "
                    "this history is larger than one poll can walk",
                    code="too_many_pages", provider_id=self.info.id,
                )
            body = self._call(count=count, offset=offset, metadata=True)
            feedback = body.get("feedback")
            if not isinstance(feedback, list):
                raise PermanentError("response carried no feedback list",
                                     code="schema", provider_id=self.info.id)
            total = _int(body.get("total_count")) if total is None else total

            hit_boundary = False
            for record in feedback:
                if not isinstance(record, dict):
                    skipped += 1
                    continue
                loved_at = _int(record.get("created"))
                if after is not None and loved_at is not None and loved_at < after:
                    # Newest first, so the rest of this response is older than
                    # the cursor and has been seen already.
                    hit_boundary = True
                    break
                item = self._to_track(record, loved_at)
                if item is None:
                    skipped += 1
                    continue
                collected.append(item)

            offset += len(feedback)
            if (hit_boundary or len(feedback) < count
                    or (total is not None and offset >= total)):
                break

        return bounded_page(cursor, collected, skipped=skipped, total=total,
                            max_items=max_items)

    def _seed(self) -> SourcePage:
        """Mark where the account stands without queueing its history.

        `metadata=false` because nothing here is being turned into a record; the
        one field this needs is the newest `created`. Taking the mark from the
        feedback itself rather than from our own clock keeps the two clocks out
        of it, and the inclusive boundary then re-delivers that one second on
        the first real poll rather than skipping whatever shares it.
        """
        body = self._call(count=1, offset=0, metadata=False)
        feedback = body.get("feedback") if isinstance(body.get("feedback"), list) else []
        newest = max((_int(_dict(r).get("created")) or 0 for r in feedback), default=0)
        return SourcePage(
            items=(),
            cursor={"after": newest or int(time.time())},
            total=_int(body.get("total_count")),
        )

    # --- mapping ---------------------------------------------------------
    def _to_track(self, record: dict, loved_at: int | None) -> LovedTrack | None:
        meta = _dict(record.get("track_metadata"))
        artist = _clean(meta.get("artist_name"))
        title = _clean(meta.get("track_name"))
        if not artist or not title:
            return None

        # The love is keyed by what it points at. Feedback always names either a
        # MusicBrainz recording or the MessyBrainz id that stands in for one, and
        # without either there is no key that stays the same across polls, which
        # would make the insert re-add the track every time.
        item_id = _clean(record.get("recording_mbid")) or _clean(record.get("recording_msid"))
        if not item_id:
            return None

        mapping = _dict(meta.get("mbid_mapping"))
        extra = _dict(meta.get("additional_info"))
        recording_mbid = _clean(record.get("recording_mbid")) or _clean(mapping.get("recording_mbid"))
        artist_mbids = tuple(
            m for m in (_clean(x) for x in mapping.get("artist_mbids") or ()) if m
        )
        return LovedTrack(
            source_id=self.info.id,
            source_item_id=item_id,
            loved_at=loved_at,
            artist=artist,
            title=title,
            album=_clean(meta.get("release_name")),
            ids=TrackIds(
                recording_mbid=recording_mbid,
                release_mbid=_clean(mapping.get("release_mbid")),
                artist_mbids=artist_mbids,
                isrc=_clean(extra.get("isrc")),
                native_id=item_id,
                native_url=f"https://listenbrainz.org/track/{recording_mbid}" if recording_mbid else None,
            ),
            duration_s=self._duration(extra),
            cover_url=self._cover(mapping),
            raw=clip_raw(record),
        )

    @staticmethod
    def _duration(extra: dict) -> int | None:
        seconds = _int(extra.get("duration"))
        if seconds:
            return seconds
        millis = _int(extra.get("duration_ms"))
        return round(millis / 1000) if millis else None

    @staticmethod
    def _cover(mapping: dict) -> str | None:
        caa_id = _int(mapping.get("caa_id"))
        release = _clean(mapping.get("caa_release_mbid"))
        if not caa_id or not release:
            return None
        return CAA_URL.format(release=release, caa_id=caa_id)

    # --- transport -------------------------------------------------------
    def _call(self, *, count: int, offset: int, metadata: bool) -> dict:
        user = self._required("username")
        headers = {}
        token = self.conf("token")
        if token:
            headers["Authorization"] = f"Token {token}"
        resp = self.http.get(
            f"{API_BASE}/1/feedback/user/{quote(user, safe='')}/get-feedback",
            params={
                "score": 1,
                "metadata": "true" if metadata else "false",
                "count": count,
                "offset": offset,
            },
            headers=headers,
            timeout=TIMEOUT_S,
        )
        body = resp.json()
        if not isinstance(body, dict):
            raise PermanentError("expected a JSON object from ListenBrainz",
                                 code="schema", provider_id=self.info.id)
        return body
