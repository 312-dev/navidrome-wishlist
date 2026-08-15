"""End to end, over the real schema, with stores stubbed at the network edge.

The test that matters most here is `test_the_incident`. In August a claim for
CHVRCHES "Lies" downloaded their `Such Great Heights (From "Tell Me Lies Season
3")` instead, because the matcher scored the claim against the store's whole
listing row and the word "lies" appears in the tie-in credit. That bug is not
reachable from a unit test of any single component: it needed a store returning
a plausible near-miss, an identity layer, and a pipeline willing to act on the
answer. So it is pinned here, at the depth where it actually happened.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from libwish.errors import LibwishError, MatchRefused, StoreAuthError, VerificationFailed
from libwish.models import (
    DownloadResult, Identifiers, MATCH_AUTO_MIN, OwnedItem, SourcePage,
    StoreCapabilities, StoreHealth, TrackIds, LovedTrack,
)

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO / ".testdata" / "queue.db"

FLAC = b"fLaC" + b"\0" * 200_000
SIGNIN_PAGE = b"<!doctype html><html><head><title>Sign in to Qobuz</title>" + b" " * 200_000


def owned(artist, title, key, album=None):
    return OwnedItem(store="stub", item_key=key, kind="track", artist=artist, title=title,
                     release_title=album, parent_key=None, purchased_at=None,
                     duration_s=None, track_number=None, formats=("flac",),
                     ids=Identifiers(), raw={})


class StubStore:
    """A store whose inventory and download bytes the test dictates."""

    id = "stub"
    name = "Stub"
    auth_kind = "none"
    capabilities = StoreCapabilities(search=False, deep_link=True, enumerate_owned=True,
                                     download=True, release_granular=False,
                                     async_prepare=False, formats=("flac",))

    def __init__(self, inventory, *, payload=FLAC, authed=True):
        self.inventory = list(inventory)
        self.payload = payload
        self.authed = authed
        self.downloaded = []

    def check(self):
        return StoreHealth(ok=True, authed=self.authed, detail="", checked_at=0,
                           owned_count=len(self.inventory))

    def buy_url(self, q):
        return "https://example.invalid/search"

    def find_offers(self, q, limit=5):
        return []

    def list_owned(self, since=None):
        if not self.authed:
            raise StoreAuthError("signed out", code="signed_out", provider_id=self.id)
        return iter(self.inventory)

    def expand(self, item):
        yield item

    def download(self, item, dest_dir, prefer, progress):
        self.downloaded.append(item.item_key)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = Path(dest_dir) / f"{item.item_key}.flac"
        dest.write_bytes(self.payload)
        progress("download", bytes=len(self.payload), total=len(self.payload))
        return DownloadResult(path=dest, requested_format="flac", bytes=len(self.payload),
                              source_host="stub", is_archive=False, notes={})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        if SNAPSHOT.is_file():
            shutil.copy(SNAPSHOT, self.tmp / "library-wishlist.db")
        for key, value in {
            "LW_CONFIG_DIR": str(self.tmp), "LW_MUSIC_DIR": str(self.tmp / "music"),
            "LW_LOG_LEVEL": "CRITICAL", "LW_RESCAN_CMD": "",
        }.items():
            os.environ[key] = value
        for stale in ("LW_SOURCE_LASTFM_API_KEY", "LW_SOURCE_LASTFM_USER"):
            os.environ.pop(stale, None)
        from libwish.settings import Settings
        from libwish.web.app import create_app
        self.app = create_app(Settings.from_env(), start_workers=False)
        self.svc = self.app.extensions["libwish"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def a_track(self, artist, title):
        conn = self.svc.db()
        try:
            cur = conn.execute(
                "INSERT INTO tracks(artist, title, added_at, status) VALUES(?,?,0,'queued')",
                (artist, title))
            return cur.lastrowid
        finally:
            conn.close()

    def confirm(self, track_id, store_id="stub"):
        """Record the user override the interface writes when someone accepts a
        candidate the matcher would only confirm."""
        import time
        from libwish import identity, match
        conn = self.svc.db()
        try:
            conn.execute(
                "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
                " matcher_version, lexicon_hash, outcome, reasons, query_json,"
                " candidates_considered, chosen_store_id)"
                " VALUES(?,?,?,?,?,?,'user_confirmed','','{}',0,?)",
                (track_id, int(time.time()), "confirm", store_id,
                 getattr(match, "MATCHER_VERSION", "1"), identity.lexicon_hash(), store_id))
        finally:
            conn.close()

    def pick(self, track_id, item_key, store_id="stub", title="", artist=""):
        """Record the choice the interface writes when someone claims a
        purchase by hand instead of disagreeing with a score."""
        import json
        import time
        from libwish import identity, match
        conn = self.svc.db()
        try:
            conn.execute(
                "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
                " matcher_version, lexicon_hash, outcome, reasons, query_json,"
                " candidate_json, candidates_considered, chosen_store_id)"
                " VALUES(?,?,?,?,?,?,'user_picked','','{}',?,0,?)",
                (track_id, int(time.time()), "pick", store_id,
                 getattr(match, "MATCHER_VERSION", "1"), identity.lexicon_hash(),
                 json.dumps({"item_key": item_key, "title": title, "artist": artist}),
                 store_id))
        finally:
            conn.close()

    def swept(self, track_id, item_key, store_id="stub", title="", artist="",
              why="matched by a purchase sweep"):
        """Record the decision a purchase sweep writes before queueing a claim.

        Same shape as `pick` and for the same reason: it names one purchase
        rather than a score. Written here rather than by running a sweep, so
        that what is under test is the claim reading the row back.
        """
        import json
        import time
        from libwish import identity, match
        conn = self.svc.db()
        try:
            conn.execute(
                "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
                " matcher_version, lexicon_hash, outcome, reasons, query_json,"
                " candidate_json, candidates_considered, chosen_store_id)"
                " VALUES(?,?,?,?,?,?,'swept',?,'{}',?,0,?)",
                (track_id, int(time.time()), "sync", store_id,
                 getattr(match, "MATCHER_VERSION", "1"), identity.lexicon_hash(), why,
                 json.dumps({"item_key": item_key, "title": title, "artist": artist}),
                 store_id))
        finally:
            conn.close()

    def purchased_item_key(self, track_id):
        conn = self.svc.db()
        try:
            row = conn.execute(
                "SELECT purchased_item_key FROM tracks WHERE id=?", (track_id,)).fetchone()
        finally:
            conn.close()
        return row["purchased_item_key"]

    def run_claim(self, track_id, store):
        from libwish.claim import ClaimPipeline
        pipeline = ClaimPipeline(self.svc, {"stub": store})
        job = type("J", (), {"id": 1, "kind": "claim", "track_id": track_id,
                             "provider_id": "stub", "phase": None, "attempts": 1})()
        seen = []
        pipeline(job, lambda phase, **kw: seen.append(phase))
        return seen


class TheIncident(Base):
    def test_the_incident(self):
        """A claim for "Lies" must not collect the Tell Me Lies tie-in."""
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([
            owned("CHVRCHES", 'Such Great Heights (From "Tell Me Lies Season 3")', "wrong"),
        ])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, [], "nothing may be downloaded on a refusal")
        self.assertEqual(self.svc.tracks.get(track_id)["status"], "queued",
                         "a refused claim leaves the track in the queue")

    def test_the_refusal_is_recorded_so_the_user_can_see_it(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([owned("CHVRCHES", 'Such Great Heights (From "Tell Me Lies Season 3")', "w")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        conn = self.svc.db()
        try:
            row = conn.execute(
                "SELECT outcome, candidates_considered, candidate_json FROM match_decision"
                " WHERE track_id=? ORDER BY id DESC LIMIT 1", (track_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "a refusal with no audit row is invisible to the user")
        self.assertEqual(row["outcome"], "refused")
        self.assertEqual(row["candidates_considered"], 1)

    def test_the_right_track_among_the_wrong_ones_still_wins(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([
            owned("CHVRCHES", 'Such Great Heights (From "Tell Me Lies Season 3")', "wrong"),
            owned("CHVRCHES", "Lies", "right", album="The Bones of What You Believe"),
        ])
        self.confirm(track_id)
        phases = self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["right"])
        # A store reports byte progress under the download phase as often as it
        # likes, so consecutive repeats are collapsed before comparing.
        distinct = [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]]
        self.assertEqual(distinct, ["session", "enumerate", "match", "download", "verify"])
        self.assertEqual(self.svc.tracks.get(track_id)["status"], "purchased")
        self.assertEqual(self.purchased_item_key(track_id), "right",
                         "an ordinary matched claim must record which item it downloaded")


class ClaimSafety(Base):
    def test_strings_alone_never_auto_claim(self):
        """Invariant 3, enforced by the running program and not only the design.

        Reaching the auto band needs a shared MBID or ISRC. An exact match on
        strings alone caps at 84, inside the confirm band, so it stops and asks
        rather than spending money on a name that merely looks right.
        """
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([owned("CHVRCHES", "Lies", "exact")])
        with self.assertRaises(MatchRefused) as caught:
            self.run_claim(track_id, store)
        self.assertEqual(caught.exception.reason, "needs_confirmation")
        self.assertEqual(store.downloaded, [])
        self.assertEqual(self.svc.tracks.get(track_id)["status"], "queued")

    def test_a_signed_out_store_is_not_an_empty_library(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        with self.assertRaises(StoreAuthError):
            self.run_claim(track_id, StubStore([owned("CHVRCHES", "Lies", "r")], authed=False))

    def test_a_signin_page_is_not_published_as_audio(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([owned("CHVRCHES", "Lies", "r")], payload=SIGNIN_PAGE)
        self.confirm(track_id)          # get past the confirm band to reach verification
        with self.assertRaises(VerificationFailed):
            self.run_claim(track_id, store)
        self.assertEqual(self.svc.tracks.get(track_id)["status"], "queued")
        library = list((self.tmp / "music").rglob("*.flac"))
        self.assertEqual(library, [], "an unverified download must never reach the library")

    def test_a_live_version_does_not_satisfy_a_studio_claim(self):
        track_id = self.a_track("Audioslave", "Like a Stone")
        store = StubStore([owned("Audioslave", "Like a Stone (Live at Madison Square Garden)", "live")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, [])

    def test_album_version_is_the_ordinary_studio_cut(self):
        track_id = self.a_track("Audioslave", "Shadow On The Sun")
        store = StubStore([owned("Audioslave", "Shadow On The Sun (Album Version)", "ok")])
        self.confirm(track_id)
        self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["ok"])


class ReaderFacingRefusals(Base):
    """What a refusal reads like on screen, not just what it decided.

    Each case checks two things a fix here could trade off against each other:
    the sentence has to be something a person can act on, and the score and
    threshold the panel's "Confidence" line needs still have to be in the row
    that sentence came from.
    """

    def refusal(self, track_id):
        from libwish.web.views import refusal_for
        conn = self.svc.db()
        try:
            return refusal_for(track_id, conn)
        finally:
            conn.close()

    def test_an_artist_mismatch_names_both_artists_in_plain_language(self):
        track_id = self.a_track("Audioslave", "Like a Stone")
        store = StubStore([owned("CHVRCHES", "Like a Stone", "wrong-artist")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        refusal = self.refusal(track_id)
        self.assertIn("different artist", refusal["reasons"])
        self.assertIn("Audioslave", refusal["reasons"])
        self.assertIn("CHVRCHES", refusal["reasons"])
        self.assertNotIn("agreement", refusal["reasons"],
                         "a match ratio is not something the reader asked for")
        self.assertEqual(refusal["threshold"], MATCH_AUTO_MIN)
        self.assertIsNotNone(refusal["score"])

    def test_a_title_mismatch_names_both_titles_in_plain_language(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([
            owned("CHVRCHES", 'Such Great Heights (From "Tell Me Lies Season 3")', "wrong-title"),
        ])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        refusal = self.refusal(track_id)
        self.assertIn("different title", refusal["reasons"])
        self.assertIn("Lies", refusal["reasons"])
        self.assertIn("Tell Me Lies", refusal["reasons"])
        self.assertNotIn("characters", refusal["reasons"],
                         "the short-title gate name is not something the reader asked for")
        self.assertEqual(refusal["threshold"], MATCH_AUTO_MIN)
        self.assertIsNotNone(refusal["score"])

    def test_nothing_close_enough_says_so_plainly(self):
        # Passes every gate (same title, an artist spelling close enough to
        # clear the gate) but the evidence adds up to well under the confirm
        # floor, the case with no single gate to blame.
        track_id = self.a_track("Radiohead", "Creep")
        store = StubStore([owned("Raidohead", "Creep", "too-thin")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        refusal = self.refusal(track_id)
        self.assertIn("came close enough", refusal["reasons"])
        self.assertEqual(refusal["threshold"], MATCH_AUTO_MIN)
        self.assertIsNotNone(refusal["score"])


class UserConfirmation(Base):
    """Overriding a refusal is a distinct, auditable act with its own limits."""

    def test_confirmation_lets_a_near_miss_through(self):
        track_id = self.a_track("Lil Peep", "Falling Down")
        store = StubStore([owned("Lil Peep", "Falling Down - Bonus Track", "near")])
        self.confirm(track_id)
        self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["near"])

    def test_confirmation_is_not_permission_to_download_anything(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([owned("Aphex Twin", "Windowlicker", "unrelated")])
        self.confirm(track_id)
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, [],
                         "a confirmation must not license an unrelated download")

    def test_a_confirmation_is_spent_once(self):
        track_id = self.a_track("Lil Peep", "Falling Down")
        self.confirm(track_id)
        self.run_claim(track_id, StubStore([owned("Lil Peep", "Falling Down - Bonus Track", "a")]))
        self.svc.tracks.set_status(track_id, "queued")
        second = StubStore([owned("Lil Peep", "Falling Down - Bonus Track", "b")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, second)
        self.assertEqual(second.downloaded, [],
                         "one override must not license every later claim on that track")

    def test_the_override_is_recorded_under_its_own_outcome(self):
        track_id = self.a_track("Lil Peep", "Falling Down")
        self.confirm(track_id)
        self.run_claim(track_id, StubStore([owned("Lil Peep", "Falling Down - Bonus Track", "a")]))
        conn = self.svc.db()
        try:
            outcome = conn.execute(
                "SELECT outcome FROM match_decision WHERE track_id=?"
                " ORDER BY id DESC LIMIT 1", (track_id,)).fetchone()["outcome"]
        finally:
            conn.close()
        self.assertEqual(outcome, "accepted_after_confirmation",
                         "the audit must distinguish what the software chose from what a person did")


class UserPick(Base):
    """Pointing at one exact purchase is a distinct, auditable act, and one the
    matcher never gets to score."""

    def test_a_pick_bypasses_scoring_entirely(self):
        """A confirmation still has to clear the confirm floor; a pick does
        not, because nobody asked the matcher to judge it."""
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([owned("Aphex Twin", "Windowlicker", "unrelated")])
        self.pick(track_id, "unrelated")
        self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["unrelated"])
        self.assertEqual(self.svc.tracks.get(track_id)["status"], "purchased")

    def test_a_pick_downloads_exactly_the_named_item_among_others(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([
            owned("CHVRCHES", "Lies", "right", album="The Bones of What You Believe"),
            owned("Aphex Twin", "Windowlicker", "wrong"),
        ])
        self.pick(track_id, "wrong")
        self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["wrong"])
        self.assertEqual(self.purchased_item_key(track_id), "wrong",
                         "a hand-picked claim must record which item it downloaded too")

    def test_a_missing_picked_item_fails_cleanly(self):
        """The purchase named at pick time is gone by the time the job runs.
        Nothing downloads, and the track is not silently dropped."""
        track_id = self.a_track("CHVRCHES", "Lies")
        store = StubStore([owned("CHVRCHES", "Lies", "right")])
        self.pick(track_id, "gone")
        with self.assertRaises(LibwishError):
            self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, [])
        self.assertEqual(self.svc.tracks.get(track_id)["status"], "queued")

    def test_a_pick_is_spent_once(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        self.pick(track_id, "a")
        self.run_claim(track_id, StubStore([owned("Aphex Twin", "Windowlicker", "a")]))
        self.svc.tracks.set_status(track_id, "queued")
        second = StubStore([owned("Aphex Twin", "Windowlicker", "b")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, second)
        self.assertEqual(second.downloaded, [],
                         "one pick must not license every later claim on that track")

    def test_the_pick_is_recorded_under_its_own_outcome_not_a_confirmation(self):
        track_id = self.a_track("Lil Peep", "Falling Down")
        self.pick(track_id, "a", title="Falling Down", artist="Lil Peep")
        conn = self.svc.db()
        try:
            row = conn.execute(
                "SELECT outcome FROM match_decision WHERE track_id=?"
                " ORDER BY id DESC LIMIT 1", (track_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["outcome"], "user_picked")
        self.assertNotEqual(row["outcome"], "user_confirmed",
                            "a hand-picked purchase is a different act from disagreeing with a score")

    def test_a_successful_pick_writes_a_second_row_so_the_spend_is_visible(self):
        """Mirrors the confirm flow: the act (`user_picked`) and the pipeline's
        own outcome (`downloaded_by_pick`) are both on the audit trail."""
        track_id = self.a_track("Lil Peep", "Falling Down")
        self.pick(track_id, "a")
        self.run_claim(track_id, StubStore([owned("Lil Peep", "Falling Down", "a")]))
        conn = self.svc.db()
        try:
            outcomes = [r["outcome"] for r in conn.execute(
                "SELECT outcome FROM match_decision WHERE track_id=? ORDER BY id", (track_id,))]
        finally:
            conn.close()
        self.assertEqual(outcomes, ["user_picked", "downloaded_by_pick"])


class ConfirmingARefusalTakesTheTrackItNamed(Base):
    """"That is the right track, download it" downloads that track.

    Reported live: a Percy Sledge single refused against a 2000 remaster, and
    the button under the refusal did nothing however many times it was pressed.
    Every gate refusal scores zero, so sending a confirmation back through the
    confirm floor refused the same near miss again, recording
    `refused_despite_confirmation` each round. The confirmation has to name the
    purchase the panel was showing and the claim has to download that one.
    """

    def a_refusal(self, track_id, store):
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)

    def confirm_over_http(self, track_id):
        response = self.app.test_client().post(
            f"/api/claim/{track_id}/confirm", json={"store": "stub"})
        self.assertEqual(response.status_code, 202)

    def test_a_version_variant_is_downloaded_once_the_user_agrees_with_it(self):
        track_id = self.a_track("Percy Sledge", "Love Me Tender")
        self.a_refusal(track_id, StubStore(
            [owned("Percy Sledge", "Love Me Tender (2000 Remaster)", "a")]))
        self.confirm_over_http(track_id)

        store = StubStore([owned("Percy Sledge", "Love Me Tender (2000 Remaster)", "a")])
        self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["a"])
        self.assertEqual(self.purchased_item_key(track_id), "a")

    def test_the_confirmation_names_the_candidate_the_refusal_showed(self):
        # Not "some purchase": the one on screen. Any other item in the account
        # is one the reader was never asked about.
        track_id = self.a_track("Percy Sledge", "Love Me Tender")
        self.a_refusal(track_id, StubStore([
            owned("Percy Sledge", "Love Me Tender (2000 Remaster)", "right"),
            owned("Percy Sledge", "When A Man Loves A Woman", "wrong"),
        ]))
        self.confirm_over_http(track_id)
        conn = self.svc.db()
        try:
            row = conn.execute(
                "SELECT outcome, candidate_json FROM match_decision WHERE track_id=?"
                " ORDER BY id DESC LIMIT 1", (track_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["outcome"], "user_confirmed")
        self.assertEqual(json.loads(row["candidate_json"])["item_key"], "right")

    def test_pressing_it_a_second_time_does_the_same_thing_as_the_first(self):
        # What was actually reported. The first press was pressed again when
        # nothing happened, and by then the newest decision was that first
        # confirmation, which names no purchase. The panel on screen was still
        # the original refusal, so that is the row this reads.
        track_id = self.a_track("Percy Sledge", "Love Me Tender")
        self.a_refusal(track_id, StubStore(
            [owned("Percy Sledge", "Love Me Tender (2000 Remaster)", "a")]))
        self.confirm_over_http(track_id)
        self.confirm_over_http(track_id)

        store = StubStore([owned("Percy Sledge", "Love Me Tender (2000 Remaster)", "a")])
        self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["a"])

    def test_the_confirmation_is_spent_once(self):
        track_id = self.a_track("Percy Sledge", "Love Me Tender")
        self.a_refusal(track_id, StubStore(
            [owned("Percy Sledge", "Love Me Tender (2000 Remaster)", "a")]))
        self.confirm_over_http(track_id)
        self.run_claim(track_id, StubStore(
            [owned("Percy Sledge", "Love Me Tender (2000 Remaster)", "a")]))

        self.svc.tracks.set_status(track_id, "queued")
        again = StubStore([owned("Percy Sledge", "Love Me Tender (2000 Remaster)", "a")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, again)
        self.assertEqual(again.downloaded, [])

    def test_the_panel_names_whatever_this_will_download(self):
        # The button says "that is the right track", so what it downloads has
        # to be the track the sentence above it described. A refusal against a
        # different artist entirely names that artist, and confirming it is a
        # person overruling the matcher about a purchase they can read.
        track_id = self.a_track("Percy Sledge", "Love Me Tender")
        self.a_refusal(track_id, StubStore([owned("Elvis Presley", "Love Me Tender", "elvis")]))
        conn = self.svc.db()
        try:
            said = conn.execute(
                "SELECT reasons FROM match_decision WHERE track_id=?"
                " ORDER BY id DESC LIMIT 1", (track_id,)).fetchone()["reasons"]
        finally:
            conn.close()
        self.assertIn("Elvis Presley", said)

        self.confirm_over_http(track_id)
        store = StubStore([owned("Elvis Presley", "Love Me Tender", "elvis")])
        self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["elvis"])

    def test_a_confirmation_naming_nothing_still_meets_the_confirm_floor(self):
        # The floor is not gone. A bare confirmation, which is what rows
        # written before this and the bulk route on a track with no prior
        # decision produce, has no item to attach the intent to, so the
        # matcher still has to find one worth downloading.
        track_id = self.a_track("Percy Sledge", "Love Me Tender")
        self.confirm(track_id)
        store = StubStore([owned("Nine Inch Nails", "Closer", "unrelated")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, [])


class SweptDecisions(Base):
    """A claim honours the purchase a sweep already chose, without rematching.

    This is the seam the version rule depends on. A sweep may accept a purchase
    the matcher scores at nothing, because it differs from the wanted track only
    by a version qualifier and is the only one it could be. If the claim then
    matched again from scratch it would refuse that exact purchase, and the
    sweep's whole answer would be undone one job later, reported to the reader
    as a failed claim on a track they had just been told was filed.
    """

    def test_a_version_variant_a_sweep_chose_is_downloaded_rather_than_rematched(self):
        track_id = self.a_track("Fleetwood Mac", "Gold Dust Woman")
        store = StubStore([owned("Fleetwood Mac", "Gold Dust Woman (2004 Remaster)", "a")])
        self.swept(track_id, "a", title="Gold Dust Woman (2004 Remaster)",
                   artist="Fleetwood Mac")
        self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, ["a"])
        self.assertEqual(self.purchased_item_key(track_id), "a")

    def test_the_same_claim_without_the_sweep_row_refuses(self):
        # The other half of the pair, and what makes the test above mean
        # something: the purchase is one the matcher genuinely will not take.
        track_id = self.a_track("Fleetwood Mac", "Gold Dust Woman")
        store = StubStore([owned("Fleetwood Mac", "Gold Dust Woman (2004 Remaster)", "a")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, store)
        self.assertEqual(store.downloaded, [])

    def test_the_choice_is_spent_once(self):
        track_id = self.a_track("Fleetwood Mac", "Gold Dust Woman")
        store = StubStore([owned("Fleetwood Mac", "Gold Dust Woman (2004 Remaster)", "a")])
        self.swept(track_id, "a", title="Gold Dust Woman (2004 Remaster)",
                   artist="Fleetwood Mac")
        self.run_claim(track_id, store)

        # Back on the list later for any reason: the old sweep row must not
        # still be licensing downloads of whatever it once chose.
        self.svc.tracks.set_status(track_id, "queued")
        again = StubStore([owned("Fleetwood Mac", "Gold Dust Woman (2004 Remaster)", "a")])
        with self.assertRaises(MatchRefused):
            self.run_claim(track_id, again)
        self.assertEqual(again.downloaded, [])

    def test_both_the_sweep_and_the_download_stay_on_the_audit_trail(self):
        # The sweep's row says a machine chose this; the pipeline's says what
        # it then did. Neither is written as a user confirmation, because
        # nobody looked at it.
        track_id = self.a_track("Fleetwood Mac", "Gold Dust Woman")
        self.swept(track_id, "a", title="Gold Dust Woman (2004 Remaster)",
                   artist="Fleetwood Mac")
        self.run_claim(
            track_id, StubStore([owned("Fleetwood Mac", "Gold Dust Woman (2004 Remaster)", "a")]))
        conn = self.svc.db()
        try:
            rows = [(r["outcome"], r["reasons"]) for r in conn.execute(
                "SELECT outcome, reasons FROM match_decision WHERE track_id=? ORDER BY id",
                (track_id,))]
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], ["swept", "downloaded_by_pick"])
        self.assertNotIn("user_confirmed", [r[0] for r in rows])
        self.assertIn("sweep", rows[1][1],
                      "the download must not describe itself as picked by hand")


class Endpoints(Base):
    def test_the_routes_the_interface_calls_all_exist(self):
        client = self.app.test_client()
        track_id = self.a_track("CHVRCHES", "Lies")
        for method, path, allowed in (
            ("get", "/api/queue", (200,)),
            ("get", f"/api/track/{track_id}", (200,)),
            ("get", f"/api/buy/{track_id}", (200,)),
            ("get", f"/api/search/{track_id}", (200,)),
            ("post", f"/api/claim/{track_id}/cancel", (200,)),
            ("post", f"/api/claim/{track_id}/confirm", (202,)),
            ("post", "/api/scan", (200, 400)),
            ("get", "/api/status", (200,)),
            ("get", "/api/jobs", (200,)),
        ):
            with self.subTest(path=path):
                response = getattr(client, method)(path)
                self.assertIn(response.status_code, allowed, f"{method.upper()} {path}")

    def test_unknown_tracks_are_404_not_500(self):
        client = self.app.test_client()
        for path in ("/api/track/999999", "/api/buy/999999", "/api/preview/999999"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)


class Ingest(Base):
    def love(self, artist, title, source="lastfm", item="i1"):
        return LovedTrack(source_id=source, source_item_id=item, loved_at=1786300000,
                          artist=artist, title=title, album=None, artists=(artist,),
                          ids=TrackIds(), duration_s=None, cover_url=None, raw={})

    def test_the_same_love_twice_makes_one_row(self):
        from libwish.runtime import Ingest as Ing
        ing = Ing(self.svc)
        first = ing("lastfm", SourcePage(items=(self.love("Jamie xx", "Gosh"),), cursor=None))
        second = ing("lastfm", SourcePage(items=(self.love("Jamie xx", "Gosh"),), cursor=None))
        self.assertEqual((first, second), (1, 0))

    def test_a_purchased_track_is_never_dragged_back_into_the_queue(self):
        from libwish.runtime import Ingest as Ing
        track_id = self.a_track("Nine Inch Nails", "Copy of A")
        self.svc.tracks.mark_purchased(track_id, "stub")
        Ing(self.svc)("lastfm", SourcePage(
            items=(self.love("Nine Inch Nails", "Copy of A", item="again"),), cursor=None))
        self.assertEqual(self.svc.tracks.get(track_id)["status"], "purchased")

    @unittest.skipUnless(SNAPSHOT.is_file(), "no live snapshot available")
    def test_the_real_database_survives_migration(self):
        conn = self.svc.db()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
            columns = [r["name"] for r in conn.execute("PRAGMA table_info(tracks)")]
        finally:
            conn.close()
        self.assertEqual(total, 164)
        for legacy in ("dedup_key", "source_platform", "bandcamp_url", "qobuz_url"):
            self.assertIn(legacy, columns, "0007 drops these, and it has not shipped")


if __name__ == "__main__":
    unittest.main()


class Search(Base):
    """Searching runs in the database, so it sees every track and not only the
    60 a window happens to hold."""

    def setUp(self):
        super().setUp()
        self.client = self.app.test_client()

    def test_it_searches_beyond_the_loaded_window(self):
        from libwish.repo import search_clause
        where, args = search_clause("chvrches")
        conn = self.svc.db()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
            hits = conn.execute(
                f"SELECT COUNT(*) AS n FROM tracks WHERE 1=1{where}", args).fetchone()["n"]
        finally:
            conn.close()
        self.assertGreater(total, 60, "the point of this test needs more rows than one window")
        self.assertGreater(hits, 0, "a match past row 60 must still be findable")

    def test_accents_and_punctuation_do_not_have_to_be_typed(self):
        self.assertEqual(len(self.svc.tracks.queued("bubl")),
                         len(self.svc.tracks.queued("bublé")))
        rows = self.svc.tracks.owned("lies") + self.svc.tracks.queued("lies")
        self.assertTrue(any("Lies" in r["title"] for r in rows))

    def test_word_order_does_not_matter(self):
        a = [r["id"] for r in self.svc.tracks.queued("chvrches mother")]
        b = [r["id"] for r in self.svc.tracks.queued("mother chvrches")]
        self.assertEqual(a, b)
        self.assertTrue(a)

    def test_an_empty_query_is_every_row_not_no_rows(self):
        self.assertEqual(len(self.svc.tracks.queued("")), len(self.svc.tracks.queued()))
        # An artist that folds to nothing must not empty the screen either.
        self.assertEqual(len(self.svc.tracks.queued("!!!")), len(self.svc.tracks.queued()))

    def test_a_query_in_the_url_still_narrows_the_list(self):
        # The box is gone from the bar, but the query behind it is not: ?q=
        # remains a working, linkable URL and the JSON API pages with the same
        # clause. Asserted by counting rows rather than by anything on screen,
        # since there is no longer a control to echo the term back into.
        import re

        def rows(path):
            body = self.client.get(path).get_data(as_text=True)
            return re.findall(r'id="row-(\d+)"', body)

        everything, hits = rows("/"), rows("/?q=chvrches")
        self.assertTrue(hits, "a query that matches must still return its rows")
        self.assertLess(len(hits), len(everything))


class JobProgress(Base):
    def test_a_handler_restating_a_job_fact_does_not_kill_the_job(self):
        """Only reachable with both halves running: the enrichment handler
        reports its own track_id, which the queue already supplies."""
        from libwish.jobs import JobQueue
        queue = JobQueue(self.svc.db, self.svc.bus)
        track_id = self.a_track("Boards of Canada", "Roygbiv")
        seen = []
        queue.register("enrich", lambda job, progress: (
            progress("lookup", track_id=job.track_id, extra="kept"), seen.append(job.id)))
        job_id = queue.enqueue("enrich", track_id=track_id)
        queue.start(workers=1)
        import time
        for _ in range(40):
            if queue.get(job_id)["state"] in ("finished", "failed"):
                break
            time.sleep(0.05)
        queue.stop()
        self.assertEqual(queue.get(job_id)["state"], "finished",
                         "a colliding progress key must not fail the job")
        self.assertEqual(seen, [job_id])


try:
    import waitress as _waitress
except ImportError:
    _waitress = None


@unittest.skipUnless(_waitress, "waitress is not installed in this environment")
class ServedByWaitress(unittest.TestCase):
    """Exercised through the real server, not the test client.

    Flask's test client and its development server both accept things waitress
    refuses. A hop-by-hop header on the event stream passed every test here and
    then returned 500 to a browser, because only a conformant WSGI server
    checks for it. Anything about how a response reaches the wire belongs in
    this class.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        import threading
        import time
        from pathlib import Path

        cls.tmp = Path(tempfile.mkdtemp())
        if SNAPSHOT.is_file():
            shutil.copy(SNAPSHOT, cls.tmp / "library-wishlist.db")
        os.environ.update(LW_CONFIG_DIR=str(cls.tmp), LW_MUSIC_DIR=str(cls.tmp / "music"),
                          LW_LOG_LEVEL="CRITICAL", LW_RESCAN_CMD="")
        from waitress.server import create_server

        from libwish.settings import Settings
        from libwish.web.app import create_app

        app = create_app(Settings.from_env(), start_workers=False)
        cls.server = create_server(app, host="127.0.0.1", port=0, threads=6)
        cls.port = cls.server.socket.getsockname()[1]
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls.server.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path, timeout=5):
        import urllib.request
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout)

    def test_the_event_stream_opens_and_stays_open(self):
        response = self._get("/api/events")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("text/event-stream"))
        # readline, not read(n): the opening frame is short and the stream then
        # idles until the keepalive tick, so asking for a fixed byte count
        # blocks on bytes that are not coming yet.
        first = response.readline()
        self.assertTrue(first.strip(), "the stream must send something immediately")
        response.close()

    def test_the_application_sets_no_hop_by_hop_headers(self):
        """PEP 3333 forbids the application from sending these, and waitress
        refuses the whole response rather than dropping the offending header.

        Checked on the response the application produces, not the one that
        arrives: a conformant server adds Connection and Transfer-Encoding
        itself, and those are legitimately its to send.
        """
        banned = {"connection", "keep-alive", "transfer-encoding", "upgrade",
                  "proxy-authenticate", "te", "trailers"}
        from libwish.settings import Settings
        from libwish.web.app import create_app

        client = create_app(Settings.from_env(), start_workers=False).test_client()
        for path in ("/", "/api/queue", "/api/status", "/api/events"):
            with self.subTest(path=path):
                response = client.get(path, buffered=False)
                sent = {k.lower() for k in response.headers.keys()} & banned
                response.close()
                self.assertFalse(sent, f"{path} sent hop-by-hop header(s) {sent}")

    def test_the_pages_render_through_the_real_server(self):
        for path in ("/", "/owned", "/ignored", "/?q=chvrches"):
            with self.subTest(path=path):
                response = self._get(path)
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 1000)


class StreamCap(unittest.TestCase):
    """Open streams are bounded, because each one holds a server worker.

    A reload leaves the previous subscriber in place until the server next
    fails to write to a socket that has gone, which takes up to two keepalive
    ticks. Without a cap those accumulate and a handful of reloads take every
    worker, at which point the application answers nothing at all.
    """

    def test_the_oldest_is_evicted_rather_than_the_newest_refused(self):
        from libwish.events import MAX_CLIENTS, EventBus

        bus = EventBus()
        subs = [bus.subscribe() for _ in range(MAX_CLIENTS)]
        self.assertEqual(bus.client_count, MAX_CLIENTS)

        fresh = bus.subscribe()
        self.assertEqual(bus.client_count, MAX_CLIENTS,
                         "the cap must hold rather than grow by one")
        self.assertIn(fresh, bus._subs, "the newest connection is the one being looked at")
        self.assertNotIn(subs[0], bus._subs, "the oldest is the one to drop")

    def test_an_evicted_stream_is_told_to_stop(self):
        from libwish.events import MAX_CLIENTS, EventBus

        bus = EventBus()
        first = bus.subscribe()
        for _ in range(MAX_CLIENTS):
            bus.subscribe()
        # close() puts a sentinel on the queue, which ends the generator, which
        # is what releases the worker thread.
        frames = list(first.stream(keepalive=0.1))
        self.assertEqual(frames, ["retry: 3000\n\n"],
                         "an evicted stream must terminate, not sit on a worker")

    def test_publishing_never_blocks_on_a_reader_that_stopped(self):
        from libwish.events import MAX_PENDING, EventBus

        bus = EventBus()
        sub = bus.subscribe()
        for i in range(MAX_PENDING * 2):
            bus.publish("track.updated", id=i)
        self.assertGreater(sub.dropped, 0, "a stalled reader drops rather than growing")


class ConnectionsPerRequest(Base):
    """One render must not open a connection per row or per group.

    Grouping calls the decorator once per group, and an artist-grouped window of
    60 rows has around 54 of them, so work done inside it ran 54 times. That put
    139 connections on a single page load, each re-applying pragmas and each
    able to wait out the 15 second busy timeout behind the enrichment writer.
    Switching tabs quickly was enough to stall the application entirely.
    """

    CEILING = 12

    def _count(self, path):
        from libwish import db as dbmod

        opened = {"n": 0}
        real = dbmod.connect
        dbmod.connect = lambda p: (opened.__setitem__("n", opened["n"] + 1), real(p))[1]
        try:
            response = self.app.test_client().get(path)
        finally:
            dbmod.connect = real
        self.assertEqual(response.status_code, 200, path)
        return opened["n"]

    def test_a_page_render_stays_under_the_ceiling(self):
        for path in ("/", "/owned", "/ignored", "/?q=doors"):
            with self.subTest(path=path):
                n = self._count(path)
                self.assertLessEqual(
                    n, self.CEILING,
                    f"{path} opened {n} connections; a per-row or per-group open has crept back")

    def test_the_count_does_not_grow_with_the_number_of_groups(self):
        """The real regression signature: cost scaling with group count."""
        for _ in range(40):
            self.a_track(f"Artist {_:03d}", f"Song {_:03d}")
        many = self._count("/?group=artist")
        few = self._count("/?group=date")
        self.assertLessEqual(
            abs(many - few), 4,
            f"artist grouping opened {many} and date grouping {few}; "
            "connection cost is tracking the number of groups")
