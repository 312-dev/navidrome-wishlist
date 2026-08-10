"""Gates, scoring, and the corpora.

The meta-test is the point of this module. A corpus that only asserts the
current implementation passes cannot tell you it has gone vacuous, so the naive
matcher that produced the 2026-08-02 incident is checked in beside the real one
and the same negative corpus is run against both. If the corpus ever stops
catching substring matching, the corpus has rotted and this file says so.
"""

from __future__ import annotations

import unittest

from libwish.errors import MatchRefused
from libwish.identity import build_identity
from libwish.match import (
    AMBIGUITY_MARGIN,
    best_match,
    explain,
    require_match,
    score,
    title_gate,
)
from libwish.models import (
    MATCH_AUTO_MIN,
    MATCH_CONFIRM_MIN,
    MATCH_DEGRADED_CAP,
    MATCH_STRING_ONLY_CAP,
)

MBID_A = "8f3471b5-7e6a-48da-86a9-c1c07a0f47ae"
MBID_B = "1e2f3a4b-5c6d-4e8f-9a0b-1c2d3e4f5a6b"

#: Pairs that must be refused. The first row is the incident: a claim for
#: CHVRCHES "Lies" against a purchase whose title carries the name of the
#: television series it was licensed to.
NEGATIVE = (
    ("CHVRCHES", "Lies", "CHVRCHES", 'Such Great Heights (From "Tell Me Lies Season 3")'),
    ("CHVRCHES", "Lies", "The Postal Service",
     'Such Great Heights (From "Tell Me Lies Season 3")'),
    ("Patsy Cline", "Crazy", "Heart", "Crazy On You"),
    ("Nine Inch Nails", "Only", "Carpenters", "We've Only Just Begun"),
    ("Nine Inch Nails", "Only", "Guitar Tribute Players", "The Only Exception"),
    ("Steely Dan", "Peg", "Buddy Holly", "Peggy Sue"),
    ("The Game", "My Life", "Kelly Clarkson", "My Life Would Suck Without You"),
    ("The Game", "My Life", "The Game", "My Life Would Suck Without You"),
    ("Feeling Blew", "Out Getting Ribs (Slowed)", "Feeling Blew", "Out Getting Ribs"),
    ("Igor Kotliarevsky", "Moonlight sonata - 1st movement",
     "Igor Kotliarevsky", "Moonlight sonata"),
    ("Guitar Tribute Players", "The Only Exception", "Paramore", "The Only Exception"),
    ("Audereus", "Clubbed To Death", "Rob Dougan", "Clubbed To Death"),
    ("Chris Brown", "X", "Chris Brown", "X (Live at the Apollo)"),
    ("Chris Brown", "X", "Chris Brown", "X (Acoustic)"),
    ("Chris Brown", "X", "Chris Brown", "X (Kaytranada Remix)"),
)

#: Pairs that must reach at least a confirmation. None of them may reach auto:
#: not one carries an identifier.
POSITIVE_STRING_ONLY = (
    ("Anna Nalick", "Breathe (2 AM)", "Anna Nalick", "Breathe (2 AM)"),
    ("Michael Bublé", "Heartache Tonight", "Michael Buble", "Heartache Tonight"),
    ("The Heavy", "Coleen feat. The Dap-Kings Horns", "The Heavy", "Coleen"),
    ("0xSleep", 'Heavenly (a Tasson Soundtrack) (feat. Eddie Watson)',
     "0xSleep", "Heavenly"),
    ("Lil Peep", "Falling Down - Bonus Track", "Lil Peep", "Falling Down"),
    ("Anna Nalick", "Breathe (2 AM)", "Anna Nalick", "Breathe"),
    ("Cutting Crew", "(I Just) Died In Your Arms", "Cutting Crew", "Died In Your Arms"),
    ("Chris Brown", "X", "Chris Brown", "X (Album Version)"),
)

NAIVE_ACCEPTS_AT = 90


def naive_match(q_artist: str, q_title: str, c_artist: str, c_title: str) -> int:
    """The shape of the implementation that bought the wrong track.

    Substring containment, then token overlap as a percentage. Present only so
    the corpus can be shown to reject it; nothing imports this outside tests.
    """
    q, c = q_title.casefold(), c_title.casefold()
    if q in c or c in q:
        return 100
    q_tokens, c_tokens = set(q.split()), set(c.split())
    return int(100 * len(q_tokens & c_tokens) / max(1, len(q_tokens)))


def naive_accepts(q_artist: str, q_title: str, c_artist: str, c_title: str) -> bool:
    return naive_match(q_artist, q_title, c_artist, c_title) >= NAIVE_ACCEPTS_AT


def real_accepts(q_artist: str, q_title: str, c_artist: str, c_title: str) -> bool:
    decision = score(build_identity(q_artist, q_title), build_identity(c_artist, c_title))
    return decision.outcome != "refused"


def assert_refuses_every_negative_pair(accepts) -> None:
    """The corpus check, written once so it can be pointed at either matcher."""
    accepted = [row for row in NEGATIVE if accepts(*row)]
    if accepted:
        rows = "; ".join(f"{q_a} - {q_t} ~~ {c_a} - {c_t}" for q_a, q_t, c_a, c_t in accepted)
        raise AssertionError(f"accepted {len(accepted)} pair(s) it had to refuse: {rows}")


class MetaTests(unittest.TestCase):
    def test_the_real_matcher_refuses_every_negative_pair(self):
        assert_refuses_every_negative_pair(real_accepts)

    def test_the_same_check_fails_against_the_naive_matcher(self):
        # If this ever stops raising, the corpus has stopped testing the thing
        # it exists to test and every assertion above it is worth nothing.
        with self.assertRaises(AssertionError) as caught:
            assert_refuses_every_negative_pair(naive_accepts)
        self.assertIn("Tell Me Lies", str(caught.exception))

    def test_the_incident_scores_100_under_the_naive_matcher(self):
        self.assertEqual(
            naive_match("CHVRCHES", "Lies", "CHVRCHES",
                        'Such Great Heights (From "Tell Me Lies Season 3")'),
            100,
        )

    def test_the_incident_is_refused_on_the_short_title_rule(self):
        q = build_identity("CHVRCHES", "Lies")
        c = build_identity("CHVRCHES", 'Such Great Heights (From "Tell Me Lies Season 3")')
        decision = score(q, c)
        self.assertEqual(decision.outcome, "refused")
        self.assertEqual(decision.gate_failed, "SHORT_TITLE_EXACT_ONLY")
        self.assertIn("too short", explain(q, c, decision))

    def test_the_incident_is_also_refused_on_the_artist_gate(self):
        # Two independent gates, because one guard is one point of failure.
        q = build_identity("CHVRCHES", "Lies")
        c = build_identity("The Postal Service",
                           'Such Great Heights (From "Tell Me Lies Season 3")')
        self.assertEqual(score(q, c).gate_failed, "ARTIST_MISMATCH")


class PositiveCorpusTests(unittest.TestCase):
    def test_every_pair_reaches_at_least_a_confirmation(self):
        for q_artist, q_title, c_artist, c_title in POSITIVE_STRING_ONLY:
            with self.subTest(q=q_title, c=c_title):
                decision = score(build_identity(q_artist, q_title),
                                 build_identity(c_artist, c_title))
                self.assertNotEqual(decision.outcome, "refused")
                self.assertGreaterEqual(decision.score, MATCH_CONFIRM_MIN)

    def test_strings_alone_never_auto_claim(self):
        # Invariant I3. Without a validated MBID or a shared ISRC there is no
        # path to the auto threshold, however well the strings agree.
        for q_artist, q_title, c_artist, c_title in POSITIVE_STRING_ONLY:
            with self.subTest(q=q_title, c=c_title):
                decision = score(build_identity(q_artist, q_title),
                                 build_identity(c_artist, c_title))
                self.assertNotEqual(decision.outcome, "auto")
                self.assertLessEqual(decision.score, MATCH_STRING_ONLY_CAP)

    def test_identical_strings_with_no_identifiers_land_in_confirm(self):
        q = build_identity("CHVRCHES", "Lies")
        c = build_identity("CHVRCHES", "Lies")
        decision = score(q, c)
        self.assertEqual(decision.outcome, "confirm")
        self.assertLess(decision.score, MATCH_AUTO_MIN)

    def test_an_accented_artist_matches_its_unaccented_spelling(self):
        decision = score(build_identity("Michael Bublé", "Heartache Tonight"),
                         build_identity("Michael Buble", "Heartache Tonight"))
        self.assertIn("ARTIST_EXACT", decision.reasons)

    def test_the_paren_stripped_rung_is_capped(self):
        q = build_identity("Cutting Crew", "(I Just) Died In Your Arms")
        c = build_identity("Cutting Crew", "Died In Your Arms")
        decision = score(q, c)
        self.assertEqual(decision.matched_via, "paren_stripped")
        self.assertIn("MATCHED_ON_PAREN_STRIPPED_FORM", decision.reasons)
        self.assertLessEqual(decision.score, MATCH_DEGRADED_CAP)


class GateTests(unittest.TestCase):
    def test_a_strict_superstring_of_a_title_is_refused(self):
        # Same artist, so only the title gate can save this one.
        q = build_identity("The Game", "My Life")
        c = build_identity("The Game", "My Life Would Suck Without You")
        self.assertEqual(score(q, c).gate_failed, "TITLE_MISMATCH")

    def test_a_short_title_is_refused_against_anything_but_itself(self):
        q = build_identity("Chris Brown", "X")
        c = build_identity("Chris Brown", "XO Tour Llif3")
        self.assertEqual(score(q, c).gate_failed, "SHORT_TITLE_EXACT_ONLY")

    def test_live_acoustic_and_remix_refuse_a_studio_claim(self):
        q = build_identity("Feeling Blew", "Out Getting Ribs")
        for qualifier in ("Live at the Fillmore", "Acoustic", "Kaytranada Remix",
                          "2010 Remaster", "Radio Edit"):
            with self.subTest(qualifier=qualifier):
                c = build_identity("Feeling Blew", f"Out Getting Ribs ({qualifier})")
                decision = score(q, c)
                self.assertEqual(decision.gate_failed, "VERSION_MISMATCH")
                self.assertIn(qualifier.casefold(), explain(q, c, decision))

    def test_album_version_is_the_ordinary_studio_cut(self):
        q = build_identity("Chris Brown", "X")
        c = build_identity("Chris Brown", "X (Album Version)")
        decision = score(q, c)
        self.assertIsNone(decision.gate_failed)
        self.assertEqual(decision.outcome, "confirm")

    def test_a_symbol_only_artist_does_not_match_everything(self):
        # `¥$` folds to nothing. An empty artist key would pass the artist gate
        # against every other unfoldable artist.
        q = build_identity("¥$", "FIELD TRIP")
        for other in ("☆", "!!!", "Some Other Band"):
            with self.subTest(other=other):
                c = build_identity(other, "FIELD TRIP")
                self.assertEqual(score(q, c).gate_failed, "ARTIST_MISMATCH")

    def test_a_degraded_identity_is_capped_below_auto(self):
        q = build_identity("¥$", "FIELD TRIP")
        c = build_identity("¥$", "FIELD TRIP", mbids=[MBID_A])
        decision = score(q, c)
        self.assertIn("DEGRADED_IDENTITY", decision.reasons)
        self.assertLessEqual(decision.score, MATCH_DEGRADED_CAP)
        self.assertNotEqual(decision.outcome, "auto")

    def test_a_duration_gap_vetoes_a_perfect_string_match(self):
        q = build_identity("CHVRCHES", "Lies", mbids=[MBID_A], duration_ms=227_000)
        c = build_identity("CHVRCHES", "Lies", mbids=[MBID_A], duration_ms=262_000)
        decision = score(q, c)
        self.assertEqual(decision.gate_failed, "DURATION_MISMATCH")
        self.assertEqual(decision.outcome, "refused")

    def test_a_duration_within_the_gate_does_not_veto(self):
        q = build_identity("CHVRCHES", "Lies", duration_ms=227_000)
        c = build_identity("CHVRCHES", "Lies", duration_ms=238_000)
        self.assertIsNone(score(q, c).gate_failed)

    def test_an_unknown_duration_on_either_side_does_not_gate(self):
        q = build_identity("CHVRCHES", "Lies", duration_ms=227_000)
        c = build_identity("CHVRCHES", "Lies")
        self.assertIsNone(score(q, c).gate_failed)

    def test_conflicting_validated_mbids_refuse(self):
        q = build_identity("CHVRCHES", "Lies", mbids=[MBID_A])
        c = build_identity("CHVRCHES", "Lies", mbids=[MBID_B])
        self.assertEqual(score(q, c).gate_failed, "MBID_CONFLICT")

    def test_an_empty_field_is_refused_rather_than_confirmed(self):
        self.assertEqual(
            score(build_identity("", ""), build_identity("", "")).gate_failed,
            "EMPTY_FIELD",
        )

    def test_the_title_gate_rungs(self):
        from libwish.identity import parse_title

        self.assertEqual(title_gate(parse_title("Lies"), parse_title("Lies")),
                         (True, "base_exact"))
        self.assertEqual(
            title_gate(parse_title("Breathe (2 AM)"), parse_title("Breathe"))[1],
            "paren_stripped",
        )
        self.assertEqual(
            title_gate(parse_title("Lies"), parse_title("Such Great Heights"))[1],
            "short_title_exact_only",
        )


class EvidenceTests(unittest.TestCase):
    def test_a_shared_validated_mbid_reaches_auto(self):
        q = build_identity("CHVRCHES", "Lies", mbids=[MBID_A])
        c = build_identity("CHVRCHES", "Lies", mbids=[MBID_A])
        decision = score(q, c)
        self.assertEqual(decision.outcome, "auto")
        self.assertGreaterEqual(decision.score, MATCH_AUTO_MIN)
        self.assertIn("MBID_AGREEMENT", decision.reasons)

    def test_a_shared_isrc_reaches_auto(self):
        q = build_identity("CHVRCHES", "Lies", isrcs=["GBAHT1300123"])
        c = build_identity("CHVRCHES", "Lies", isrcs=["GBAHT1300123", "USUM71300456"])
        decision = score(q, c)
        self.assertEqual(decision.outcome, "auto")
        self.assertIn("ISRC_INTERSECTION", decision.reasons)

    def test_disagreeing_isrcs_are_evidence_of_nothing(self):
        # One recording legitimately carries several ISRCs, so a disagreement
        # must not disqualify the way an MBID conflict does.
        q = build_identity("CHVRCHES", "Lies", isrcs=["GBAHT1300123"])
        c = build_identity("CHVRCHES", "Lies", isrcs=["USUM71300456"])
        decision = score(q, c)
        self.assertIsNone(decision.gate_failed)
        self.assertEqual(decision.outcome, "confirm")

    def test_the_string_only_cap_bites_when_the_evidence_would_exceed_it(self):
        # Everything a string can offer at once: exact artist, exact title,
        # durations three seconds apart, the same edition declared on both
        # sides. Still short of auto, because none of it is an identifier.
        q = build_identity("Lil Peep", "Falling Down - Bonus Track", duration_ms=189_000)
        c = build_identity("Lil Peep", "Falling Down - Bonus Track", duration_ms=189_500)
        decision = score(q, c)
        self.assertTrue(decision.capped)
        self.assertIn("STRING_ONLY_CAP", decision.reasons)
        self.assertEqual(decision.score, MATCH_STRING_ONLY_CAP)
        self.assertEqual(decision.outcome, "confirm")

    def test_agreeing_credits_are_worth_something(self):
        q = build_identity("Chromeo", "Come Alive (feat. Toro y Moi)")
        agreeing = score(q, build_identity("Chromeo", "Come Alive (feat. Toro y Moi)"))
        silent = score(q, build_identity("Chromeo", "Come Alive"))
        self.assertIn("CREDITS_AGREE", agreeing.reasons)
        self.assertGreater(agreeing.score, silent.score)

    def test_a_guest_artist_does_not_inherit_the_track(self):
        # A store crediting `Chromeo` for a track our queue credits to the
        # guest is a different artist's recording, and the artist gate is what
        # says so; a scoring penalty could be outvoted, a gate cannot.
        decision = score(build_identity("Toro y Moi", "Come Alive"),
                         build_identity("Chromeo", "Come Alive (feat. Toro y Moi)"))
        self.assertEqual(decision.gate_failed, "ARTIST_MISMATCH")


class BestMatchTests(unittest.TestCase):
    def test_no_candidates(self):
        decision, index = best_match(build_identity("CHVRCHES", "Lies"), [])
        self.assertEqual(decision.gate_failed, "NO_CANDIDATES")
        self.assertIsNone(index)

    def test_two_close_survivors_refuse_rather_than_pick_one(self):
        q = build_identity("CHVRCHES", "Lies")
        candidates = [build_identity("CHVRCHES", "Lies"),
                      build_identity("CHVRCHES", "Lies")]
        decision, index = best_match(q, candidates)
        self.assertEqual(decision.gate_failed, "AMBIGUOUS_TOP_CANDIDATES")
        self.assertIsNone(index)
        self.assertIn(str(AMBIGUITY_MARGIN), explain(q, None, decision))

    def test_a_clear_winner_is_returned_with_its_index(self):
        q = build_identity("CHVRCHES", "Lies", isrcs=["GBAHT1300123"])
        candidates = [
            build_identity("CHVRCHES", "Gun"),
            build_identity("CHVRCHES", "Lies", isrcs=["GBAHT1300123"]),
        ]
        decision, index = best_match(q, candidates)
        self.assertEqual(index, 1)
        self.assertEqual(decision.outcome, "auto")

    def test_a_total_refusal_still_names_the_closest_candidate(self):
        q = build_identity("CHVRCHES", "Lies")
        candidates = [build_identity("The Postal Service", "Such Great Heights")]
        decision, index = best_match(q, candidates)
        self.assertEqual(decision.outcome, "refused")
        self.assertEqual(index, 0)

    def test_require_match_carries_the_rejected_candidate(self):
        q = build_identity("CHVRCHES", "Lies")
        candidate = build_identity(
            "CHVRCHES", 'Such Great Heights (From "Tell Me Lies Season 3")'
        )
        with self.assertRaises(MatchRefused) as caught:
            require_match(q, [candidate])
        self.assertIs(caught.exception.candidate, candidate)
        self.assertEqual(caught.exception.reason, "SHORT_TITLE_EXACT_ONLY")
        self.assertIn("Tell Me Lies", caught.exception.message)


class ExplanationTests(unittest.TestCase):
    def test_a_refusal_names_the_specific_evidence(self):
        cases = {
            ("Patsy Cline", "Crazy", "Heart", "Crazy On You"): "Heart",
            ("Feeling Blew", "Out Getting Ribs", "Feeling Blew",
             "Out Getting Ribs (Acoustic)"): "acoustic",
        }
        for (q_artist, q_title, c_artist, c_title), expected in cases.items():
            with self.subTest(c=c_title):
                q, c = build_identity(q_artist, q_title), build_identity(c_artist, c_title)
                self.assertIn(expected, explain(q, c, score(q, c)))

    def test_a_duration_refusal_prints_both_durations(self):
        q = build_identity("CHVRCHES", "Lies", duration_ms=227_000)
        c = build_identity("CHVRCHES", "Lies", duration_ms=262_000)
        message = explain(q, c, score(q, c))
        self.assertIn("227s", message)
        self.assertIn("262s", message)


if __name__ == "__main__":
    unittest.main()
