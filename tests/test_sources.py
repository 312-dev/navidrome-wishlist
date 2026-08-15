"""Source provider tests.

Every payload here is a trimmed capture of the real response shape, kept inline
so the suite is one file with no fixture directory to keep in sync. Nothing in
the default run touches the network: the providers are handed a fake that
answers from a queue and records what was asked for, which is the whole reason
`ProviderContext.http` exists.

The two tests that matter most are `test_same_second_love_is_returned` and
`test_storage_failure_leaves_the_cursor_unmoved`. Each one covers a bug that
lost loves in the shipped poller, and each is written so that it fails if the
behaviour is reverted rather than only if an assertion is edited.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from libwish import db as dbmod
from libwish import sources
from libwish.errors import AuthExpired, ConfigError, PermanentError, RateLimited, TransientError
from libwish.http import Response
from libwish.log import get
from libwish.models import LovedTrack, ProviderContext
from libwish.sources import lastfm, listenbrainz
from libwish.sources.lastfm import LastfmSource
from libwish.sources.listenbrainz import ListenBrainzSource

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Recorded payloads
# ---------------------------------------------------------------------------

SIGUR_ARTIST_MBID = "f6f2326f-6b25-4170-b89d-e235b25508e8"
SIGUR_RECORDING_MBID = "b3f0f4b5-1b0a-4a1c-9a1b-0d5a2a6a1f11"
SIGUR_RELEASE_MBID = "1cd77e2a-1c67-4b0d-9a2f-8f5b8e2c3d44"


def lastfm_track(name, uts, *, artist="Sigur Rós", mbid="", url=None,
                 artist_mbid=SIGUR_ARTIST_MBID):
    return {
        "artist": {
            "url": "https://www.last.fm/music/Sigur+R%C3%B3s",
            "name": artist,
            "mbid": artist_mbid,
        },
        "date": {"uts": str(uts), "#text": "05 Aug 2026, 19:11"},
        "mbid": mbid,
        "url": url if url is not None else f"https://www.last.fm/music/x/_/{uts}",
        "name": name,
        "image": [{"size": "small", "#text": "https://lastfm.freetls.fastly.net/i/u/34s/x.png"}],
        "streamable": {"fulltrack": "0", "#text": "0"},
    }


def lastfm_body(tracks, *, page=1, total_pages=1, total=None):
    return {
        "lovedtracks": {
            "track": tracks,
            "@attr": {
                "user": "a-listener",
                "page": str(page),
                "perPage": "50",
                "totalPages": str(total_pages),
                "total": str(total if total is not None else len(tracks)),
            },
        }
    }


#: HTTP 200 carrying a failure. This is the shape that makes status-only error
#: handling wrong for Last.fm.
LASTFM_SUSPENDED_KEY = {"error": 26, "message": "Suspended API key - Access for your account has been suspended, please contact Last.fm"}
LASTFM_RATE_LIMITED = {"error": 29, "message": "Rate limit exceeded - Your IP has made too many requests in a short period"}
LASTFM_SERVICE_OFFLINE = {"error": 11, "message": "Service Offline - This service is temporarily offline. Try again later."}


def lb_feedback(title, created, *, artist="Sigur Rós", recording_mbid=SIGUR_RECORDING_MBID,
                msid=None, release="( )", with_mapping=True):
    metadata = {
        "artist_name": artist,
        "track_name": title,
        "release_name": release,
        "additional_info": {"duration_ms": 425000, "isrc": "GBAYE0601498"},
    }
    if with_mapping:
        metadata["mbid_mapping"] = {
            "recording_mbid": recording_mbid,
            "release_mbid": SIGUR_RELEASE_MBID,
            "artist_mbids": [SIGUR_ARTIST_MBID],
            "caa_id": 34019214531,
            "caa_release_mbid": SIGUR_RELEASE_MBID,
        }
    return {
        "created": created,
        "recording_mbid": recording_mbid,
        "recording_msid": msid,
        "score": 1,
        "user_id": "a-listener",
        "track_metadata": metadata,
    }


def lb_body(feedback, *, offset=0, total=None):
    return {
        "count": len(feedback),
        "offset": offset,
        "total_count": total if total is not None else len(feedback),
        "feedback": feedback,
    }


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeHttp:
    """Answers from a queue and remembers every request.

    Returns the real `Response` type so that JSON decoding, and its failure
    mode, are the ones the providers will meet in production.
    """

    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls = []

    def get(self, url, *, params=None, headers=None, timeout=None, **kw):
        self.calls.append({
            "url": url,
            "params": dict(params or {}),
            "headers": dict(headers or {}),
            "timeout": timeout,
        })
        if not self.queued:
            raise AssertionError(f"unexpected request to {url}")
        item = self.queued.pop(0)
        if isinstance(item, BaseException):
            raise item
        body = item if isinstance(item, (bytes, str)) else json.dumps(item)
        if isinstance(body, str):
            body = body.encode()
        return Response(200, url, body, {})


def make_ctx(provider_id, http, **conf):
    """A `ProviderContext` with only the fields a source is allowed to use.

    The rest are None on purpose: a source that reaches for the database, the
    credential store or the paths service fails here rather than in production.
    """
    return ProviderContext(
        provider_id=provider_id,
        settings=None,
        log=get("test", provider=provider_id),
        conf=lambda key: conf.get(key),
        creds=None,
        http=http,
        state=None,
        db=None,
        paths=None,
    )


def lastfm_with(*responses, **conf):
    http = FakeHttp(*responses)
    conf.setdefault("api_key", "recorded-key-not-a-real-one")
    conf.setdefault("username", "a-listener")
    return LastfmSource(make_ctx("lastfm", http, **conf)), http


def lb_with(*responses, **conf):
    http = FakeHttp(*responses)
    conf.setdefault("username", "a-listener")
    return ListenBrainzSource(make_ctx("listenbrainz", http, **conf)), http


class StubSource:
    """A provider that fails in a way providers are not permitted to fail."""

    info = LastfmSource.info

    def __init__(self, error):
        self.error = error

    def poll(self, cursor, *, mode="incremental", max_items=500):
        raise self.error


# ---------------------------------------------------------------------------
# The inclusive boundary
# ---------------------------------------------------------------------------


class InclusiveBoundaryTest(unittest.TestCase):
    """A love landing in the same second as the cursor must still arrive.

    Both APIs timestamp to the second, and Last.fm's own bulk imports give a
    whole batch of loves one identical second. An exclusive boundary drops
    everything in that second after the first one, permanently.
    """

    def test_same_second_love_is_returned(self):
        cursor = {"after": 1786157895}
        page_body = lastfm_body([
            lastfm_track("Glósóli", 1786157896, url="u/newer"),
            lastfm_track("Hoppípolla", 1786157895, url="u/on-the-boundary"),
            lastfm_track("Sæglópur", 1786157894, url="u/older"),
        ])
        source, http = lastfm_with(page_body)

        page = source.poll(cursor, mode="incremental")

        titles = [item.title for item in page.items]
        self.assertEqual(titles, ["Hoppípolla", "Glósóli"])
        self.assertEqual(len(http.calls), 1)

    def test_listenbrainz_same_second_love_is_returned(self):
        cursor = {"after": 1785200523}
        source, _ = lb_with(lb_body([
            lb_feedback("Untitled #4", 1785200524, recording_mbid="a" * 36),
            lb_feedback("Untitled #8", 1785200523, recording_mbid="b" * 36),
            lb_feedback("Svefn-g-englar", 1785200522, recording_mbid="c" * 36),
        ]))

        page = source.poll(cursor, mode="incremental")

        self.assertEqual([i.title for i in page.items], ["Untitled #8", "Untitled #4"])

    def test_cursor_advances_to_the_newest_item_stored(self):
        source, _ = lastfm_with(lastfm_body([
            lastfm_track("Glósóli", 1786157896, url="u/1"),
            lastfm_track("Hoppípolla", 1786157895, url="u/2"),
        ]))

        page = source.poll({"after": 1786157800}, mode="incremental")

        self.assertEqual(page.cursor, {"after": 1786157896})

    def test_cursor_never_moves_backwards(self):
        """A source reporting an older timestamp cannot rewind the position.

        Rewinding would re-deliver the entire history behind the cursor, which
        is harmless for correctness and ruinous for a first-run experience.
        """
        self.assertEqual(
            sources.next_cursor({"after": 1786157895}, [
                LovedTrack(source_id="lastfm", source_item_id="x", loved_at=17,
                           artist="a", title="b"),
            ]),
            {"after": 1786157895},
        )


# ---------------------------------------------------------------------------
# The cursor moves only after the write
# ---------------------------------------------------------------------------


class CursorAfterWriteTest(unittest.TestCase):
    """The other live data-loss bug: a mark recorded before the rows landed.

    `advance` is the seam. It calls `store` and only then produces a cursor, so
    there is no path that hands back a moved position without the items behind
    it having been written.
    """

    def setUp(self):
        self.body = lastfm_body([
            lastfm_track("Glósóli", 1786157896, url="u/1"),
            lastfm_track("Hoppípolla", 1786157895, url="u/2"),
        ])

    def test_storage_failure_leaves_the_cursor_unmoved(self):
        held = {"after": 1786157800}
        source, _ = lastfm_with(self.body)

        def failing_store(page):
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            held = sources.advance(source, held, store=failing_store).cursor

        self.assertEqual(held, {"after": 1786157800})

    def test_the_next_poll_returns_the_same_items(self):
        held = {"after": 1786157800}
        source, _ = lastfm_with(self.body, self.body)
        first_attempt = []

        def failing_store(page):
            first_attempt.extend(item.source_item_id for item in page.items)
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            held = sources.advance(source, held, store=failing_store).cursor

        stored = []
        held = sources.advance(source, held, store=lambda p: stored.extend(
            item.source_item_id for item in p.items)).cursor

        self.assertEqual(stored, first_attempt)
        self.assertEqual(held, {"after": 1786157896})

    def test_a_failing_store_produces_no_result_to_persist(self):
        """The failure must reach the caller, not be absorbed into a cursor.

        Absorbing it is the shipped bug in a different costume: the poll looks
        successful, the position moves, and the loves it covered were never
        written down.
        """
        source, _ = lastfm_with(self.body)
        result = None

        def failing_store(page):
            raise sqlite3.OperationalError("disk I/O error")

        with self.assertRaises(sqlite3.OperationalError):
            result = sources.advance(source, {"after": 1}, store=failing_store)

        self.assertIsNone(result)

    def test_a_successful_store_yields_the_cursor_it_covered(self):
        source, _ = lastfm_with(self.body)
        seen = []
        result = sources.advance(source, None, store=lambda p: seen.append(p.items))
        self.assertEqual(len(seen[0]), 2)
        self.assertEqual(result.cursor, {"after": 1786157896})


# ---------------------------------------------------------------------------
# Modes and pagination
# ---------------------------------------------------------------------------


class SeedModeTest(unittest.TestCase):
    def test_lastfm_seed_emits_nothing_and_sets_a_cursor(self):
        source, http = lastfm_with(lastfm_body(
            [lastfm_track("Glósóli", 1786157896)], total_pages=61, total=3012))

        page = source.poll(None, mode="seed")

        self.assertEqual(page.items, ())
        self.assertEqual(page.cursor, {"after": 1786157896})
        self.assertEqual(page.total, 3012)
        self.assertEqual(http.calls[0]["params"]["limit"], 1)

    def test_listenbrainz_seed_emits_nothing_and_sets_a_cursor(self):
        source, http = lb_with(lb_body([lb_feedback("Untitled #4", 1785200523)], total=812))

        page = source.poll(None, mode="seed")

        self.assertEqual(page.items, ())
        self.assertEqual(page.cursor, {"after": 1785200523})
        self.assertEqual(page.total, 812)
        self.assertEqual(http.calls[0]["params"]["metadata"], "false")

    def test_seed_on_an_empty_account_still_sets_a_cursor(self):
        source, _ = lastfm_with(lastfm_body([]))
        page = source.poll(None, mode="seed")
        self.assertEqual(page.items, ())
        self.assertGreater(sources.cursor_after(page.cursor), 0)


class PaginationTest(unittest.TestCase):
    def test_lastfm_backfill_assembles_every_page_in_order(self):
        source, http = lastfm_with(
            lastfm_body([lastfm_track("p1a", 300, url="u/300"),
                         lastfm_track("p1b", 290, url="u/290")],
                        page=1, total_pages=3, total=6),
            lastfm_body([lastfm_track("p2a", 200, url="u/200"),
                         lastfm_track("p2b", 190, url="u/190")],
                        page=2, total_pages=3, total=6),
            lastfm_body([lastfm_track("p3a", 100, url="u/100"),
                         lastfm_track("p3b", 90, url="u/90")],
                        page=3, total_pages=3, total=6),
        )

        page = source.poll(None, mode="backfill")

        self.assertEqual([i.loved_at for i in page.items], [90, 100, 190, 200, 290, 300])
        self.assertEqual([c["params"]["page"] for c in http.calls], [1, 2, 3])
        self.assertFalse(page.more)
        self.assertEqual(page.cursor, {"after": 300})

    def test_lastfm_stops_at_the_boundary_without_reading_further_pages(self):
        source, http = lastfm_with(
            lastfm_body([lastfm_track("p1a", 300, url="u/300"),
                         lastfm_track("p1b", 290, url="u/290")],
                        page=1, total_pages=3, total=6),
            lastfm_body([lastfm_track("p2a", 200, url="u/200"),
                         lastfm_track("p2b", 190, url="u/190")],
                        page=2, total_pages=3, total=6),
        )

        page = source.poll({"after": 200}, mode="incremental")

        self.assertEqual([i.loved_at for i in page.items], [200, 290, 300])
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(len(http.queued), 0)

    def test_listenbrainz_walks_offsets_and_stops_on_the_total(self):
        full = listenbrainz.PAGE_BACKFILL
        total = full + 5
        feedback = [lb_feedback(f"t{i}", 500 - i, recording_mbid=f"{i:036d}")
                    for i in range(total)]
        source, http = lb_with(
            lb_body(feedback[:full], offset=0, total=total),
            lb_body(feedback[full:], offset=full, total=total),
        )

        page = source.poll(None, mode="backfill")

        self.assertEqual(len(page.items), total)
        self.assertEqual([c["params"]["offset"] for c in http.calls], [0, full])
        self.assertEqual([i.loved_at for i in page.items[:3]],
                         [500 - total + 1, 500 - total + 2, 500 - total + 3])

    def test_listenbrainz_stops_at_the_boundary(self):
        source, http = lb_with(lb_body([
            lb_feedback("newer", 900, recording_mbid="a" * 36),
            lb_feedback("boundary", 800, recording_mbid="b" * 36),
            lb_feedback("older", 700, recording_mbid="c" * 36),
        ], total=99))

        page = source.poll({"after": 800}, mode="incremental")

        self.assertEqual([i.title for i in page.items], ["boundary", "newer"])
        self.assertEqual(len(http.calls), 1)

    def test_a_walk_that_cannot_finish_fails_instead_of_answering_short(self):
        """A gap at the old end has no cursor that can describe it.

        The walk runs newest first, so stopping partway leaves the oldest loves
        unread while the cursor claims everything from that point on has been
        delivered. Every love below the gap would be stranded, which is worse
        than the poll failing and being retried.
        """
        self.addCleanup(setattr, lastfm, "PAGE_LIMIT", lastfm.PAGE_LIMIT)
        lastfm.PAGE_LIMIT = 2
        pages = [lastfm_body([lastfm_track(f"p{n}", 900 - n, url=f"u/{n}")],
                             page=n, total_pages=9, total=9) for n in range(1, 4)]
        source, _ = lastfm_with(*pages)

        with self.assertRaises(PermanentError) as caught:
            source.poll(None, mode="backfill")

        self.assertEqual(caught.exception.code, "too_many_pages")

    def test_listenbrainz_walk_that_cannot_finish_fails(self):
        self.addCleanup(setattr, listenbrainz, "PAGE_LIMIT", listenbrainz.PAGE_LIMIT)
        listenbrainz.PAGE_LIMIT = 1
        full = listenbrainz.PAGE_BACKFILL
        feedback = [lb_feedback(f"t{i}", 900 - i, recording_mbid=f"{i:036d}")
                    for i in range(full * 2)]
        source, _ = lb_with(
            lb_body(feedback[:full], offset=0, total=full * 2),
            lb_body(feedback[full:], offset=full, total=full * 2),
        )

        with self.assertRaises(PermanentError) as caught:
            source.poll(None, mode="backfill")

        self.assertEqual(caught.exception.code, "too_many_pages")

    def test_an_oversized_window_keeps_the_oldest_and_asks_to_be_called_again(self):
        """Truncating from the newest end is what keeps the cursor honest.

        Keeping the newest items instead would leave a cursor past the older
        ones that were dropped, and nothing would ever ask for them again.
        """
        source, _ = lastfm_with(lastfm_body(
            [lastfm_track(f"t{i}", 1000 + i, url=f"u/{i}") for i in range(10)]))

        page = source.poll(None, mode="incremental", max_items=4)

        self.assertEqual([i.loved_at for i in page.items], [1000, 1001, 1002, 1003])
        self.assertTrue(page.more)
        self.assertEqual(page.cursor, {"after": 1003})


# ---------------------------------------------------------------------------
# The error contract
# ---------------------------------------------------------------------------


class ErrorContractTest(unittest.TestCase):
    def test_an_unexpected_exception_does_not_escape_as_itself(self):
        source = StubSource(KeyError("track_metadata"))

        with self.assertRaises(TransientError) as caught:
            sources.safe_poll(source, None, mode="incremental")

        self.assertEqual(caught.exception.code, "unexpected")
        self.assertIsInstance(caught.exception.__cause__, KeyError)

    def test_a_provider_error_travels_unchanged(self):
        original = RateLimited("slow down", retry_after=30, code="rate_limited")
        with self.assertRaises(RateLimited) as caught:
            sources.safe_poll(StubSource(original), None)
        self.assertIs(caught.exception, original)

    def test_lastfm_reads_auth_failure_from_the_body_not_the_status(self):
        source, _ = lastfm_with(LASTFM_SUSPENDED_KEY)
        with self.assertRaises(AuthExpired) as caught:
            source.poll(None, mode="incremental")
        self.assertEqual(caught.exception.code, "auth_expired")

    def test_lastfm_rate_limit_is_not_an_auth_failure(self):
        source, _ = lastfm_with(LASTFM_RATE_LIMITED)
        with self.assertRaises(RateLimited):
            source.poll(None, mode="incremental")

    def test_lastfm_service_offline_is_retryable(self):
        source, _ = lastfm_with(LASTFM_SERVICE_OFFLINE)
        with self.assertRaises(TransientError):
            source.poll(None, mode="incremental")

    def test_a_body_that_is_not_json_is_permanent(self):
        source, _ = lastfm_with(b"<html>502 Bad Gateway</html>")
        with self.assertRaises(PermanentError) as caught:
            source.poll(None, mode="incremental")
        self.assertEqual(caught.exception.code, "bad_json")

    def test_a_missing_block_is_a_schema_error(self):
        source, _ = lb_with({"count": 0, "offset": 0, "total_count": 0})
        with self.assertRaises(PermanentError) as caught:
            source.poll(None, mode="incremental")
        self.assertEqual(caught.exception.code, "schema")

    def test_missing_configuration_names_the_variable(self):
        source, _ = lastfm_with(lastfm_body([]), api_key=None)
        with self.assertRaises(ConfigError) as caught:
            source.poll(None, mode="incremental")
        self.assertIn("LW_SOURCE_LASTFM_API_KEY", str(caught.exception))

    def test_a_provider_does_not_read_the_environment(self):
        """`ctx.conf` is the only way in, so a set env var must not be found."""
        os.environ["LW_SOURCE_LASTFM_API_KEY"] = "should-not-be-visible"
        self.addCleanup(os.environ.pop, "LW_SOURCE_LASTFM_API_KEY", None)
        source, _ = lastfm_with(lastfm_body([]), api_key=None)
        with self.assertRaises(ConfigError):
            source.poll(None, mode="incremental")

    def test_check_config_reports_what_is_missing(self):
        source, _ = lastfm_with(api_key=None)
        status = source.check_config()
        self.assertFalse(status.ok)
        self.assertEqual(status.missing, ("api_key",))


# ---------------------------------------------------------------------------
# Raw means raw
# ---------------------------------------------------------------------------


class NoNormalizationTest(unittest.TestCase):
    """A provider that cleans a string destroys the evidence the matcher needs.

    The identity layer scores against the bytes the source gave, and the store
    search is handed the same bytes. Any tidying here would make those two
    disagree with each other and with the source.
    """

    def test_lastfm_strings_arrive_byte_identical(self):
        source, _ = lastfm_with(lastfm_body([
            lastfm_track("Falling Down (Bonus Track)", 1786157896, artist="Sigur Rós"),
        ]))

        item = source.poll(None, mode="incremental").items[0]

        self.assertEqual(item.artist, "Sigur Rós")
        self.assertEqual(item.title, "Falling Down (Bonus Track)")
        self.assertEqual(item.artist.encode(), b"Sigur R\xc3\xb3s")

    def test_listenbrainz_strings_arrive_byte_identical(self):
        source, _ = lb_with(lb_body([
            lb_feedback("Falling Down (Bonus Track)", 1785200523, artist="Sigur Rós"),
        ]))

        item = source.poll(None, mode="incremental").items[0]

        self.assertEqual(item.artist, "Sigur Rós")
        self.assertEqual(item.title, "Falling Down (Bonus Track)")

    def test_identifiers_are_never_invented(self):
        source, _ = lastfm_with(lastfm_body([
            lastfm_track("Glósóli", 1786157896, mbid="", artist_mbid=""),
        ]))

        item = source.poll(None, mode="incremental").items[0]

        self.assertIsNone(item.ids.recording_mbid)
        self.assertEqual(item.ids.artist_mbids, ())

    def test_a_record_without_an_artist_is_skipped_not_emptied(self):
        body = lastfm_body([
            lastfm_track("Glósóli", 1786157896, url="u/1"),
            {"name": "orphan", "date": {"uts": "1786157897"}, "url": "u/2"},
        ])
        source, _ = lastfm_with(body)

        page = source.poll(None, mode="incremental")

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.skipped, 1)

    def test_listenbrainz_without_a_stable_id_is_skipped(self):
        source, _ = lb_with(lb_body([
            lb_feedback("Untitled #4", 1785200523, recording_mbid=None, msid=None),
        ]))

        page = source.poll(None, mode="incremental")

        self.assertEqual(page.items, ())
        self.assertEqual(page.skipped, 1)

    def test_listenbrainz_carries_the_identifiers_it_was_given(self):
        source, _ = lb_with(lb_body([lb_feedback("Untitled #4", 1785200523)]))

        item = source.poll(None, mode="incremental").items[0]

        self.assertEqual(item.ids.recording_mbid, SIGUR_RECORDING_MBID)
        self.assertEqual(item.ids.release_mbid, SIGUR_RELEASE_MBID)
        self.assertEqual(item.ids.artist_mbids, (SIGUR_ARTIST_MBID,))
        self.assertEqual(item.ids.isrc, "GBAYE0601498")
        self.assertEqual(item.duration_s, 425)
        self.assertIn("34019214531", item.cover_url)

    def test_a_last_fm_love_with_no_url_still_gets_a_stable_key(self):
        body = lastfm_body([lastfm_track("Glósóli", 1786157896, url="")])
        first = lastfm_with(body)[0].poll(None, mode="incremental").items[0]
        second = lastfm_with(body)[0].poll(None, mode="incremental").items[0]
        self.assertEqual(first.source_item_id, second.source_item_id)
        self.assertTrue(first.source_item_id.startswith("sha1:"))

    def test_raw_is_bounded(self):
        bulky = {"pad": "x" * (sources.RAW_LIMIT * 2)}
        clipped = sources.clip_raw(bulky)
        self.assertEqual(set(clipped), {"truncated"})
        self.assertEqual(len(clipped["truncated"]), sources.RAW_LIMIT)


# ---------------------------------------------------------------------------
# Registry and cursor plumbing
# ---------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):
    def test_both_sources_are_registered(self):
        self.assertEqual(sources.ids(), ("lastfm", "listenbrainz"))

    def test_an_unknown_source_is_named_in_the_error(self):
        with self.assertRaises(ConfigError) as caught:
            sources.get_class("spotify")
        self.assertIn("lastfm", str(caught.exception))

    def test_a_duplicate_id_is_rejected_at_registration(self):
        class Impostor:
            info = LastfmSource.info

        with self.assertRaises(ConfigError):
            sources.register(Impostor)

    def test_an_id_that_could_not_be_stored_is_rejected(self):
        from dataclasses import replace

        class Bad:
            info = replace(LastfmSource.info, id="import:deezer-unobtainable")

        with self.assertRaises(ConfigError):
            sources.register(Bad)

    def test_discover_builds_only_configured_sources(self):
        built = sources.discover(
            lambda source_id: make_ctx(source_id, FakeHttp()),
            configured=["lastfm", "not_a_source"],
        )
        self.assertEqual(list(built), ["lastfm"])

    def test_every_source_declares_two_poll_tiers(self):
        for info in sources.infos():
            self.assertIsNotNone(info.poll)
            self.assertEqual((info.poll.hot, info.poll.cold), (30, 600))

    def test_a_cursor_survives_a_round_trip_through_json(self):
        source, _ = lastfm_with(lastfm_body([lastfm_track("Glósóli", 1786157896)]))
        cursor = source.poll(None, mode="incremental").cursor
        self.assertEqual(json.loads(json.dumps(cursor)), cursor)
        self.assertEqual(sources.cursor_after(json.loads(json.dumps(cursor))), 1786157896)

    def test_an_unreadable_cursor_starts_from_the_beginning(self):
        for junk in (None, {}, {"after": None}, {"after": "yesterday"}, {"after": -1}):
            self.assertIsNone(sources.cursor_after(junk))


# ---------------------------------------------------------------------------
# Migration 0004
# ---------------------------------------------------------------------------


class MigrationTest(unittest.TestCase):
    """0004 applied over the live table shape, with the live provenance values.

    The files are executed directly rather than through `db.migrate` because the
    legacy rows have to exist between the baseline and 0004: the provenance
    import is the part being tested, and it has nothing to read otherwise.
    """

    LEGACY_ROWS = [
        (1, "lastfm", 1786157895),
        (2, "listenbrainz", 1785200523),
        (3, "deezer-unobtainable", 1780000000),
        (4, "deezer-unobtainable", None),
        (5, None, 1780000001),
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "library-wishlist.db"
        migrations = sorted((REPO_ROOT / "libwish" / "migrations").glob("*.sql"))
        self.conn = dbmod.connect(self.path)
        self.addCleanup(self.conn.close)
        baseline, rest = migrations[0], migrations[1:]
        self.conn.executescript(baseline.read_text())
        self.conn.executemany(
            "INSERT INTO tracks(id, artist, title, source_platform, added_at) "
            "VALUES(?,'artist','title',?,?)",
            [(track_id, platform, added) for track_id, platform, added in self.LEGACY_ROWS],
        )
        for path in rest:
            self.conn.executescript(path.read_text())

    def rows(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT source_id, source_item_id, track_id, loved_at, first_seen_at "
            "FROM track_sources ORDER BY track_id")]

    def test_the_tables_exist(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("track_sources", names)
        self.assertIn("source_state", names)

    def test_real_sources_keep_their_own_id(self):
        by_track = {r[2]: r[0] for r in self.rows()}
        self.assertEqual(by_track[1], "lastfm")
        self.assertEqual(by_track[2], "listenbrainz")

    def test_the_deezer_tag_is_provenance_not_a_source(self):
        """`deezer-unobtainable` records a failed lookup, not a place loved.

        It keeps its own name behind `import:`, which no provider id can match
        because a provider id cannot contain a colon.
        """
        by_track = {r[2]: r[0] for r in self.rows()}
        self.assertEqual(by_track[3], "import:deezer-unobtainable")
        self.assertNotIn("deezer", sources.ids())
        for source_id, *_ in self.rows():
            if source_id.startswith("import:"):
                self.assertNotIn(source_id, sources.REGISTRY)

    def test_a_row_with_no_platform_is_left_alone(self):
        self.assertNotIn(5, {r[2] for r in self.rows()})

    def test_a_missing_added_at_does_not_break_the_import(self):
        first_seen = {r[2]: r[4] for r in self.rows()}
        loved = {r[2]: r[3] for r in self.rows()}
        self.assertEqual(first_seen[4], 0)
        self.assertIsNone(loved[4])

    def test_redelivering_a_love_is_an_ignored_insert(self):
        self.conn.execute(
            "INSERT INTO track_sources(source_id, source_item_id, track_id, "
            "loved_at, first_seen_at, last_seen_at) VALUES('lastfm','legacy:1',1,1,1,1) "
            "ON CONFLICT(source_id, source_item_id) DO UPDATE SET last_seen_at=99")
        row = self.conn.execute(
            "SELECT last_seen_at FROM track_sources WHERE source_item_id='legacy:1'").fetchone()
        self.assertEqual(row[0], 99)
        self.assertEqual(len(self.rows()), 4)


# ---------------------------------------------------------------------------
# Live smoke test
# ---------------------------------------------------------------------------


@unittest.skipUnless(os.environ.get("LW_LIVE_SOURCE_SMOKE"),
                     "set LW_LIVE_SOURCE_SMOKE=1 and the LW_SOURCE_* variables to run")
class LiveSmokeTest(unittest.TestCase):
    """The only test here that talks to the real APIs.

    Credentials come from the environment, the same variables the application
    reads, so nothing is written down. It seeds rather than polls, which is one
    request per source and adds nothing to anybody's queue.
    """

    def build(self, kind_id, cls):
        from libwish.http import HttpClient
        from libwish.settings import provider_conf

        conf = provider_conf("source", kind_id)
        if not conf("username"):
            self.skipTest(f"LW_SOURCE_{kind_id.upper()}_USERNAME is not set")
        ctx = ProviderContext(
            provider_id=kind_id, settings=None, log=get("smoke", provider=kind_id),
            conf=conf, creds=None,
            http=HttpClient(user_agent="library-wishlist/1.0 (smoke test)",
                            provider_id=kind_id),
            state=None, db=None, paths=None,
        )
        return cls(ctx)

    def test_lastfm_seed(self):
        page = self.build("lastfm", LastfmSource).poll(None, mode="seed")
        self.assertEqual(page.items, ())
        self.assertGreater(sources.cursor_after(page.cursor), 0)

    def test_listenbrainz_seed(self):
        page = self.build("listenbrainz", ListenBrainzSource).poll(None, mode="seed")
        self.assertEqual(page.items, ())
        self.assertGreater(sources.cursor_after(page.cursor), 0)


class TheExampleEnvNamesRealSettings(unittest.TestCase):
    """Every LW_SOURCE_* in .env.example is a key some provider actually reads.

    A wrong name here is worse than an absent one, and silently so. One
    variable under a provider's prefix is enough for `configured_provider_ids`
    to build that provider, so a misspelled key produces a source that starts,
    polls, and raises for the name it wanted instead, every time, forever. The
    file shipped `LW_SOURCE_LASTFM_USER` against a spec that reads `username`,
    which is exactly that.

    Asserted against the specs rather than a hand-kept list, so adding a
    setting to a provider cannot leave the example describing the old one.
    """

    def documented(self):
        from pathlib import Path
        import re

        text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        return re.findall(r"^#?(LW_SOURCE_([A-Z0-9]+)_([A-Z0-9_]+))=", text, re.M)

    def test_the_example_documents_at_least_one_source(self):
        # Without this the assertions below pass on an empty list, which is the
        # shape this whole class would have if the regex ever stopped matching.
        self.assertTrue(self.documented())

    def test_every_documented_name_is_a_key_its_provider_reads(self):
        from libwish import sources

        for full, provider, key in self.documented():
            with self.subTest(variable=full):
                cls = sources.REGISTRY.get(provider.lower())
                self.assertIsNotNone(cls, f"{full} names no registered source")
                keys = {spec.key.upper() for spec in cls.info.config}
                self.assertIn(key, keys,
                              f"{full} is not one of {sorted(keys)}")

    def test_every_required_setting_is_documented(self):
        # The other direction: a required setting missing from the example is
        # how someone configures a source that then cannot start.
        from libwish import sources

        documented = {full for full, _, _ in self.documented()}
        for source_id in sources.ids():
            info = sources.REGISTRY[source_id].info
            for spec in info.config:
                if not spec.required:
                    continue
                name = f"LW_SOURCE_{source_id.upper()}_{spec.key.upper()}"
                with self.subTest(variable=name):
                    self.assertIn(name, documented)


if __name__ == "__main__":
    unittest.main()
