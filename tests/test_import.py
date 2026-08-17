"""Filing a file bought where there is no API to read.

Exercised through the HTTP route rather than the function under it, because
what is being claimed is that dropping a file on the page ends with the track
in the library and on the owned list. A test of `import_file` alone would pass
with the route wired to nothing.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_tags import a_song

#: Past `claim.MIN_BYTES`, which is the floor that catches an HTML error page
#: served in place of a download. A structurally valid file under it is still
#: refused, deliberately, and one test says so.
PADDING = b"\x00" * 120_000


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
        self.client = self.app.test_client()
        self.music = Path(os.environ["LW_MUSIC_DIR"])

        # No artwork lookup unless a test asks for one. Left live, every import
        # here reaches Apple and Deezer over the network, which makes the suite
        # slow, flaky and dependent on what those two happen to carry today:
        # the first run of this wrote a real Percy Sledge sleeve into a
        # temporary directory from the live Deezer.
        import libwish.artwork as art
        self._real_lookup = art.for_imported_file
        art.for_imported_file = lambda http, path, found: ""

    def tearDown(self):
        import libwish.artwork as art
        art.for_imported_file = self._real_lookup
        shutil.rmtree(self.tmp, ignore_errors=True)

    def song(self, artist, title, album="", *, pad=PADDING, ftyp=b"M4A "):
        named = {"artist": artist, "title": title}
        if album:
            named["album"] = album
        return a_song(ftyp=ftyp, **named) + pad

    def send(self, *files):
        """POST files as a browser would. Each is (filename, bytes)."""
        data = [("files", (io.BytesIO(blob), name)) for name, blob in files]
        return self.client.post("/api/import", data={"files": [f[1] for f in data]},
                                content_type="multipart/form-data")

    def owned(self):
        return [dict(r) for r in self.svc.tracks.owned()]

    def library(self):
        return sorted(p.relative_to(self.music).as_posix()
                      for p in self.music.rglob("*") if p.is_file())


class ADroppedPurchaseBecomesAnOwnedTrack(Base):
    def test_the_file_lands_in_the_library_and_the_track_is_owned(self):
        res = self.send(("01 Love Me Tender.m4a",
                         self.song("Percy Sledge", "Love Me Tender",
                                   "The Atlantic Recordings")))
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["filed"], 1)

        owned = self.owned()
        self.assertEqual(len(owned), 1)
        self.assertEqual((owned[0]["artist"], owned[0]["title"]),
                         ("Percy Sledge", "Love Me Tender"))
        self.assertEqual(owned[0]["purchased_via"], "itunes")
        self.assertEqual(
            self.library(),
            ["Percy Sledge/The Atlantic Recordings/Love Me Tender.m4a"])

    def test_the_filename_is_not_what_it_is_filed_as(self):
        # iTunes names the file after its track number. The tags are the record.
        self.send(("01 01 Track.m4a", self.song("Twin Peaks", "Irene")))
        self.assertEqual(self.owned()[0]["title"], "Irene")

    def test_a_track_already_on_the_want_list_turns_owned_rather_than_doubling(self):
        # The point of running the same identity ladder a love goes through: a
        # track loved on Last.fm and then bought on iTunes is one track, and the
        # row the reader has been watching is the row that changes.
        conn = self.svc.db()
        try:
            conn.execute("INSERT INTO tracks(artist, title, added_at, status)"
                         " VALUES('Percy Sledge','Love Me Tender',0,'queued')")
            conn.commit()
        finally:
            conn.close()
        from libwish import identity
        conn = self.svc.db()
        try:
            identity.recompute_identity_columns(conn)
            conn.commit()
        finally:
            conn.close()
        before = len(self.svc.tracks.queued()) + len(self.owned())

        self.send(("track.m4a", self.song("Percy Sledge", "Love Me Tender")))
        self.assertEqual(len(self.svc.tracks.queued()) + len(self.owned()), before)
        self.assertEqual(len(self.owned()), 1)

    def test_enrichment_is_queued_for_it_like_any_other_row(self):
        # The whole reason the file's tags are read only to identify it: the
        # cover, the duration and the MBIDs come from the same place they come
        # from for a track that arrived from a source.
        self.send(("track.m4a", self.song("Bastille", "Happier")))
        conn = self.svc.db()
        try:
            kinds = [r["kind"] for r in conn.execute(
                "SELECT kind FROM jobs WHERE kind='enrich'")]
        finally:
            conn.close()
        self.assertEqual(kinds, ["enrich"])

    def test_several_files_at_once_are_each_reported(self):
        res = self.send(
            ("a.m4a", self.song("Rihanna", "Pon de Replay")),
            ("b.m4a", self.song("OneRepublic", "Apologize")),
            ("c.m4a", self.song("Merle Haggard", "Going Where the Lonely Go")),
        )
        body = res.get_json()
        self.assertEqual(body["filed"], 3)
        self.assertEqual([r["title"] for r in body["results"]],
                         ["Pon de Replay", "Apologize", "Going Where the Lonely Go"])
        self.assertEqual(len(self.owned()), 3)


class TheSleeveForAFileThatHasNone(Base):
    """An iTunes purchase carries no artwork, so the import goes and finds it.

    Two destinations, because they are two readers. The cache is what the want
    list draws. The file beside the audio is what a music server reads, and it
    reads the file rather than asking this application anything: without it, a
    purchased track sits in Navidrome as a blue placeholder.
    """

    def setUp(self):
        super().setUp()
        self.asked = []
        import libwish.artwork as art

        def fake_for_imported_file(http, path, found):
            self.asked.append((str(path), found.artist, found.album))
            return "https://apple.test/1000x1000bb.jpg" if found.album else ""

        # Over the top of the Base's do-nothing stub, which tearDown restores.
        art.for_imported_file = fake_for_imported_file

        # The cover cache does its own fetching, so that is stubbed too: what
        # is under test is where the bytes end up, not how they were fetched.
        from libwish.media import CoverCache
        self.real_ensure = CoverCache.ensure

        def fake_ensure(cache, track_id, url):
            # Past media.MIN_BYTES, which is the floor that catches an error
            # page served where an image was asked for.
            return cache.store(track_id, b"\xff\xd8\xff\xe0" + b"\x00" * 400)

        CoverCache.ensure = fake_ensure

    def tearDown(self):
        from libwish.media import CoverCache
        CoverCache.ensure = self.real_ensure
        super().tearDown()

    def test_a_cover_lands_beside_the_audio_for_the_music_server(self):
        self.send(("01 TalkTalk.m4a", self.song("A Perfect Circle", "TalkTalk",
                                                "Eat the Elephant")))
        self.assertEqual(
            self.library(),
            ["A Perfect Circle/Eat the Elephant/TalkTalk.m4a",
             "A Perfect Circle/Eat the Elephant/cover.jpg"])

    def test_the_want_list_gets_the_same_picture(self):
        self.send(("01 TalkTalk.m4a", self.song("A Perfect Circle", "TalkTalk",
                                                "Eat the Elephant")))
        track_id = self.owned()[0]["id"]
        from libwish.enrich import cover_cache
        self.assertTrue(cover_cache(self.svc).exists(track_id))

    def test_the_file_itself_is_never_rewritten(self):
        # A purchased file comes out byte for byte the way the shop sold it.
        # Embedding artwork would be the one place this application edits audio.
        blob = self.song("A Perfect Circle", "TalkTalk", "Eat the Elephant")
        self.send(("01 TalkTalk.m4a", blob))
        landed = self.music / "A Perfect Circle" / "Eat the Elephant" / "TalkTalk.m4a"
        self.assertEqual(landed.read_bytes(), blob)

    def test_a_cover_already_in_the_folder_is_left_alone(self):
        folder = self.music / "A Perfect Circle" / "Eat the Elephant"
        folder.mkdir(parents=True)
        (folder / "cover.jpg").write_bytes(b"chosen by hand")
        self.send(("01 TalkTalk.m4a", self.song("A Perfect Circle", "TalkTalk",
                                                "Eat the Elephant")))
        self.assertEqual((folder / "cover.jpg").read_bytes(), b"chosen by hand")

    def test_no_cover_found_still_files_the_track(self):
        # Artwork is the most optional thing here. The purchase is recorded and
        # the file is in the library before anything asks about a picture.
        res = self.send(("x.m4a", self.song("Someone", "Untitled")))
        self.assertTrue(res.get_json()["results"][0]["ok"])
        self.assertEqual(len(self.owned()), 1)


class WhatItRefuses(Base):
    def test_one_bad_file_does_not_fail_the_others(self):
        # Dropping a folder is the normal case, and a request that failed as a
        # whole would mean sorting the folder by hand before trying again.
        res = self.send(
            ("good.m4a", self.song("Stereophonics", "Have A Nice Day")),
            ("sleeve.pdf", b"%PDF-1.7" + PADDING),
        )
        body = res.get_json()
        self.assertEqual(body["filed"], 1)
        self.assertEqual([r["ok"] for r in body["results"]], [True, False])
        self.assertIn("sleeve.pdf", body["results"][1]["file"])
        self.assertEqual(len(self.owned()), 1)

    def test_a_file_with_no_artist_and_no_usable_name_is_refused(self):
        res = self.send(("audio.m4a", self.song("", "")))
        body = res.get_json()
        self.assertFalse(body["results"][0]["ok"])
        self.assertIn("nothing to file it as", body["results"][0]["msg"])
        self.assertEqual(self.owned(), [])

    def test_a_file_with_no_tags_falls_back_to_an_artist_dash_title_name(self):
        res = self.send(("Patsy Cline - Crazy.m4a", self.song("", "")))
        self.assertTrue(res.get_json()["results"][0]["ok"])
        self.assertEqual(self.owned()[0]["artist"], "Patsy Cline")

    def test_a_music_video_is_refused_by_its_track_handler(self):
        from tests.test_tags import box, hdlr
        video = (box(b"ftyp", b"M4V \x00\x00\x00\x00")
                 + box(b"moov", box(b"trak", box(b"mdia", hdlr(b"vide")))) + PADDING)
        res = self.send(("clip.m4v", video))
        body = res.get_json()
        self.assertFalse(body["results"][0]["ok"])
        self.assertIn("no audio track", body["results"][0]["msg"])

    def test_a_refused_file_leaves_nothing_behind(self):
        # A rejected upload that leaves bytes in staging fills the volume with
        # files nobody can see and nothing will collect.
        self.send(("nope.txt", b"not audio at all" + PADDING))
        staging = self.svc.paths.staging_dir
        self.assertEqual([] if not staging.is_dir() else list(staging.iterdir()), [])

    def test_a_file_too_small_to_be_audio_is_refused(self):
        res = self.send(("tiny.m4a", self.song("Twin Peaks", "Irene", pad=b"")))
        self.assertFalse(res.get_json()["results"][0]["ok"])

    def test_a_refusal_names_the_file_the_reader_dropped(self):
        # Staging renames a file for the transfer. A refusal about
        # "upload-0-sleeve.pdf" is a refusal about a file nobody has ever seen.
        res = self.send(("sleeve.pdf", b"%PDF-1.7" + PADDING))
        msg = res.get_json()["results"][0]["msg"]
        self.assertIn("sleeve.pdf", msg)
        self.assertNotIn("upload-", msg)

    def test_a_refusal_does_not_blame_a_store_that_was_not_involved(self):
        # The same check runs for a download, where an unrecognised file really
        # is usually a sign-in page. Nobody signed into anything here.
        res = self.send(("sleeve.pdf", b"%PDF-1.7" + PADDING))
        self.assertNotIn("store", res.get_json()["results"][0]["msg"])

    def test_sending_no_files_says_so(self):
        res = self.client.post("/api/import", data={}, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)


class WhenTheLibraryAlreadyHasIt(Base):
    def test_the_existing_file_is_kept_and_the_track_still_reads_as_owned(self):
        # The file on disk may be a better master than an AAC from iTunes, and
        # replacing it would be this application deciding otherwise on its own.
        first = self.song("Extreme", "More Than Words")
        self.send(("a.m4a", first))
        dest = self.music / "Extreme" / "More Than Words.m4a"
        dest.write_bytes(b"the master already held")

        res = self.send(("a.m4a", first))
        body = res.get_json()
        self.assertTrue(body["results"][0]["ok"])
        self.assertTrue(body["results"][0]["already_held"])
        self.assertEqual(dest.read_bytes(), b"the master already held")
        self.assertEqual(len(self.owned()), 1)


if __name__ == "__main__":
    unittest.main()
