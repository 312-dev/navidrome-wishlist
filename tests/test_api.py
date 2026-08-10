"""The JSON API, over the real schema and a copy of the real queue.

These exercise the parts of the API a browser depends on being cheap: paging so
that opening the queue does not ship 160 rows, and covers served off local disk
so that opening it does not fan out into 160 requests to Deezer. Both are
easily broken in ways that still look like a working page, so the assertions
are about the mechanism and not only the status code.

The snapshot is copied per test. Nothing here may touch the original.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO / ".testdata" / "queue.db"

PNG = bytes.fromhex("89504e470d0a1a0a") + b"a cached cover, near enough for a byte compare"


class StubStore:
    """A store that only has to answer `buy_url`, which is all buy links need."""

    def __init__(self, store_id="bandcamp", name="Bandcamp"):
        self.id = store_id
        self.name = name

    def buy_url(self, q):
        return f"https://example.invalid/search?q={q.artist} {q.title}"


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
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def a_track(self, artist="CHVRCHES", title="Lies", bandcamp_url=None):
        conn = self.svc.db()
        try:
            cur = conn.execute(
                "INSERT INTO tracks(artist, title, added_at, status, bandcamp_url)"
                " VALUES(?,?,0,'queued',?)", (artist, title, bandcamp_url))
            return cur.lastrowid
        finally:
            conn.close()

    def rows(self, sql, args=()):
        conn = self.svc.db()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def cache_a_cover(self, track_id, suffix=".png", body=PNG):
        directory = self.tmp / "covers"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{track_id}{suffix}"
        path.write_bytes(body)
        return path


class Pagination(Base):
    def test_the_default_page_is_one_screenful_of_a_long_queue(self):
        from libwish.web.api import DEFAULT_LIMIT

        response = self.client.get("/api/queue")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        total = len(self.svc.tracks.queued())
        self.assertGreater(total, DEFAULT_LIMIT, "the snapshot has to be longer than one page")
        self.assertEqual(len(body), DEFAULT_LIMIT)
        self.assertEqual(response.headers["X-Total-Count"], str(total),
                         "the header is how the interface knows there is more")

    def test_the_body_stays_a_bare_array(self):
        self.assertIsInstance(self.client.get("/api/queue").get_json(), list)

    def test_offset_walks_the_same_order_without_repeating_a_row(self):
        everything = [row["id"] for row in self.svc.tracks.queued()]
        first = [row["id"] for row in self.client.get("/api/queue?limit=5").get_json()]
        second = [row["id"] for row in self.client.get("/api/queue?limit=5&offset=5").get_json()]
        self.assertEqual(first, everything[:5])
        self.assertEqual(second, everything[5:10])
        self.assertEqual(set(first) & set(second), set(), "a page must not repeat the last one")

    def test_an_offset_past_the_end_is_empty_and_still_counts(self):
        response = self.client.get("/api/queue?limit=10&offset=100000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])
        self.assertEqual(response.headers["X-Total-Count"], str(len(self.svc.tracks.queued())))

    def test_an_oversized_limit_is_clamped_rather_than_honoured(self):
        from libwish.web.api import MAX_LIMIT

        response = self.client.get("/api/queue?limit=100000")
        self.assertEqual(response.status_code, 200)
        total = len(self.svc.tracks.queued())
        self.assertEqual(len(response.get_json()), min(MAX_LIMIT, total))

    def test_junk_paging_is_refused_rather_than_guessed_at(self):
        for query in ("limit=abc", "limit=-1", "limit=1.5", "offset=-3", "offset=nine",
                      "limit=٩", "limit=one&offset=two"):
            with self.subTest(query=query):
                response = self.client.get(f"/api/queue?{query}")
                self.assertEqual(response.status_code, 400, query)
                self.assertIn("error", response.get_json())

    def test_zero_is_a_valid_limit_and_not_junk(self):
        response = self.client.get("/api/queue?limit=0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])
        self.assertEqual(response.headers["X-Total-Count"], str(len(self.svc.tracks.queued())))

    def test_every_list_route_pages_the_same_way(self):
        for path, rows in (("/api/queue", self.svc.tracks.queued()),
                           ("/api/ignored", self.svc.tracks.ignored()),
                           ("/api/owned", self.svc.tracks.owned())):
            with self.subTest(path=path):
                response = self.client.get(f"{path}?limit=1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["X-Total-Count"], str(len(rows)))
                self.assertLessEqual(len(response.get_json()), 1)
                self.assertEqual(self.client.get(f"{path}?limit=x").status_code, 400)


class Covers(Base):
    def fetch(self, path, **kwargs):
        """A cover response, closed at teardown.

        A served file keeps its handle open until the response is closed, and a
        test that leaves them open reports a ResourceWarning per request rather
        than a result.
        """
        response = self.client.get(path, **kwargs)
        self.addCleanup(response.close)
        return response

    def test_a_track_with_nothing_cached_is_a_404(self):
        track_id = self.a_track()
        response = self.fetch(f"/api/cover/{track_id}")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())

    def test_a_missing_cover_is_never_a_redirect_to_the_origin(self):
        """The cache exists so the browser stops talking to Deezer.

        A redirect on a cache miss would send it there anyway, which is the one
        outcome caching was supposed to prevent, and it would do it while
        reporting success.
        """
        track_id = self.a_track()
        response = self.fetch(f"/api/cover/{track_id}")
        self.assertNotIn(response.status_code, (301, 302, 303, 307, 308))
        self.assertIsNone(response.headers.get("Location"))

    def test_a_cached_cover_is_served_from_disk(self):
        track_id = self.a_track()
        self.cache_a_cover(track_id)
        response = self.fetch(f"/api/cover/{track_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PNG)
        self.assertEqual(response.headers["Content-Type"], "image/png")

    def test_a_cached_cover_carries_a_long_lifetime_and_an_etag(self):
        track_id = self.a_track()
        self.cache_a_cover(track_id)
        response = self.fetch(f"/api/cover/{track_id}")
        self.assertIn("max-age=", response.headers["Cache-Control"])
        self.assertGreaterEqual(response.cache_control.max_age, 86_400)
        self.assertTrue(response.headers.get("ETag"))

    def test_a_browser_that_already_has_it_gets_a_304(self):
        track_id = self.a_track()
        self.cache_a_cover(track_id)
        etag = self.fetch(f"/api/cover/{track_id}").headers["ETag"]
        again = self.fetch(f"/api/cover/{track_id}", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.data, b"")

    def test_a_jpeg_is_served_as_a_jpeg(self):
        track_id = self.a_track()
        self.cache_a_cover(track_id, suffix=".jpg", body=b"\xff\xd8\xff not really a jpeg")
        response = self.fetch(f"/api/cover/{track_id}")
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")


class BulkConfirm(Base):
    def decisions(self, track_id):
        return self.rows("SELECT * FROM match_decision WHERE track_id=?", (track_id,))

    def claim_jobs(self, track_id):
        return self.rows("SELECT * FROM jobs WHERE kind='claim' AND track_id=?", (track_id,))

    def test_one_audit_row_and_one_job_per_track(self):
        ids = [self.a_track("A", "one"), self.a_track("B", "two"), self.a_track("C", "three")]
        response = self.client.post("/api/claim/confirm",
                                    json={"track_ids": ids, "store": "qobuz"})
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["confirmed"], 3)
        self.assertEqual(len(body["job_ids"]), 3)
        self.assertEqual(len(set(body["job_ids"])), 3, "each track gets its own claim")
        self.assertFalse(body["truncated"])
        for track_id in ids:
            with self.subTest(track=track_id):
                rows = self.decisions(track_id)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["outcome"], "user_confirmed")
                self.assertEqual(rows[0]["provider"], "qobuz")
                self.assertEqual(rows[0]["phase"], "confirm")
                self.assertEqual(len(self.claim_jobs(track_id)), 1)

    def test_the_audit_row_records_what_was_confirmed(self):
        track_id = self.a_track("Lil Peep", "Falling Down")
        self.client.post("/api/claim/confirm", json={"track_ids": [track_id]})
        row = self.decisions(track_id)[0]
        self.assertEqual(json.loads(row["query_json"]),
                         {"artist": "Lil Peep", "title": "Falling Down"})
        self.assertTrue(row["reasons"], "a decision with no reason is not an audit trail")
        self.assertTrue(row["matcher_version"])
        self.assertTrue(row["lexicon_hash"])

    def test_past_the_cap_the_response_says_so(self):
        from libwish.web.api import MAX_CONFIRM_BATCH

        ids = [self.a_track("Artist", f"song {n}") for n in range(MAX_CONFIRM_BATCH + 5)]
        body = self.client.post("/api/claim/confirm", json={"track_ids": ids}).get_json()
        self.assertTrue(body["truncated"], "a silently truncated bulk action reads as success")
        self.assertEqual(body["confirmed"], MAX_CONFIRM_BATCH)
        self.assertEqual(body["requested"], MAX_CONFIRM_BATCH + 5)
        self.assertEqual(body["cap"], MAX_CONFIRM_BATCH)
        self.assertIn(str(MAX_CONFIRM_BATCH), body["msg"])
        written = self.rows("SELECT COUNT(*) AS n FROM match_decision")[0]["n"]
        self.assertEqual(written, MAX_CONFIRM_BATCH)
        self.assertEqual(self.decisions(ids[-1]), [], "the tail was reported, not confirmed")

    def test_an_unknown_id_fails_the_whole_request(self):
        track_id = self.a_track()
        response = self.client.post("/api/claim/confirm",
                                    json={"track_ids": [track_id, 999999]})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["unknown"], [999999])
        self.assertEqual(self.decisions(track_id), [],
                         "a rejected batch must not leave half an audit trail")
        self.assertEqual(self.claim_jobs(track_id), [])

    def test_a_repeated_id_is_confirmed_once(self):
        track_id = self.a_track()
        body = self.client.post("/api/claim/confirm",
                                json={"track_ids": [track_id, track_id]}).get_json()
        self.assertEqual(body["confirmed"], 1)
        self.assertEqual(len(self.decisions(track_id)), 1)

    def test_junk_bodies_are_refused(self):
        for payload in ({}, {"track_ids": []}, {"track_ids": "1,2"},
                        {"track_ids": ["1"]}, {"track_ids": [None]}, {"track_ids": [True]},
                        {"track_ids": {"a": 1}}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/claim/confirm", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM match_decision")[0]["n"], 0)

    def test_the_single_track_route_still_writes_the_same_row(self):
        """The bulk route is the same act repeated, so the rows must match."""
        one = self.a_track("A", "single")
        many = self.a_track("B", "bulk")
        self.client.post(f"/api/claim/{one}/confirm", json={"store": "qobuz"})
        self.client.post("/api/claim/confirm", json={"track_ids": [many], "store": "qobuz"})
        shared = ("phase", "provider", "outcome", "candidates_considered", "chosen_store_id",
                  "matcher_version", "lexicon_hash")
        single_row, bulk_row = self.decisions(one)[0], self.decisions(many)[0]
        for column in shared:
            with self.subTest(column=column):
                self.assertEqual(single_row[column], bulk_row[column])


class BuyLinks(Base):
    def setUp(self):
        super().setUp()
        # Wiring real stores needs credentials this test has no business
        # holding, and the link logic under test is the API's, not the store's.
        self.svc.stores = {"bandcamp": StubStore()}

    def link(self, track_id, store="bandcamp"):
        body = self.client.get(f"/api/buy/{track_id}").get_json()
        return next(l for l in body["links"] if l["store"] == store)

    def test_a_resolved_link_is_used_instead_of_a_search(self):
        url = "https://chvrches.bandcamp.com/track/lies"
        track_id = self.a_track("CHVRCHES", "Lies", bandcamp_url=url)
        link = self.link(track_id)
        self.assertEqual(link["url"], url)
        self.assertTrue(link["direct"])

    def test_a_track_without_one_falls_back_to_search(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        link = self.link(track_id)
        self.assertIn("search", link["url"])
        self.assertFalse(link["direct"])

    def test_an_empty_stored_link_is_not_a_link(self):
        track_id = self.a_track("CHVRCHES", "Lies", bandcamp_url="   ")
        link = self.link(track_id)
        self.assertFalse(link["direct"])
        self.assertIn("search", link["url"])

    def test_the_response_distinguishes_the_two(self):
        direct = self.a_track("A", "direct", bandcamp_url="https://a.bandcamp.com/track/x")
        searched = self.a_track("B", "searched")
        self.assertNotEqual(self.link(direct)["direct"], self.link(searched)["direct"])

    def test_a_resolved_link_is_what_the_redirect_sends_you_to(self):
        url = "https://chvrches.bandcamp.com/track/lies"
        track_id = self.a_track("CHVRCHES", "Lies", bandcamp_url=url)
        response = self.client.get(f"/api/buy/{track_id}?store=bandcamp")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], url)

    def test_the_stored_link_belongs_to_bandcamp_alone(self):
        """Another store's link is its own; the column names the one it came from."""
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        track_id = self.a_track("A", "b", bandcamp_url="https://a.bandcamp.com/track/x")
        self.assertFalse(self.link(track_id, store="qobuz")["direct"])


class UnknownTracks(Base):
    def test_every_route_answers_404_for_an_id_that_is_not_there(self):
        for method, path in (("get", "/api/track/999999"),
                             ("get", "/api/buy/999999"),
                             ("get", "/api/preview/999999"),
                             ("get", "/api/cover/999999"),
                             ("get", "/api/search/999999"),
                             ("post", "/api/claim/999999"),
                             ("post", "/api/claim/999999/confirm"),
                             ("post", "/api/ignore/999999"),
                             ("post", "/api/restore/999999")):
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)(path).status_code, 404)

    def test_a_cover_id_that_is_not_a_number_is_a_404_the_interface_can_read(self):
        response = self.client.get("/api/cover/nonsense")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())


class WhichStore(Base):
    """That the store the reader picked survives the trip to the queue.

    Losing it is silent and total: the job is enqueued, reports itself started,
    and is then refused for naming no store, so the page shows a claim that
    failed for a reason the reader had already answered.
    """

    def claim_job(self, track_id):
        rows = self.rows("SELECT * FROM jobs WHERE kind='claim' AND track_id=?", (track_id,))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_a_form_post_names_the_store_as_well_as_a_json_one(self):
        # htmx form-encodes by default, so this is what the page actually
        # sends. Reading only a JSON body drops it on the floor.
        track_id = self.a_track()
        response = self.client.post(f"/api/claim/{track_id}", data={"store": "qobuz"})
        self.assertEqual(response.status_code, 202)
        self.assertIn("qobuz", json.dumps(self.claim_job(track_id)))

    def test_json_still_works(self):
        track_id = self.a_track()
        self.client.post(f"/api/claim/{track_id}", json={"store": "bandcamp"})
        self.assertIn("bandcamp", json.dumps(self.claim_job(track_id)))

    def test_a_buy_link_that_names_a_store_is_a_redirect_not_a_document(self):
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz"),
                           "bandcamp": StubStore("bandcamp", "Bandcamp")}
        response = self.client.get(f"/api/buy/{track_id}?store=qobuz")
        self.assertEqual(response.status_code, 302)
        self.assertIn("example.invalid", response.headers["Location"])

    def test_a_bare_buy_link_is_what_put_the_reader_on_a_page_of_json(self):
        # Kept as documentation of the endpoint's contract rather than changed:
        # with two stores configured there is no single right answer, so the
        # list is correct here and it is the row template's job to name one.
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        response = self.client.get(f"/api/buy/{track_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("links", response.get_json())

    def test_the_row_offers_every_store_when_none_is_assigned(self):
        from libwish.web.views import view_track

        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz"),
                           "bandcamp": StubStore("bandcamp", "Bandcamp")}
        with self.app.test_request_context("/"):
            row = view_track({"id": 1, "artist": "A", "title": "t", "status": "queued"}, "artist")
        self.assertEqual([s["id"] for s in row["stores"]], ["bandcamp", "qobuz"])
        # Nothing assigned is not the same fact as nowhere to buy, and a row
        # that can be bought can be selected for a bulk claim.
        self.assertTrue(row["selectable"])

    def test_a_wanted_row_draws_no_plate_but_keeps_its_anchor(self):
        """Nothing is drawn, and the element the live connection needs stays.

        The swap that turns a wanted row into a claiming one finds the row by
        the plate's id and reads the state it is coming from off the same
        element. Dropping the markup entirely would leave a claim with nowhere
        to appear, and would do it only on the rows a claim starts from.
        """
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz"),
                           "bandcamp": StubStore("bandcamp", "Bandcamp")}
        html = self.client.get(f"/ui/plate/{track_id}").get_data(as_text=True)
        self.assertIn(f'id="plate-{track_id}"', html)
        self.assertIn('data-state="wanted"', html)
        self.assertIn("hidden", html)
        self.assertNotIn("WANTED", html)
        self.assertNotIn("plate__line", html)

    def test_a_state_worth_reading_still_draws_one(self):
        track_id = self.a_track()
        self.svc.stores = {}
        html = self.client.get(f"/ui/plate/{track_id}").get_data(as_text=True)
        self.assertIn("NO STORE", html)


class CookieSession(unittest.TestCase):
    """That a store authenticating with a browser session gets one.

    The failure this covers is silent and total. Both halves existed and were
    tested on their own: the broker took a jar from the extension, and the
    store asked its credential handle for a client wired to one. Nothing joined
    them in the application, so every request went out with no cookies and the
    first claim died reporting an expired login for a session that was alive.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.jar = self.tmp / "qobuz_jar.json"
        self.jar.write_text(json.dumps({
            "cookies": {"php_session": "seeded-by-the-extension", "user_id": "42"},
            "meta": {},
        }))
        # Restored rather than left set. This class points the app at a
        # temporary directory that is deleted on teardown, and a later test
        # inheriting those paths looks for a database that has been removed.
        changed = {
            "LW_CONFIG_DIR": str(self.tmp), "LW_MUSIC_DIR": str(self.tmp / "music"),
            "LW_DB_PATH": str(self.tmp / "t.db"), "LW_LOG_LEVEL": "CRITICAL",
            "LW_STORE_QOBUZ_JAR_PATH": str(self.jar),
        }
        before = {key: os.environ.get(key) for key in changed}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) if v is not None
                                 else os.environ.pop(k, None)
                                 for k, v in before.items()])
        os.environ.update(changed)
        from libwish.settings import Settings
        from libwish.web.app import create_app
        self.svc = create_app(Settings.from_env(), start_workers=False).extensions["libwish"]

    def test_the_store_is_handed_the_live_session(self):
        client = self.svc.stores["qobuz"].ctx.creds.http_client()
        # `is not None`, never truthiness: an empty jar is falsy, so a plain
        # `if client.cookie_jar` reports a correctly wired client as broken.
        self.assertIsNotNone(client.cookie_jar, "the store would send no cookies at all")

    def test_a_jar_seeded_before_this_version_is_read_as_it_stands(self):
        client = self.svc.stores["qobuz"].ctx.creds.http_client()
        self.assertEqual(sorted(c.name for c in client.cookie_jar),
                         ["php_session", "user_id"])
        self.assertEqual({c.domain for c in client.cookie_jar}, {"www.qobuz.com"})

    def test_the_keeper_owns_the_path_the_extension_writes_to(self):
        self.assertEqual(self.svc.keepers["qobuz"].cfg.jar_path, str(self.jar))


class TabCounts(Base):
    """The numbers on the tabs, and that they follow a track that moves."""

    def counts(self):
        response = self.client.get("/api/counts")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_every_tab_is_counted(self):
        self.assertEqual(set(self.counts()), {"wanted", "owned", "ignored"})

    def test_ignoring_a_track_moves_it_between_two_of_them(self):
        track_id = self.a_track()
        before = self.counts()
        self.client.post(f"/api/ignore/{track_id}")
        after = self.counts()
        self.assertEqual(after["wanted"], before["wanted"] - 1)
        self.assertEqual(after["ignored"], before["ignored"] + 1)
        self.client.post(f"/api/restore/{track_id}")
        self.assertEqual(self.counts(), before)

    def test_a_purchase_counts_as_owned(self):
        track_id = self.a_track()
        before = self.counts()
        self.svc.tracks.mark_purchased(track_id, "qobuz")
        after = self.counts()
        self.assertEqual(after["owned"], before["owned"] + 1)
        self.assertEqual(after["wanted"], before["wanted"] - 1)

    def test_the_tabs_and_the_lists_under_them_agree(self):
        """One table decides both, so a track can never be counted in a tab it
        cannot be found in."""
        counts = self.counts()
        for view, path in (("wanted", "/api/queue"), ("owned", "/api/owned"),
                           ("ignored", "/api/ignored")):
            with self.subTest(view=view):
                listed = self.client.get(f"{path}?limit=0").headers["X-Total-Count"]
                self.assertEqual(counts[view], int(listed))


class Installable(Base):
    """What has to hold for the app to install and to survive a bad deploy.

    Both failures these cover are silent. A manifest naming an icon that is not
    there installs with a blank tile, and a precache list with one dead URL
    fails `addAll` as a whole, which leaves the worker never installed and the
    page working perfectly until the day the network is gone.
    """

    def manifest(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        return response, json.loads(response.get_data(as_text=True))

    def test_the_manifest_is_served_as_a_manifest(self):
        response, _ = self.manifest()
        self.assertEqual(response.mimetype, "application/manifest+json")

    def test_it_carries_what_a_browser_needs_to_offer_an_install(self):
        _, manifest = self.manifest()
        for key in ("name", "short_name", "start_url", "scope", "display", "icons"):
            with self.subTest(key=key):
                self.assertTrue(manifest.get(key), f"manifest is missing {key}")
        self.assertIn(manifest["display"], ("standalone", "fullscreen", "minimal-ui"))

    def test_android_gets_both_a_plain_icon_and_a_maskable_one(self):
        _, manifest = self.manifest()
        by_purpose = {(i["sizes"], i.get("purpose", "any")) for i in manifest["icons"]}
        self.assertIn(("192x192", "any"), by_purpose)
        self.assertIn(("512x512", "any"), by_purpose)
        # Separate files, never one declared "any maskable": the padding a mask
        # needs is dead space everywhere else.
        self.assertIn(("512x512", "maskable"), by_purpose)

    def test_every_icon_the_manifest_names_is_actually_there(self):
        _, manifest = self.manifest()
        srcs = [i["src"] for i in manifest["icons"]]
        srcs += [i["src"] for s in manifest.get("shortcuts", []) for i in s.get("icons", [])]
        for src in srcs:
            with self.subTest(src=src):
                self.assertEqual(self.client.get(src).status_code, 200)

    def worker(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        return response, response.get_data(as_text=True)

    def test_the_worker_is_served_from_the_root(self):
        response, _ = self.worker()
        # Not from /static: a worker controls the path it was served from and
        # below, so one under /static could never answer a navigation.
        self.assertIn("javascript", response.mimetype)

    def test_nothing_it_precaches_is_missing(self):
        _, body = self.worker()
        block = body.split("const PRECACHE = [", 1)[1].split("];", 1)[0]
        urls = re.findall(r"'([^']+)'", block)
        self.assertTrue(urls, "the worker precaches nothing at all")
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_it_leaves_the_event_stream_alone(self):
        from libwish.web.views import API

        _, body = self.worker()
        self.assertIn(API["events"], body)

    def test_editing_any_shell_file_changes_the_cache_key(self):
        """The one that bites once a worker is answering from cache first.

        A key that fingerprints the stylesheet alone leaves a JavaScript-only
        change addressed by the same URL as the code it replaces, so a browser
        that has already installed keeps serving the old file out of its cache
        indefinitely, and does it while reporting a successful deploy.
        """
        from unittest import mock

        from libwish.web import views

        with tempfile.TemporaryDirectory() as tmp:
            static = Path(tmp)
            for name in views.SHELL_ASSETS:
                path = static / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"one")
            with mock.patch.object(views, "STATIC_DIR", static):
                before = views._asset_version()
                for name in views.SHELL_ASSETS:
                    with self.subTest(changed=name):
                        (static / name).write_bytes(b"two")
                        self.assertNotEqual(views._asset_version(), before,
                                            f"a change to {name} left the key alone")
                        (static / name).write_bytes(b"one")
                self.assertEqual(views._asset_version(), before,
                                 "an unchanged deploy must not expire every cache")


if __name__ == "__main__":
    unittest.main()
