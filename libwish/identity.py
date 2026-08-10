"""Normalization, the qualifier lexicon, and the fingerprint that replaces `dedup_key`.

Three decisions here carry the whole subsystem.

Folding decomposes to NFKD and drops combining marks before it touches anything
else, so `Michael Bublé` and `Michael Buble` reduce to the same key instead of
one of them losing its last syllable. Everything that is not an ASCII
alphanumeric then becomes a space rather than being deleted, because deleting it
fuses two words into a token that matches nothing (`Dap-Kings` became
`dapkings`, `Uncle Albert/Admiral Halsey` became `uncle albertadmiral`). A fold
that empties a field is a real outcome, not an edge case: the artist `¥$` folds
to nothing, and an empty key would compare equal to every other empty key and
behave as a wildcard. Such a field falls back to the casefolded raw string and
the identity is marked degraded, which caps it below the auto threshold.

A parenthetical is a qualifier only when its contents match the closed lexicon
below; anything else stays in the base title. Keeping unrecognised text makes
matching stricter, and the two failure directions are not symmetric: a needless
refusal costs a click, a wrong match costs a wrong file in the library. Text
inside a media tie-in qualifier is the one thing that is deleted outright and
never restored, because no legitimate alternate spelling of a title contains the
name of the television series it was licensed to, and treating that text as
evidence is what bought the wrong recording on 2026-08-02.

Nothing here compares one side's text against the other. That is match.py's job,
and `NormalizedTitle.__contains__` raises so that a substring test written by
accident is a crash rather than a silent false positive.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from typing import Iterable

from .models import Identifiers, LovedTrack, NormalizedTitle, OwnedItem, TrackIds, TrackIdentity

# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------

#: Typographic characters that carry no distinguishing information but that
#: stores and sources spell inconsistently within the same catalogue.
PUNCT_MAP = {
    "‘": "'", "’": "'",      # curly single quotes
    "“": '"', "”": '"',      # curly double quotes
    "–": "-", "—": "-",      # en and em dash
    "…": "...",                   # ellipsis
}

_AMP_RE = re.compile(r"\s*[&+]\s*")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SPACE_RE = re.compile(r"\s+")


def fold(s: str) -> str:
    """Reduce a display string to a comparable key.

    Order matters: compatibility folding before casefold so full-width and
    ligature forms collapse first, decomposition after casefold so a mark on an
    upper-case letter is still stripped.
    """
    s = unicodedata.normalize("NFKC", s)
    s = "".join(PUNCT_MAP.get(c, c) for c in s)
    s = s.casefold()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _AMP_RE.sub(" and ", s)
    s = s.replace("'", "")            # apostrophes vanish: "Aint" must equal "Ain't"
    s = _NON_ALNUM_RE.sub(" ", s)
    return _SPACE_RE.sub(" ", s).strip()


def _fallback(raw: str) -> str:
    """The key for a field the fold emptied.

    A script the fold cannot represent still identifies its owner, so the raw
    casefolded text is kept rather than an empty string that would compare equal
    to every other unfoldable field.
    """
    return raw.casefold().strip() or "?"


# ---------------------------------------------------------------------------
# The qualifier lexicon
# ---------------------------------------------------------------------------

#: Patterns are matched against the *folded* contents of a qualifier, in this
#: order. `credit` and `tiein` come first because their bodies are free text
#: that a later `version` pattern could otherwise claim. A remaster is treated
#: as a `version`, which disqualifies: a remaster is a different master, and
#: defaulting strict means the user gets asked rather than silently handed audio
#: they did not pick.
#:
#: `standard` is the class for a qualifier that names the ordinary studio cut.
#: `Album Version` is a store's way of distinguishing a track from the radio
#: edit or single version pressed alongside it, so it describes the same audio a
#: plain title describes and is dropped rather than compared. Every other member
#: of that family (`Radio Edit`, `Single Version`, `Extended Version`) names a
#: genuinely different recording and stays a `version`.
LEXICON: tuple[tuple[str, str], ...] = (
    ("credit",  r"^(feat|ft|featuring|with)\b\.?\s+.+"),
    ("tiein",   r"^(from|theme from)\b.*$"),
    ("tiein",   r"^.*\b(soundtrack|motion picture|original score|ost)\b.*$"),
    ("standard", r"^album version$"),
    ("version", r"^(\d{4}\s+)?remaster(ed)?(\s+\d{4})?$"),
    ("version", r"^(radio|single|album|extended|club|dance)\s+(edit|version|mix)$"),
    ("version", r"^(extended|acoustic|instrumental|demo|live|unplugged|remix|edit|mix|dub"
                r"|slowed|sped up|nightcore|reverb|vip|karaoke|a cappella|orchestral|piano)"
                r"( version)?$"),
    ("version", r"^.+\s+(remix|mix|edit|version|rework|bootleg)$"),
    ("version", r"^(mono|stereo)$"),
    ("version", r"^(live|recorded)\b.*$"),
    ("edition", r"^(bonus track|deluxe|reissue|anniversary edition|special edition|expanded)$"),
    ("edition", r"^(explicit|clean)$"),
)

_COMPILED_LEXICON = tuple((cls, re.compile(pat)) for cls, pat in LEXICON)

#: Qualifiers recognised as a bare trailing phrase, with no bracket and no dash
#: to announce them. Deliberately much smaller than the bracketed lexicon: an
#: unbracketed suffix is a guess about where the title ends.
_STRICT_SUFFIX_RE = re.compile(
    r"^(?P<head>.+?)\s+(?P<qual>extended version|radio edit|album version|single version"
    r"|original mix|remastered|remaster)$"
)

_BRACKET_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_DASH_RE = re.compile(r"\s+-\s+")
_FEAT_RE = re.compile(r"\s*\b(feat|ft|featuring)\b\.?\s+", re.IGNORECASE)
_CREDIT_MARKER_RE = re.compile(r"^(feat|ft|featuring|with)\s+")


def lexicon_hash() -> str:
    """Identity of the lexicon that produced a decision.

    Stored on every `match_decision` row so a later replay can tell whether a
    decision predates a lexicon change without reading the repository history.
    """
    payload = "\n".join(f"{cls}\t{pat}" for cls, pat in LEXICON)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify(folded_qualifier: str) -> str | None:
    """The lexicon class of one qualifier, or None when nothing matches."""
    for cls, rx in _COMPILED_LEXICON:
        if rx.match(folded_qualifier):
            return cls
    return None


class _Qualifiers:
    """Accumulator for one pass over a title."""

    def __init__(self) -> None:
        self.version: set[str] = set()
        self.edition: set[str] = set()
        self.credits: list[str] = []
        self.unclassified: list[str] = []
        self.tie_in = False

    def add(self, cls: str, text: str) -> None:
        if cls == "version":
            self.version.add(text)
        elif cls == "edition":
            self.edition.add(text)
        elif cls == "credit":
            self.credits.append(text)
        elif cls == "tiein":
            self.tie_in = True      # contents are dropped on the floor, by design
        # A `standard` qualifier is recorded nowhere: naming the ordinary studio
        # cut says the same thing as saying nothing, so the two forms of the
        # title have to produce the same fingerprint.


def _strip_brackets(raw: str, q: _Qualifiers, *, drop_unclassified: bool) -> str:
    """Remove classified bracket groups, recording what each one was.

    An unrecognised group stays in the text for the `base` pass and is removed
    for the `base_alt` pass, which is the only form allowed to be more
    permissive than `base` and is capped accordingly by the scorer.
    """
    out: list[str] = []
    last = 0
    for m in _BRACKET_RE.finditer(raw):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        cls = classify(fold(inner))
        out.append(raw[last:m.start()])
        if cls is None:
            q.unclassified.append(fold(inner))
            if not drop_unclassified:
                out.append(" " + inner + " ")
        else:
            q.add(cls, fold(inner))
        last = m.end()
    out.append(raw[last:])
    return "".join(out)


def _strip_dash_trailers(raw: str, q: _Qualifiers) -> str:
    """Peel trailing ` - ` segments that the lexicon recognises.

    Only recognised segments are peeled. `Moonlight sonata - 1st movement` keeps
    its movement, which is the case that kills a blanket strip-after-the-dash
    rule, and it is in the live queue today.
    """
    while True:
        matches = list(_DASH_RE.finditer(raw))
        if not matches:
            return raw
        last = matches[-1]
        tail = fold(raw[last.end():])
        cls = classify(tail)
        if cls is None:
            return raw
        q.add(cls, tail)
        raw = raw[:last.start()]


def _strip_inline_feat(raw: str, q: _Qualifiers) -> str:
    """Split an unbracketed `feat.` credit off the end of a title."""
    m = _FEAT_RE.search(raw)
    if not m or not raw[:m.start()].strip() or not raw[m.end():].strip():
        return raw
    q.add("credit", fold(raw[m.end():]))
    return raw[:m.start()]


def _strip_strict_suffix(folded: str, q: _Qualifiers) -> str:
    while True:
        m = _STRICT_SUFFIX_RE.match(folded)
        if not m:
            return folded
        cls = classify(m.group("qual"))
        if cls is None:
            return folded
        q.add(cls, m.group("qual"))
        folded = m.group("head")


def _reduce(raw: str, *, drop_unclassified: bool) -> tuple[str, _Qualifiers]:
    q = _Qualifiers()
    text = _strip_brackets(raw, q, drop_unclassified=drop_unclassified)
    text = _strip_inline_feat(text, q)
    text = _strip_dash_trailers(text, q)
    return _strip_strict_suffix(fold(text), q), q


def parse_title(raw: str) -> NormalizedTitle:
    """Split a title into a comparable base plus its classified qualifiers."""
    base, q = _reduce(raw, drop_unclassified=False)
    base_alt, _ = _reduce(raw, drop_unclassified=True)
    if not base:
        base = _fallback(raw)
    if not base_alt:
        base_alt = base
    words = base.split()
    return NormalizedTitle(
        base=base,
        base_alt=base_alt,
        tokens=frozenset(words),
        token_count=len(words),
        version=frozenset(q.version),
        edition=frozenset(q.edition),
        credits=tuple(q.credits),
        tie_in=q.tie_in,
        unclassified=tuple(q.unclassified),
    )


def credit_names(credits: Iterable[str]) -> frozenset[str]:
    """Credit bodies with the marker word removed, so a bracketed
    `feat. X` and an unbracketed `feat. X` compare equal."""
    names = {_CREDIT_MARKER_RE.sub("", c).strip() for c in credits}
    return frozenset(n for n in names if n)


# ---------------------------------------------------------------------------
# Artists
# ---------------------------------------------------------------------------

ART_SPLIT = re.compile(
    r"\s*(?:,|/|;|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\band\b|\bx\b|\bvs\b\.?|&)\s*",
    re.IGNORECASE,
)


def normalize_artist(raw: str) -> tuple[str, ...]:
    """Candidate names for one artist credit, whole string first.

    The fragments exist so a queue row credited `Warren G, Nate Dogg, The Game`
    can match a store item credited `Warren G`. They are never the whole
    identity: `artist_key` is always the first entry, the whole folded string.
    An empty result is impossible on purpose, because an empty name would
    compare equal to every other unfoldable name.
    """
    whole = fold(raw)
    parts = [f for f in (fold(p) for p in ART_SPLIT.split(raw)) if f]
    names = [n for n in [whole] + [p for p in parts if p != whole] if n]
    return tuple(dict.fromkeys(names)) or (_fallback(raw),)


def artist_fragment_usable(name: str, index: int) -> bool:
    """Whether a candidate name may carry a match on its own.

    `Crosby, Stills & Nash` fragments into `nash`, and an unrelated artist
    called Nash must not match on that. Four characters and either two words or
    the whole-string entry itself.
    """
    return len(name) >= 4 and (index == 0 or len(name.split()) >= 2)


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def clean_isrc(raw: str | None) -> str | None:
    """An ISRC in comparable form, or None if it is not one.

    Stores and file tags write ISRCs with the separators the standard allows,
    so they are removed before the grammar check rather than after.
    """
    if not raw:
        return None
    candidate = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return candidate if _ISRC_RE.match(candidate) else None


def clean_mbid(raw: str | None) -> str | None:
    """A UUID in comparable form, or None. Says nothing about validation."""
    if not raw:
        return None
    candidate = raw.strip().lower()
    return candidate if _UUID_RE.match(candidate) else None


# ---------------------------------------------------------------------------
# Identity construction
# ---------------------------------------------------------------------------


def build_identity(
    artist: str,
    title: str,
    *,
    mbids: Iterable[str] = (),
    isrcs: Iterable[str] = (),
    duration_ms: int | None = None,
    store: str | None = None,
    store_id: str | None = None,
    raw: dict | None = None,
) -> TrackIdentity:
    """One side of a comparison.

    `mbids` are recording MBIDs that have already been validated against
    MusicBrainz. Nothing else belongs there: Last.fm hands out MBIDs that do not
    resolve and track MBIDs where a recording MBID is expected, and both are
    UUIDs, so shape proves nothing. An unvalidated hint is stored on the
    `track_mbid` row with `validated = 0` and carries no weight in scoring.
    """
    artists = normalize_artist(artist)
    parsed = parse_title(title)
    degraded = not fold(artist) or not fold(title)
    return TrackIdentity(
        artist_raw=artist,
        title_raw=title,
        artists=artists,
        artist_key=artists[0],
        title=parsed,
        recording_mbids=frozenset(m for m in (clean_mbid(x) for x in mbids) if m),
        isrcs=frozenset(i for i in (clean_isrc(x) for x in isrcs) if i),
        duration_ms=duration_ms,
        identity_degraded=degraded,
        store=store,
        store_id=store_id,
        raw=raw,
    )


def from_loved_track(track: LovedTrack, *, validated_mbids: Iterable[str] = ()) -> TrackIdentity:
    """Identity of a queue row, from what a source reported.

    `track.ids.recording_mbid` is deliberately not promoted here. It reaches
    `recording_mbids` only by being passed back in as `validated_mbids` after a
    MusicBrainz lookup confirms it, which is a job, never part of ingest.
    """
    ids: TrackIds = track.ids
    return build_identity(
        track.artist,
        track.title,
        mbids=validated_mbids,
        isrcs=(ids.isrc,) if ids.isrc else (),
        duration_ms=None if track.duration_s is None else int(track.duration_s * 1000),
        raw=track.raw,
    )


def from_owned_item(item: OwnedItem, *, validated_mbids: Iterable[str] = ()) -> TrackIdentity:
    """Identity of something a store says the user already owns.

    `OwnedItem.duration_s` is a store-reported float; the millisecond integer is
    the unit every comparison and every column uses, so the conversion happens
    here at the boundary and nowhere else.
    """
    ids: Identifiers = item.ids
    return build_identity(
        item.artist,
        item.title,
        mbids=validated_mbids,
        isrcs=(ids.isrc,) if ids.isrc else (),
        duration_ms=None if item.duration_s is None else int(round(item.duration_s * 1000)),
        store=item.store,
        store_id=item.item_key,
        raw=item.raw or None,
    )


FP_SEP = "\x1f"
QUALIFIER_SEP = "\x1e"


def qualifier_key(ident: TrackIdentity) -> str:
    """Sorted version qualifiers. Editions and credits are excluded because they
    do not change which recording this is."""
    return QUALIFIER_SEP.join(sorted(ident.title.version))


def fingerprint(ident: TrackIdentity) -> str:
    """The tier-3 identity: artist, title and version qualifiers, joined."""
    return FP_SEP.join((ident.artist_key, ident.title.base, qualifier_key(ident)))


# ---------------------------------------------------------------------------
# Offline backfill, called by a job rather than by a migration
# ---------------------------------------------------------------------------


def recompute_identity_columns(
    conn: sqlite3.Connection,
) -> list[tuple[str, list[int]]]:
    """Fill the identity columns on every `tracks` row from `artist` and `title`.

    `dedup_key` is never read. The live database holds two incompatible dialects
    of it in one column, so parsing it would inherit whichever dialect a given
    row happened to be written by, along with the bugs that produced each.

    Pure, offline and idempotent, so it can be run again against a snapshot.
    Rows whose fingerprint collides with another row's keep `fp_key` NULL and
    are returned for a human to look at: a wrong merge silently loses a queue
    row, so nothing is merged automatically.
    """
    rows = conn.execute("SELECT id, artist, title FROM tracks").fetchall()
    computed: list[tuple[int, TrackIdentity, str]] = []
    seen: dict[str, list[int]] = {}
    for row in rows:
        track_id, artist, title = row[0], row[1] or "", row[2] or ""
        ident = build_identity(artist, title)
        fp = fingerprint(ident)
        computed.append((track_id, ident, fp))
        seen.setdefault(fp, []).append(track_id)

    collisions = {fp: ids for fp, ids in seen.items() if len(ids) > 1}
    conn.executemany(
        "UPDATE tracks SET artist_key=?, title_key=?, qualifier_key=?, fp_key=?, "
        "identity_degraded=? WHERE id=?",
        [
            (
                ident.artist_key,
                ident.title.base,
                qualifier_key(ident),
                None if fp in collisions else fp,
                1 if ident.identity_degraded else 0,
                track_id,
            )
            for track_id, ident, fp in computed
        ],
    )
    return sorted((fp, ids) for fp, ids in collisions.items())


def rows_due_for_identity_lookup(
    conn: sqlite3.Connection, now: int, *, limit: int = 500
) -> list[int]:
    """Track ids whose identity is still string-only and due a MusicBrainz try.

    The lookup itself is networked and rate limited to one request per second,
    which is why it belongs to a job and not to a migration: migrations run
    inside a transaction at startup, on a box that may have no route out.
    """
    rows = conn.execute(
        "SELECT id FROM tracks "
        "WHERE identity_tier = 'string' AND merged_into IS NULL "
        "  AND (identity_lookup_after IS NULL OR identity_lookup_after <= ?) "
        "ORDER BY id LIMIT ?",
        (now, limit),
    ).fetchall()
    return [row[0] for row in rows]


def find_existing(conn: sqlite3.Connection, ident: TrackIdentity) -> int | None:
    """The track this identity already is, walking the ladder strongest first."""
    for mbid in sorted(ident.recording_mbids):
        row = conn.execute(
            "SELECT track_id FROM track_mbid "
            "WHERE mbid=? AND validated=1 AND kind='recording'",
            (mbid,),
        ).fetchone()
        if row:
            return row[0]
    for isrc in sorted(ident.isrcs):
        row = conn.execute(
            "SELECT track_id FROM track_isrc WHERE isrc=?", (isrc,)
        ).fetchone()
        if row:
            return row[0]
    row = conn.execute(
        "SELECT id FROM tracks WHERE fp_key=? AND merged_into IS NULL",
        (fingerprint(ident),),
    ).fetchone()
    return row[0] if row else None


__all__ = [
    "ART_SPLIT",
    "LEXICON",
    "PUNCT_MAP",
    "artist_fragment_usable",
    "build_identity",
    "classify",
    "clean_isrc",
    "clean_mbid",
    "credit_names",
    "find_existing",
    "fingerprint",
    "fold",
    "from_loved_track",
    "from_owned_item",
    "lexicon_hash",
    "normalize_artist",
    "parse_title",
    "qualifier_key",
    "recompute_identity_columns",
    "rows_due_for_identity_lookup",
]
