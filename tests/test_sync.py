"""Sweeping every shop's purchases and filing what is recognised.

The assertions worth reading are the ones about restraint. A sweep runs without
anyone watching it, so the interesting question is not what it files but what it
declines to: a near miss must leave no mark on a track nobody was looking at,
and a purchase already filed must not be filed twice however often the button
is pressed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from libwish.models import MATCH_CONFIRM_MIN, OwnedItem


class FakeHealth:
    def __init__(self, ok=True, authed=True, detail="", owned_count=0):
        self.ok, self.authed, self.detail, self.owned_count = ok, authed, detail, owned_count


class FakeStore:
    """A shop that answers with whatever the test hands it."""

    def __init__(self, store_id, name, items, health=None, explode=False):
        self.id, self.name, self._items = store_id, name, items
        self._health = health or FakeHealth()
        self._explode = explode
        self.enumerations = 0

    def check(self):
        return self._health

    def list_owned(self, since=None):
        if self._explode:
            raise RuntimeError("the shop fell over")
        self.enumerations += 1
        return iter(self._items)


def item(key, artist, title, store="qobuz"):
    return OwnedItem(store=store, item_key=key, kind="track", title=title,
                     artist=artist, release_title="", purchased_at=1600000000)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for key, value in {
            "LW_CONFIG_DIR": str(self.tmp), "LW_MUSIC_DIR": str(self.tmp / "music"),
            "LW_LOG_LEVEL": "CRITICAL", "LW_RESCAN_CMD": "",
        }.items():
            os.environ[key] = value
        from libwish.settings import Settings
        from libwish.web.app import create_app
        self.app = create_app(Settings.from_env(), start_workers=False)
        self.svc = self.app.extensions["libwish"]
        self.enqueued = []
        # The sweep hands everything it recognises to the claim queue rather
        # than downloading anything itself, so the queue is where its decisions
        # are observable.
        self.svc.jobs.enqueue = lambda kind, **kw: (
            self.enqueued.append({"kind": kind, **kw}) or len(self.enqueued))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def a_track(self, artist, title):
        conn = self.svc.db()
        try:
            cur = conn.execute(
                "INSERT INTO tracks(artist, title, added_at, status)"
                " VALUES(?,?,0,'queued')", (artist, title))
            return cur.lastrowid
        finally:
            conn.close()

    def sweep(self, stores):
        from libwish.sync import SyncPipeline
        seen = []
        SyncPipeline(self.svc, {s.id: s for s in stores})(
            job=None, progress=lambda phase, **d: seen.append({"phase": phase, **d}))
        return {row["phase"]: row for row in seen}

    def status_of(self, track_id):
        return self.svc.tracks.get(track_id)["status"]


class Files(Base):
    def test_an_exact_purchase_is_queued_as_an_ordinary_claim(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        shop = FakeStore("qobuz", "Qobuz", [item("k1", "CHVRCHES", "Lies")])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 1)
        # A claim, not a private download path: the sweep must not become a
        # second way for a file to reach the library.
        self.assertEqual(self.enqueued,
                         [{"kind": "claim", "track_id": track_id, "provider_id": "qobuz"}])

    def test_each_shop_is_read_once_however_many_rows_match(self):
        for title in ("Lies", "Gun", "Recover"):
            self.a_track("CHVRCHES", title)
        shop = FakeStore("qobuz", "Qobuz", [
            item("k1", "CHVRCHES", "Lies"),
            item("k2", "CHVRCHES", "Gun"),
            item("k3", "CHVRCHES", "Recover"),
        ])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 3)
        # The whole reason this exists: claiming three tracks one at a time
        # reads the shop three times.
        self.assertEqual(shop.enumerations, 1)


class Restraint(Base):
    def test_a_near_miss_leaves_no_mark_on_the_track(self):
        track_id = self.a_track("Audioslave", "Like a Stone")
        shop = FakeStore("qobuz", "Qobuz", [item("k1", "CHVRCHES", "Lies")])
        out = self.sweep([shop])

        self.assertEqual(out["queue"]["queued"], 0)
        self.assertEqual(self.enqueued, [])
        # Still wanted, still unclaimed. A sweep marking this owned would take
        # it off the list, and nothing would ever say it was a guess.
        self.assertEqual(self.status_of(track_id), "queued")
        rows = self.svc.tracks.get(track_id)
        self.assertIsNone(rows["purchased_at"])

    def test_an_unrelated_purchase_is_not_called_a_near_miss(self):
        # A wanted track with nothing resembling it in the account. Reporting
        # that as "too close to call" is what the first live sweep did: four
        # unrelated purchases were each reported as nearly matching whichever
        # row happened to sort first, all at zero. "You have not bought this"
        # is not a near miss.
        self.a_track("Audioslave", "Like a Stone")
        shop = FakeStore("qobuz", "Qobuz", [item("k1", "CHVRCHES", "Lies")])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 0)
        self.assertEqual(out["queue"]["near"], [])

    def test_a_purchase_already_filed_is_not_filed_again(self):
        # The filed purchase belongs to a DIFFERENT track, and a second track
        # wanting the same recording is still on the list. Filing it against
        # the first track is what must not happen twice.
        #
        # Written this way because the obvious version marks the only wanted
        # track purchased, which takes it off the list entirely: the sweep then
        # queues nothing because there is nothing to queue, and the test passes
        # without the skip existing at all. Found by deleting the skip and
        # watching nothing fail.
        first = self.a_track("CHVRCHES", "Lies")
        self.svc.tracks.mark_purchased(first, "qobuz", "k1")
        self.a_track("CHVRCHES", "Lies")

        shop = FakeStore("qobuz", "Qobuz", [item("k1", "CHVRCHES", "Lies")])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 0, out["queue"])
        self.assertEqual(self.enqueued, [])

    def test_two_indistinguishable_purchases_are_refused_rather_than_guessed(self):
        self.a_track("CHVRCHES", "Lies")
        shop = FakeStore("qobuz", "Qobuz", [
            item("k1", "CHVRCHES", "Lies"),
            item("k2", "CHVRCHES", "Lies"),
        ])
        out = self.sweep([shop])
        # The matcher refuses a tie rather than picking the first, and a sweep
        # inherits that. Choosing one unattended is exactly the guess this is
        # built not to make.
        self.assertEqual(out["queue"]["queued"], 0)
        self.assertEqual(self.enqueued, [])

class TheOnlyVersionOfASong(Base):
    """A version qualifier is allowed to be the one thing that differs.

    "Gold Dust Woman" against "Gold Dust Woman (2004 Remaster)" is
    VERSION_MISMATCH, and a claim refuses it: a remaster is a different
    recording, and a person is there to be shown that and asked. A sweep has
    nobody to ask, so where the purchase is the only one the track could be it
    counts. What fences it is uniqueness in both directions, not a lower bar.
    """

    def test_the_only_version_in_the_account_is_filed(self):
        track_id = self.a_track("Fleetwood Mac", "Gold Dust Woman")
        shop = FakeStore("qobuz", "Qobuz",
                         [item("k1", "Fleetwood Mac", "Gold Dust Woman (2004 Remaster)")])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 1, out["queue"])
        self.assertEqual(self.enqueued,
                         [{"kind": "claim", "track_id": track_id, "provider_id": "qobuz"}])

    def test_the_audit_row_says_the_version_differed(self):
        # The reason this was accepted has to survive in the record, because
        # the file that arrives is not the recording the list asked for and
        # nobody was asked about it.
        track_id = self.a_track("Fleetwood Mac", "Gold Dust Woman")
        shop = FakeStore("qobuz", "Qobuz",
                         [item("k1", "Fleetwood Mac", "Gold Dust Woman (2004 Remaster)")])
        self.sweep([shop])
        conn = self.svc.db()
        try:
            row = conn.execute("SELECT outcome, reasons, candidate_json FROM match_decision"
                               " WHERE track_id=? ORDER BY id DESC LIMIT 1",
                               (track_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["outcome"], "swept")
        self.assertIn("version", row["reasons"])
        # The claim downloads what this row names rather than matching again,
        # so an audit row without the key would queue a claim that refuses.
        self.assertIn("k1", row["candidate_json"])

    def test_two_versions_of_the_same_song_are_refused(self):
        # The case the uniqueness fence exists for. Picking one unattended is
        # a guess, and a wrong one is silent: the track leaves the list and
        # the library gains a recording nobody chose.
        self.a_track("Fleetwood Mac", "Gold Dust Woman")
        shop = FakeStore("qobuz", "Qobuz", [
            item("k1", "Fleetwood Mac", "Gold Dust Woman (2004 Remaster)"),
            item("k2", "Fleetwood Mac", "Gold Dust Woman (Live)"),
        ])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 0, out["queue"])
        self.assertEqual(self.enqueued, [])
        self.assertEqual(len(out["queue"]["near"]), 1)

    def test_one_purchase_two_list_entries_wanting_it_is_refused(self):
        # The other direction of the same fence: the purchase is unique to
        # each track, but neither track is unique to the purchase.
        self.a_track("Fleetwood Mac", "Gold Dust Woman")
        self.a_track("Fleetwood Mac", "Gold Dust Woman (Remastered)")
        shop = FakeStore("qobuz", "Qobuz",
                         [item("k1", "Fleetwood Mac", "Gold Dust Woman (2004 Remaster)")])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 0, out["queue"])
        self.assertEqual(self.enqueued, [])

    def test_a_different_song_is_still_refused_however_alone_it_is(self):
        # Only the version qualifier is relaxed. The artist and title gates
        # still have to pass, or "the only purchase in the account" would
        # become a licence to file anything at all.
        self.a_track("Audioslave", "Like a Stone")
        shop = FakeStore("qobuz", "Qobuz", [item("k1", "CHVRCHES", "Lies")])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 0, out["queue"])
        self.assertEqual(self.enqueued, [])

    def test_an_exact_match_wins_the_purchase_before_a_version_variant_sees_it(self):
        # Written because the version pass runs second and could otherwise
        # spend a purchase that the ordinary pass was going to file exactly.
        exact = self.a_track("Fleetwood Mac", "Gold Dust Woman (2004 Remaster)")
        self.a_track("Fleetwood Mac", "Gold Dust Woman")
        shop = FakeStore("qobuz", "Qobuz",
                         [item("k1", "Fleetwood Mac", "Gold Dust Woman (2004 Remaster)")])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["queued"], 1, out["queue"])
        self.assertEqual([e["track_id"] for e in self.enqueued], [exact])


class PurchasesOnNoList(Base):
    """A recent purchase matching nothing on the list is taken in anyway.

    The anchor is the newest purchase already filed at that shop. A shop lists
    newest first, so everything above that row arrived since the last time this
    caught up, and everything below it is old buying that was deliberately
    never on the list.

    The live account is the shape to keep in mind: three purchases nothing on
    the want list matched, one of them newer than anything filed and two of
    them older, with the two older ones already in the library by other means.
    Taking all three would have been wrong.
    """

    def filed(self, artist, title, key, store="qobuz"):
        """A track already owned, filed against a purchase at that shop."""
        track_id = self.a_track(artist, title)
        self.svc.tracks.mark_purchased(track_id, store, key)
        return track_id

    def test_a_purchase_newer_than_the_last_filed_one_is_taken(self):
        self.filed("Percy Sledge", "Love Me Tender", "68907204/2")
        shop = FakeStore("qobuz", "Qobuz", [
            item("69004497/1", "Queens Of The Stone Age", "First It Giveth"),
            item("68907204/2", "Percy Sledge", "Love Me Tender"),
        ])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["adopted"], 1)
        self.assertEqual([n["title"] for n in out["queue"]["new"]], ["First It Giveth"])

        # It becomes an ordinary claim, like everything else this queues. The
        # download, the verification and the audit row are the ones that exist.
        self.assertEqual([j["kind"] for j in self.enqueued], ["claim"])

    def test_a_purchase_older_than_the_last_filed_one_is_left(self):
        self.filed("Percy Sledge", "Love Me Tender", "68907204/2")
        shop = FakeStore("qobuz", "Qobuz", [
            item("68907204/2", "Percy Sledge", "Love Me Tender"),
            item("68471357/1", "CHVRCHES", "Lies"),
            item("68261774/1", "Audioslave", "Shadow On The Sun"),
        ])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["adopted"], 0)
        self.assertEqual(self.enqueued, [])

    def test_the_row_it_creates_is_the_purchase_as_the_shop_names_it(self):
        self.filed("Percy Sledge", "Love Me Tender", "68907204/2")
        shop = FakeStore("qobuz", "Qobuz", [
            item("69004497/1", "Queens Of The Stone Age", "First It Giveth"),
            item("68907204/2", "Percy Sledge", "Love Me Tender"),
        ])
        self.sweep([shop])
        titles = {t["title"] for t in self.svc.tracks.queued()}
        self.assertIn("First It Giveth", titles)

    def test_a_shop_with_nothing_filed_yet_adopts_nothing(self):
        # No filed row means no anchor, so every purchase in the account looks
        # new. Taking a whole back catalogue is not what "since the last one"
        # means, and it would be a lot of downloads to discover that.
        shop = FakeStore("qobuz", "Qobuz", [
            item("69004497/1", "Queens Of The Stone Age", "First It Giveth"),
            item("68471357/1", "CHVRCHES", "Lies"),
        ])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["adopted"], 0)

    def test_a_purchase_that_matched_a_wanted_track_is_not_taken_twice(self):
        self.filed("Percy Sledge", "Love Me Tender", "68907204/2")
        wanted = self.a_track("Queens Of The Stone Age", "First It Giveth")
        shop = FakeStore("qobuz", "Qobuz", [
            item("69004497/1", "Queens Of The Stone Age", "First It Giveth"),
            item("68907204/2", "Percy Sledge", "Love Me Tender"),
        ])
        out = self.sweep([shop])
        self.assertEqual((out["queue"]["queued"], out["queue"]["adopted"]), (1, 0))
        self.assertEqual([j["track_id"] for j in self.enqueued], [wanted])

    def test_an_ignored_track_is_not_dragged_back_by_its_purchase(self):
        # Ignoring is a decision. Downloading it anyway because the shop still
        # has it would overrule a person with a rule.
        self.filed("Percy Sledge", "Love Me Tender", "68907204/2")
        ignored = self.a_track("Bastille", "Happier")
        self.svc.tracks.set_status(ignored, "ignored")
        from libwish import identity
        conn = self.svc.db()
        try:
            identity.recompute_identity_columns(conn)
            conn.commit()
        finally:
            conn.close()

        shop = FakeStore("qobuz", "Qobuz", [
            item("69004497/1", "Bastille", "Happier"),
            item("68907204/2", "Percy Sledge", "Love Me Tender"),
        ])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["adopted"], 0)
        self.assertEqual(self.status_of(ignored), "ignored")

    def test_more_than_one_sweep_will_take_is_reported_not_swallowed(self):
        from libwish import sync
        self.filed("Percy Sledge", "Love Me Tender", "old/1")
        many = [item(f"new/{n}", "Artist %d" % n, "Track %d" % n)
                for n in range(sync.ADOPT_LIMIT + 3)]
        shop = FakeStore("qobuz", "Qobuz", many + [item("old/1", "Percy Sledge",
                                                        "Love Me Tender")])
        out = self.sweep([shop])
        self.assertEqual(out["queue"]["adopted"], sync.ADOPT_LIMIT)
        self.assertEqual(out["queue"]["over_cap"], 3)

    def test_it_files_nothing_by_itself(self):
        # The sweep queues claims and nothing else. A track is owned when a
        # file is in the library, which is the claim pipeline's to say.
        self.filed("Percy Sledge", "Love Me Tender", "68907204/2")
        shop = FakeStore("qobuz", "Qobuz", [
            item("69004497/1", "Queens Of The Stone Age", "First It Giveth"),
            item("68907204/2", "Percy Sledge", "Love Me Tender"),
        ])
        self.sweep([shop])
        new = [t for t in self.svc.tracks.queued() if t["title"] == "First It Giveth"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["status"], "queued")


class OneShopDown(Base):
    def test_a_signed_out_shop_does_not_sink_the_others(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        good = FakeStore("qobuz", "Qobuz", [item("k1", "CHVRCHES", "Lies")])
        bad = FakeStore("bandcamp", "Bandcamp", [], health=FakeHealth(ok=True, authed=False))
        out = self.sweep([good, bad])

        self.assertEqual(out["queue"]["queued"], 1)
        self.assertEqual(self.enqueued[0]["track_id"], track_id)
        # Named, not swallowed: a shop that was skipped is the reason a
        # purchase the reader is looking for did not turn up.
        skipped = out["queue"]["shops_skipped"]
        self.assertEqual([s["shop"] for s in skipped], ["Bandcamp"])
        self.assertIn("signed out", skipped[0]["why"])

    def test_a_shop_that_throws_is_reported_rather_than_crashing_the_sweep(self):
        self.a_track("CHVRCHES", "Lies")
        good = FakeStore("qobuz", "Qobuz", [item("k1", "CHVRCHES", "Lies")])
        bad = FakeStore("bandcamp", "Bandcamp", [], explode=True)
        out = self.sweep([good, bad])
        self.assertEqual(out["queue"]["queued"], 1)
        self.assertEqual([s["shop"] for s in out["queue"]["shops_skipped"]], ["Bandcamp"])

    def test_every_shop_down_is_an_error_rather_than_a_quiet_success(self):
        from libwish.errors import LibwishError
        self.a_track("CHVRCHES", "Lies")
        bad = FakeStore("qobuz", "Qobuz", [], health=FakeHealth(ok=True, authed=False))
        # "Nothing to file" and "we could not look" are different answers, and
        # reporting the first for the second is the failure worth guarding.
        with self.assertRaises(LibwishError):
            self.sweep([bad])


if __name__ == "__main__":
    unittest.main()
