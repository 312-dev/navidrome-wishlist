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
        self.assertEqual(response.headers["Location"], f"{url}#lw={track_id}")

    def test_the_stored_link_belongs_to_bandcamp_alone(self):
        """Another store's link is its own; the column names the one it came from."""
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        track_id = self.a_track("A", "b", bandcamp_url="https://a.bandcamp.com/track/x")
        self.assertFalse(self.link(track_id, store="qobuz")["direct"])


class BuyFragment(Base):
    """The `lw=<id>` marker the redirect carries, for the extension that reads
    it back off the store's page with `URLSearchParams`.
    """

    def setUp(self):
        super().setUp()
        self.svc.stores = {"bandcamp": StubStore()}

    def test_the_redirect_names_the_track_in_the_fragment(self):
        track_id = self.a_track()
        response = self.client.get(f"/api/buy/{track_id}?store=bandcamp")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"#lw={track_id}"))

    def test_a_store_url_with_its_own_fragment_gets_the_marker_appended(self):
        url = "https://chvrches.bandcamp.com/track/lies#player"
        track_id = self.a_track("CHVRCHES", "Lies", bandcamp_url=url)
        response = self.client.get(f"/api/buy/{track_id}?store=bandcamp")
        self.assertEqual(response.headers["Location"], f"{url}&lw={track_id}")

    def test_the_bare_link_list_carries_no_fragment(self):
        # The marker is only for the redirect; the list is a page of JSON with
        # nowhere for a fragment to go, and no store lookup runs to build one.
        track_id = self.a_track()
        response = self.client.get(f"/api/buy/{track_id}")
        self.assertEqual(response.status_code, 200)
        link = next(l for l in response.get_json()["links"] if l["store"] == "bandcamp")
        self.assertNotIn("lw=", link["url"])


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

    def refuse_at(self, track_id, store, score=10):
        """Leave a track the way a real refusal does: a failed claim job
        naming the store it tried, and the match_decision row the refusal
        panel reads. Both are needed, the same way the live incident that
        motivated this fix had both."""
        import time
        from libwish import identity, match

        conn = self.svc.db()
        try:
            conn.execute(
                "INSERT INTO jobs(kind, state, track_id, provider_id, created_at)"
                " VALUES('claim','failed',?,?,?)",
                (track_id, store, int(time.time())))
            conn.execute(
                "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
                " matcher_version, lexicon_hash, outcome, score, reasons,"
                " query_json, candidates_considered)"
                " VALUES(?,?,'claim',?,?,?,'refused',?,'[]','{}',0)",
                (track_id, int(time.time()), store,
                 getattr(match, "MATCHER_VERSION", "1"), identity.lexicon_hash(), score))
        finally:
            conn.close()

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

    def test_a_track_with_no_claim_yet_still_offers_every_store(self):
        # The same fact as the test above, through the real query rather than
        # a hand-built dict: a track no claim has ever touched must not pick
        # up a store from the correlated subquery in repo.py.
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz"),
                           "bandcamp": StubStore("bandcamp", "Bandcamp")}
        row = self.svc.tracks.get(track_id)
        self.assertIsNone(row["last_claim_store"])
        with self.app.test_request_context("/"):
            from libwish.web.views import view_track
            decorated = view_track(row, "artist")
        self.assertEqual([s["id"] for s in decorated["stores"]], ["bandcamp", "qobuz"])

    def test_a_refused_row_carries_the_store_of_its_last_claim(self):
        # The bug: a retry that named no store, because the row's assigned
        # store was read off `chosen_source` (the loved-from platform, never
        # a store) or a `store` column that does not exist. The fact is on
        # the jobs table instead.
        from libwish.web.views import _decorate

        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz"),
                           "bandcamp": StubStore("bandcamp", "Bandcamp")}
        self.refuse_at(track_id, "qobuz")
        with self.app.test_request_context("/"):
            row = _decorate([self.svc.tracks.get(track_id)], "artist")[0]
        self.assertEqual(row["state"], "refused")
        self.assertEqual(row["store_id"], "qobuz")
        # Refused at one store narrows the row to that store, same as a row
        # the reader assigned by hand: it was refused at a particular shop,
        # and that is where the retry has to go.
        self.assertEqual([s["id"] for s in row["stores"]], ["qobuz"])

    def test_the_retry_button_carries_the_store_it_was_refused_at(self):
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz"),
                           "bandcamp": StubStore("bandcamp", "Bandcamp")}
        self.refuse_at(track_id, "qobuz")
        html = self.client.get(f"/ui/row/{track_id}").get_data(as_text=True)
        # Alpine's `store` is seeded from the row's own scope, and the button
        # sends it back on the retry the same way "I bought it" already did.
        self.assertIn("store: 'qobuz'", html)
        self.assertIn(':hx-vals="JSON.stringify({ store: store })"', html)

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


class RefusalDismiss(Base):
    """That a refusal can be cleared off the screen without erasing the
    `match_decision` row it was read from, and that clearing one refusal never
    hides a later one.
    """

    def refuse_at(self, track_id, store="qobuz", score=10, failed_job=False):
        """Leave a track refused, the way a real claim does: a `match_decision`
        row the refusal panel reads, and optionally a failed job alongside it
        for the tests that care about the plain-failure fallback.
        """
        import time
        from libwish import identity, match

        conn = self.svc.db()
        try:
            if failed_job:
                conn.execute(
                    "INSERT INTO jobs(kind, state, track_id, provider_id, created_at)"
                    " VALUES('claim','failed',?,?,?)",
                    (track_id, store, int(time.time())))
            conn.execute(
                "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
                " matcher_version, lexicon_hash, outcome, score, reasons,"
                " query_json, candidates_considered)"
                " VALUES(?,?,'claim',?,?,?,'refused',?,'[]','{}',0)",
                (track_id, int(time.time()), store,
                 getattr(match, "MATCHER_VERSION", "1"), identity.lexicon_hash(), score))
        finally:
            conn.close()

    def decisions(self, track_id):
        return self.rows("SELECT * FROM match_decision WHERE track_id=? ORDER BY id", (track_id,))

    def state_of(self, track_id):
        from libwish.web.views import _decorate

        with self.app.test_request_context("/"):
            return _decorate([self.svc.tracks.get(track_id)], "artist")[0]

    def test_dismissing_clears_the_refused_state(self):
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        self.refuse_at(track_id)
        self.assertEqual(self.state_of(track_id)["state"], "refused")

        response = self.client.post(f"/api/refusal/{track_id}/dismiss")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        self.assertEqual(self.state_of(track_id)["state"], "wanted")

    def test_the_decision_row_survives_with_dismissed_at_set(self):
        track_id = self.a_track()
        self.refuse_at(track_id)
        self.client.post(f"/api/refusal/{track_id}/dismiss")

        rows = self.decisions(track_id)
        self.assertEqual(len(rows), 1, "dismissing must not delete the audit row")
        self.assertEqual(rows[0]["outcome"], "refused")
        self.assertIsNotNone(rows[0]["dismissed_at"])

    def test_a_later_refusal_shows_again(self):
        """The naive fix, tightened to fail: hiding refusals for the track
        rather than dismissing one decision would leave the row silently
        refused forever the second time too.
        """
        from libwish.models import MATCH_AUTO_MIN

        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        self.refuse_at(track_id, score=10)
        self.client.post(f"/api/refusal/{track_id}/dismiss")
        self.assertEqual(self.state_of(track_id)["state"], "wanted")

        self.refuse_at(track_id, score=20)  # a fresh claim, refused again
        after = self.state_of(track_id)
        self.assertEqual(after["state"], "refused")
        self.assertEqual(after["plate"]["l2"], f"20 / {MATCH_AUTO_MIN}")

        rows = self.decisions(track_id)
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]["dismissed_at"], "the old decision stays dismissed")
        self.assertIsNone(rows[1]["dismissed_at"], "the new one starts undismissed")

    def test_dismissing_an_unknown_track_is_a_404(self):
        response = self.client.post("/api/refusal/999999/dismiss")
        self.assertEqual(response.status_code, 404)

    def test_a_double_dismiss_is_not_an_error(self):
        track_id = self.a_track()
        self.refuse_at(track_id)
        first = self.client.post(f"/api/refusal/{track_id}/dismiss")
        second = self.client.post(f"/api/refusal/{track_id}/dismiss")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        rows = self.decisions(track_id)
        self.assertEqual(len(rows), 1, "a repeat dismiss must not touch an earlier decision")
        self.assertIsNotNone(rows[0]["dismissed_at"])

    def test_a_dismissed_refusal_lets_the_failure_panel_show(self):
        # The watch-for case: dismissed_at must not just blank the panel, it
        # has to fall through to the plain failure exactly as if the refusal
        # had never been recorded.
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        self.refuse_at(track_id, failed_job=True)
        self.client.post(f"/api/refusal/{track_id}/dismiss")

        row = self.state_of(track_id)
        self.assertIsNone(row.get("refusal"))
        self.assertIsNotNone(row.get("failure"))


class FailureDismiss(Base):
    """That a broken claim can be cleared off the screen without erasing the
    `jobs` row it came from, and that clearing one failure never hides a
    later one.
    """

    def fail_at(self, track_id, store="qobuz", state="failed", phase="match"):
        """Leave a track broken, the way a real claim does: a `jobs` row in a
        terminal state, with a phase for the panel to read.
        """
        import time

        conn = self.svc.db()
        try:
            conn.execute(
                "INSERT INTO jobs(kind, state, track_id, provider_id, phase, created_at)"
                " VALUES('claim',?,?,?,?,?)",
                (state, track_id, store, phase, int(time.time())))
        finally:
            conn.close()

    def jobs_for(self, track_id):
        return self.rows("SELECT * FROM jobs WHERE track_id=? ORDER BY id", (track_id,))

    def state_of(self, track_id):
        from libwish.web.views import _decorate

        with self.app.test_request_context("/"):
            return _decorate([self.svc.tracks.get(track_id)], "artist")[0]

    def test_dismissing_clears_the_failure_panel(self):
        track_id = self.a_track()
        self.fail_at(track_id)
        self.assertIsNotNone(self.state_of(track_id).get("failure"))

        response = self.client.post(f"/api/failure/{track_id}/dismiss")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        self.assertIsNone(self.state_of(track_id).get("failure"))

    def test_the_job_row_survives_with_dismissed_at_set(self):
        track_id = self.a_track()
        self.fail_at(track_id)
        self.client.post(f"/api/failure/{track_id}/dismiss")

        rows = self.jobs_for(track_id)
        self.assertEqual(len(rows), 1, "dismissing must not delete the job row")
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIsNotNone(rows[0]["dismissed_at"])

    def test_a_later_failure_shows_again(self):
        """The naive fix, tightened to fail: hiding failures for the track
        rather than dismissing one job would leave the row silently clear
        forever the second time too.
        """
        track_id = self.a_track()
        self.fail_at(track_id, phase="match")
        self.client.post(f"/api/failure/{track_id}/dismiss")
        self.assertIsNone(self.state_of(track_id).get("failure"))

        self.fail_at(track_id, phase="download")  # a fresh claim, broken again
        after = self.state_of(track_id)
        self.assertIsNotNone(after.get("failure"))
        self.assertEqual(after["failure"]["phase"], "download")

        rows = self.jobs_for(track_id)
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]["dismissed_at"], "the old job stays dismissed")
        self.assertIsNone(rows[1]["dismissed_at"], "the new one starts undismissed")

    def test_a_running_job_is_never_hidden_by_this(self):
        # The other half of the watch-for: the dismissed filter belongs to
        # `broke` only. A claim in flight is not something to dismiss, and it
        # must keep showing as working even with a dismissed failure sitting
        # underneath it in the same track's job history.
        track_id = self.a_track()
        self.fail_at(track_id, phase="match")
        self.client.post(f"/api/failure/{track_id}/dismiss")
        self.fail_at(track_id, state="running", phase="download")

        row = self.state_of(track_id)
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row.get("failure"))

    def test_dismissing_an_unknown_track_is_a_404(self):
        response = self.client.post("/api/failure/999999/dismiss")
        self.assertEqual(response.status_code, 404)

    def test_a_double_dismiss_is_not_an_error(self):
        track_id = self.a_track()
        self.fail_at(track_id)
        first = self.client.post(f"/api/failure/{track_id}/dismiss")
        second = self.client.post(f"/api/failure/{track_id}/dismiss")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        rows = self.jobs_for(track_id)
        self.assertEqual(len(rows), 1, "a repeat dismiss must not touch an earlier job")
        self.assertIsNotNone(rows[0]["dismissed_at"])


class BuyAnchorExtensionMarker(Base):
    """`data-buy` is what the wishlist browser extension's click handler in
    libwish.js keys off to send a plain click at the store in the same tab
    instead of a new one. `target="_blank"` and `rel="noopener"` have to stay
    too: they are the whole behaviour with no extension installed, and what a
    modifier-click still gets even with one.
    """

    def test_the_single_store_button_carries_the_marker(self):
        track_id = self.a_track()
        self.svc.stores = {"bandcamp": StubStore()}
        html = self.client.get(f"/ui/row/{track_id}").get_data(as_text=True)
        self.assertEqual(html.count("data-buy"), 1)
        self.assertIn('data-buy x-show="!opened" target="_blank" rel="noopener"', html)

    def test_every_store_in_the_dropdown_carries_the_marker(self):
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz"),
                           "bandcamp": StubStore("bandcamp", "Bandcamp")}
        html = self.client.get(f"/ui/row/{track_id}").get_data(as_text=True)
        self.assertEqual(html.count("data-buy"), 2)
        self.assertEqual(html.count('class="menu__item" data-buy'), 2)
        self.assertEqual(html.count('target="_blank"'), 2)


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


class SyncSurvivesANavigation(Base):
    """Where the sync button gets its state after the page is thrown away.

    Every tab is an ordinary link, so a sweep started on one page is watched
    from a document that never saw it start. Kept only in the browser, the
    button came back enabled and its line blank while the sweep was still
    running, which reads as "nothing is happening" and invites the second
    press `POST /api/sync` exists to refuse.
    """

    def a_sync(self, state="running", phase=None, progress=None, error=None,
               finished_at=None):
        import time
        conn = self.svc.db()
        try:
            cur = conn.execute(
                "INSERT INTO jobs(kind, state, phase, progress, error, created_at,"
                " finished_at) VALUES('sync',?,?,?,?,?,?)",
                (state, phase, json.dumps(progress) if progress is not None else None,
                 error, int(time.time()), finished_at))
            return cur.lastrowid
        finally:
            conn.close()

    def state(self):
        response = self.client.get("/api/sync")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_no_sweep_has_ever_run(self):
        # Distinct from one that ran and found nothing: there is no sentence to
        # restore, so the page keeps the blank line it rendered with.
        self.assertIsNone(self.state()["state"])

    def test_a_running_sweep_reports_the_phase_it_reached(self):
        self.a_sync(phase="enumerate", progress={"purchases": 8})
        answer = self.state()
        self.assertEqual(answer["state"], "running")
        self.assertEqual(answer["phase"], "enumerate")
        # The same pair the live stream sends, so one sentence serves both.
        self.assertEqual(answer["progress"], {"purchases": 8})

    def test_a_finished_sweep_still_carries_its_counts(self):
        # The counts are the only place a reader learns that nothing matched,
        # which is a different answer from nothing having been bought. Losing
        # them to a tab change loses that distinction.
        self.a_sync(state="finished", phase="queue", finished_at=1786400000,
                    progress={"queued": 5, "near_misses": 1, "shops_skipped": []})
        answer = self.state()
        self.assertEqual(answer["state"], "finished")
        self.assertEqual(answer["progress"]["queued"], 5)
        self.assertEqual(answer["finished_at"], 1786400000)

    def test_the_newest_sweep_is_the_one_answered_for(self):
        self.a_sync(state="finished", phase="queue", progress={"queued": 1})
        newest = self.a_sync(phase="session")
        self.assertEqual(self.state()["id"], newest)

    def test_another_kind_of_job_is_not_mistaken_for_a_sweep(self):
        # Claims outnumber sweeps and a sweep queues them, so the newest job
        # after a successful sweep is almost never the sweep.
        track_id = self.a_track()
        conn = self.svc.db()
        try:
            conn.execute("INSERT INTO jobs(kind, state, track_id, created_at)"
                         " VALUES('claim','running',?,9999999999)", (track_id,))
        finally:
            conn.close()
        self.assertIsNone(self.state()["state"])

    def test_a_failed_sweep_says_why_rather_than_spinning(self):
        self.a_sync(state="failed", phase="session", error="Qobuz is signed out.")
        answer = self.state()
        self.assertEqual(answer["state"], "failed")
        self.assertEqual(answer["error"], "Qobuz is signed out.")

    def test_unreadable_progress_is_an_empty_object_not_a_failure(self):
        # The column holds whatever a handler last wrote. A crash mid-write
        # should cost the sentence under the button, not the page.
        self.a_sync(state="finished", phase="queue")
        conn = self.svc.db()
        try:
            conn.execute("UPDATE jobs SET progress='{not json' WHERE kind='sync'")
        finally:
            conn.close()
        self.assertEqual(self.state()["progress"], {})


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


class PurchaseStore:
    """A store the test controls the ownership and enumerate-ability of."""

    def __init__(self, store_id="qobuz", name="Qobuz", *, enumerate_owned=True,
                 inventory=None, authed=True):
        from libwish.models import StoreCapabilities

        self.id = store_id
        self.name = name
        self.capabilities = StoreCapabilities(
            search=False, deep_link=True, enumerate_owned=enumerate_owned, download=True,
            release_granular=False, async_prepare=False, formats=("flac",))
        self.inventory = inventory if inventory is not None else []
        self.authed = authed

    def buy_url(self, q):
        return "https://example.invalid/search"

    def list_owned(self, since=None):
        from libwish.errors import StoreAuthError

        if not self.authed:
            raise StoreAuthError("signed out", code="signed_out", provider_id=self.id)
        return iter(self.inventory)


class Purchases(Base):
    def owned_item(self, key, title, artist, release=None, purchased_at=None):
        from libwish.models import Identifiers, OwnedItem

        return OwnedItem(store="qobuz", item_key=key, kind="track", artist=artist, title=title,
                         release_title=release, parent_key=None, purchased_at=purchased_at,
                         duration_s=None, track_number=None, formats=("flac",),
                         ids=Identifiers(), raw={})

    def test_it_lists_what_the_store_owns(self):
        self.svc.stores = {"qobuz": PurchaseStore(inventory=[
            self.owned_item("a", "Lies", "CHVRCHES", release="The Bones of What You Believe"),
            self.owned_item("b", "Falling Down", "Lil Peep"),
        ])}
        response = self.client.get("/api/purchases/qobuz")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["store"], "qobuz")
        self.assertEqual([p["item_key"] for p in body["purchases"]], ["a", "b"])
        self.assertEqual(body["purchases"][0]["title"], "Lies")
        self.assertEqual(body["purchases"][0]["artist"], "CHVRCHES")
        self.assertEqual(body["purchases"][0]["release_title"], "The Bones of What You Believe")

    def test_an_unconfigured_store_is_404(self):
        self.svc.stores = {}
        response = self.client.get("/api/purchases/qobuz")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())

    def test_a_store_that_cannot_enumerate_is_an_error_not_an_empty_list(self):
        """"You own nothing" and "this shop cannot tell us" are different
        facts, and only one of them is true of a store with no enumerate
        capability at all."""
        self.svc.stores = {"bandcamp": PurchaseStore("bandcamp", "Bandcamp",
                                                      enumerate_owned=False)}
        response = self.client.get("/api/purchases/bandcamp")
        self.assertNotEqual(response.status_code, 200)
        self.assertIn("error", response.get_json())

    def test_a_dead_session_is_not_reported_as_owning_nothing(self):
        self.svc.stores = {"qobuz": PurchaseStore(authed=False)}
        response = self.client.get("/api/purchases/qobuz")
        self.assertNotEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("error", body)
        self.assertNotIn("purchases", body)

    def test_the_result_is_capped_the_same_way_other_listings_are(self):
        from libwish.web.api import DEFAULT_LIMIT, MAX_LIMIT

        self.svc.stores = {"qobuz": PurchaseStore(inventory=[
            self.owned_item(str(n), f"Track {n}", "Artist") for n in range(MAX_LIMIT + 10)
        ])}
        self.assertEqual(len(self.client.get("/api/purchases/qobuz").get_json()["purchases"]),
                         DEFAULT_LIMIT)
        capped = self.client.get(f"/api/purchases/qobuz?limit={MAX_LIMIT + 50}")
        self.assertEqual(len(capped.get_json()["purchases"]), MAX_LIMIT)
        self.assertEqual(len(self.client.get("/api/purchases/qobuz?limit=2")
                             .get_json()["purchases"]), 2)
        self.assertEqual(self.client.get("/api/purchases/qobuz?limit=abc").status_code, 400)

    def test_a_claimed_purchase_is_not_offered(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        self.svc.tracks.mark_purchased(track_id, "qobuz", "a")
        self.svc.stores = {"qobuz": PurchaseStore(inventory=[
            self.owned_item("a", "Lies", "CHVRCHES"),
            self.owned_item("b", "Falling Down", "Lil Peep"),
        ])}
        body = self.client.get("/api/purchases/qobuz").get_json()
        self.assertEqual([p["item_key"] for p in body["purchases"]], ["b"])

    def test_an_unclaimed_purchase_still_shows(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        self.svc.tracks.mark_purchased(track_id, "qobuz", "a")
        self.svc.stores = {"qobuz": PurchaseStore(inventory=[
            self.owned_item("a", "Lies", "CHVRCHES"),
            self.owned_item("b", "Falling Down", "Lil Peep"),
        ])}
        body = self.client.get("/api/purchases/qobuz").get_json()
        self.assertIn("b", [p["item_key"] for p in body["purchases"]])

    def test_the_hidden_count_and_reason_are_reported(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        self.svc.tracks.mark_purchased(track_id, "qobuz", "a")
        self.svc.stores = {"qobuz": PurchaseStore(inventory=[
            self.owned_item("a", "Lies", "CHVRCHES"),
            self.owned_item("b", "Falling Down", "Lil Peep"),
        ])}
        body = self.client.get("/api/purchases/qobuz").get_json()
        self.assertEqual(body["hidden"], 1)
        self.assertIn("hidden_reason", body)

        # An untouched inventory reports the count honestly as zero, present
        # rather than omitted, so a consumer never has to guess whether the
        # key is missing because nothing was hidden or because the build is
        # older than this field.
        self.svc.stores = {"qobuz": PurchaseStore(inventory=[
            self.owned_item("b", "Falling Down", "Lil Peep"),
        ])}
        clean = self.client.get("/api/purchases/qobuz").get_json()
        self.assertEqual(clean["hidden"], 0)
        self.assertNotIn("hidden_reason", clean)

    def test_the_same_key_at_a_different_store_still_shows(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        self.svc.tracks.mark_purchased(track_id, "qobuz", "a")
        self.svc.stores = {"bandcamp": PurchaseStore("bandcamp", "Bandcamp", inventory=[
            self.owned_item("a", "Lies", "CHVRCHES"),
        ])}
        body = self.client.get("/api/purchases/bandcamp").get_json()
        self.assertEqual(body["hidden"], 0)
        self.assertEqual([p["item_key"] for p in body["purchases"]], ["a"])

    def test_a_purchase_with_no_recorded_key_still_shows(self):
        """A purchase filed before `purchased_item_key` existed (or through any
        path that never learned the key) has nothing to compare against, and
        guessing off artist and title would risk hiding a purchase the reader
        still needs: two different purchases of the same song are real. It
        stays offered rather than being hidden on a guess.
        """
        track_id = self.a_track("CHVRCHES", "Lies")
        self.svc.tracks.mark_purchased(track_id, "qobuz")  # no item_key
        self.svc.stores = {"qobuz": PurchaseStore(inventory=[
            self.owned_item("a", "Lies", "CHVRCHES"),
        ])}
        body = self.client.get("/api/purchases/qobuz").get_json()
        self.assertEqual([p["item_key"] for p in body["purchases"]], ["a"])
        self.assertEqual(body["hidden"], 0)

    def test_a_purchase_restored_to_the_want_list_is_offered_again(self):
        """Moving a track back to `queued` must not leave its old purchase
        hidden, or the reader has no way to re-file it. `restore` never clears
        `purchased_at`/`purchased_via`/`purchased_item_key`, so what makes the
        purchase choosable again is the status leaving `('purchased','owned')`,
        not the key being erased.
        """
        track_id = self.a_track("CHVRCHES", "Lies")
        self.svc.tracks.mark_purchased(track_id, "qobuz", "a")
        self.svc.tracks.set_status(track_id, "queued")
        self.svc.stores = {"qobuz": PurchaseStore(inventory=[
            self.owned_item("a", "Lies", "CHVRCHES"),
        ])}
        body = self.client.get("/api/purchases/qobuz").get_json()
        self.assertEqual([p["item_key"] for p in body["purchases"]], ["a"])
        self.assertEqual(body["hidden"], 0)


class Pick(Base):
    def decisions(self, track_id):
        return self.rows("SELECT * FROM match_decision WHERE track_id=?", (track_id,))

    def test_picking_records_a_user_picked_decision_naming_the_purchase(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        response = self.client.post(f"/api/claim/{track_id}/pick", json={
            "store": "qobuz", "item_key": "abc123", "title": "Lies", "artist": "CHVRCHES",
        })
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["picked"])
        rows = self.decisions(track_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "user_picked")
        self.assertNotEqual(rows[0]["outcome"], "user_confirmed")
        self.assertEqual(rows[0]["provider"], "qobuz")
        self.assertEqual(rows[0]["phase"], "pick")
        self.assertIn("abc123", rows[0]["candidate_json"])
        self.assertIn("Lies", rows[0]["candidate_json"])

    def test_picking_enqueues_a_claim_carrying_the_store(self):
        track_id = self.a_track("CHVRCHES", "Lies")
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        response = self.client.post(f"/api/claim/{track_id}/pick",
                                    json={"store": "qobuz", "item_key": "abc123"})
        job = self.svc.jobs.get(response.get_json()["job_id"])
        self.assertEqual(job["kind"], "claim")
        self.assertEqual(job["track_id"], track_id)
        self.assertEqual(job["provider_id"], "qobuz")

    def test_a_form_post_names_the_store_as_well_as_a_json_one(self):
        track_id = self.a_track()
        self.svc.stores = {"qobuz": StubStore("qobuz", "Qobuz")}
        response = self.client.post(f"/api/claim/{track_id}/pick",
                                    data={"store": "qobuz", "item_key": "x"})
        self.assertEqual(response.status_code, 202)

    def test_an_unknown_track_is_404(self):
        response = self.client.post("/api/claim/999999/pick",
                                    json={"store": "qobuz", "item_key": "x"})
        self.assertEqual(response.status_code, 404)

    def test_a_store_is_required(self):
        track_id = self.a_track()
        response = self.client.post(f"/api/claim/{track_id}/pick", json={"item_key": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.decisions(track_id), [])

    def test_an_item_key_is_required(self):
        track_id = self.a_track()
        response = self.client.post(f"/api/claim/{track_id}/pick", json={"store": "qobuz"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.decisions(track_id), [])

    def test_an_unconfigured_store_is_404(self):
        track_id = self.a_track()
        self.svc.stores = {}
        response = self.client.post(f"/api/claim/{track_id}/pick",
                                    json={"store": "qobuz", "item_key": "x"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.decisions(track_id), [])


if __name__ == "__main__":
    unittest.main()
