"""Deciding whether two identities are the same recording.

Gates run first and can only refuse; only survivors are scored. That ordering is
the load-bearing part. Under a plain weighted sum a perfect title can drag a
wrong artist over the line, which is exactly the shape a store search returns
for a cover or a tribute recording, and the queue already holds four of those.
A gate cannot be outvoted.

The score is an integer out of 100, a sum of named contributions, so a decision
can be read back later and explained by naming which contributions fired. Two
ceilings sit above it. Without a validated MusicBrainz recording MBID shared by
both sides, or an ISRC in common, the score cannot pass the auto threshold no
matter how well the strings agree, so the worst case for a string-only store is
one confirmation click. A match reached only by stripping an unrecognised
parenthetical, or made from a degraded identity, is capped lower still.

Nothing here compares one side's text against the other with `in`,
`startswith`, `endswith` or a regex built from either side's data. Comparisons
are whole-field equality, set equality, set intersection, and a bounded
similarity ratio that is never the sole reason anything passes a gate.

One reason code is not in `04-identity.md`'s list: `EMPTY_FIELD`, for an
identity built from a blank artist or a blank title. A blank field is not a
degraded identity that can be confirmed by a human, it is nothing to confirm.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Sequence

from .errors import MatchRefused
from .identity import artist_fragment_usable, credit_names
from .models import (
    MATCH_ARTIST_GATE,
    MATCH_AUTO_MIN,
    MATCH_CONFIRM_MIN,
    MATCH_DEGRADED_CAP,
    MATCH_DURATION_GATE_S,
    MATCH_STRING_ONLY_CAP,
    MATCH_TITLE_FUZZY_GATE,
    MatchDecision,
    NormalizedTitle,
    TrackIdentity,
)

#: Semver of this scorer, written onto every `match_decision` row. A replay
#: reads it to tell whether a stored decision predates a rule change.
MATCHER_VERSION = "1.0.0"

#: Two surviving candidates this close together are two masters of the same
#: song more often than they are one right answer, and picking one silently is
#: how the wrong master ends up in the library.
AMBIGUITY_MARGIN = 5

#: Editions that describe different audio rather than different packaging, so
#: they are scored apart from the rest of the edition set.
_LYRIC_EDITIONS = frozenset({"explicit", "clean"})

_NO_CANDIDATE = MatchDecision(
    outcome="refused",
    score=0,
    gate_failed="NO_CANDIDATES",
    matched_via="",
    reasons=("NO_CANDIDATES",),
    capped=False,
)


def _strip_leading_the(name: str) -> str:
    head, sep, rest = name.partition(" ")
    return rest if head == "the" and sep else name


def artist_score(q: TrackIdentity, c: TrackIdentity) -> float:
    """How well two artist credits agree, 0..1.

    The fuzzy rung is capped below the exact rungs so that a close spelling can
    reach the gate but can never present itself as exact and earn the exact
    bonus.
    """
    a, b = q.artists, c.artists
    if not a or not b or not a[0] or not b[0]:
        return 0.0
    if a[0] == b[0]:
        return 1.00
    if _strip_leading_the(a[0]) == _strip_leading_the(b[0]):
        return 0.98
    for names, other in ((a, frozenset(b)), (b, frozenset(a))):
        for index, name in enumerate(names):
            if artist_fragment_usable(name, index) and name in other:
                return 0.95
    return min(SequenceMatcher(None, a[0], b[0]).ratio(), 0.90)


def title_ratio(q: NormalizedTitle, c: NormalizedTitle) -> float:
    return SequenceMatcher(None, q.base, c.base).ratio()


def title_gate(q: NormalizedTitle, c: NormalizedTitle) -> tuple[bool, str]:
    """Whether two titles may be compared further, and on which rung.

    The short-title rule is the one that stops the 2026-08-02 incident on its
    own. A four-character title carries almost no information, so it is required
    to carry all of it exactly.
    """
    if q.base == c.base:
        return True, "base_exact"
    if q.token_count <= 1 or len(q.base) <= 6:
        return False, "short_title_exact_only"
    if q.tokens == c.tokens:
        return True, "token_set_equal"
    if (
        title_ratio(q, c) >= MATCH_TITLE_FUZZY_GATE
        and abs(q.token_count - c.token_count) <= 1
    ):
        return True, "fuzzy"
    if q.base_alt and q.base_alt == c.base_alt and len(q.base_alt) > 6:
        return True, "paren_stripped"
    return False, "title_mismatch"


def _refused(gate: str, reasons: tuple[str, ...], matched_via: str = "") -> MatchDecision:
    return MatchDecision(
        outcome="refused",
        score=0,
        gate_failed=gate,
        matched_via=matched_via,
        reasons=reasons,
        capped=False,
    )


def score(q: TrackIdentity, c: TrackIdentity) -> MatchDecision:
    """Decide one candidate against one query."""
    if not q.artist_raw.strip() or not q.title_raw.strip():
        return _refused("EMPTY_FIELD", ("EMPTY_FIELD",))
    if not c.artist_raw.strip() or not c.title_raw.strip():
        return _refused("EMPTY_FIELD", ("EMPTY_FIELD",))

    mbid_agreement = bool(q.recording_mbids & c.recording_mbids)
    if q.recording_mbids and c.recording_mbids and not mbid_agreement:
        return _refused("MBID_CONFLICT", ("MBID_CONFLICT",))

    a_score = artist_score(q, c)
    if a_score < MATCH_ARTIST_GATE:
        return _refused("ARTIST_MISMATCH", ("ARTIST_MISMATCH",))

    passed, matched_via = title_gate(q.title, c.title)
    if not passed:
        gate = (
            "SHORT_TITLE_EXACT_ONLY"
            if matched_via == "short_title_exact_only"
            else "TITLE_MISMATCH"
        )
        return _refused(gate, (gate,), matched_via)

    if q.title.version != c.title.version:
        return _refused("VERSION_MISMATCH", ("VERSION_MISMATCH",), matched_via)

    if (
        q.duration_ms is not None
        and c.duration_ms is not None
        and abs(q.duration_ms - c.duration_ms) > MATCH_DURATION_GATE_S * 1000
    ):
        return _refused("DURATION_MISMATCH", ("DURATION_MISMATCH",), matched_via)

    points = 0.0
    reasons: list[str] = []

    if a_score >= 0.98:
        points += 40
        reasons.append("ARTIST_EXACT")
    else:
        points += 25 * a_score

    if matched_via == "fuzzy":
        points += 18 * title_ratio(q.title, c.title)
    else:
        # base_exact, token_set_equal and paren_stripped are all exact equality,
        # each on a different form of the title. How much weaker the alt form is
        # gets expressed by the cap below, not by a discount here: discounting
        # twice puts a perfect string match on the refusal boundary and makes
        # every soft penalty a veto.
        points += 30
        reasons.append("TITLE_EXACT")

    if mbid_agreement:
        points += 40
        reasons.append("MBID_AGREEMENT")
    isrc_agreement = bool(q.isrcs & c.isrcs)
    if isrc_agreement:
        points += 30
        reasons.append("ISRC_INTERSECTION")

    if q.duration_ms is not None and c.duration_ms is not None:
        delta = abs(q.duration_ms - c.duration_ms)
        if delta <= 3000:
            points += 12
            reasons.append("DURATION_AGREEMENT")
        elif delta <= 10000:
            points += 6
            reasons.append("DURATION_AGREEMENT")

    # An edition is only evidence when both sides state one. A store listing
    # "Falling Down" without "- Bonus Track" is not asserting that its copy is
    # not the bonus track, it just did not say, and an edition never changes
    # which recording this is.
    q_lyric = q.title.edition & _LYRIC_EDITIONS
    c_lyric = c.title.edition & _LYRIC_EDITIONS
    if q_lyric and c_lyric and q_lyric != c_lyric:
        points -= 8
        reasons.append("EXPLICIT_CLEAN_MISMATCH")

    q_edition = q.title.edition - _LYRIC_EDITIONS
    c_edition = c.title.edition - _LYRIC_EDITIONS
    if q_edition and c_edition:
        if q_edition == c_edition:
            points += 6
        else:
            points -= 6
            reasons.append("EDITION_MISMATCH")

    # Stores put `feat.` on either side of the artist/title line, so a credit
    # that agrees is worth something and one that does not is worth nothing.
    q_credits = credit_names(q.title.credits)
    if q_credits and q_credits == credit_names(c.title.credits):
        points += 4
        reasons.append("CREDITS_AGREE")

    if q.title.unclassified or c.title.unclassified:
        reasons.append("UNCLASSIFIED_QUALIFIER")

    total = max(0, min(100, int(round(points))))
    capped = False

    if not (mbid_agreement or isrc_agreement) and total > MATCH_STRING_ONLY_CAP:
        total = MATCH_STRING_ONLY_CAP
        capped = True
        reasons.append("STRING_ONLY_CAP")

    if matched_via == "paren_stripped":
        if total > MATCH_DEGRADED_CAP:
            total = MATCH_DEGRADED_CAP
            capped = True
        reasons.append("MATCHED_ON_PAREN_STRIPPED_FORM")

    if q.identity_degraded or c.identity_degraded:
        if total > MATCH_DEGRADED_CAP:
            total = MATCH_DEGRADED_CAP
            capped = True
        reasons.append("DEGRADED_IDENTITY")

    if total >= MATCH_AUTO_MIN:
        outcome = "auto"
    elif total >= MATCH_CONFIRM_MIN:
        outcome = "confirm"
    else:
        outcome = "refused"

    return MatchDecision(
        outcome=outcome,
        score=total,
        gate_failed=None,
        matched_via=matched_via,
        reasons=tuple(reasons),
        capped=capped,
    )


def best_match(
    q: TrackIdentity, candidates: Sequence[TrackIdentity]
) -> tuple[MatchDecision, int | None]:
    """Score every candidate and pick a winner, or refuse.

    The index is the candidate the decision is about, so the interface can print
    the item that was rejected as well as the one that was chosen. It is None
    when the decision is about the field as a whole: no candidates at all, or
    two that are too close to separate.
    """
    if not candidates:
        return _NO_CANDIDATE, None

    scored = [(score(q, c), i) for i, c in enumerate(candidates)]
    survivors = sorted(
        (s for s in scored if s[0].outcome != "refused"),
        key=lambda s: s[0].score,
        reverse=True,
    )

    if not survivors:
        best = max(scored, key=lambda s: s[0].score)
        return best[0], best[1]

    if len(survivors) > 1 and survivors[0][0].score - survivors[1][0].score <= AMBIGUITY_MARGIN:
        return _refused("AMBIGUOUS_TOP_CANDIDATES", ("AMBIGUOUS_TOP_CANDIDATES",)), None

    return survivors[0][0], survivors[0][1]


def explain(q: TrackIdentity, c: TrackIdentity | None, decision: MatchDecision) -> str:
    """One line naming the evidence, for the audit trail.

    This is diagnostic text: score thresholds, similarity ratios, gate names.
    It goes into a log line or an enrichment `match_decision` row, read by
    someone debugging the matcher, not by the person who was refused a
    download. `reader_message`, below, is what that person sees; the two
    exist separately so that fixing one wording never quietly changes the
    other. `c` is None only for the two outcomes that are about the candidate
    list rather than about one candidate.
    """
    mine = f"{q.artist_raw} - {q.title_raw}"
    gate = decision.gate_failed

    if gate == "NO_CANDIDATES":
        return f"nothing to compare against {mine}"
    if gate == "AMBIGUOUS_TOP_CANDIDATES":
        return (
            f"two candidates for {mine} scored within {AMBIGUITY_MARGIN} points "
            "of each other, so neither was picked"
        )
    if c is None:
        return f"no decision recorded for {mine}"

    theirs = f"{c.artist_raw} - {c.title_raw}"
    if gate == "EMPTY_FIELD":
        return f"{mine} or {theirs} has no artist or no title to compare"
    if gate == "MBID_CONFLICT":
        return f"{theirs} is a different MusicBrainz recording from {mine}"
    if gate == "ARTIST_MISMATCH":
        return (
            f"artist {c.artist_raw!r} does not match {q.artist_raw!r} "
            f"(agreement {artist_score(q, c):.2f}, needs {MATCH_ARTIST_GATE:.2f})"
        )
    if gate == "SHORT_TITLE_EXACT_ONLY":
        return (
            f"{q.title_raw!r} is {len(q.title.base)} characters, too short to match "
            f"anything but itself, and {c.title_raw!r} is not it"
        )
    if gate == "TITLE_MISMATCH":
        return (
            f"title {c.title_raw!r} does not match {q.title_raw!r} "
            f"(similarity {title_ratio(q.title, c.title):.2f}, "
            f"needs {MATCH_TITLE_FUZZY_GATE:.2f})"
        )
    if gate == "VERSION_MISMATCH":
        return (
            f"{theirs} is a different version: "
            f"{sorted(c.title.version) or 'no qualifier'} against "
            f"{sorted(q.title.version) or 'no qualifier'}"
        )
    if gate == "DURATION_MISMATCH":
        return (
            f"{theirs} runs {(c.duration_ms or 0) / 1000:.0f}s against "
            f"{(q.duration_ms or 0) / 1000:.0f}s, more than "
            f"{MATCH_DURATION_GATE_S}s apart"
        )
    verdict ="matched" if decision.outcome == "auto" else "needs confirming"
    return (
        f"{theirs} {verdict} at {decision.score}/{MATCH_AUTO_MIN} "
        f"via {decision.matched_via} ({', '.join(decision.reasons) or 'no evidence'})"
    )


def reader_message(q: TrackIdentity, c: TrackIdentity | None, decision: MatchDecision) -> str:
    """The one sentence a person reads when a claim is refused.

    Where `explain` names the evidence for someone debugging the matcher, this
    names the situation for someone who just spent money and needs to know
    what to do next: which candidate was closest, and specifically what was
    wrong with it. No score, no ratio, no threshold; those already have their
    own place in the refusal panel next to this sentence. The real artist and
    title are named wherever that is the fact that matters, and always with
    which side said what, because "the track you wanted" against "the closest
    purchase" is the one distinction a refusal cannot afford to blur.
    """
    if decision.gate_failed == "NO_CANDIDATES" or c is None:
        return "Nothing in the account came close enough to be this track."
    if decision.gate_failed == "AMBIGUOUS_TOP_CANDIDATES":
        return ("Two purchases in the account are equally close matches, so neither"
                " was picked automatically.")
    if decision.gate_failed == "EMPTY_FIELD":
        return ("The track you wanted or the closest purchase has no artist or no"
                " title to compare.")
    if decision.gate_failed == "MBID_CONFLICT":
        return "The closest purchase is tagged as a different recording than the track you wanted."
    if decision.gate_failed == "ARTIST_MISMATCH":
        return (
            "The closest purchase we found is by a different artist than the track"
            f" you wanted: you want {q.artist_raw!r}, and the closest purchase is"
            f" credited to {c.artist_raw!r}."
        )
    if decision.gate_failed in ("TITLE_MISMATCH", "SHORT_TITLE_EXACT_ONLY"):
        return (
            "The closest purchase has a different title: you want"
            f" {q.title_raw!r}, and the closest purchase is {c.title_raw!r}."
        )
    if decision.gate_failed == "VERSION_MISMATCH":
        theirs_v = ", ".join(sorted(c.title.version)) or "no qualifier"
        ours_v = ", ".join(sorted(q.title.version)) or "no qualifier"
        return (
            "The closest purchase is a different version of the track you wanted:"
            f" {theirs_v} instead of {ours_v}."
        )
    if decision.gate_failed == "DURATION_MISMATCH":
        theirs_s = (c.duration_ms or 0) / 1000
        ours_s = (q.duration_ms or 0) / 1000
        return (
            f"The lengths do not match: the closest purchase runs {theirs_s:.0f}s,"
            f" and the track you wanted runs {ours_s:.0f}s."
        )
    if decision.outcome == "confirm":
        # Passed every gate, so nothing here is wrong the way the branches
        # above are wrong; it is short of the auto threshold and wants a
        # human nod rather than a reason to be turned away.
        return (
            f"The closest purchase, {c.artist_raw} - {c.title_raw}, looks right but did"
            " not score high enough to download automatically."
        )
    # No gate failed and the outcome is still refused: every candidate was
    # weighed and none of them, on the whole, was a strong enough match.
    return "Nothing in the account came close enough to be this track."


def require_match(
    q: TrackIdentity, candidates: Sequence[TrackIdentity]
) -> tuple[MatchDecision, int]:
    """`best_match`, but a refusal stops the caller.

    Claiming spends money and writes a file, so the caller of that path wants
    the refusal as control flow rather than as a value it might forget to check.
    The exception carries `reader_message(...)` as its message, because that
    message is what the refusal panel shows; `reason` stays the short gate
    code, because that is what the audit trail records.
    """
    decision, index = best_match(q, candidates)
    if decision.outcome == "refused" or index is None:
        rejected = candidates[index] if index is not None else None
        raise MatchRefused(
            reader_message(q, rejected, decision),
            score=decision.score,
            candidate=rejected,
            reason=decision.gate_failed or "refused",
        )
    return decision, index


__all__ = [
    "AMBIGUITY_MARGIN",
    "MATCHER_VERSION",
    "artist_score",
    "best_match",
    "explain",
    "reader_message",
    "require_match",
    "score",
    "title_gate",
    "title_ratio",
]
