"""Finding a sleeve for a file that arrived without one.

The Deezer half of this is the half worth testing hard. Asked for an album it
does not carry, Deezer answers with a different album by the same artist, and
that answer looks exactly like a good one: right artist, real record, real
cover URL. Every test below that refuses something is refusing a response of
that shape.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from libwish import artwork, tags
from libwish.errors import ProviderError
from tests.test_tags import a_song, box, meta, number


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.body = json.dumps(payload).encode()

    def json(self):
        return self.payload


class FakeHttp:
    """Answers by whichever canned reply matches the URL, and remembers asks."""

    def __init__(self, **replies):
        self.replies = replies
        self.asked: list[str] = []

    def get(self, url, **kw):
        self.asked.append(url)
        for fragment, payload in self.replies.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(payload)
        return FakeResponse({})


def apple(collection: int, art: str = "https://mzstatic.test/a/100x100bb.jpg"):
    return {"resultCount": 1, "results": [
        {"wrapperType": "collection", "collectionId": collection,
         "artistName": "A Perfect Circle", "collectionName": "Eat the Elephant",
         "artworkUrl100": art}]}


def album(artist: str, title: str, cover: str = "https://dz.test/cover.jpg"):
    return {"artist": {"name": artist}, "title": title, "cover_xl": cover}


class ApplesOwnAnswer(unittest.TestCase):
    def test_the_release_id_in_the_file_is_looked_up_directly(self):
        http = FakeHttp(**{"itunes.apple.com": apple(1471554984)})
        self.assertEqual(artwork.apple_cover(http, 1471554984),
                         "https://mzstatic.test/a/1000x1000bb.jpg")
        self.assertIn("id=1471554984", http.asked[0])

    def test_the_thumbnail_size_is_swapped_for_a_usable_one(self):
        # Apple hands out a hundred pixels square by default, which is a
        # thumbnail of a thumbnail on any display made this decade.
        http = FakeHttp(**{"itunes.apple.com": apple(1, "https://x.test/1/100x100bb.jpg")})
        self.assertTrue(artwork.apple_cover(http, 1).endswith("1000x1000bb.jpg"))

    def test_an_empty_answer_is_no_cover_rather_than_an_error(self):
        http = FakeHttp(**{"itunes.apple.com": {"resultCount": 0, "results": []}})
        self.assertEqual(artwork.apple_cover(http, 1), "")

    def test_a_lookup_that_fails_is_no_cover_rather_than_an_error(self):
        # The file is already in the library and the purchase already recorded
        # by the time anything asks about a picture.
        http = FakeHttp(**{"itunes.apple.com": ProviderError("apple is down")})
        self.assertEqual(artwork.apple_cover(http, 1), "")


class TheDeezerGuessIsChecked(unittest.TestCase):
    def test_a_matching_album_is_taken(self):
        http = FakeHttp(**{"search/album": {"data": [album("A Perfect Circle", "Mer De Noms")]}})
        self.assertEqual(
            artwork.deezer_album_cover(http, "A Perfect Circle", "Mer de Noms"),
            "https://dz.test/cover.jpg")

    def test_a_different_album_by_the_right_artist_is_refused(self):
        # The live case. Deezer does not carry Eat the Elephant, and answers
        # with eMOTIVe: right artist, real record, wrong sleeve entirely.
        http = FakeHttp(**{"search/album": {"data": [
            album("A Perfect Circle", "eMOTIVe"),
            album("A Perfect Circle", "Mer De Noms"),
        ]}})
        self.assertEqual(
            artwork.deezer_album_cover(http, "A Perfect Circle", "Eat the Elephant"), "")

    def test_the_right_album_by_a_different_artist_is_refused(self):
        http = FakeHttp(**{"search/album": {"data": [album("Weezer", "Eat the Elephant")]}})
        self.assertEqual(
            artwork.deezer_album_cover(http, "A Perfect Circle", "Eat the Elephant"), "")

    def test_the_right_album_further_down_the_list_is_still_found(self):
        http = FakeHttp(**{"search/album": {"data": [
            album("A Perfect Circle", "eMOTIVe"),
            album("A Perfect Circle", "Thirteenth Step"),
            album("A Perfect Circle", "Eat the Elephant", "https://dz.test/right.jpg"),
        ]}})
        self.assertEqual(
            artwork.deezer_album_cover(http, "A Perfect Circle", "Eat the Elephant"),
            "https://dz.test/right.jpg")

    def test_case_and_spacing_do_not_make_it_a_different_record(self):
        http = FakeHttp(**{"search/album": {"data": [album("a perfect circle", "MER DE NOMS")]}})
        self.assertEqual(
            artwork.deezer_album_cover(http, "A Perfect Circle", "Mer de Noms"),
            "https://dz.test/cover.jpg")

    def test_nothing_at_all_is_no_cover(self):
        http = FakeHttp(**{"search/album": {"data": []}})
        self.assertEqual(artwork.deezer_album_cover(http, "Nobody", "Nothing"), "")


class WhichSourceIsAsked(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def written(self, blob: bytes, name: str = "t.m4a") -> Path:
        path = self.tmp / name
        path.write_bytes(blob)
        return path

    #: What the file says about itself, which is the same in every case here.
    FOUND = tags.FileTags(artist="A Perfect Circle", title="TalkTalk",
                          album="Eat the Elephant")

    def test_apple_is_asked_first_and_deezer_is_not_asked_at_all(self):
        # Nothing needs matching when the seller wrote down which release it
        # is, so there is no reason to go guessing at names as well. Deezer is
        # primed with the wrong album on purpose: if it were consulted, this
        # would come back with a cover, and the wrong one.
        path = self.written(a_song(collection=1471554984, title="TalkTalk",
                                   artist="A Perfect Circle", album="Eat the Elephant"))
        http = FakeHttp(**{"itunes.apple.com": apple(1471554984),
                           "search/album": {"data": [album("A Perfect Circle", "eMOTIVe")]}})
        self.assertEqual(artwork.for_imported_file(http, path, self.FOUND),
                         "https://mzstatic.test/a/1000x1000bb.jpg")
        self.assertEqual(len(http.asked), 1)
        self.assertIn("itunes.apple.com", http.asked[0])

    def test_deezer_answers_for_a_file_apple_did_not_sell(self):
        path = self.written(a_song(title="Mer De Noms", artist="A Perfect Circle",
                                   album="Mer de Noms"))
        http = FakeHttp(**{"search/album": {"data": [album("A Perfect Circle", "Mer de Noms")]}})
        found = tags.FileTags(artist="A Perfect Circle", title="Mer De Noms",
                              album="Mer de Noms")
        self.assertEqual(artwork.for_imported_file(http, path, found),
                         "https://dz.test/cover.jpg")

    def test_apple_going_quiet_falls_through_to_deezer(self):
        path = self.written(a_song(collection=9, title="Mer De Noms",
                                   artist="A Perfect Circle", album="Mer de Noms"))
        http = FakeHttp(**{"itunes.apple.com": {"resultCount": 0, "results": []},
                           "search/album": {"data": [album("A Perfect Circle", "Mer de Noms")]}})
        found = tags.FileTags(artist="A Perfect Circle", title="Mer De Noms",
                              album="Mer de Noms")
        self.assertEqual(artwork.for_imported_file(http, path, found),
                         "https://dz.test/cover.jpg")

    def test_no_cover_beats_the_wrong_cover(self):
        # The live case end to end: an album Deezer does not carry, a file with
        # no id, and a plausible wrong answer sitting there to be taken.
        path = self.written(a_song(title="TalkTalk", artist="A Perfect Circle",
                                   album="Eat the Elephant"))
        http = FakeHttp(**{"search/album": {"data": [album("A Perfect Circle", "eMOTIVe")]}})
        self.assertEqual(artwork.for_imported_file(http, path, self.FOUND), "")

    def test_a_file_with_no_album_and_no_id_asks_nobody(self):
        path = self.written(a_song(title="Untitled", artist="Someone"))
        http = FakeHttp()
        found = tags.FileTags(artist="Someone", title="Untitled")
        self.assertEqual(artwork.for_imported_file(http, path, found), "")
        self.assertEqual(http.asked, [])


class TheFolderCover(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.album = self.tmp / "Album"
        self.album.mkdir()
        self.cached = self.tmp / "168.jpg"
        self.cached.write_bytes(b"\xff\xd8\xff\xe0 pretend jpeg")

    def test_it_is_written_beside_the_audio(self):
        # A music server reads the file next to the track. It does not ask this
        # application anything, so leaving one there is the whole mechanism.
        out = artwork.write_folder_cover(self.cached, self.album)
        self.assertEqual(out, self.album / "cover.jpg")
        self.assertEqual(out.read_bytes(), self.cached.read_bytes())

    def test_an_existing_cover_is_left_alone(self):
        (self.album / "cover.png").write_bytes(b"chosen by hand")
        self.assertIsNone(artwork.write_folder_cover(self.cached, self.album))
        self.assertEqual((self.album / "cover.png").read_bytes(), b"chosen by hand")

    def test_nothing_is_left_behind_when_it_cannot_write(self):
        missing = self.tmp / "not-there"
        self.assertIsNone(artwork.write_folder_cover(self.cached, missing))
        self.assertFalse(missing.exists())

    def test_a_cover_that_is_not_there_writes_nothing(self):
        self.assertIsNone(artwork.write_folder_cover(self.tmp / "gone.jpg", self.album))
        self.assertEqual(list(self.album.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
