"""Enrichment and the local cover cache.

The Deezer payloads below are real, captured from the public API on 2026-08-09
rather than invented, because the case that matters is a ranking quirk nobody
would think to make up: a search for CHVRCHES "Lies" answers with a different
song first.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from libwish import db as dbmod, enrich, media
from libwish.errors import RateLimited, VerificationFailed
from libwish.http import Response
from libwish.repo import TrackRepo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: api.deezer.com/search/track?q=artist:"CHVRCHES" track:"Lies", verbatim except
#: for fields nothing here reads. The first entry is a soundtrack single with
#: "Lies" in the tie-in, the second is the song that was asked for, and the
#: fourth is a live recording of it.
CHVRCHES_LIES = [
    {
        "id": 3834113401,
        "title": 'Such Great Heights (From "Tell Me Lies Season 3 ")',
        "duration": 268,
        "artist": {"id": 613, "name": "CHVRCHES"},
        "album": {"id": 1, "title": "Such Great Heights",
                  "cover_medium": "https://cdn-images.dzcdn.net/images/cover/d4c0/250x250.jpg",
                  "cover_big": "https://cdn-images.dzcdn.net/images/cover/d4c0/500x500.jpg"},
    },
    {
        "id": 3025963851,
        "title": "Lies",
        "duration": 221,
        "artist": {"id": 613, "name": "CHVRCHES"},
        "album": {"id": 2, "title": "The Bones of What You Believe",
                  "cover_medium": "https://cdn-images.dzcdn.net/images/cover/e903/250x250.jpg",
                  "cover_big": "https://cdn-images.dzcdn.net/images/cover/e903/500x500.jpg"},
    },
    {
        "id": 3763867962,
        "title": 'Addicted to Love (From "Tell Me Lies Season 3 ")',
        "duration": 185,
        "artist": {"id": 613, "name": "CHVRCHES"},
        "album": {"id": 3, "title": "Addicted to Love",
                  "cover_big": "https://cdn-images.dzcdn.net/images/cover/73b6/500x500.jpg"},
    },
    {
        "id": 3025967931,
        "title": "Lies (Live at Ancienne Belgique / 2013)",
        "duration": 242,
        "artist": {"id": 613, "name": "CHVRCHES"},
        "album": {"id": 4, "title": "Recover EP",
                  "cover_big": "https://cdn-images.dzcdn.net/images/cover/a751/500x500.jpg"},
    },
]

#: The same query for The Black Crowes, trimmed to the entries that survive the
#: artist and version gates. Five exact title matches on the right artist, and
#: the recordings run 45 seconds apart.
BLACK_CROWES = [
    {"id": 65707240, "title": "She Talks To Angels", "duration": 330,
     "artist": {"name": "The Black Crowes"},
     "album": {"title": "Greatest Hits 1990-1999",
               "cover_big": "https://cdn-images.dzcdn.net/images/cover/c656/500x500.jpg"}},
    {"id": 143538840, "title": "She Talks to Angels", "duration": 370,
     "artist": {"name": "The Black Crowes"},
     "album": {"title": "Freak 'N' Roll...Into the Fog",
               "cover_big": "https://cdn-images.dzcdn.net/images/cover/cf3f/500x500.jpg"}},
    {"id": 1776868437, "title": "She Talks To Angels", "duration": 330,
     "artist": {"name": "The Black Crowes"},
     "album": {"title": "She Talks To Angels",
               "cover_big": "https://cdn-images.dzcdn.net/images/cover/774b/500x500.jpg"}},
    {"id": 16284256, "title": "She Talks To Angels", "duration": 362,
     "artist": {"name": "The Black Crowes"},
     "album": {"title": "Live",
               "cover_big": "https://cdn-images.dzcdn.net/images/cover/e702/500x500.jpg"}},
    {"id": 64876749, "title": "She Talks To Angels", "duration": 375,
     "artist": {"name": "The Black Crowes"},
     "album": {"title": "Wiser for the Time",
               "cover_big": "https://cdn-images.dzcdn.net/images/cover/fb2f/500x500.jpg"}},
]

#: Two releases of one recording, four seconds apart, plus a live cut. Nothing
#: here is ambiguous about how long the song is.
CAGE_THE_ELEPHANT = [
    {"id": 71127716, "title": "Come a Little Closer", "duration": 229,
     "artist": {"name": "Cage The Elephant"},
     "album": {"title": "Melophobia",
               "cover_big": "https://cdn-images.dzcdn.net/images/cover/e6c0/500x500.jpg"}},
    {"id": 384171411, "title": "Come a Little Closer (Unpeeled)", "duration": 225,
     "artist": {"name": "Cage The Elephant"},
     "album": {"title": "Unpeeled",
               "cover_big": "https://cdn-images.dzcdn.net/images/cover/41aa/500x500.jpg"}},
    {"id": 87132591, "title": "Come a Little Closer (Live From Guitar Center)",
     "duration": 239, "artist": {"name": "Cage The Elephant"},
     "album": {"title": "Deep Hands: Live Session",
               "cover_big": "https://cdn-images.dzcdn.net/images/cover/373c/500x500.jpg"}},
]

#: A one pixel JPEG, padded past `media.MIN_BYTES` so the size floor is not what
#: any test is really asserting on.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 400 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400

#: What a rate-limited CDN actually serves: HTTP 200, an image content type, and
#: an HTML page.
HTML_ERROR = (b"<!DOCTYPE html>\n<html><head><title>429 Too Many Requests</title></head>"
              b"<body><h1>Too Many Requests</h1><p>Slow down.</p></body></html>"
              + b"<!-- padding -->" * 30)


class FakeHttp:
    """Answers a fixed script and counts calls."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bytes):
            return Response(200, url, result, {"Content-Type": "image/jpeg"})
        import json as jsonlib
        return Response(200, url, jsonlib.dumps(result).encode())


class NeverCalled:
    def get(self, url, **kw):
        raise AssertionError(f"the network was used when it should not have been: {url}")


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def publish(self, name, **data):
        self.events.append((name, data))


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple] = []

    def enqueue(self, kind, *, track_id=None, provider_id=None, dedupe=True):
        self.enqueued.append((kind, track_id))
        return len(self.enqueued)


class Base(unittest.TestCase):
    """A migrated database, a cover cache and a service bundle, per test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "libwish.db"
        dbmod.migrate(self.db_path, backup=False)

        def db_factory():
            return dbmod.connect(self.db_path)

        self.db = db_factory
        self.covers = media.CoverCache(self.root, http=NeverCalled())
        self.bus = FakeBus()
        self.jobs = FakeJobs()
        self.svc = SimpleNamespace(
            settings=SimpleNamespace(config_dir=self.root,
                                     http_user_agent="test/1.0",
                                     http_timeout_seconds=5),
            db=db_factory,
            tracks=TrackRepo(db_factory),
            bus=self.bus,
            jobs=self.jobs,
        )

    def add_track(self, artist: str, title: str, **cols) -> int:
        conn = self.db()
        try:
            cur = conn.execute(
                "INSERT INTO tracks(artist, title, added_at, status, duration_ms)"
                " VALUES(?,?,?,?,?)",
                (artist, title, int(time.time()), cols.get("status", "queued"),
                 cols.get("duration_ms")),
            )
            return int(cur.lastrowid)
        finally:
            conn.close()

    def duration_of(self, track_id: int):
        conn = self.db()
        try:
            return conn.execute("SELECT duration_ms FROM tracks WHERE id=?",
                                (track_id,)).fetchone()[0]
        finally:
            conn.close()

    def decisions(self, track_id: int) -> list[sqlite3.Row]:
        conn = self.db()
        try:
            return conn.execute(
                "SELECT * FROM match_decision WHERE track_id=? ORDER BY id", (track_id,)
            ).fetchall()
        finally:
            conn.close()

    def cache_files(self) -> list[str]:
        d = self.root / "covers"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# ---------------------------------------------------------------------------
# The cover cache
# ---------------------------------------------------------------------------


class DetectTests(unittest.TestCase):
    def test_extension_comes_from_the_bytes(self):
        self.assertEqual(media.detect(JPEG), "jpg")
        self.assertEqual(media.detect(PNG), "png")
        webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 400
        self.assertEqual(media.detect(webp), "webp")

    def test_html_is_not_an_image(self):
        with self.assertRaises(VerificationFailed) as caught:
            media.detect(HTML_ERROR)
        self.assertIn("known image signature", str(caught.exception))

    def test_a_truncated_body_is_refused_before_the_magic_check(self):
        with self.assertRaises(VerificationFailed) as caught:
            media.detect(JPEG[:10])
        self.assertIn("too small", str(caught.exception))

    def test_an_oversized_body_is_refused(self):
        with self.assertRaises(VerificationFailed) as caught:
            media.detect(JPEG + b"\x00" * media.MAX_BYTES)
        self.assertIn("cap", str(caught.exception))


class CoverCacheTests(Base):
    def test_an_html_error_page_is_rejected_and_nothing_is_written(self):
        cache = media.CoverCache(self.root, http=FakeHttp(HTML_ERROR))
        with self.assertRaises(VerificationFailed):
            cache.ensure(7, "https://cdn.example/cover.jpg")
        self.assertEqual(self.cache_files(), [])
        self.assertIsNone(cache.path_for(7))
        self.assertFalse(cache.exists(7))

    def test_a_partial_write_is_never_visible_under_the_final_name(self):
        cache = media.CoverCache(self.root, http=FakeHttp(JPEG))
        dest = self.root / "covers" / "7.jpg"
        seen: list[list[str]] = []
        real_replace = os.replace

        def watching_replace(src, dst):
            # At the moment of publication the bytes are complete and sitting
            # under a name no reader matches, and the final name is still absent.
            seen.append(sorted(p.name for p in (self.root / "covers").iterdir()))
            self.assertFalse(dest.exists())
            self.assertEqual(Path(src).read_bytes(), JPEG)
            return real_replace(src, dst)

        media.os.replace = watching_replace
        try:
            cache.ensure(7, "https://cdn.example/cover.jpg")
        finally:
            media.os.replace = real_replace

        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].startswith(".part-7-"))
        self.assertNotIn("7.jpg", seen[0])
        self.assertEqual(self.cache_files(), ["7.jpg"])
        self.assertEqual(dest.read_bytes(), JPEG)

    def test_a_failed_write_leaves_no_partial_behind(self):
        cache = media.CoverCache(self.root, http=FakeHttp(JPEG))
        real_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("disk went away")

        media.os.replace = failing_replace
        try:
            with self.assertRaises(OSError):
                cache.ensure(7, "https://cdn.example/cover.jpg")
        finally:
            media.os.replace = real_replace
        self.assertEqual(self.cache_files(), [])

    def test_a_cached_cover_is_not_fetched_again(self):
        http = FakeHttp(JPEG)
        cache = media.CoverCache(self.root, http=http)
        first = cache.ensure(7, "https://cdn.example/cover.jpg")
        second = cache.ensure(7, "https://cdn.example/cover.jpg")
        self.assertEqual(first, second)
        self.assertEqual(len(http.calls), 1)

    def test_a_new_format_replaces_the_old_file(self):
        cache = media.CoverCache(self.root, http=NeverCalled())
        cache.store(7, JPEG)
        cache.store(7, PNG)
        self.assertEqual(self.cache_files(), ["7.png"])

    def test_a_track_id_that_is_not_a_number_is_refused(self):
        cache = media.CoverCache(self.root, http=NeverCalled())
        with self.assertRaises(ValueError):
            cache.path_for("../../etc/passwd")

    def test_partials_from_a_dead_process_are_swept(self):
        cache = media.CoverCache(self.root, http=NeverCalled())
        (self.root / "covers").mkdir(parents=True)
        (self.root / "covers" / ".part-7-abcd").write_bytes(b"half")
        cache.store(8, JPEG)
        self.assertEqual(cache.sweep_partials(), 1)
        self.assertEqual(self.cache_files(), ["8.jpg"])


# ---------------------------------------------------------------------------
# Matching a Deezer result
# ---------------------------------------------------------------------------


class ChooseTests(unittest.TestCase):
    def test_deezer_ranking_is_not_evidence(self):
        """The first result for CHVRCHES "Lies" is a different song."""
        from libwish import identity
        from libwish import identity
        want = identity.build_identity("CHVRCHES", "Lies")
        decision, index, accepted, detail, cover_ok = enrich.choose(want, CHVRCHES_LIES[:1])
        self.assertFalse(accepted)
        self.assertEqual(decision.gate_failed, "SHORT_TITLE_EXACT_ONLY")

    def test_the_right_result_further_down_the_list_is_the_one_taken(self):
        from libwish import identity
        from libwish import identity
        want = identity.build_identity("CHVRCHES", "Lies")
        decision, index, accepted, detail, cover_ok = enrich.choose(want, CHVRCHES_LIES)
        self.assertTrue(accepted)
        self.assertEqual(CHVRCHES_LIES[index]["id"], 3025963851)
        self.assertEqual(decision.matched_via, "base_exact")

    def test_a_live_recording_does_not_stand_in_for_the_studio_cut(self):
        from libwish import identity
        from libwish import identity
        want = identity.build_identity("CHVRCHES", "Lies")
        decision, index, accepted, detail, cover_ok = enrich.choose(want, [CHVRCHES_LIES[3]])
        self.assertFalse(accepted)
        self.assertEqual(decision.gate_failed, "VERSION_MISMATCH")

    def test_a_different_artist_is_refused(self):
        from libwish import identity
        from libwish import identity
        want = identity.build_identity("CHVRCHES", "Lies")
        other = dict(CHVRCHES_LIES[1], artist={"id": 9, "name": "Marina Kaye"})
        decision, index, accepted, detail, cover_ok = enrich.choose(want, [other])
        self.assertFalse(accepted)
        self.assertEqual(decision.gate_failed, "ARTIST_MISMATCH")

    def test_releases_that_disagree_about_the_runtime_are_refused(self):
        from libwish import identity
        from libwish import identity
        want = identity.build_identity("The Black Crowes", "She Talks to Angels")
        decision, index, accepted, detail, cover_ok = enrich.choose(want, BLACK_CROWES)
        self.assertFalse(accepted)
        self.assertIn("330s to 375s", detail)

    def test_releases_that_agree_about_the_runtime_are_not_ambiguous(self):
        """One recording on two albums is a presentation difference, not a doubt."""
        from libwish import identity
        from libwish import identity
        want = identity.build_identity("Cage The Elephant", "Come a Little Closer")
        decision, index, accepted, detail, cover_ok = enrich.choose(want, CAGE_THE_ELEPHANT)
        self.assertTrue(accepted)
        self.assertEqual(CAGE_THE_ELEPHANT[index]["duration"], 229)

    def test_seconds_become_milliseconds(self):
        ident = enrich.candidate_identity(CHVRCHES_LIES[1])
        self.assertEqual(CHVRCHES_LIES[1]["duration"], 221)
        self.assertEqual(ident.duration_ms, 221_000)

    def test_the_largest_offered_cover_is_used(self):
        self.assertTrue(enrich.cover_url(CHVRCHES_LIES[1]).endswith("500x500.jpg"))
        no_big = {"album": {"cover_medium": "https://cdn.example/250.jpg"}}
        self.assertEqual(enrich.cover_url(no_big), "https://cdn.example/250.jpg")
        self.assertEqual(enrich.cover_url({}), "")


# ---------------------------------------------------------------------------
# Enriching a row
# ---------------------------------------------------------------------------


class EnrichTrackTests(Base):
    def test_a_confirmed_result_fills_the_duration_and_caches_the_cover(self):
        track_id = self.add_track("CHVRCHES", "Lies")
        http = FakeHttp({"data": CHVRCHES_LIES})
        covers = media.CoverCache(self.root, http=FakeHttp(JPEG))
        result = enrich.enrich_track(self.svc, track_id, http=http, covers=covers,
                                     throttle=enrich.Throttle(0.0))
        self.assertEqual(result["outcome"], "enriched")
        self.assertEqual(self.duration_of(track_id), 221_000)
        self.assertEqual(self.cache_files(), [f"{track_id}.jpg"])
        self.assertEqual([n for n, _ in self.bus.events], ["track.updated"])

    def test_the_wrong_track_is_not_written(self):
        """Only the soundtrack single is offered, so nothing is believed."""
        track_id = self.add_track("CHVRCHES", "Lies")
        http = FakeHttp({"data": CHVRCHES_LIES[:1]})
        result = enrich.enrich_track(self.svc, track_id, http=http,
                                     covers=media.CoverCache(self.root, http=NeverCalled()),
                                     throttle=enrich.Throttle(0.0))
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIsNone(self.duration_of(track_id))
        self.assertEqual(self.cache_files(), [])
        rows = self.decisions(track_id)
        self.assertEqual([r["outcome"] for r in rows], ["refused"])
        self.assertEqual(rows[0]["phase"], "enrich")
        self.assertIsNone(rows[0]["candidate_json"])

    def test_an_empty_result_set_is_recorded_rather_than_left_silent(self):
        track_id = self.add_track("Some Band", "A Song Nobody Has")
        http = FakeHttp({"data": []})
        result = enrich.enrich_track(self.svc, track_id, http=http,
                                     covers=media.CoverCache(self.root, http=NeverCalled()),
                                     throttle=enrich.Throttle(0.0))
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(self.decisions(track_id)[0]["gate_failed"], "NO_CANDIDATES")

    def test_an_empty_scoped_query_falls_back_to_a_plain_one(self):
        track_id = self.add_track("CHVRCHES", "Lies")
        http = FakeHttp({"data": []}, {"data": CHVRCHES_LIES})
        covers = media.CoverCache(self.root, http=FakeHttp(JPEG))
        result = enrich.enrich_track(self.svc, track_id, http=http, covers=covers,
                                     throttle=enrich.Throttle(0.0))
        self.assertEqual(result["outcome"], "enriched")
        self.assertEqual(len(http.calls), 2)
        self.assertIn("artist%3A", http.calls[0])
        self.assertNotIn("artist%3A", http.calls[1])

    def test_a_scoped_query_that_answers_is_not_asked_twice(self):
        track_id = self.add_track("CHVRCHES", "Lies")
        http = FakeHttp({"data": CHVRCHES_LIES}, {"data": []})
        covers = media.CoverCache(self.root, http=FakeHttp(JPEG))
        enrich.enrich_track(self.svc, track_id, http=http, covers=covers,
                            throttle=enrich.Throttle(0.0))
        self.assertEqual(len(http.calls), 1)

    def test_an_already_enriched_track_is_not_fetched_again(self):
        track_id = self.add_track("CHVRCHES", "Lies", duration_ms=221_000)
        self.covers.store(track_id, JPEG)
        result = enrich.enrich_track(self.svc, track_id, http=NeverCalled(),
                                     covers=self.covers, throttle=enrich.Throttle(0.0))
        self.assertEqual(result["outcome"], "skipped")
        self.assertEqual(self.decisions(track_id), [])

    def test_a_duration_already_on_the_row_is_not_overwritten(self):
        track_id = self.add_track("CHVRCHES", "Lies", duration_ms=999_000)
        http = FakeHttp({"data": CHVRCHES_LIES})
        covers = media.CoverCache(self.root, http=FakeHttp(JPEG))
        enrich.enrich_track(self.svc, track_id, http=http, covers=covers,
                            throttle=enrich.Throttle(0.0))
        self.assertEqual(self.duration_of(track_id), 999_000)
        self.assertEqual(self.cache_files(), [f"{track_id}.jpg"])

    def test_a_cover_that_is_an_error_page_does_not_discard_the_duration(self):
        track_id = self.add_track("CHVRCHES", "Lies")
        http = FakeHttp({"data": CHVRCHES_LIES})
        covers = media.CoverCache(self.root, http=FakeHttp(HTML_ERROR))
        result = enrich.enrich_track(self.svc, track_id, http=http, covers=covers,
                                     throttle=enrich.Throttle(0.0))
        self.assertEqual(result["outcome"], "enriched")
        self.assertEqual(self.duration_of(track_id), 221_000)
        self.assertEqual(self.cache_files(), [])

    def test_a_quota_error_served_with_http_200_is_a_rate_limit(self):
        track_id = self.add_track("CHVRCHES", "Lies")
        http = FakeHttp({"error": {"type": "Exception", "message": "Quota limit exceeded",
                                   "code": 4}})
        throttle = enrich.Throttle(0.0, sleep=lambda s: None)
        with self.assertRaises(RateLimited):
            enrich.enrich_track(self.svc, track_id, http=http, covers=self.covers,
                                throttle=throttle)
        self.assertGreater(throttle.blocked_for(), 1.0)
        self.assertIsNone(self.duration_of(track_id))


class ThrottleTests(unittest.TestCase):
    def test_calls_are_spaced(self):
        slept: list[float] = []
        throttle = enrich.Throttle(0.5, sleep=slept.append)
        throttle.wait()
        throttle.wait()
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 0.5, places=1)

    def test_rate_limited_backs_off_instead_of_retrying_immediately(self):
        slept: list[float] = []
        throttle = enrich.Throttle(0.0, sleep=slept.append)
        http = FakeHttp(RateLimited("deezer: rate limited", retry_after=5.0,
                                    provider_id="deezer"))
        with self.assertRaises(RateLimited):
            enrich.search(http, "CHVRCHES", "Lies", throttle=throttle)
        # One request, and the next caller is held off for the interval the
        # server named rather than walking straight back into it.
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(slept, [])
        self.assertGreater(throttle.blocked_for(), 4.0)
        throttle.wait()
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 4.0)

    def test_a_refusal_with_no_retry_after_uses_the_local_cooldown(self):
        throttle = enrich.Throttle(0.0, sleep=lambda s: None)
        self.assertEqual(throttle.penalise(None), enrich.COOLDOWN_S)
        self.assertGreater(throttle.blocked_for(), enrich.COOLDOWN_S - 1)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


class SweepTests(Base):
    def test_rows_missing_either_half_are_queued(self):
        a = self.add_track("CHVRCHES", "Lies")
        b = self.add_track("Beach House", "Space Song", duration_ms=321_000)
        c = self.add_track("Grimes", "Oblivion", duration_ms=250_000)
        self.covers.store(c, JPEG)
        queued = enrich.sweep(self.svc, 10, covers=self.covers)
        # Newest first. The reader is looking at the top of their list, so
        # filling the oldest rows first leaves every cover on the first screen
        # a miss for as long as the backlog takes.
        self.assertEqual(queued, [b, a])
        self.assertEqual(self.jobs.enqueued, [("enrich", b), ("enrich", a)])

    def test_the_batch_is_capped(self):
        ids = [self.add_track("Artist %d" % i, "Song %d" % i) for i in range(5)]
        # The cap takes the newest two, which are the last two added here.
        self.assertEqual(enrich.sweep(self.svc, 2, covers=self.covers),
                         list(reversed(ids))[:2])

    def test_an_inconclusive_lookup_is_not_asked_again_immediately(self):
        track_id = self.add_track("CHVRCHES", "Lies")
        http = FakeHttp({"data": CHVRCHES_LIES[:1]})
        enrich.enrich_track(self.svc, track_id, http=http, covers=self.covers,
                            throttle=enrich.Throttle(0.0))
        self.assertEqual(enrich.pending(self.svc, 10, covers=self.covers), [])
        # It is a cooling-off period, not a tombstone: a lexicon change deserves
        # a second opinion eventually.
        later = int(time.time()) + enrich.RETRY_AFTER_S + 1
        self.assertEqual(enrich.pending(self.svc, 10, covers=self.covers, now=later),
                         [track_id])


class HandlerTests(Base):
    def test_the_handler_reports_progress_and_returns_the_result(self):
        track_id = self.add_track("CHVRCHES", "Lies")
        covers = media.CoverCache(self.root, http=FakeHttp(JPEG))
        handler = enrich.make_handler(self.svc, http=FakeHttp({"data": CHVRCHES_LIES}),
                                      covers=covers)
        phases: list[str] = []
        job = SimpleNamespace(id=1, kind="enrich", track_id=track_id)
        result = handler(job, lambda phase, **data: phases.append(phase))
        self.assertEqual(result["outcome"], "enriched")
        self.assertEqual(phases, ["lookup", "enriched"])
        self.assertEqual(self.duration_of(track_id), 221_000)


# ---------------------------------------------------------------------------
# The real thing, off by default
# ---------------------------------------------------------------------------


@unittest.skipUnless(os.environ.get("LW_LIVE_DEEZER"),
                     "set LW_LIVE_DEEZER=1 to call the public Deezer API")
class LiveDeezerTests(Base):
    def test_chvrches_lies_against_the_live_api(self):
        """Deezer really does rank a different song first for this query."""
        from libwish.http import HttpClient
        http = HttpClient(user_agent="library-wishlist/1.0 (+test)", timeout=20,
                          provider_id="deezer")
        rows = enrich.search(http, "CHVRCHES", "Lies", throttle=enrich.Throttle(0.0))
        self.assertTrue(rows)
        self.assertNotEqual(rows[0]["title"], "Lies")

        track_id = self.add_track("CHVRCHES", "Lies")
        result = enrich.enrich_track(self.svc, track_id, http=http,
                                     covers=media.CoverCache(self.root),
                                     throttle=enrich.Throttle(0.0))
        self.assertEqual(result["outcome"], "enriched")
        self.assertEqual(self.duration_of(track_id), 221_000)
        self.assertEqual(self.cache_files(), [f"{track_id}.jpg"])


if __name__ == "__main__":
    unittest.main()


class CoverSurvivesARuntimeRefusal(Base):
    """Refusing a runtime must not also refuse the artwork.

    The two carry different risks. A wrong runtime silently vetoes a correct
    purchase later; a sleeve from another pressing of the same song is cosmetic.
    Applying the stricter rule to both left 61 of 164 real rows with no cover.
    """

    def test_disagreeing_runtimes_still_yield_a_cover(self):
        from libwish import identity
        want = identity.build_identity("The Black Crowes", "She Talks To Angels")
        rows = [
            {"title": "She Talks To Angels", "duration": 330,
             "artist": {"name": "The Black Crowes"},
             "album": {"cover_medium": "https://example.invalid/a.jpg"}},
            {"title": "She Talks To Angels", "duration": 375,
             "artist": {"name": "The Black Crowes"},
             "album": {"cover_medium": "https://example.invalid/b.jpg"}},
        ]
        decision, index, accepted, detail, cover_ok = enrich.choose(want, rows)
        self.assertFalse(accepted, "45s apart is beyond the gate, so no runtime")
        self.assertTrue(cover_ok, "but it is plainly the same song, so the sleeve stands")
        self.assertIsNotNone(index)

    def test_a_different_song_yields_neither(self):
        from libwish import identity
        want = identity.build_identity("CHVRCHES", "Lies")
        rows = [{"title": 'Such Great Heights (From "Tell Me Lies Season 3")', "duration": 268,
                 "artist": {"name": "CHVRCHES"},
                 "album": {"cover_medium": "https://example.invalid/c.jpg"}}]
        decision, index, accepted, detail, cover_ok = enrich.choose(want, rows)
        self.assertFalse(accepted)
        self.assertFalse(cover_ok, "the wrong song's sleeve is still the wrong sleeve")
