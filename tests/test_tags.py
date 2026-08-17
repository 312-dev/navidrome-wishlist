"""Reading an MP4 file's own tags, and refusing to read a file that is not one.

The fixtures here are built byte by byte rather than checked in as binaries.
An MP4 box is size-then-type all the way down, so a builder that composes them
is shorter than the file it produces, and a test that says which bytes it wrote
is the only kind that can show what a parser got wrong.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from libwish import tags


def box(kind: bytes, payload: bytes) -> bytes:
    """One MP4 box: its own length including this header, then its type."""
    return struct.pack(">I", len(payload) + 8) + kind + payload


def big_box(kind: bytes, payload: bytes) -> bytes:
    """A box using the 64-bit size escape, which is what any real album uses.

    Size 1 means the true size follows the type as eight more bytes. A reader
    that takes the 32-bit field literally reads 1 and walks into the middle of
    the box it was standing on.
    """
    return struct.pack(">I", 1) + kind + struct.pack(">Q", len(payload) + 16) + payload


def text(kind: bytes, value: str) -> bytes:
    """An `ilst` entry: the tag name wrapping a typed `data` box."""
    body = struct.pack(">I", 1) + b"\x00" * 4 + value.encode("utf-8")
    return box(kind, box(b"data", body))


def number(kind: bytes, value: int, width: int = 8) -> bytes:
    """An `ilst` entry holding an integer, which is how Apple writes its ids."""
    body = struct.pack(">I", 21) + b"\x00" * 4 + value.to_bytes(width, "big")
    return box(kind, box(b"data", body))


def ilst(collection: int | None = None, **named: str) -> bytes:
    keys = {"title": b"\xa9nam", "artist": b"\xa9ART", "album": b"\xa9alb",
            "album_artist": b"aART"}
    entries = [text(keys[k], v) for k, v in named.items()]
    if collection is not None:
        entries.append(number(b"plID", collection))
    return box(b"ilst", b"".join(entries))


def meta(children: bytes, *, full_box: bool = True) -> bytes:
    """`meta`, with or without the version and flags a full box should carry."""
    return box(b"meta", (b"\x00" * 4 if full_box else b"") + children)


def hdlr(handler: bytes) -> bytes:
    return box(b"hdlr", b"\x00" * 8 + handler)


def a_song(ftyp: bytes = b"M4A ", collection: int | None = None, **named: str) -> bytes:
    """A minimal but structurally honest iTunes-shaped file."""
    return (
        box(b"ftyp", ftyp + b"\x00" * 4)
        + box(b"moov",
              box(b"trak", box(b"mdia", hdlr(b"soun")))
              + box(b"udta", meta(ilst(collection, **named))))
        + box(b"mdat", b"\x00" * 64)
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def written(self, blob: bytes, name: str = "track.m4a") -> Path:
        path = self.tmp / name
        path.write_bytes(blob)
        return path


class WhatAFileSaysAboutItself(Base):
    def test_artist_title_and_album_come_out_of_an_itunes_file(self):
        path = self.written(a_song(title="Love Me Tender", artist="Percy Sledge",
                                   album="The Atlantic Recordings"))
        found = tags.read(path)
        self.assertEqual((found.artist, found.title, found.album),
                         ("Percy Sledge", "Love Me Tender", "The Atlantic Recordings"))
        self.assertTrue(found.identifies())

    def test_the_version_qualifier_is_left_in_the_title(self):
        # The matcher reads qualifiers, so stripping one here would throw away
        # the fact that this is the remaster and not the original.
        path = self.written(a_song(title="Love Me Tender (2000 Remaster)",
                                   artist="Percy Sledge"))
        self.assertEqual(tags.read(path).title, "Love Me Tender (2000 Remaster)")

    def test_an_album_artist_stands_in_when_there_is_no_track_artist(self):
        path = self.written(a_song(title="Gosh", album_artist="Jamie xx"))
        self.assertEqual(tags.read(path).artist, "Jamie xx")

    def test_a_track_artist_wins_over_an_album_artist(self):
        # A compilation credits the album to "Various Artists", which identifies
        # nothing and would file every track on it under one name.
        path = self.written(a_song(title="Crazy", artist="Patsy Cline",
                                   album_artist="Various Artists"))
        self.assertEqual(tags.read(path).artist, "Patsy Cline")

    def test_utf8_survives(self):
        path = self.written(a_song(title="Où est ma tête", artist="Sébastien Tellier"))
        self.assertEqual(tags.read(path).artist, "Sébastien Tellier")

    def test_a_sixty_four_bit_box_is_walked_rather_than_believed(self):
        # Every album with a large mdat writes one of these. Reading the 32-bit
        # size as 1 lands the next read inside this box instead of after it.
        blob = (
            box(b"ftyp", b"M4A \x00\x00\x00\x00")
            + big_box(b"mdat", b"\x00" * 128)
            + box(b"moov", box(b"udta", meta(ilst(title="Irene", artist="Twin Peaks"))))
        )
        self.assertEqual(tags.read(self.written(blob)).title, "Irene")

    def test_meta_without_its_version_and_flags_still_reads(self):
        blob = (box(b"ftyp", b"M4A \x00\x00\x00\x00")
                + box(b"moov", box(b"udta", meta(ilst(title="Happier", artist="Bastille"),
                                                 full_box=False))))
        self.assertEqual(tags.read(self.written(blob)).artist, "Bastille")

    def test_a_file_with_no_tags_reports_nothing_rather_than_raising(self):
        blob = box(b"ftyp", b"M4A \x00\x00\x00\x00") + box(b"moov", b"")
        found = tags.read(self.written(blob))
        self.assertEqual((found.artist, found.title), ("", ""))
        self.assertFalse(found.identifies())

    def test_a_file_that_is_not_mp4_at_all_reports_nothing(self):
        self.assertFalse(tags.read(self.written(b"fLaC" + b"\x00" * 200)).identifies())

    def test_a_truncated_file_does_not_hang_or_raise(self):
        # Half a box, which is what a download killed partway through leaves.
        whole = a_song(title="Never", artist="The Earls")
        self.assertFalse(tags.read(self.written(whole[:len(whole) // 2])).identifies())

    def test_nonsense_lengths_are_abandoned_rather_than_followed(self):
        # A box claiming to be smaller than its own header. Following it means
        # never advancing, and a parser that does that never returns.
        blob = (box(b"ftyp", b"M4A \x00\x00\x00\x00")
                + struct.pack(">I", 2) + b"moov" + b"\x00" * 40)
        self.assertFalse(tags.read(self.written(blob)).identifies())


class ApplesOwnReleaseId(Base):
    """The one identifier in the file that was written by the seller.

    Everything else here is a name that has to be matched against something.
    This is a pointer, and it is what makes finding the right sleeve for a
    purchase a lookup rather than a search.
    """

    def test_it_is_read_off_a_purchased_file(self):
        path = self.written(a_song(collection=1471554984, title="TalkTalk",
                                   artist="A Perfect Circle"))
        self.assertEqual(tags.apple_collection_id(path), 1471554984)

    def test_apple_writes_it_at_four_bytes_as_well_as_eight(self):
        blob = (box(b"ftyp", b"M4A \x00\x00\x00\x00")
                + box(b"moov", box(b"udta", meta(
                    box(b"ilst", number(b"plID", 1471554984, width=4))))))
        self.assertEqual(tags.apple_collection_id(self.written(blob)), 1471554984)

    def test_a_file_from_anywhere_else_has_none(self):
        path = self.written(a_song(title="Gosh", artist="Jamie xx"))
        self.assertIsNone(tags.apple_collection_id(path))

    def test_a_file_it_cannot_parse_has_none(self):
        self.assertIsNone(tags.apple_collection_id(self.written(b"fLaC" + b"\x00" * 200)))


class WhetherThereIsAudioInIt(Base):
    def test_a_song_has_a_sound_track(self):
        self.assertTrue(tags.has_audio_track(self.written(a_song(title="x", artist="y"))))

    def test_a_music_video_does_not(self):
        # iTunes sells these in the same container, and the brand does not
        # separate them. A library of songs is the wrong home for one.
        blob = (box(b"ftyp", b"M4V \x00\x00\x00\x00")
                + box(b"moov", box(b"trak", box(b"mdia", hdlr(b"vide")))))
        self.assertFalse(tags.has_audio_track(self.written(blob, "clip.m4v")))

    def test_a_file_it_cannot_parse_answers_no(self):
        self.assertFalse(tags.has_audio_track(self.written(b"not an mp4 at all")))


class ReadingAFilename(unittest.TestCase):
    def test_artist_dash_title(self):
        found = tags.from_filename("Percy Sledge - Love Me Tender.m4a")
        self.assertEqual((found.artist, found.title), ("Percy Sledge", "Love Me Tender"))

    def test_a_leading_track_number_is_dropped(self):
        found = tags.from_filename("03 Percy Sledge - Love Me Tender.m4a")
        self.assertEqual(found.artist, "Percy Sledge")

    def test_a_hyphenated_title_keeps_its_hyphen(self):
        # Split on the first " - " only, so the rest of the name survives.
        found = tags.from_filename("Godspeed You! Black Emperor - Storm - Lift Yr Skinny Fists.m4a")
        self.assertEqual(found.title, "Storm - Lift Yr Skinny Fists")

    def test_a_name_that_is_not_that_shape_identifies_nothing(self):
        # Refusing beats guessing. "01 Track 01" is not an artist and a title.
        for name in ("01 Track 01.m4a", "audio.m4a", "Love Me Tender.m4a",
                     "Percy Sledge-Love Me Tender.m4a"):
            self.assertFalse(tags.from_filename(name).identifies(), name)


if __name__ == "__main__":
    unittest.main()
