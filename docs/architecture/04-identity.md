# Track identity and matching

Agent 4 deliverable. Written 2026-08-09.

Scope: canonical track identity across sources and stores, the replacement for
`dedup_key`, normalization rules, confidence scoring with a refusal threshold, and
the audit trail that makes a wrong match diagnosable afterwards.

Everything below was checked against the live 164 rows exported to
`docs/architecture/_tracks-sample.json`. Where I cite a number or a parse result it
came from running the prototype in section 5, not from imagination.

---

## Decisions

1. **Identity is a three-tier ladder, stored as separate columns, not one opaque
   string.** Validated MusicBrainz recording MBID, then ISRC, then a structured
   string fingerprint. The tier in use is recorded per row (`identity_tier`) so the
   UI and the matcher both know how much the row's identity is worth.

2. **An identifier is not an identity until it has been validated against
   MusicBrainz.** Last.fm hands out MBIDs that do not resolve, and per
   [LB-431](https://community.metabrainz.org/t/lb-431-last-fm-api-returns-track-mbid-instead-of-recording-mbid-for-new-scrobbles/431016)
   it returns *track* MBIDs where a *recording* MBID is expected. Both are UUIDs and
   nothing about the shape tells them apart. An unvalidated MBID is a hint stored in
   `track_mbid` with `validated=0`; it carries zero weight in scoring.

3. **A parenthetical is a qualifier only if its contents match a closed lexicon.
   Otherwise it stays in the base title.** This is the resolution of the tension the
   brief names. `(I Just)`, `(2 AM)` and `(Ooh, Ooh, Ooh)` match nothing in the
   lexicon and are kept; `(feat. Toro y Moi)`, `(a Tasson Soundtrack)`,
   `(from "Hazbin Hotel")`, `(Slowed)` and `(Remastered)` match and are extracted.
   The default direction on an unknown parenthetical is **keep**, because keeping
   text makes matching stricter and the two failure modes are not symmetric: a
   needless refusal costs one click, a wrong match costs a wrong file in the library.

4. **Text inside a media tie-in qualifier is deleted and never restored.** No
   alternate form of a title ever contains `Tell Me Lies Season 3`. This is the
   structural kill for the 2026-08-02 incident, independent of any scoring.

5. **There is no substring comparison anywhere in the matcher, and this is enforced
   by types, not by discipline.** Comparison functions take `NormalizedTitle`, whose
   `__contains__` raises `TypeError`. `title in candidate` is not a bug you can write
   by accident; it is a crash with a message naming the incident.

6. **Gates disqualify, evidence scores.** Six boolean gates run first and can only
   refuse. Only survivors get a score. This means a candidate cannot accumulate
   enough soft evidence to overcome a hard mismatch, which is how the original bug
   would have been re-derived under a pure-score design.

7. **String evidence alone can never auto-claim.** With no validated MBID and no
   shared ISRC, the score is hard-capped at 84, below the 90 auto threshold. The
   worst case for a Bandcamp purchase is one confirmation click, and the product
   already has a human in the loop at Buy time, so a second click at Claim time is a
   cheap price for correctness.

8. **`dedup_key` is not migrated, it is recomputed.** The 164 live rows already
   contain *two incompatible dedup_key dialects* (section 6). Any migration that
   parses the existing key inherits both bugs. Recompute from `artist` and `title`.

9. **Every match decision, including every refusal and every losing candidate, is
   written to SQLite with the raw candidate payload as received.** This makes the
   matcher replayable offline: `replay --since <ts>` re-runs today's matcher over
   yesterday's stored candidates and reports which decisions flip, with no store
   access.

10. **The matcher is one module used in all three places** (dedup at ingest, enrich,
    claim). The incident happened in a bespoke implementation in a sibling job.
    There are no per-caller variants.

---

## 1. What actually went wrong, restated precisely

The 2026-08-02 failure was not "fuzzy matching was too loose". It was three separate
structural mistakes stacked:

| Mistake | Consequence |
|---|---|
| Search-then-take-first-hit | The store's relevance ranking became the identity decision |
| Comparison over the whole raw title string | Words injected by a soundtrack parenthetical became matchable evidence |
| Verification by `title in downloaded` | Tautological: any hit containing the query term passes |

The third one is the reason the audit pass agreed with the bug. A verification that
shares an implementation assumption with the thing it verifies is not a verification.
Every guard in this document is written so that it can fail, and section 8 specifies
the CI test that proves each one does.

Concretely: `Lies` (4 characters) appeared inside `Tell Me Lies Season 3`. Under the
design here, that candidate is stopped **three independent times**:

- the tie-in parenthetical is deleted before any comparison, so `Lies` is not
  present in the candidate's comparable text at all;
- the base titles are `lies` and `such great heights`, which are not equal, and the
  short-title rule forbids fuzzy comparison for a 4-character title;
- the artists do not agree, which is a gate, not a deduction.

Any one of the three refuses. That redundancy is deliberate; a single guard is a
single point of failure.

---

## 2. Identity model

### 2.1 The ladder

```
tier 1  recording_mbid   validated MusicBrainz recording MBID
tier 2  isrc             one or more ISRCs, upper-case, 12 chars, [A-Z]{2}[A-Z0-9]{3}[0-9]{7}
tier 3  fingerprint      (artist_key, title_key, qualifier_key) derived from strings
```

A row always has tier 3. It gains tier 2 and tier 1 opportunistically, and never
loses them.

**Where identifiers come from:**

| Source | MBID | ISRC | Notes |
|---|---|---|---|
| ListenBrainz feedback | recording MBID, native | no | The only source that supplies a trustworthy recording MBID directly |
| Last.fm loves | sometimes, unreliable | no | May be a track MBID or an MBID that does not resolve; must be validated |
| Subsonic / Navidrome `getStarred` | only if the local file is tagged (Picard) | from file tags | The user's own library, so tag quality is whatever their tagger did |
| Deezer favourites and Deezer metadata | no | yes, `track.isrc` | Also `/track/isrc:<ISRC>` for reverse lookup |
| Qobuz | no | yes, on the track object | |
| 7digital | no | yes | |
| Bandcamp | no | no | Structurally tier 3 only; see section 7.4 |
| MusicBrainz `/ws/2/isrc/{isrc}` | yes | (input) | The bridge from tier 2 up to tier 1 |

**ISRC semantics, stated honestly.** An ISRC identifies a recording, so different
edits, remixes and live versions get different ISRCs. That makes ISRC *equality*
strong evidence. But the reverse does not hold: one recording legitimately carries
several ISRCs (territorial distributors, re-registration on reissue), and some labels
reuse an ISRC across a remaster in violation of the standard. Therefore:

- **ISRC equality is strong positive evidence** (+30) and a valid dedup key.
- **ISRC disagreement is not evidence of anything** and never disqualifies.
- ISRCs are stored as a set per track, not a column.

### 2.2 Getting to tier 1 when you start with neither

Two lookups, in order, both rate limited:

1. **ListenBrainz MBID mapper** -
   `GET https://api.listenbrainz.org/1/metadata/lookup/?artist_name=&recording_name=`.
   This is MetaBrainz's own solution to exactly this problem and it is a better first
   call than raw MB search because it already carries their normalization.
2. **MusicBrainz search** -
   `GET https://musicbrainz.org/ws/2/recording?query=artist:"..." AND recording:"..."&fmt=json&inc=isrcs`,
   [1 request per second, meaningful `User-Agent` required](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)
   (`library-wishlist/<version> ( <contact-url> )`). Exceeding this gets the IP
   blocked, so the client serialises through one token bucket shared by the whole
   process.

**The mapper's answer is a candidate, not an identity.** It goes through the same
`score()` as a store candidate. If the scorer refuses, the row stays tier 3. Skipping
this is how you move the trust problem instead of solving it.

### 2.3 What to do with neither

Nothing special. Tier 3 is a first-class state, not a failure:

- the row sits in the queue and displays normally;
- buy links are generated from the normalized strings (that direction is safe, it
  produces a URL for a human to look at, not a download);
- a Claim can still complete, but only through CONFIRM, never AUTO;
- an opportunistic backfill retries the tier-1 lookup when the row is next touched,
  with an exponential backoff recorded in `identity_lookup_after`, so a track that
  MusicBrainz gains next month is picked up without a scan.

Of the 164 live rows, my expectation is that roughly the 8 `lastfm`/`listenbrainz`
rows may carry a usable MBID and the 156 imported rows carry nothing, so the backfill
in section 6 is the only path to tier 1 for almost the whole queue.

---

## 3. Normalization

Two separate operations that must not be confused: **parsing** (splitting a title
into base plus qualifiers, done on the original text so the lexicon can see word
boundaries and quotes) and **folding** (reducing a string to a comparable key).

### 3.1 Folding, in order

```python
def fold(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)          # 1 width/compat folding
    s = "".join(PUNCT_MAP.get(c, c) for c in s)   # 2 ’‘ -> ' , “” -> " , –— -> - , … -> ...
    s = s.casefold()                              # 3 casefold, not lower()
    s = unicodedata.normalize("NFKD", s)          # 4 decompose
    s = "".join(c for c in s if not unicodedata.combining(c))   # 5 drop diacritics
    s = re.sub(r"\s*[&+]\s*", " and ", s)         # 6 & and + become "and"
    s = s.replace("'", "")                        # 7 apostrophes vanish entirely
    s = re.sub(r"[^a-z0-9]+", " ", s)             # 8 everything else becomes a SPACE
    return re.sub(r"\s+", " ", s).strip()         # 9 collapse
```

Every step earns its place from the live data:

| Step | Row that requires it | Current production behaviour |
|---|---|---|
| 4+5 diacritic fold | `Michael Bublé` | current key is `michael bubl`, the `é` is **deleted**, not folded |
| 7 apostrophe removal | `Kenneth Weines - Aint That Strong` vs the canonical `Ain't`; also `Que' Onda Guero` (misplaced apostrophe for `Qué`) | inconsistent |
| 8 punctuation to **space** | `Coleen feat. The Dap-Kings Horns`, `Uncle Albert/Admiral Halsey` | current keys are `dapkings` and `uncle albertadmiral`, two words fused into a token that matches nothing |
| 6 `&` and `+` | `Alice Coltrane - Turiya & Ramakrishna`, `Florence + The Machine`, `Crosby, Stills & Nash` | `&` deleted, so `past future` from `Past & Future` |
| 3 casefold | `USHER`, `CONTINENTAL BREAKFAST`, `skyemane`, `Right Back Where We Started from` | ok |

Step 8 replacing with a space rather than nothing is the single highest-value fix in
this section, and it is a live bug today.

**Digits are left alone.** No "2" -> "two" expansion. `Breathe (2 AM)`,
`Tech N9ne`, `38 Special`, `3 Doors Down`, `32 Leaves` all keep their digits, and the
store spells them the same way.

**A fold must never produce an empty field.** `¥$` folds to `""`, which is why the
live `dedup_key` for that row is `"\tfield trip"` - the artist component is *gone*,
and any other symbol-only artist with a track called `FIELD TRIP` would collide with
it. Rule: if `fold()` returns empty, fall back to `raw.casefold().strip()` and set
`identity_degraded = 1` on the row. A degraded row is capped at CONFIRM regardless of
score and is shown in the UI with a "needs identity" affordance.

### 3.2 Parsing: the qualifier lexicon

A qualifier is recognised in three positions, all subject to the same lexicon:

1. bracketed: `(...)` or `[...]`
2. unbracketed trailing after ` - ` (Spotify and Deezer style)
3. unbracketed trailing suffix, strict sub-lexicon only (`Extended Version`,
   `Radio Edit`, `Album Version`, `Single Version`, `Original Mix`, `Remastered`)
4. unbracketed inline `feat.` / `ft.` / `featuring`, which splits the title at that
   point regardless of brackets

The lexicon, with the class each entry maps to:

| Class | Patterns (folded, anchored) |
|---|---|
| `credit` | `^(feat\|ft\|featuring\|with)\b\.?\s+.+` |
| `version` | `^(\d{4}\s+)?remaster(ed)?(\s+\d{4})?$`; `^(radio\|single\|album\|extended\|club\|dance)\s+(edit\|version\|mix)$`; `^(extended\|acoustic\|instrumental\|demo\|live\|unplugged\|remix\|edit\|mix\|dub\|slowed\|sped up\|nightcore\|reverb\|vip\|karaoke\|a cappella\|orchestral\|piano)( version)?$`; `^.+\s+(remix\|mix\|edit\|version\|rework\|bootleg)$`; `^(mono\|stereo)$`; `^(live\|recorded)\b.*$` |
| `edition` | `^(bonus track\|deluxe\|reissue\|anniversary edition\|special edition\|expanded)$`; `^(explicit\|clean)$` |
| `tiein` | `^(from\|theme from)\b.*$`; `^.*\b(soundtrack\|motion picture\|original score\|ost)\b.*$` |
| *(no match)* | stays in the base, and is appended to `unclassified` |

**`unclassified` is instrumented, not ignored.** Every unclassified parenthetical
increments a counter keyed by its folded text. After a few weeks the top entries are
the real lexicon gaps, and the lexicon grows from observed data rather than from
guessing. This matters because the lexicon is the one part of this design that is
inherently incomplete.

### 3.3 What that produces on the live rows

Verbatim prototype output for every non-trivial title in the 164:

```
Cutting Crew - (I Just) Died In Your Arms
    base='i just died in your arms'  alt='died in your arms'  unclassified=['i just']
Anna Nalick - Breathe (2 AM)
    base='breathe 2 am'              alt='breathe'            unclassified=['2 am']
Rich Homie Quan - Flex (Ooh, Ooh, Ooh)
    base='flex ooh ooh ooh'          alt='flex'               unclassified=['ooh ooh ooh']
0xSleep - Heavenly (a Tasson Soundtrack) (feat. Eddie Watson)
    base='heavenly'   credits=['feat eddie watson']   tiein=True
One Project - Brighter (from "Hazbin Hotel") Extended Version
    base='brighter'   version={'extended version'}    tiein=True
Chromeo - Come Alive (feat. Toro y Moi)
    base='come alive' credits=['feat toro y moi']
The Heavy - Coleen feat. The Dap-Kings Horns
    base='coleen'     credits=['the dap kings horns']
Feeling Blew - Out Getting Ribs (Slowed)
    base='out getting ribs'          version={'slowed'}
Yoke Lore - Truly Madly Deeply - Recorded at  Studios NYC
    base='truly madly deeply'        version={'recorded at studios nyc'}
Lil Peep - Falling Down - Bonus Track
    base='falling down'              edition={'bonus track'}
Igor Kotliarevsky - Moonlight sonata - 1st movement
    base='moonlight sonata 1st movement'      (dash NOT split: '1st movement' is not a qualifier)
Columbia Symphony Orchestra - Symphony No. 4 in E Minor, Op. 98: I. Allegro non troppo
    base='symphony no 4 in e minor op 98 i allegro non troppo'   (colon is never a split point)
```

The two rows the brief flags as the tension resolve correctly and in opposite
directions, from a single rule. `Moonlight sonata - 1st movement` is the case that
kills a naive "strip after the dash" rule, and `Symphony No. 4 ... Op. 98: I. Allegro`
is the case that kills a "split on the colon" rule; neither is hypothetical, both are
in the queue today.

### 3.4 Qualifier classes and what a mismatch means

| Class | Effect on recording identity | On mismatch |
|---|---|---|
| `version` | Different recording. `(Slowed)`, `(Live at ...)`, `(Extended Version)`, `(Radio Edit)` are genuinely different audio | **Gate 4 disqualifies** |
| `edition` | Same audio, different packaging. `Bonus Track`, `Deluxe`, `Reissue` | -6, still eligible |
| `edition` explicit/clean | Genuinely different audio, but usually acceptable to a buyer | -8 and reason code `EXPLICIT_CLEAN_MISMATCH` surfaced in the UI, never silent |
| `credit` | Same recording; stores place `feat.` inconsistently between the artist and title fields | free on mismatch, +4 on agreement |
| `tiein` | Does not change the audio, but injects arbitrary words | neutral for scoring, contents **deleted** and never comparable |
| `remaster` (a `version` entry) | Debatable. A remaster is a different master but the same performance | treated as `version` (disqualifying) by default, with a config flag `MATCH_REMASTER_EQUIVALENT=0`. Defaulting to strict means the user gets asked; defaulting loose means they silently get the wrong master |

### 3.5 Artist normalization

Artist strings in the live data carry at least five different shapes. Rather than try
to parse them into a single canonical name, produce a **tuple of candidate names,
whole string first**:

```python
ART_SPLIT = re.compile(r"\s*(?:,|/|;|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\band\b|\bx\b|\bvs\b\.?|&)\s*", re.I)

def normalize_artist(raw: str) -> tuple[str, ...]:
    whole = fold(raw)
    parts = [fold(p) for p in ART_SPLIT.split(raw) if fold(p)]
    names = [whole] + [p for p in parts if p != whole]
    return tuple(dict.fromkeys(names)) or (raw.casefold().strip() or "?",)
```

Prototype output on the live shapes:

```
'Michael Bublé'                     -> ('michael buble',)
'Yusuf / Cat Stevens'               -> ('yusuf cat stevens', 'yusuf', 'cat stevens')
'Warren G, Nate Dogg, The Game'     -> ('warren g nate dogg the game', 'warren g', 'nate dogg', 'the game')
'Crosby, Stills & Nash'             -> ('crosby stills and nash', 'crosby', 'stills', 'nash')
'Florence + The Machine'            -> ('florence and the machine',)
'Tom Petty And The Heartbreakers'   -> ('tom petty and the heartbreakers', 'tom petty', 'the heartbreakers')
'¥$'                                -> ('',)   -> degraded fallback
```

`artist_key` is `names[0]`, the whole folded string. The fragments exist only to let a
queue row credited `Warren G, Nate Dogg, The Game` match a store item credited
`Warren G`.

**The fragment guard.** `Crosby, Stills & Nash` fragments into `nash`, and a store
item by an unrelated artist called Nash must not match on that. So a fragment is
usable as containment evidence only if it is at least 4 characters **and** (at least
two tokens **or** it is the first fragment). `nash` fails both, `warren g` passes,
`the game` passes.

`artist_score` is the max over these rungs:

| Rung | Score |
|---|---|
| whole strings equal | 1.00 |
| equal after stripping a leading `the` | 0.98 |
| a guard-passing name from one side appears in the other side's name set | 0.95 |
| `SequenceMatcher(None, a, b).ratio()` on the whole strings | that ratio, capped at 0.90 |

The cap on the fuzzy rung matters: fuzzy can reach the artist gate at 0.85 but can
never present itself as exact, so it can never earn the +40 exact-artist bonus.

`difflib.SequenceMatcher` is stdlib, which keeps the one-external-dependency posture
from locked decision 3. It is a mediocre similarity metric and it is used only for the
lowest rung, never as the sole reason anything passes a gate.

---

## 4. Matching: gates, then score, then a threshold

### 4.1 Gates (boolean, can only refuse)

Evaluated in order; the first failure is recorded as `gate_failed` and stops
evaluation.

| # | Gate | Reason code |
|---|---|---|
| 0 | Both sides have a validated recording MBID and they differ | `MBID_CONFLICT` |
| 1 | `artist_score < 0.85` | `ARTIST_MISMATCH` |
| 2 | Title gate fails (see below) | `TITLE_MISMATCH` |
| 3 | Query base is <= 1 token or <= 6 characters and the titles are not exactly equal | `SHORT_TITLE_EXACT_ONLY` |
| 4 | `version` qualifier sets differ | `VERSION_MISMATCH` |
| 5 | Both durations known and they differ by more than 15s | `DURATION_MISMATCH` |

```python
def title_gate(q: NormalizedTitle, c: NormalizedTitle) -> tuple[bool, str]:
    if q.base == c.base:                      return True,  "base_exact"
    if q.token_count <= 1 or len(q.base) <= 6: return False, "short_title_exact_only"
    if q.tokens == c.tokens:                  return True,  "token_set_equal"
    if (SequenceMatcher(None, q.base, c.base).ratio() >= 0.92
            and abs(q.token_count - c.token_count) <= 1):
        return True, "fuzzy"
    if q.base_alt and q.base_alt == c.base_alt and len(q.base_alt) > 6:
        return True, "paren_stripped"        # forces CONFIRM, caps score at 74
    return False, "title_mismatch"
```

**Gate 1 is a gate and not a score component on purpose.** The dangerous shape is a
perfect title with a poor artist, because that is exactly what a store search returns
for a cover or tribute recording. The queue contains at least four:
`Guitar Tribute Players - The Only Exception` (Paramore), `Audereus - Clubbed To Death`
(Rob Dougan), `Trey Anastasio - Clint Eastwood` (Gorillaz), `Done Again -
Uncle Albert/Admiral Halsey` (Paul and Linda McCartney). Searching a store for any of
those titles will surface the famous original with a perfect title score. Under a
weighted-sum design a strong title could drag a weak artist over the line; under a
gate it cannot, and the refusal carries the reason code
`ARTIST_MISMATCH` so the UI can say "this looks like a different artist's version".

**Gate 3, the short-title rule**, is the one that would have stopped the incident on
its own. Within the 164 live rows there are already five title-substring pairs:

```
Steely Dan - Peg           inside   Buddy Holly - Peggy Sue
Nine Inch Nails - Only     inside   Guitar Tribute Players - The Only Exception
Nine Inch Nails - Only     inside   Carpenters - We've Only Just Begun
The Game - My Life         inside   Kelly Clarkson - My Life Would Suck Without You
Patsy Cline - Crazy        inside   Heart - Crazy On You
```

Plus `Chris Brown - X`, a one-character title that is a substring of a large fraction
of the recorded catalogue. Short titles carry almost no information, so they are
required to carry all of it exactly.

**The `paren_stripped` rung** is the concession that keeps rule 3 from being too
strict in the other direction. A store that lists `Died In Your Arms` without the
`(I Just)` would otherwise be unreachable. That rung is allowed to satisfy the gate,
but it forces `outcome = confirm` and caps the score at 74, and records
`MATCHED_ON_PAREN_STRIPPED_FORM`. Note it can only ever *remove* text; the tie-in
contents were already deleted upstream, so no form of the title ever reintroduces
`Tell Me Lies Season 3` as evidence. That is invariant I2 below.

### 4.2 Score (only for gate survivors)

```
+40  artist exact (rung 1 or 2)          |  +25 * artist_score otherwise
+30  title base exact                    |  +18 * title_ratio otherwise
+40  validated recording MBIDs equal
+30  ISRCs intersect
+12  durations within 3s                 |  +6 within 10s
 +6  edition qualifier sets identical
 +4  credit sets agree
 -8  explicit/clean mismatch
 -6  edition qualifier mismatch
-10  the queue artist appears on the candidate only as a featured credit
cap 100
```

Then, unconditionally:

```python
if not (mbid_agreement or isrc_intersection):
    score = min(score, 84)          # invariant I3
if matched_via == "paren_stripped" or query.identity_degraded:
    score = min(score, 74)
```

### 4.3 Thresholds

| Score | Outcome | Behaviour |
|---|---|---|
| >= 90 | `auto` | Proceed. Reachable only with a validated MBID or a shared ISRC |
| 70 - 89 | `confirm` | Show both sides side by side, require a click. **Never downloads on its own** |
| < 70 | `refused` | `match_status = 'no_confident_match'`, reason codes shown, offer manual store-URL paste |

A refusal is a normal outcome and must look like one in the UI, not like an error. The
correct copy is "I could not confirm this is the same recording", with the two titles
shown and the specific reason, not "match failed".

### 4.4 The three invariants

- **I1 - no substring.** No comparison uses `in`, `startswith`, `endswith`, or a regex
  built from one side's data applied to the other. Only whole-field equality, token
  set equality, and a bounded ratio.
- **I2 - tie-in text is not evidence.** Text inside a classified `tiein` qualifier is
  removed before scoring and is not present in `base`, `base_alt`, or `tokens`.
- **I3 - strings never auto-claim.** Without a validated MBID or a shared ISRC the
  score cannot reach the auto threshold.

I1 is enforced by making it a type error:

```python
@dataclass(frozen=True)
class NormalizedTitle:
    base: str
    base_alt: str
    tokens: frozenset[str]
    token_count: int
    version: frozenset[str]
    edition: frozenset[str]
    credits: tuple[str, ...]
    tie_in: bool
    unclassified: tuple[str, ...]

    def __contains__(self, item):
        raise TypeError(
            "substring containment is not a match; see docs/architecture/04-identity.md"
        )
```

### 4.5 Public surface

```python
# identity.py
def fold(s: str) -> str: ...
def normalize_artist(raw: str) -> tuple[str, ...]: ...
def parse_title(raw: str) -> NormalizedTitle: ...
def build_identity(artist: str, title: str, *, mbids=(), isrcs=(),
                   duration_ms=None) -> TrackIdentity: ...
def fingerprint(ident: TrackIdentity) -> str: ...   # artist_key \x1f title_key \x1f qualifier_key

# match.py
def score(q: TrackIdentity, c: TrackIdentity) -> MatchDecision: ...
def best_match(q: TrackIdentity,
               candidates: Sequence[TrackIdentity]) -> tuple[MatchDecision, int | None]: ...
```

```python
@dataclass(frozen=True)
class TrackIdentity:
    artist_raw: str
    title_raw: str
    artists: tuple[str, ...]          # whole string first, then guard-passing fragments
    artist_key: str
    title: NormalizedTitle
    recording_mbids: frozenset[str]   # validated only
    isrcs: frozenset[str]
    duration_ms: int | None
    identity_degraded: bool = False
    store: str | None = None
    store_id: str | None = None
    raw: dict | None = None           # the untouched payload, carried for the audit log

@dataclass(frozen=True)
class MatchDecision:
    outcome: Literal["auto", "confirm", "refused"]
    score: int
    gate_failed: str | None
    matched_via: str                  # base_exact | token_set_equal | fuzzy | paren_stripped
    reasons: tuple[str, ...]
    capped: bool
```

`best_match` scores every candidate, returns the winner, and **returns `refused` if
the top two survivors are within 5 points of each other** (`AMBIGUOUS_TOP_CANDIDATES`).
Two near-identical candidates usually means two masters of the same song, and picking
one silently is how the user ends up with the wrong one.

### 4.6 Reason codes

Refusals: `MBID_CONFLICT`, `ARTIST_MISMATCH`, `TITLE_MISMATCH`,
`SHORT_TITLE_EXACT_ONLY`, `VERSION_MISMATCH`, `DURATION_MISMATCH`,
`AMBIGUOUS_TOP_CANDIDATES`, `NO_CANDIDATES`.

Caps and warnings: `STRING_ONLY_CAP`, `MATCHED_ON_PAREN_STRIPPED_FORM`,
`DEGRADED_IDENTITY`, `EXPLICIT_CLEAN_MISMATCH`, `EDITION_MISMATCH`,
`UNVALIDATED_MBID_IGNORED`, `UNCLASSIFIED_QUALIFIER`.

Positive: `MBID_AGREEMENT`, `ISRC_INTERSECTION`, `ARTIST_EXACT`, `TITLE_EXACT`,
`DURATION_AGREEMENT`, `CREDITS_AGREE`.

### 4.7 Verified behaviour

Prototype run over the incident and over pairs drawn from the live rows. `BLOCK` means
a gate refused; `PASS` means it reached scoring.

```
BLOCK [short_title_exact_only /ARTIST]  CHVRCHES - Lies  ~~  Such Great Heights (From "Tell Me Lies Season 3")
BLOCK [short_title_exact_only /ARTIST]  Patsy Cline - Crazy  ~~  Heart - Crazy On You
BLOCK [short_title_exact_only /ARTIST]  Nine Inch Nails - Only  ~~  Carpenters - We've Only Just Begun
BLOCK [short_title_exact_only /ARTIST]  Nine Inch Nails - Only  ~~  Guitar Tribute Players - The Only Exception
BLOCK [short_title_exact_only /ARTIST]  Steely Dan - Peg  ~~  Buddy Holly - Peggy Sue
BLOCK [title_mismatch /ARTIST]          The Game - My Life  ~~  Kelly Clarkson - My Life Would Suck Without You
BLOCK [base_exact /VERSION_MISMATCH]    Chris Brown - X  ~~  Chris Brown - X (Album Version)
BLOCK [base_exact /VERSION_MISMATCH]    Feeling Blew - Out Getting Ribs (Slowed)  ~~  Out Getting Ribs
BLOCK [base_exact /VERSION_MISMATCH]    Yoke Lore - Truly Madly Deeply - Recorded at Studios NYC  ~~  Truly Madly Deeply
BLOCK [base_exact /VERSION_MISMATCH]    Cutting Crew - (I Just) Died In Your Arms  ~~  (I Just) Died in Your Arms - 2010 Remaster
BLOCK [title_mismatch]                  Igor Kotliarevsky - Moonlight sonata - 1st movement  ~~  Moonlight sonata
PASS  [base_exact]                      CHVRCHES - Lies  ~~  CHVRCHES - Lies
PASS  [base_exact]                      Michael Bublé - Heartache Tonight  ~~  Michael Buble - Heartache Tonight
PASS  [base_exact]                      The Heavy - Coleen feat. The Dap-Kings Horns  ~~  The Heavy - Coleen
PASS  [base_exact]                      0xSleep - Heavenly (a Tasson Soundtrack) (feat. Eddie Watson)  ~~  0xSleep - Heavenly
PASS  [base_exact]                      Lil Peep - Falling Down - Bonus Track  ~~  Lil Peep - Falling Down
PASS  [base_exact]                      Anna Nalick - Breathe (2 AM)  ~~  Anna Nalick - Breathe (2 AM)
PASS  [paren_stripped]                  Anna Nalick - Breathe (2 AM)  ~~  Anna Nalick - Breathe
PASS  [paren_stripped]                  Cutting Crew - (I Just) Died In Your Arms  ~~  Cutting Crew - Died In Your Arms
```

Note the CHVRCHES line: **two independent gates fire**, not one. And the two
`paren_stripped` passes are exactly the cases the brief asked to be handled without
corrupting `(I Just)` or `(2 AM)`; they reach scoring, get capped at 74, and land in
CONFIRM where the user sees both titles.

The `Moonlight sonata - 1st movement` block is the honest cost of the conservative
dash rule. A store listing that track as `Moonlight Sonata` alone would refuse. That
is one click, not a wrong file.

---

## 5. Prototype

The parse, fold, artist-split and gate logic in this document exist as a runnable
prototype used to produce every quoted result. It is roughly 90 lines and depends on
`json`, `re`, `unicodedata`, `collections` and `difflib` only. It should land as
`libwish/identity.py` and `libwish/match.py`, and the fixture in section 8 should be
generated from the live export.

Nothing was written to the Mini or to `~/music-stack` (locked decision, brief section 7).

---

## 6. Schema and the migration of 164 rows

### 6.1 The finding that shapes the migration

The live `dedup_key` values are **not internally consistent**. Comparing each row's
stored key against `artist.lower() + "\t" + title.lower()`:

| Group | Count | Key dialect |
|---|---|---|
| `lastfm` + `listenbrainz` + 115 `deezer-unobtainable` | 123 | exactly `lower()`, punctuation preserved |
| 41 `deezer-unobtainable` | 41 | punctuation stripped by a different importer |

Visible in adjacent rows: `Lil Peep - Falling Down - Bonus Track` stores
`lil peep\tfalling down - bonus track` (hyphen kept) while `Yoke Lore - Truly Madly
Deeply - Recorded at  Studios NYC` stores `...truly madly deeply recorded at studios
nyc` (hyphen and double space gone). Two rows, two rules, one column.

Consequence: **the migration must recompute from `artist` and `title` and must never
read `dedup_key`.** A migration that parses the old key inherits whichever dialect it
happens to hit, including the `michael bubl` and empty-artist bugs.

### 6.2 New schema

```sql
-- v2 additions to tracks
ALTER TABLE tracks ADD COLUMN artist_key     TEXT NOT NULL DEFAULT '';
ALTER TABLE tracks ADD COLUMN title_key      TEXT NOT NULL DEFAULT '';
ALTER TABLE tracks ADD COLUMN qualifier_key  TEXT NOT NULL DEFAULT '';  -- sorted version qualifiers, \x1e joined
ALTER TABLE tracks ADD COLUMN fp_key         TEXT;                      -- artist_key \x1f title_key \x1f qualifier_key
ALTER TABLE tracks ADD COLUMN identity_tier  TEXT NOT NULL DEFAULT 'string';  -- mbid | isrc | string
ALTER TABLE tracks ADD COLUMN identity_degraded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tracks ADD COLUMN duration_ms    INTEGER;
ALTER TABLE tracks ADD COLUMN identity_lookup_after INTEGER;             -- backoff for tier-1 retry
ALTER TABLE tracks ADD COLUMN match_status   TEXT NOT NULL DEFAULT 'unmatched';
                                     -- unmatched | needs_confirm | confirmed | no_confident_match
ALTER TABLE tracks ADD COLUMN merged_into    INTEGER REFERENCES tracks(id);

CREATE TABLE track_mbid(
  track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  mbid       TEXT NOT NULL,
  kind       TEXT NOT NULL,                -- recording | track | unknown
  validated  INTEGER NOT NULL DEFAULT 0,   -- 1 only after a successful MB lookup
  source     TEXT NOT NULL,                -- listenbrainz | lastfm | mb-search | lb-mapper | user
  first_seen INTEGER NOT NULL,
  PRIMARY KEY(track_id, mbid)
);
CREATE INDEX ix_track_mbid_lookup ON track_mbid(mbid)
  WHERE validated = 1 AND kind = 'recording';

CREATE TABLE track_isrc(
  track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  isrc       TEXT NOT NULL,                -- upper-case, validated against the ISRC grammar
  source     TEXT NOT NULL,                -- deezer | qobuz | 7digital | musicbrainz | tags
  first_seen INTEGER NOT NULL,
  PRIMARY KEY(track_id, isrc)
);
CREATE INDEX ix_track_isrc_lookup ON track_isrc(isrc);

CREATE UNIQUE INDEX ux_tracks_fp ON tracks(fp_key)
  WHERE fp_key IS NOT NULL AND merged_into IS NULL;
```

The fingerprint uniqueness is a *partial* index scoped to unmerged rows, so a merge
frees the key rather than deadlocking on it. MBID and ISRC uniqueness are deliberately
**not** enforced by an index, because one recording legitimately carries several
ISRCs and because a row can gain an MBID after insert, which would turn a routine
enrichment into a constraint violation. Deduplication goes through an explicit lookup
ladder instead:

```python
def find_existing(conn, ident: TrackIdentity) -> int | None:
    for mbid in ident.recording_mbids:                        # tier 1
        row = conn.execute("SELECT track_id FROM track_mbid "
                           "WHERE mbid=? AND validated=1 AND kind='recording'",
                           (mbid,)).fetchone()
        if row: return row[0]
    for isrc in ident.isrcs:                                  # tier 2
        row = conn.execute("SELECT track_id FROM track_isrc WHERE isrc=?",
                           (isrc,)).fetchone()
        if row: return row[0]
    row = conn.execute("SELECT id FROM tracks "               # tier 3
                       "WHERE fp_key=? AND merged_into IS NULL",
                       (fingerprint(ident),)).fetchone()
    return row[0] if row else None
```

and a merge is explicit and recorded, never implicit:

```python
def merge_track(conn, loser_id: int, winner_id: int, reason: str) -> None: ...
```

which moves `track_mbid` / `track_isrc` rows, sets `merged_into`, preserves the
strongest `status` (`purchased` beats `queued` beats `ignored`), and writes a
`match_decision` row with `phase='dedup'`.

### 6.3 Migration procedure

1. Gate on `PRAGMA user_version = 1`. Snapshot with `VACUUM INTO
   '/config/queue.db.pre-v2'`. Never migrate in place without the snapshot.
2. Apply the DDL above. **Leave `dedup_key` in place and unused for one release** as
   the rollback path and as something to diff against.
3. Offline backfill, no network: compute `artist_key`, `title_key`, `qualifier_key`,
   `fp_key`, `identity_degraded` for all 164 rows from `artist` and `title`.
4. Emit a collision report **before** creating `ux_tracks_fp`. Do not auto-merge; a
   wrong merge silently loses a queue row, and 164 rows is a readable list.
   Verified on the live export: **164 distinct `fp_key` values for 164 rows, zero
   collisions**, so this step is expected to be a no-op today. It is in the procedure
   because it will not be a no-op on someone else's database.
5. Set `identity_degraded = 1` on the rows where `fold()` empties a field. On the live
   data that is exactly one row, `¥$ - FIELD TRIP`.
6. Online backfill, rate limited to 1 req/sec, so under three minutes for 164 rows:
   ListenBrainz mapper then MB search per row. Feed each response through `score()`.
   `auto` writes a validated recording MBID plus any ISRCs and promotes
   `identity_tier`. `confirm` writes `match_status='needs_confirm'` and nothing else.
   `refused` leaves the row at tier 3 and sets `identity_lookup_after` to now + 7 days.
7. Every one of those 164 lookups is written to `match_decision` with
   `phase='backfill'`, so the backfill itself is auditable and replayable. If the
   lexicon changes next month, `replay` reports which of the 164 identities would now
   be decided differently.
8. The `needs_confirm` rows surface as a one-time "confirm identity" list in the UI.
   Nothing is guessed on the user's behalf.
9. `PRAGMA user_version = 2`.
10. Drop `dedup_key` in the v3 migration, one release later.

Steps 3 to 5 are pure functions of the existing columns and are idempotent, so the
migration can be re-run against the snapshot as many times as needed.

### 6.4 A data-hygiene note for Agents 1 and 5

156 of the 164 rows have `source_platform = 'deezer-unobtainable'`, which is a
*status* stored in a *source* column. Whatever the source-provider contract ends up
being, that value is not a source and will need a separate `unobtainable` flag or
status value. Flagging it here because it will otherwise be inherited into the new
schema.

---

## 7. The audit trail

### 7.1 Tables

```sql
CREATE TABLE match_decision(
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id       INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  decided_at     INTEGER NOT NULL,
  phase          TEXT NOT NULL,   -- dedup | enrich | identity | claim | backfill
  provider       TEXT NOT NULL,   -- qobuz | bandcamp | 7digital | deezer | musicbrainz | lb-mapper
  matcher_version TEXT NOT NULL,  -- semver of match.py
  lexicon_hash   TEXT NOT NULL,   -- sha256 of the serialised qualifier lexicon
  outcome        TEXT NOT NULL,   -- auto | confirm | refused | user_confirmed | user_rejected | user_override
  score          INTEGER,
  gate_failed    TEXT,
  matched_via    TEXT,
  reasons        TEXT NOT NULL,   -- JSON array of reason codes
  query_json     TEXT NOT NULL,   -- full serialised TrackIdentity of the queue row
  candidate_json TEXT,            -- RAW provider payload of the winner, NULL on refusal
  candidates_considered INTEGER NOT NULL,
  chosen_store_id TEXT,
  file_path      TEXT,
  file_sha256    TEXT,
  duration_ms    INTEGER          -- of the file actually written, when known
);
CREATE INDEX ix_md_track   ON match_decision(track_id, decided_at DESC);
CREATE INDEX ix_md_outcome ON match_decision(outcome, decided_at DESC);

CREATE TABLE match_candidate(
  decision_id    INTEGER NOT NULL REFERENCES match_decision(id) ON DELETE CASCADE,
  rank           INTEGER NOT NULL,
  score          INTEGER,
  gate_failed    TEXT,
  reasons        TEXT NOT NULL,
  candidate_json TEXT NOT NULL,   -- RAW provider payload
  PRIMARY KEY(decision_id, rank)
);
```

### 7.2 The five properties that make a wrong match diagnosable

1. **Refusals are logged.** Most of the diagnostic value is in what was rejected and
   why. A log that only records successes cannot answer "why did nothing happen".
2. **Losing candidates are logged.** Without `match_candidate` you cannot tell whether
   the correct item was present and lost to a better-scoring wrong one, or was never
   returned by the store at all. Those two failures need completely different fixes.
3. **The candidate payload is stored raw, as received.** Not the parsed form. When a
   match is wrong the first question is whether the store lied or the parser
   misread, and a stored parse cannot answer it. This is also what makes replay
   possible with no network access and no live store session.
4. **`matcher_version` and `lexicon_hash` are stored.** They answer "was this decided
   before or after the rule change" without archaeology, and they drive replay.
5. **`file_sha256` and the written `duration_ms` are stored.** They tie a decision to
   the bytes that actually landed in the library, so a wrong file found six months
   later resolves back to the decision that fetched it.

### 7.3 Replay

```
libwish audit replay --since 2026-08-01
libwish audit replay --track 163
```

Re-runs the current `score()` over every stored `query_json` / `candidate_json` pair
and reports each decision whose outcome would change, grouped by reason code. It needs
no store credentials and no network. This is the tool that would have caught the
incident in review rather than in the library, and it is also the regression check for
any change to the lexicon.

Retention: keep `match_decision` forever (the whole history for 164 tracks is
kilobytes). Prune `match_candidate` rows older than 180 days if it grows; a
`VACUUM` after.

### 7.4 The magic-byte check is not identity

`fetch_purchase` currently checks the `fLaC` magic bytes. That check should stay, but
it is worth stating plainly for whoever reads Agent 2's document alongside this one:
**magic bytes prove format, not identity.** The wrong track that was downloaded on
2026-08-02 was a perfectly valid FLAC file. The identity check is the matcher and the
duration comparison, not the file header.

After a download, three post-conditions are checked and recorded on the decision row:

| Check | On failure |
|---|---|
| container magic bytes match the declared format | quarantine, do not move into the library |
| decoded duration is within 15s of the matched candidate's | quarantine, `DURATION_MISMATCH_POST_DOWNLOAD` |
| embedded ISRC tag, if present, intersects the track's ISRC set | warn and surface, do not quarantine (tag quality is poor) |

Quarantine means the file stays in a staging directory, the track stays in the queue,
and the UI shows the problem. That preserves the validate-before-remove guarantee
already in `fetch_purchase`.

### 7.5 Stores that cannot supply identifiers

Bandcamp is the case. No ISRC, no MBID, and the artist-supplied title is often the
most accurate string in the whole system. Such a store is permanently tier 3, so every
claim goes through CONFIRM. Two mitigations, neither of which weakens invariant I3:

- Bandcamp purchases are enumerated from the user's own purchase list, so the
  candidate set is small and entirely made of things the user demonstrably bought. A
  CONFIRM against a five-item list is a trivial interaction.
- The downloaded file's tags can be run back through the ListenBrainz mapper to gain
  an MBID *after* the fact, which upgrades the row's tier for future dedup.

I considered a per-store `trust_string_match` opt-in that would raise the cap to 92
for a named store. I am recommending against it: it is a switch whose only function is
to disable the guard that exists because of a real incident, and the thing it saves is
one click.

---

## 8. Tests that can fail

The audit that reproduced the bug is the reason this section exists. Three test
classes:

**1. Positive corpus** - `tests/corpus/positive.tsv`, pairs that must reach at least
CONFIRM. Seeded from the live rows: `Breathe (2 AM)` vs `Breathe (2 AM)`,
`Michael Bublé` vs `Michael Buble`, `Coleen feat. The Dap-Kings Horns` vs `Coleen`,
`Heavenly (a Tasson Soundtrack) (feat. Eddie Watson)` vs `Heavenly`,
`Falling Down - Bonus Track` vs `Falling Down`.

**2. Negative corpus** - `tests/corpus/negative.tsv`, pairs that must be REFUSED, led
by the real incident:

```
CHVRCHES	Lies	The Postal Service	Such Great Heights (From "Tell Me Lies Season 3")
Patsy Cline	Crazy	Heart	Crazy On You
Nine Inch Nails	Only	Carpenters	We've Only Just Begun
Nine Inch Nails	Only	Guitar Tribute Players	The Only Exception
Steely Dan	Peg	Buddy Holly	Peggy Sue
The Game	My Life	Kelly Clarkson	My Life Would Suck Without You
Chris Brown	X	Chris Brown	X (Album Version)
Feeling Blew	Out Getting Ribs (Slowed)	Feeling Blew	Out Getting Ribs
Igor Kotliarevsky	Moonlight sonata - 1st movement	Igor Kotliarevsky	Moonlight sonata
Guitar Tribute Players	The Only Exception	Paramore	The Only Exception
Audereus	Clubbed To Death	Rob Dougan	Clubbed To Death
```

The last two are the cover-version gate: perfect title, wrong artist, which is the
exact shape a store search returns.

**3. The meta-test, which is the point.** A deliberately naive matcher is checked into
the test module:

```python
def naive_match(q_artist, q_title, c_artist, c_title):
    """The 2026-08-02 implementation. Present only so the corpus can be shown to reject it."""
    return q_title.lower() in c_title.lower()

def test_negative_corpus_rejects_the_naive_matcher():
    failures = [row for row in NEGATIVE if naive_match(*row)]
    assert failures, (
        "the negative corpus no longer catches substring matching; "
        "it has stopped testing what it exists to test"
    )
```

If the corpus ever stops catching the original bug, the corpus itself has rotted and
CI says so. A corpus that only asserts the current implementation passes cannot tell
you it has become vacuous.

**4. Invariant tests.**

```python
def test_normalized_title_refuses_containment():
    with pytest.raises(TypeError):
        _ = "lies" in parse_title('Such Great Heights (From "Tell Me Lies Season 3")')

def test_tie_in_text_is_not_reachable():
    t = parse_title('Such Great Heights (From "Tell Me Lies Season 3")')
    for field in (t.base, t.base_alt, " ".join(t.tokens)):
        assert "lies" not in field and "tell me" not in field

def test_string_only_evidence_cannot_auto():
    for q, c in POSITIVE_STRING_ONLY:
        assert score(q, c).outcome != "auto"
```

**5. A grep-level guard.** A test that reads `match.py` and `identity.py` and asserts
no `in `, `.startswith(`, `.endswith(` or `.find(` appears against a title or artist
variable. Cruder than the type guard and it will produce the occasional false
positive, but it catches a helper written in a hurry outside the dataclass.

**6. Live-corpus fixture.** `tests/fixtures/live-164.json`, the export in
`docs/architecture/_tracks-sample.json`, with an assertion that all 164 rows produce
164 distinct fingerprints and that exactly one row is `identity_degraded`. That pins
the current known state, so any normalization change that starts merging live rows
fails loudly.

---

## Open questions and risks

1. **The lexicon is inherently incomplete and is the weakest part of this design.**
   Every guard downstream is sound; the lexicon is a list of patterns someone wrote
   down. Mitigation is the `unclassified` counter in section 3.2, which turns the gap
   into observable data. But there will be a period where real qualifiers sit in base
   titles and cause refusals. I think that is the right failure direction and I want
   it stated rather than smoothed over.

2. **`feat` as an ordinary word.** The unbracketed `feat.` rule uses
   `\b(feat|ft|featuring)\b\.?\s+`. `Feats Don't Fail Me Now` is safe because of the
   word boundary, but I have not proven there is no title where `feat` appears as a
   standalone word followed by more words. The risk is a truncated base title, which
   causes a refusal, not a wrong match. Acceptable, but it should be on the
   `unclassified` dashboard.

3. **Remaster equivalence is a genuine judgement call and I have defaulted strict.**
   Treating `(2011 Remaster)` as a different recording means a user whose queue row
   came from a plain title will be asked to confirm a remastered purchase. Some people
   will find that annoying. `MATCH_REMASTER_EQUIVALENT=1` makes remaster an `edition`
   rather than a `version`. I do not have data on how often this fires and would want
   to see the first month's `match_decision` rows before choosing a different default.

4. **`explicit` vs `clean` is classed as `edition` with a penalty, which is arguably
   wrong.** They are different audio. I classed it soft because a buyer usually
   accepts either and because stores are inconsistent about labelling. If it turns out
   users care, promoting it to `version` is a one-line change.

5. **AcoustID / Chromaprint is the strongest possible post-download verification and I
   am not recommending it for v1.** It would confirm the audio itself rather than the
   metadata, which is the only check that is genuinely independent of the store's
   strings. The cost is the `fpcalc` native binary in a multi-arch image plus
   `pyacoustid`, against locked decision 3's minimal-dependency posture and locked
   decision 7's multi-arch build. Worth revisiting once the image exists; the duration
   check in 7.4 is the cheap 80 percent.

6. **`lb-matching-tools` (`MetadataCleaner`) does the suffix cleaning we are
   reimplementing.** I am recommending against a runtime dependency, partly for the
   dependency posture and mainly because we need the parse *structure* (which
   qualifier class, kept separately) and it returns a cleaned string. But it should be
   used as a **dev-time oracle**: a test that runs both over the live 164 and reports
   divergences is a cheap way to find lexicon gaps we would not think of.

7. **`SequenceMatcher` is a mediocre similarity function.** Jaro-Winkler would be
   better for the artist rung. It is confined to the lowest rung and capped at 0.90 so
   it can never be the sole cause of an exact-match bonus, but the 0.85 artist gate and
   the 0.92 title ratio are thresholds I picked from reasoning about the live data, not
   from measurement. They should be re-derived from the first few hundred logged
   decisions.

8. **I have not validated the ListenBrainz mapper endpoint shape against a live
   call.** The endpoint and its purpose are documented, but the exact response fields
   and error behaviour need checking before implementation. Same for whether Qobuz's
   cookie-session responses actually include `isrc` on the objects
   `qobuz_fetch.py` already retrieves; that determines whether Qobuz claims can reach
   AUTO or are stuck at CONFIRM, which is a material UX difference and should be
   checked early.

9. **Interaction with Agent 1's "same track from two sources" question.** This document
   provides `find_existing()` and `merge_track()`; it does not decide the policy for
   which `source_platform` wins on a merge, or whether a track loved on two services
   should show both. That is Agent 1's call, and `merge_track` preserving the strongest
   `status` is the only behaviour I have assumed.

10. **The confirm queue could become a chore.** If the tier-1 backfill mostly refuses,
    the user faces a long list of confirmations. I do not have a way to estimate the
    hit rate without running it. If it is bad, the answer is better batching in the UI
    (grouping by artist, confirming several at once), not a lower threshold.

---

Sources consulted:
[MusicBrainz Identifier](https://musicbrainz.org/doc/MusicBrainz_Identifier),
[MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API),
[MusicBrainz API rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting),
[MusicBrainz ISRC](https://musicbrainz.org/doc/ISRC),
[ISRC standard](https://en.wikipedia.org/wiki/International_Standard_Recording_Code),
[ListenBrainz MBID mapper](https://musicbrainz.org/doc/ListenBrainz/MBIDMappingDocumentation),
[lb-matching-tools](https://github.com/metabrainz/listenbrainz-matching-tools),
[LB-431, Last.fm returning track MBIDs](https://community.metabrainz.org/t/lb-431-last-fm-api-returns-track-mbid-instead-of-recording-mbid-for-new-scrobbles/431016),
[Last.fm getLovedTracks](https://lastfm-docs.github.io/api-docs/user/getLovedTracks/),
[Deezer track resource](https://deezer-python.readthedocs.io/en/stable/api_reference/resources/track.html).
