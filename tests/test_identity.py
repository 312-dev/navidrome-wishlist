"""Normalization, the qualifier lexicon, fingerprints, and the two invariants
that live in identity.py rather than in the scorer.

Every parse expectation here was taken from the verified prototype output in
docs/architecture/04-identity.md section 3.3, and the fingerprint counts from
the 164 real rows exported beside it, so a change to the lexicon that starts
merging live rows fails here rather than in someone's library.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from libwish import db, identity
from libwish.identity import (
    build_identity,
    classify,
    clean_isrc,
    clean_mbid,
    fingerprint,
    fold,
    lexicon_hash,
    normalize_artist,
    parse_title,
)

REPO = Path(__file__).resolve().parent.parent
LIVE_ROWS = json.loads((REPO / "docs/architecture/_tracks-sample.json").read_text())


class FoldTests(unittest.TestCase):
    def test_diacritics_fold_rather_than_truncate(self):
        # The live dedup_key for this artist is "michael bubl": the accented
        # character was deleted along with the rest of the word.
        self.assertEqual(fold("Michael Bublé"), "michael buble")
        self.assertEqual(fold("Michael Buble"), fold("Michael Bublé"))

    def test_folding_is_not_an_ascii_character_class(self):
        # An ASCII-only filter would drop the mark and keep nothing else; NFKD
        # plus a combining-mark filter has to survive precomposed and
        # decomposed spellings of the same name identically.
        self.assertEqual(fold("Qué Onda Guero"), fold("Qué Onda Guero"))
        self.assertEqual(fold("Sigur Rós"), "sigur ros")
        self.assertEqual(fold("ＵＳＨＥＲ"), "usher")

    def test_punctuation_becomes_a_space_and_never_fuses_words(self):
        self.assertEqual(fold("The Dap-Kings Horns"), "the dap kings horns")
        self.assertEqual(fold("Uncle Albert/Admiral Halsey"), "uncle albert admiral halsey")

    def test_ampersand_and_plus_become_and(self):
        self.assertEqual(fold("Turiya & Ramakrishna"), "turiya and ramakrishna")
        self.assertEqual(fold("Florence + The Machine"), "florence and the machine")

    def test_apostrophes_vanish_entirely(self):
        self.assertEqual(fold("Ain't That Strong"), fold("Aint That Strong"))

    def test_digits_are_left_alone(self):
        self.assertEqual(fold("Breathe (2 AM)"), "breathe 2 am")
        self.assertEqual(fold("Tech N9ne"), "tech n9ne")

    def test_an_unfoldable_field_is_not_a_wildcard(self):
        # `¥$` folds to nothing. An empty key would compare equal to every other
        # unfoldable key, so every symbol-only artist would be one artist.
        self.assertEqual(fold("¥$"), "")
        self.assertNotEqual(normalize_artist("¥$"), ("",))
        self.assertNotEqual(normalize_artist("¥$"), normalize_artist("☆"))
        self.assertTrue(all(name for name in normalize_artist("¥$")))


class ParseTitleTests(unittest.TestCase):
    def assert_parse(self, raw, *, base, alt=None, version=(), edition=(),
                     credits=(), tie_in=False, unclassified=()):
        t = parse_title(raw)
        self.assertEqual(t.base, base, raw)
        self.assertEqual(t.base_alt, alt if alt is not None else base, raw)
        self.assertEqual(t.version, frozenset(version), raw)
        self.assertEqual(t.edition, frozenset(edition), raw)
        self.assertEqual(t.credits, tuple(credits), raw)
        self.assertEqual(t.tie_in, tie_in, raw)
        self.assertEqual(t.unclassified, tuple(unclassified), raw)

    def test_unknown_parenthetical_stays_in_the_base(self):
        self.assert_parse(
            "(I Just) Died In Your Arms",
            base="i just died in your arms",
            alt="died in your arms",
            unclassified=["i just"],
        )
        self.assert_parse(
            "Breathe (2 AM)", base="breathe 2 am", alt="breathe", unclassified=["2 am"]
        )
        self.assert_parse(
            "Flex (Ooh, Ooh, Ooh)",
            base="flex ooh ooh ooh",
            alt="flex",
            unclassified=["ooh ooh ooh"],
        )

    def test_credits_split_bracketed_and_unbracketed(self):
        self.assert_parse(
            "Come Alive (feat. Toro y Moi)",
            base="come alive",
            credits=["feat toro y moi"],
        )
        self.assert_parse(
            "Coleen feat. The Dap-Kings Horns",
            base="coleen",
            credits=["the dap kings horns"],
        )

    def test_version_qualifiers_are_extracted(self):
        self.assert_parse("Out Getting Ribs (Slowed)", base="out getting ribs",
                          version=["slowed"])
        self.assert_parse("Truly Madly Deeply - Recorded at  Studios NYC",
                          base="truly madly deeply",
                          version=["recorded at studios nyc"])
        self.assert_parse("Lies (Live at the Fillmore)", base="lies",
                          version=["live at the fillmore"])

    def test_album_version_is_the_ordinary_studio_cut(self):
        # A store writes "Album Version" to tell a track apart from the radio
        # edit pressed beside it, so it describes the same audio a plain title
        # describes and must leave no trace in the fingerprint.
        self.assert_parse("X (Album Version)", base="x")
        self.assertEqual(
            fingerprint(build_identity("Chris Brown", "X (Album Version)")),
            fingerprint(build_identity("Chris Brown", "X")),
        )

    def test_edition_qualifiers_are_extracted(self):
        self.assert_parse("Falling Down - Bonus Track", base="falling down",
                          edition=["bonus track"])

    def test_a_dash_is_split_only_when_the_lexicon_recognises_the_tail(self):
        self.assert_parse("Moonlight sonata - 1st movement",
                          base="moonlight sonata 1st movement")

    def test_a_colon_is_never_a_split_point(self):
        self.assert_parse(
            "Symphony No. 4 in E Minor, Op. 98: I. Allegro non troppo",
            base="symphony no 4 in e minor op 98 i allegro non troppo",
        )

    def test_tie_in_text_is_deleted_and_never_reachable(self):
        # Invariant I2. No alternate spelling of a title contains the name of
        # the series it was licensed to, so that text is not evidence of
        # anything and is removed before any comparison can see it.
        t = parse_title('Such Great Heights (From "Tell Me Lies Season 3")')
        self.assertTrue(t.tie_in)
        for field in (t.base, t.base_alt, " ".join(sorted(t.tokens))):
            self.assertNotIn("lies", field)
            self.assertNotIn("tell me", field)
            self.assertNotIn("season", field)
        self.assertEqual(t.unclassified, ())

    def test_a_tie_in_and_a_credit_survive_together(self):
        self.assert_parse(
            'Heavenly (a Tasson Soundtrack) (feat. Eddie Watson)',
            base="heavenly",
            credits=["feat eddie watson"],
            tie_in=True,
        )
        self.assert_parse(
            'Brighter (from "Hazbin Hotel") Extended Version',
            base="brighter",
            version=["extended version"],
            tie_in=True,
        )

    def test_containment_on_a_title_is_a_type_error(self):
        # Invariant I1. `x in title` is the shape of the 2026-08-02 bug, so it
        # crashes rather than returning a plausible answer.
        title = parse_title('Such Great Heights (From "Tell Me Lies Season 3")')
        with self.assertRaises(TypeError):
            "lies" in title  # noqa: B015


class ArtistTests(unittest.TestCase):
    def test_shapes_from_the_live_rows(self):
        self.assertEqual(normalize_artist("Michael Bublé"), ("michael buble",))
        self.assertEqual(
            normalize_artist("Yusuf / Cat Stevens"),
            ("yusuf cat stevens", "yusuf", "cat stevens"),
        )
        self.assertEqual(
            normalize_artist("Warren G, Nate Dogg, The Game"),
            ("warren g nate dogg the game", "warren g", "nate dogg", "the game"),
        )
        self.assertEqual(
            normalize_artist("Florence + The Machine"), ("florence and the machine",)
        )

    def test_the_fragment_guard(self):
        # `Crosby, Stills & Nash` fragments into `nash`, and an unrelated artist
        # called Nash must not be able to match on it.
        names = normalize_artist("Crosby, Stills & Nash")
        self.assertEqual(names[3], "nash")
        self.assertFalse(identity.artist_fragment_usable("nash", 3))
        self.assertTrue(identity.artist_fragment_usable("warren g", 1))
        self.assertTrue(identity.artist_fragment_usable("usher", 0))


class IdentifierTests(unittest.TestCase):
    def test_isrc_grammar(self):
        self.assertEqual(clean_isrc("us-abc-12-34567"), "USABC1234567")
        self.assertIsNone(clean_isrc("not an isrc"))
        self.assertIsNone(clean_isrc(""))
        self.assertIsNone(clean_isrc(None))

    def test_mbid_shape(self):
        mbid = "8f3471b5-7e6a-48da-86a9-c1c07a0f47ae"
        self.assertEqual(clean_mbid(mbid.upper()), mbid)
        self.assertIsNone(clean_mbid("123"))

    def test_a_source_supplied_mbid_is_not_promoted_by_ingest(self):
        # Last.fm returns MBIDs that do not resolve, and track MBIDs where a
        # recording MBID is expected. Both are UUIDs, so shape proves nothing
        # and only a MusicBrainz lookup may fill recording_mbids.
        from libwish.models import LovedTrack, TrackIds

        loved = LovedTrack(
            source_id="lastfm",
            source_item_id="1",
            loved_at=None,
            artist="CHVRCHES",
            title="Lies",
            ids=TrackIds(recording_mbid="8f3471b5-7e6a-48da-86a9-c1c07a0f47ae",
                         isrc="GBAHT1300123"),
            duration_s=200,
        )
        ident = identity.from_loved_track(loved)
        self.assertEqual(ident.recording_mbids, frozenset())
        self.assertEqual(ident.isrcs, frozenset({"GBAHT1300123"}))
        self.assertEqual(ident.duration_ms, 200_000)

        promoted = identity.from_loved_track(
            loved, validated_mbids=["8f3471b5-7e6a-48da-86a9-c1c07a0f47ae"]
        )
        self.assertEqual(len(promoted.recording_mbids), 1)

    def test_owned_item_duration_converts_at_the_boundary(self):
        from libwish.models import Identifiers, OwnedItem

        item = OwnedItem(
            store="qobuz",
            item_key="42",
            kind="track",
            artist="CHVRCHES",
            title="Lies",
            duration_s=203.4,
            ids=Identifiers(isrc="GBAHT1300123"),
        )
        ident = identity.from_owned_item(item)
        self.assertEqual(ident.duration_ms, 203_400)
        self.assertEqual(ident.store, "qobuz")
        self.assertEqual(ident.store_id, "42")


class LiveCorpusTests(unittest.TestCase):
    def test_164_rows_produce_164_distinct_fingerprints(self):
        seen = {}
        for row in LIVE_ROWS:
            ident = build_identity(row["artist"] or "", row["title"] or "")
            seen.setdefault(fingerprint(ident), []).append(row)
        self.assertEqual(len(LIVE_ROWS), 164)
        collisions = {fp: rows for fp, rows in seen.items() if len(rows) > 1}
        self.assertEqual(collisions, {})

    def test_exactly_one_live_row_is_degraded(self):
        degraded = [
            row for row in LIVE_ROWS
            if build_identity(row["artist"] or "", row["title"] or "").identity_degraded
        ]
        self.assertEqual([(r["artist"], r["title"]) for r in degraded],
                         [("¥$", "FIELD TRIP")])


class LexiconTests(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(classify("feat eddie watson"), "credit")
        self.assertEqual(classify("from tell me lies season 3"), "tiein")
        self.assertEqual(classify("a tasson soundtrack"), "tiein")
        self.assertEqual(classify("2010 remaster"), "version")
        self.assertEqual(classify("radio edit"), "version")
        self.assertEqual(classify("acoustic"), "version")
        self.assertEqual(classify("album version"), "standard")
        self.assertEqual(classify("bonus track"), "edition")
        self.assertIsNone(classify("2 am"))
        self.assertIsNone(classify("i just"))

    def test_hash_is_stable_and_covers_the_patterns(self):
        first = lexicon_hash()
        self.assertEqual(first, lexicon_hash())
        self.assertEqual(len(first), 64)


class BackfillTests(unittest.TestCase):
    """The offline part of migration 0003, which is a function rather than SQL
    because folding is not expressible in SQLite."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "queue.db"
        db.migrate(self.path, backup=False)
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def insert(self, artist, title, dedup_key):
        self.conn.execute(
            "INSERT INTO tracks(artist, title, dedup_key, added_at) VALUES(?,?,?,0)",
            (artist, title, dedup_key),
        )

    def test_keys_come_from_artist_and_title_and_never_from_dedup_key(self):
        # The live database holds two dialects of dedup_key written by two
        # importers, one of which lost the accent in "Bublé" entirely.
        self.insert("Michael Bublé", "Heartache Tonight", "michael bubl\theartache tonight")
        identity.recompute_identity_columns(self.conn)
        row = self.conn.execute(
            "SELECT artist_key, title_key, qualifier_key, fp_key, identity_degraded "
            "FROM tracks"
        ).fetchone()
        self.assertEqual(row["artist_key"], "michael buble")
        self.assertEqual(row["title_key"], "heartache tonight")
        self.assertEqual(row["qualifier_key"], "")
        self.assertEqual(row["identity_degraded"], 0)
        self.assertEqual(row["fp_key"], "michael buble\x1fheartache tonight\x1f")

    def test_the_degraded_row_is_flagged(self):
        self.insert("¥$", "FIELD TRIP", "\tfield trip")
        identity.recompute_identity_columns(self.conn)
        row = self.conn.execute("SELECT artist_key, identity_degraded FROM tracks").fetchone()
        self.assertEqual(row["identity_degraded"], 1)
        self.assertEqual(row["artist_key"], "¥$")

    def test_a_collision_is_reported_and_never_merged(self):
        self.insert("Volbeat", "Still Counting", "a")
        self.insert("VOLBEAT", "Still  Counting", "b")
        collisions = identity.recompute_identity_columns(self.conn)
        self.assertEqual(len(collisions), 1)
        fp, ids = collisions[0]
        self.assertEqual(len(ids), 2)
        left = self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        self.assertEqual(left, 2, "a collision must not lose a queue row")
        null_fp = self.conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE fp_key IS NULL"
        ).fetchone()[0]
        self.assertEqual(null_fp, 2)

    def test_recompute_is_idempotent(self):
        self.insert("Volbeat", "Still Counting", "a")
        identity.recompute_identity_columns(self.conn)
        before = self.conn.execute("SELECT fp_key FROM tracks").fetchone()[0]
        identity.recompute_identity_columns(self.conn)
        self.assertEqual(self.conn.execute("SELECT fp_key FROM tracks").fetchone()[0], before)

    def test_the_unique_fingerprint_index_is_live(self):
        self.insert("Volbeat", "Still Counting", "a")
        identity.recompute_identity_columns(self.conn)
        fp = self.conn.execute("SELECT fp_key FROM tracks").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO tracks(artist, title, fp_key, added_at) VALUES(?,?,?,0)",
                ("Volbeat", "Still Counting", fp),
            )

    def test_find_existing_walks_the_ladder(self):
        self.insert("Volbeat", "Still Counting", "a")
        identity.recompute_identity_columns(self.conn)
        track_id = self.conn.execute("SELECT id FROM tracks").fetchone()[0]
        ident = build_identity("volbeat", "still counting")
        self.assertEqual(identity.find_existing(self.conn, ident), track_id)
        self.assertIsNone(
            identity.find_existing(self.conn, build_identity("Volbeat", "Sad Man's Tongue"))
        )

    def test_rows_due_for_lookup_respect_the_backoff(self):
        self.insert("Volbeat", "Still Counting", "a")
        self.conn.execute("UPDATE tracks SET identity_lookup_after = 5000")
        self.assertEqual(identity.rows_due_for_identity_lookup(self.conn, 4000), [])
        self.assertEqual(len(identity.rows_due_for_identity_lookup(self.conn, 6000)), 1)


class NoSubstringGuardTests(unittest.TestCase):
    """Cruder than the type guard on NormalizedTitle, and it catches a helper
    written in a hurry outside the dataclass."""

    BANNED = (".startswith(", ".endswith(", ".find(", ".index(")
    FIELDS = ("base", "base_alt", "title_raw", "artist_raw", "artist_key",
              "q_title", "c_title", "folded")

    def source_lines(self):
        for name in ("identity", "match"):
            path = Path(identity.__file__).with_name(f"{name}.py")
            for number, line in enumerate(path.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                yield f"{name}.py:{number}", code

    def test_no_substring_helpers(self):
        for where, code in self.source_lines():
            for banned in self.BANNED:
                self.assertNotIn(banned, code, where)

    def test_no_containment_against_a_title_or_artist_field(self):
        import re

        pattern = re.compile(r"\bin\s+\S*(" + "|".join(self.FIELDS) + r")\b")
        for where, code in self.source_lines():
            self.assertIsNone(pattern.search(code), f"{where}: {code.strip()}")


if __name__ == "__main__":
    unittest.main()
