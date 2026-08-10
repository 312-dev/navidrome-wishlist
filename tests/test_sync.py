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

    def test_a_near_miss_is_reported_rather_than_silently_dropped(self):
        # Unconditional. This assertion was once written as `if near:`, which
        # passes when the list is empty, i.e. exactly when the behaviour it
        # claims to check is absent.
        self.a_track("Audioslave", "Like a Stone")
        shop = FakeStore("qobuz", "Qobuz", [item("k1", "CHVRCHES", "Lies")])
        out = self.sweep([shop])
        near = out["queue"]["near"]
        self.assertEqual(len(near), 1, near)
        self.assertEqual(near[0]["shop"], "Qobuz")
        self.assertEqual(near[0]["purchase"], "Lies")
        self.assertEqual(near[0]["needs"], MATCH_CONFIRM_MIN)

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

    def test_one_purchase_cannot_be_spent_on_two_tracks(self):
        self.a_track("CHVRCHES", "Lies")
        shop = FakeStore("qobuz", "Qobuz", [
            item("k1", "CHVRCHES", "Lies"),
            item("k2", "CHVRCHES", "Lies"),
        ])
        out = self.sweep([shop])
        # Two claims for one row would race each other to the same file.
        self.assertEqual(out["queue"]["queued"], 1)
        self.assertEqual(len(self.enqueued), 1)


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
