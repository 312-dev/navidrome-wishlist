# 01 - Source provider interface

Agent 1 deliverable. Written 2026-08-09 against the live app on the Mac Mini
(`~/music-stack/queue_ingest.py`, `queue_lib.py`) and its live queue database.

A "source" is a place that knows which tracks the user loved. Last.fm loves,
ListenBrainz feedback, Subsonic/Navidrome starred songs, Deezer favourites.
A source's only job is to hand the app a stream of loved-track records with
whatever identifiers it has. It does not resolve, normalize, match, dedup, or
touch the database.

Credential acquisition is out of scope here; see `03-auth.md`. Canonical track
identity and matching are out of scope here; see `04-identity.md`. This document
specifies what a source hands over and how the runtime drives it.

---

## Decisions

1. **A source is one class implementing `SourceProvider`, and one registry line.**
   No other file changes. The class does HTTP and parsing only. It never opens
   the database, never writes files, never sleeps for rate limiting (the
   scheduler owns pacing), and never normalizes text.

2. **The yielded record is `LovedTrack`, a frozen dataclass carrying raw strings
   plus every identifier the source happened to supply.** Raw means raw: the
   title `Falling Down - Bonus Track` (real row 164 in the live DB) arrives with
   that suffix intact. Stripping suffixes at the source destroys evidence the
   identity layer needs, and every source would strip differently.

3. **Cursors move out of flat files into SQLite, in a new `source_state` table,
   and are committed in the same transaction as the items they cover.** The
   current code writes `queue/lastfm_hw.txt` *inside* `lastfm_new()` and only
   then returns the list for the caller to insert. Any crash, exception, or
   `add_track` failure between those two points loses those loves permanently
   and silently. That is a real bug in the shipped code, not a hypothetical.

4. **Timestamp cursor comparison is inclusive (`>=`), not exclusive (`>`).**
   Last.fm `date.uts` and ListenBrainz `created` are second-granular. Loving
   three tracks in the same second, with the cursor landing mid-second, silently
   drops the rest under `>`. Inclusive comparison re-delivers the boundary, and
   re-delivery is free because `track_sources` has a `(source_id,
   source_item_id)` primary key. Losing a love is unrecoverable; re-seeing one
   costs an ignored insert.

5. **Four cursor kinds, declared by the provider**: `TIMESTAMP`, `OFFSET`,
   `OPAQUE`, `SNAPSHOT`. Subsonic is `SNAPSHOT` (the API returns the whole
   starred set every call), which is the only kind that can detect un-loving.

6. **First connect defaults to forward-only seed, with backfill as an explicit
   opt-in that runs as a separate resumable job.** This preserves the current
   behaviour (`hw == 0 and not BACKFILL` records the mark and returns nothing)
   and keeps a 3000-love Last.fm history from detonating the wishlist on day one.
   Backfill carries its own cursor so it can run alongside incremental polling.

7. **`tracks.source_platform` is replaced by a `track_sources` join table.** The
   column is already overloaded in production: 156 of 164 live rows carry
   `deezer-unobtainable`, which is not a source at all but a provenance tag
   written by a sibling script (`unreachable_to_wishlist.py:85`). One track can
   legitimately be loved on three services and the current schema cannot say so.

8. **Per-source poll intervals with an active/idle pair and a hard floor**, under
   the adaptive-polling rule. Navidrome is the user's own server and gets polled
   at 5s when a browser is watching. Deezer gets 60s at best.

9. **Failures are classified, not printed.** The live ingest log has 36 error
   lines: 24 ListenBrainz read timeouts, 8 Last.fm 500s and timeouts, plus a 502,
   a 500, and two TLS handshake failures. Today every one of them prints to a log
   nobody reads and the run silently produces zero tracks. Each becomes typed
   health state with backoff, and only `AUTH` and sustained `TRANSIENT` reach the
   UI.

10. **Every provider must pass a shared conformance suite, and each conformance
    test must first be demonstrated failing against a deliberately broken stub.**
    Section 6 of the brief records an audit that passed a genuine mismatch. A
    test that cannot fail is not a test.

---

## 1. The record shape

```python
# lw/sources/types.py

from dataclasses import dataclass, field
from typing import Literal, Optional

@dataclass(frozen=True, slots=True)
class TrackIds:
    """Every identifier the source supplied. All optional. Never invented."""
    recording_mbid: Optional[str] = None   # MusicBrainz recording
    release_mbid:   Optional[str] = None
    artist_mbids:   tuple[str, ...] = ()
    isrc:           Optional[str] = None
    native_id:      Optional[str] = None   # the source's own track id
    native_url:     Optional[str] = None   # canonical page on the source

@dataclass(frozen=True, slots=True)
class LovedTrack:
    # --- provenance ---
    source_id:      str            # "lastfm", "listenbrainz", "subsonic", "deezer"
    source_item_id: str            # stable id of THIS love, within this source
    loved_at:       Optional[int]  # epoch seconds, None if the source has no timestamp

    # --- raw display strings, verbatim from the source, never cleaned ---
    artist:         str
    title:          str
    album:          Optional[str] = None
    artists:        tuple[str, ...] = ()   # when the source splits them; else ()

    # --- identifiers ---
    ids:            TrackIds = field(default_factory=TrackIds)

    # --- optional enrichment the source got for free ---
    duration_s:     Optional[int] = None
    cover_url:      Optional[str] = None

    # --- diagnosis ---
    raw:            Optional[dict] = None  # the provider's own record, truncated
```

### Rules on the record

- `artist` and `title` are **required and non-empty**. A provider that cannot
  produce both must skip the item and count it in `FetchPage.skipped`, not emit
  a record with empty strings. The current ListenBrainz loop already does this
  (`if m.get("artist_name") and m.get("track_name")`); formalize it.
- `source_item_id` must be **stable across polls** for the same love. It is the
  idempotency key. Per source:

| Source | `source_item_id` | Fallback if absent |
|---|---|---|
| `listenbrainz` | `recording_mbid` | `recording_msid` |
| `subsonic` | song `id` | none, always present |
| `deezer` | track `id` | none, always present |
| `lastfm` | track `url` | `mbid`, else `sha1(artist + "\t" + title)` |

  Last.fm exposes no love-event id, so an un-love followed by a re-love reuses
  the same key and will not re-add. That is correct behaviour anyway under the
  purchased/ignored rule below.
- `raw` is stored for diagnosis, truncated to 4 KB of JSON, and cleared when the
  track is purchased or after 90 days. It is the source-side half of the match
  audit trail that `04-identity.md` specifies.
- **No normalization.** Not lowercasing, not `feat.` rewriting, not
  parenthetical stripping, not unicode folding. The identity layer needs the
  original bytes to score a match, and it needs them to be the same bytes the
  store search will be given.

---

## 2. The provider interface

```python
# lw/sources/base.py

from typing import Protocol, Optional, Iterable

CursorKind = Literal["timestamp", "offset", "opaque", "snapshot"]
FetchMode  = Literal["seed", "incremental", "backfill"]
AuthKind   = Literal["none", "api_key", "lastfm_session", "user_token",
                     "subsonic_password", "oauth2", "cookie_jar"]

@dataclass(frozen=True, slots=True)
class Budget:
    """Bounds one fetch call so a single source cannot monopolize the scheduler."""
    max_items:    int = 500
    max_requests: int = 10
    deadline_ts:  float = 0.0     # monotonic deadline; 0 means none

@dataclass(frozen=True, slots=True)
class FetchPage:
    items:    tuple[LovedTrack, ...]
    cursor:   Optional[dict]   # JSON-serializable, opaque to everything but the provider
    more:     bool = False     # provider hit its budget, call again immediately
    partial:  bool = False     # some pages failed; cursor covers only what succeeded
    skipped:  int  = 0         # records dropped for missing artist/title
    total:    Optional[int] = None   # source-reported total, for backfill progress

class SourceError(Exception):
    kind: Literal["transient", "rate_limited", "auth", "config", "schema"]
    retry_after: Optional[int] = None   # seconds, from Retry-After if given

class SourceProvider(Protocol):
    # --- static declaration, read by the UI and the scheduler ---
    id:                str
    label:             str
    auth:              AuthKind
    cursor_kind:       CursorKind
    supports_backfill: bool
    detects_unlove:    bool
    interval_active:   int    # seconds, when a browser is attached
    interval_idle:     int    # seconds, when nobody is watching
    interval_floor:    int    # seconds, never poll faster than this

    def configured(self, cfg: "Config") -> bool:
        """True if the credentials/settings this provider needs are present.
        Pure check, no network."""

    def check(self, cfg: "Config") -> None:
        """One cheap authenticated call. Raises SourceError on failure.
        Used by the UI 'Test connection' button and after a credential repair."""

    def fetch(self, cfg: "Config", cursor: Optional[dict], *,
              mode: FetchMode, budget: Budget) -> FetchPage:
        """Return loves at or after `cursor`, newest-safe, within `budget`.
        Raises SourceError. Must not sleep for pacing, must not touch the DB."""
```

### What the provider must not do

| Not allowed | Who owns it instead |
|---|---|
| Open SQLite, read or write | the runtime (`05-runtime.md`) |
| `time.sleep()` for pacing | the scheduler |
| Retry on failure | the scheduler's backoff |
| Normalize or clean strings | `04-identity.md` |
| Deduplicate | the runtime, via `track_sources` |
| Read env vars directly | the `Config` object handed in |
| Acquire or refresh credentials | `03-auth.md` |

This is what makes a provider testable with recorded fixtures and what keeps the
conformance suite meaningful.

### `seed` mode

`fetch(mode="seed")` returns `items=()` and a cursor positioned at "now" for this
source. It exists so the first connect is a single cheap call rather than a
special case in the scheduler. For a `snapshot` source, seed still fetches the
full set (there is no other way to know the current state) but emits nothing, and
stores the id set as the baseline.

---

## 3. Cursors and high-water marks

### 3.1 Where they live

New table in the app database in `/config`:

```sql
CREATE TABLE source_state (
  source_id            TEXT PRIMARY KEY,
  enabled              INTEGER NOT NULL DEFAULT 0,
  mode                 TEXT    NOT NULL DEFAULT 'incremental',
                               -- seed | incremental | backfill | paused
  cursor               TEXT,   -- JSON, provider-owned, opaque here
  boundary_ids         TEXT,   -- JSON array of source_item_ids at the cursor instant
  backfill_cursor      TEXT,   -- JSON, independent walk toward the past
  backfill_total       INTEGER,
  backfill_seen        INTEGER NOT NULL DEFAULT 0,
  backfill_done        INTEGER NOT NULL DEFAULT 0,

  health               TEXT    NOT NULL DEFAULT 'unconfigured',
                               -- ok | unconfigured | needs_auth | rate_limited
                               -- | degraded | error | paused
  last_ok_at           INTEGER,
  last_attempt_at      INTEGER,
  last_error           TEXT,
  last_error_kind      TEXT,
  last_error_at        INTEGER,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  next_due_at          INTEGER NOT NULL DEFAULT 0,

  items_seen           INTEGER NOT NULL DEFAULT 0,
  items_added          INTEGER NOT NULL DEFAULT 0
);
```

Reasons to leave flat files behind, beyond tidiness:

- **Atomicity.** The cursor update and the item inserts become one transaction.
  This is the fix for the loss window described in Decision 3.
- **One volume.** A Docker user mounting only `/config` gets their cursors
  backed up with their queue. A stray `queue/lastfm_hw.txt` outside the volume
  would silently reset every source on container recreation.
- **Resettable from the UI.** "Re-scan this source from scratch" is a row update,
  not a file deletion the user must SSH in to perform.
- **Health lives with it.** Backoff and `next_due_at` are cursor-adjacent state
  and belong in the same row.

**Migration from the live install:** read `queue/lastfm_hw.txt` (currently
`1786157895`) and `queue/lb_hw.txt` (`1785200523`) once, write them as
`{"kind":"timestamp","after":<value>}` into `source_state`, and rename the files
to `*.migrated`. Never delete them; if the import is wrong the values are the
only way to recover the position.

### 3.2 Cursor payloads by kind

```jsonc
// timestamp  (lastfm, listenbrainz, deezer)
{"kind": "timestamp", "after": 1786157895}

// offset  (only during a backfill walk)
{"kind": "offset", "index": 400, "anchor": 1786157895}

// snapshot  (subsonic)
{"kind": "snapshot", "at": 1786157895}   // id set lives in track_sources, not here

// opaque  (a source with continuation tokens)
{"kind": "opaque", "token": "eyJwYWdlIjoz..."}
```

### 3.3 The inclusive boundary

An incremental fetch selects items with `loved_at >= cursor.after`. The provider
returns them; the runtime inserts them; `(source_id, source_item_id)` makes the
repeats no-ops. `boundary_ids` holds the item ids observed exactly at
`cursor.after`, purely so the runtime can report "0 new" instead of "3 new, all
ignored" and keep the SSE stream quiet.

The new cursor is `max(loved_at)` over the items **actually committed**, never
over the items fetched. On `partial=True` the cursor advances only to the last
fully successful page.

### 3.4 Snapshot sources and un-loving

Subsonic `getStarred2` has no cursor. Every call returns the entire starred set.
The runtime diffs it:

- ids present in the response, absent in `track_sources` -> new loves
- ids in `track_sources` with `still_loved=1`, absent from the response ->
  un-starred; set `still_loved=0`, do **not** delete or requeue anything
- everything else -> bump `last_seen_at`

ListenBrainz can also express un-loving (`score` flips to `0` or `-1` and
`created` updates), so it appears in the feed and gets the same `still_loved=0`
treatment. Last.fm and Deezer cannot report it at all with the endpoints in use.

**Policy: un-loving never removes a track from the wishlist.** A wishlist is not
a mirror of a streaming service's heart button, and a track you un-loved after
buying it is still a record you own. The flag exists so the UI can offer a
"loved-elsewhere-only" filter and so a future "tidy up" action has data to work
from. Nothing automatic.

---

## 4. First connect: backfill vs forward-only

Three modes, chosen once per source at connect time, presented in the UI:

| Mode | What it does | Default |
|---|---|---|
| `seed` | Records the cursor at now, imports nothing | **yes** |
| `backfill` | Walks the whole history oldest-first, then flips to incremental | opt-in |
| `paused` | Configured but not polled | no |

`seed` is the default because it matches what the app does today and because the
failure mode of the alternative is severe: a user with a decade of Last.fm loves
gets several thousand wishlist rows on first launch, each of which then wants
resolution against Deezer, Bandcamp and Qobuz. The Deezer/Bandcamp resolvers are
rate-limited third-party endpoints; that first run would be indistinguishable
from abuse.

Where a source reports a total (Last.fm `@attr.totalPages`, ListenBrainz
`total_count`, Deezer `total`), the connect screen shows "You have N loved
tracks. Import all of them, or start from today?" before the choice is made.

### Backfill mechanics

- Runs as a job under `05-runtime.md`'s job model, not inline in the poll loop.
- Owns `backfill_cursor`, distinct from `cursor`, so the incremental poll keeps
  running forward while the backfill walks backward. They meet, and
  `backfill_done` is set.
- Bounded per tick by `Budget` and by a per-source page delay (the current code's
  `time.sleep(1)` between backfill pages is right; it moves into the scheduler).
- Resumable. A restart mid-backfill continues from `backfill_cursor`, which is
  why offset paging needs the `anchor` field: without pinning the walk to a
  timestamp, new loves arriving during a long backfill shift every offset and
  the walk silently skips items.
- Progress is `backfill_seen / backfill_total` and streams to the UI over SSE.

### The purchased/ignored rule survives

The live code's strongest invariant is that a purchased or ignored track never
comes back, enforced by the unique `dedup_key`. Keep it, and move it up a level:

> A source may deliver a track at any time. The queue refuses to create a new
> row for any track whose canonical identity already resolves to a row with
> status `purchased` or `ignored`, regardless of which source delivered it.

The delivery is still recorded in `track_sources` (so "loved on ListenBrainz too"
stays true), only the queue row is not resurrected. Without this, turning on
backfill on a second source re-queues everything already bought.

---

## 5. The four in-scope sources

### 5.1 `lastfm`

```
GET http://ws.audioscrobbler.com/2.0/
    ?method=user.getlovedtracks&user=<u>&api_key=<k>&format=json&limit=50&page=1
```

- Cursor: `timestamp` on `track[].date.uts`. Response is newest-first.
- Ids: `track[].mbid` (frequently empty), `track[].artist.mbid`, `track[].url`.
  No ISRC. Cover art comes back as `image[]` but is usually the artist placeholder;
  treat as low quality and prefer the resolver's.
- Rate limit: documented at roughly 5 requests/second per key, which is far above
  anything this app does. The observed failures are 500s and timeouts, not
  throttling.
- Known behaviour: `date.uts` is the love timestamp. Loves imported in bulk by
  Last.fm itself can share a single second, which is exactly the case the
  inclusive boundary protects.
- `supports_backfill: True` (`page` + `@attr.totalPages`), `detects_unlove: False`.

### 5.2 `listenbrainz`

```
GET https://api.listenbrainz.org/1/feedback/user/<u>/get-feedback
    ?score=1&metadata=true&count=100&offset=0
```

- Cursor: `timestamp` on `feedback[].created`.
- Ids: the richest of the four. `recording_mbid`, `recording_msid`, and under
  `track_metadata.mbid_mapping` a `release_mbid`, `artist_mbids[]`, and
  `caa_id`/`caa_release_mbid` for Cover Art Archive artwork. This is the source
  whose records should win metadata precedence.
- Reliability: this is the flakiest of the four in production. 24 of the 36
  logged errors are `The read operation timed out` against this endpoint, plus a
  502, a 500 and two TLS handshake failures. `metadata=true` is expensive
  server-side. Mitigations: 25-second timeout (the current value) is too tight
  for `metadata=true`; raise to 45s for this provider, and use `count=25` for
  incremental polls, `count=100` only during backfill.
- `supports_backfill: True` (`offset` + `total_count`), `detects_unlove: True`.

### 5.3 `subsonic` (Navidrome, Airsonic, Gonic)

```
GET <base>/rest/getStarred2.view
    ?u=<user>&t=<token>&s=<salt>&v=1.16.1&c=library-wishlist&f=json
```

Verified against the live Navidrome: `serverVersion 0.63.2`, `type navidrome`,
`openSubsonic: true`.

- Cursor kind: `snapshot`. There is no incremental form of `getStarred2`; it
  returns `starred2.song[]` entire. For a personal library this is a small
  response and the user's own hardware, which is why it gets the fastest poll.
- Ids: song `id` (server-local), `musicBrainzId` where the server populates it
  (OpenSubsonic field; **needs verifying against 0.63.2 with real data**), plus
  `artist`, `album`, `duration`, `coverArt`, and `starred` as ISO 8601.
- `starred` gives a real `loved_at`, so a snapshot source still produces ordered
  records.
- This is the only source that reliably detects un-starring.
- Distinct value: it is the one source whose loves can originate *from the app's
  own library*, closing the loop where a user stars something in Navidrome that
  they only have as a lossy rip and wants to buy it properly.
- `supports_backfill: True` trivially (the snapshot is the backfill),
  `detects_unlove: True`.

### 5.4 `deezer`

```
GET https://api.deezer.com/user/me/tracks?access_token=<t>&index=0&limit=50
```

- Cursor: `timestamp` on `data[].time_add`. Response is newest-first, `index`
  and `limit` page it, `total` gives the count.
- Ids: track `id`, `link`. **No MBID, and no ISRC on this endpoint** - ISRC needs
  a per-track `GET /track/{id}`, which is one extra request per item. Do that
  during backfill only if the user opts in, never on the incremental path.
- Rate limit: roughly 50 requests per 5 seconds per app, and unlike the others
  this one costs an OAuth token that has to stay valid. Poll it slowest.
- The app already talks to Deezer's *public* search API in `queue_lib.deezer_meta`
  for preview and cover art. That is unauthenticated and unrelated. Keep the two
  clients separate so a favourites-token failure cannot break preview resolution.
- `supports_backfill: True`, `detects_unlove: False`.

### 5.5 Interval table

`enabled` sources only. Effective interval is
`clamp(base * backoff_multiplier, interval_floor, 3600)`.

| Source | `interval_active` | `interval_idle` | `interval_floor` | Rationale |
|---|---|---|---|---|
| `subsonic` | 5s | 60s | 2s | user's own server, zero external cost |
| `listenbrainz` | 15s | 300s | 10s | public non-profit, and already timing out |
| `lastfm` | 30s | 300s | 5s | generous limit, but a commercial third party |
| `deezer` | 60s | 900s | 10s | tightest limit, token-backed |

"Active" means at least one SSE client has been attached within the last 30
seconds. The scheduler asks a `presence()` callable supplied by `05-runtime.md`;
sources know nothing about it.

A **manual poke** (the UI's refresh control) marks every enabled source due
immediately, subject to `interval_floor` only. This gives the user a way to
resolve "I just loved it, where is it" without lowering any interval.

---

## 6. Failure handling

### 6.1 Classification

```python
def classify(exc, response=None) -> str:
    # 429, or a documented throttle body          -> "rate_limited"
    # 401, 403, invalid session key, expired token -> "auth"
    # 5xx, timeout, connection reset, TLS failure  -> "transient"
    # missing user/api key/base url                -> "config"
    # 200 that will not parse, or a shape change   -> "schema"
```

The mapping is per provider, because the wire signals differ. Last.fm returns
HTTP 200 with `{"error": 9, "message": "Invalid session key"}`, so a Last.fm
provider that classifies on status code alone will report an auth failure as
success. Subsonic likewise: the live probe returned HTTP 200 carrying
`{"status":"failed", "error":{"code":40,"message":"Wrong username or password"}}`.
**Both of these must be classified from the body, and the conformance suite must
contain that exact fixture for each.**

### 6.2 Response per kind

| Kind | Cursor | Backoff | UI |
|---|---|---|---|
| `transient` | unchanged | full-jitter exponential from the base interval, capped at 30 min | silent until 5 consecutive failures or 30 min without a success, then a quiet "having trouble reaching X" |
| `rate_limited` | unchanged | `max(Retry-After, backoff)` | silent; shown only on the source detail panel |
| `auth` | unchanged | polling stops, `health='needs_auth'` | immediate and prominent; `03-auth.md` owns the repair flow |
| `config` | unchanged | not polled at all | source shows as "not set up", never as an error |
| `schema` | unchanged | backoff, and log the first 2 KB of the offending body | "X is returning data we do not understand" |

Backoff: `delay = base * 2**min(consecutive_failures, 6)`, multiplied by a
uniform random factor in `[0.5, 1.0]`, clamped to `[interval_floor, 1800]`. Full
jitter, so several sources failing on the same network outage do not retry in
lockstep.

A success resets `consecutive_failures` to 0 and `health` to `ok`.

### 6.3 The rule that matters most

**A failed fetch never advances a cursor.** Not partially, not optimistically,
not "we probably got most of them". The 24 ListenBrainz timeouts in the live log
each represent a poll that returned nothing; under the current design that is
harmless because the file write happens only on success, and this property must
be preserved exactly when the cursor moves into SQLite. Multi-page fetches commit
per page and stop at the first failure with `partial=True`.

---

## 7. The same track from two sources

### 7.1 Schema

```sql
CREATE TABLE track_sources (
  source_id      TEXT    NOT NULL,
  source_item_id TEXT    NOT NULL,
  track_id       INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  loved_at       INTEGER,
  first_seen_at  INTEGER NOT NULL,
  last_seen_at   INTEGER NOT NULL,
  still_loved    INTEGER NOT NULL DEFAULT 1,
  raw            TEXT,
  PRIMARY KEY (source_id, source_item_id)
);
CREATE INDEX idx_track_sources_track ON track_sources(track_id);
```

The composite primary key is the idempotency mechanism. Re-delivering the
boundary second is an `INSERT ... ON CONFLICT DO UPDATE SET last_seen_at=?`
that touches one column.

### 7.2 Ingest algorithm

```
for item in page.items:
    with tx:
        existing = SELECT track_id FROM track_sources
                   WHERE source_id=? AND source_item_id=?
        if existing:
            UPDATE track_sources SET last_seen_at=now, still_loved=1 ...
            merge_metadata(existing.track_id, item)     # fill nulls only
            continue

        track_id = identity.resolve(item)               # 04-identity.md
        if track_id is None:
            track_id = INSERT INTO tracks(...)          # new wishlist row
        elif tracks[track_id].status in ('purchased', 'ignored'):
            pass                                        # record the love, no requeue
        else:
            merge_metadata(track_id, item)

        INSERT INTO track_sources(...)
    emit_sse('track.added' | 'track.source_added', track_id)

with tx:
    UPDATE source_state SET cursor=?, boundary_ids=?, ... WHERE source_id=?
```

Note the cursor update is a separate final transaction covering only committed
items. Per-item transactions keep a mid-batch failure from rolling back work
already done, and the cursor stays behind until the batch is complete.

### 7.3 Metadata merge precedence

When two sources describe the same track, **never overwrite a non-null field with
a null**, and prefer the record with more identifiers. Default precedence, most
identifier-rich first:

```
listenbrainz > subsonic > deezer > lastfm
```

ListenBrainz leads because it natively supplies recording, release and artist
MBIDs. Subsonic is second because it is the user's own tagged library, which in
practice is Picard-tagged and therefore MBID-bearing. Deezer beats Last.fm
because a Deezer track id resolves to an ISRC on demand, while a Last.fm love
often carries no identifier at all.

Ties break on `first_seen_at`. `tracks.loved_at` is `min(loved_at)` across
sources, so ordering reflects when the user *first* loved it, not when the app
first noticed.

### 7.4 Migration of the 164 live rows

```sql
-- every existing row becomes its own track_sources entry
INSERT INTO track_sources(source_id, source_item_id, track_id,
                          loved_at, first_seen_at, last_seen_at, still_loved)
SELECT
  CASE source_platform
    WHEN 'lastfm'       THEN 'lastfm'
    WHEN 'listenbrainz' THEN 'listenbrainz'
    ELSE 'import:' || source_platform      -- 'import:deezer-unobtainable'
  END,
  'legacy:' || id,        -- no original id survives; synthesize a stable one
  id, added_at, added_at, added_at, 1
FROM tracks;
```

`import:*` is a pseudo-source class: it appears in `track_sources` for
provenance, has no provider, is never polled, and is excluded from the sources
UI list. This keeps the 156 `deezer-unobtainable` rows honest without pretending
Deezer is a configured source. `source_platform` stays on `tracks` for one
release marked deprecated, then drops.

---

## 8. Worked example: adding a new source

Hypothetical: **Funkwhale favourites**. A self-hosted server with a REST API at
`/api/v1/favorites/tracks/`, cursor-paginated, token authenticated. Chosen
because it exercises the awkward cases: an opaque continuation cursor, a
user-supplied base URL, and a `creation_date` that is ISO 8601 rather than epoch.

### 8.1 Files touched

| File | Change | Lines |
|---|---|---|
| `lw/sources/funkwhale.py` | new, the whole provider | ~90 |
| `lw/sources/__init__.py` | one import + one registry entry | 2 |
| `lw/sources/tests/fixtures/funkwhale/*.json` | recorded responses | n/a |
| `lw/sources/tests/test_funkwhale.py` | provider-specific cases only | ~40 |

Nothing else. No schema change, no scheduler change, no UI change: the sources
screen renders from the registry, and `03-auth.md`'s credential UI renders from
the declared `auth` kind.

### 8.2 The provider

```python
# lw/sources/funkwhale.py
"""Funkwhale favourites.

Cursor-paginated API. The `next` URL is the cursor; there is no usable
timestamp filter, so incremental polling walks the first page and stops as
soon as it sees an id already delivered. That works because the endpoint is
ordered newest-first and favourites are append-only from the client's view.
"""

from datetime import datetime, timezone
from .base import SourceProvider, FetchPage, Budget, SourceError
from .types import LovedTrack, TrackIds
from .http import get_json, HttpError      # shared client: UA, timeout, no retries


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00"))
               .astimezone(timezone.utc).timestamp())


class FunkwhaleSource:
    id                = "funkwhale"
    label             = "Funkwhale"
    auth              = "user_token"        # 03-auth.md renders the right form
    cursor_kind       = "opaque"
    supports_backfill = True
    detects_unlove    = False               # no deletion feed on this endpoint

    interval_active   = 10                  # usually the user's own server
    interval_idle     = 300
    interval_floor    = 5

    # ---- configuration -------------------------------------------------
    def configured(self, cfg) -> bool:
        return bool(cfg.get("FUNKWHALE_URL") and cfg.secret("funkwhale", "token"))

    def _headers(self, cfg):
        return {"Authorization": "Bearer " + cfg.secret("funkwhale", "token")}

    def _url(self, cfg, path):
        return cfg.get("FUNKWHALE_URL").rstrip("/") + path

    # ---- health --------------------------------------------------------
    def check(self, cfg) -> None:
        self._call(cfg, self._url(cfg, "/api/v1/favorites/tracks/?page_size=1"))

    # ---- fetch ---------------------------------------------------------
    def fetch(self, cfg, cursor, *, mode, budget) -> FetchPage:
        if mode == "seed":
            # Record the newest id as the stop marker, emit nothing.
            d = self._call(cfg, self._url(cfg, "/api/v1/favorites/tracks/?page_size=1"))
            newest = (d.get("results") or [{}])[0].get("id")
            return FetchPage(items=(), cursor={"kind": "opaque", "stop_id": newest},
                             total=d.get("count"))

        stop_id = (cursor or {}).get("stop_id")
        url     = (cursor or {}).get("next") if mode == "backfill" else None
        url     = url or self._url(cfg, "/api/v1/favorites/tracks/?page_size=50")

        items, requests, newest, partial = [], 0, stop_id, False
        while url and requests < budget.max_requests and len(items) < budget.max_items:
            d = self._call(cfg, url)
            requests += 1
            for fav in d.get("results", []):
                if newest is stop_id and requests == 1:
                    newest = fav.get("id")            # first item of first page
                if mode == "incremental" and stop_id and fav.get("id") == stop_id:
                    url = None
                    break
                rec = self._to_track(fav)
                if rec:
                    items.append(rec)
            else:
                url = d.get("next")
                continue
            break

        new_cursor = {"kind": "opaque", "stop_id": newest}
        if mode == "backfill" and url:
            new_cursor["next"] = url
        return FetchPage(items=tuple(items), cursor=new_cursor,
                         more=bool(url and mode == "backfill"),
                         partial=partial, total=d.get("count") if requests else None)

    # ---- mapping -------------------------------------------------------
    def _to_track(self, fav) -> LovedTrack | None:
        t = fav.get("track") or {}
        artist = ((t.get("artist_credit") or [{}])[0].get("artist") or {}).get("name")
        title  = t.get("title")
        if not artist or not title:
            return None                    # skipped, never an empty-string record
        album = (t.get("album") or {}).get("title")
        return LovedTrack(
            source_id      = self.id,
            source_item_id = str(fav["id"]),
            loved_at       = _epoch(fav.get("creation_date")),
            artist         = artist,       # verbatim
            title          = title,        # verbatim, suffixes intact
            album          = album,
            ids            = TrackIds(recording_mbid=t.get("mbid"),
                                      native_id=str(t.get("id") or ""),
                                      native_url=t.get("fid")),
            cover_url      = ((t.get("album") or {}).get("cover") or {}).get("urls", {}).get("medium_square_crop"),
            raw            = fav,
        )

    # ---- error classification -----------------------------------------
    def _call(self, cfg, url):
        try:
            return get_json(url, headers=self._headers(cfg), timeout=25)
        except HttpError as e:
            if e.status in (401, 403):
                raise SourceError("funkwhale rejected the token", kind="auth") from e
            if e.status == 429:
                raise SourceError("rate limited", kind="rate_limited",
                                  retry_after=e.retry_after) from e
            if e.status and e.status >= 500:
                raise SourceError(f"server error {e.status}", kind="transient") from e
            raise SourceError(str(e), kind="transient") from e
        except ValueError as e:                       # JSON did not parse
            raise SourceError("unparseable response", kind="schema") from e
```

### 8.3 Registration

```python
# lw/sources/__init__.py
from .funkwhale import FunkwhaleSource
REGISTRY = {p.id: p for p in (LastfmSource(), ListenBrainzSource(),
                              SubsonicSource(), DeezerSource(),
                              FunkwhaleSource())}
```

Whether this stays a hand-maintained registry or becomes entry-point discovery is
`05-runtime.md`'s call. Either way the provider file itself is unchanged.

### 8.4 Checklist for a new source

1. Pick the `cursor_kind`. If the API has no incremental filter and no
   continuation, it is `snapshot` and you must be prepared to fetch everything
   each poll. If the response is small (a personal server) that is fine; if it is
   not, the source is not viable.
2. Pick a `source_item_id` that is stable across polls. If none exists, the
   source cannot be made idempotent and you must fall back to a content hash,
   documented in the module docstring as a limitation.
3. Set intervals honestly. If it is a third party's public API, `interval_active`
   is 30 seconds at best.
4. Classify errors from the **body**, not only the status code. Last.fm, Subsonic
   and Funkwhale all return 200 for at least one failure mode.
5. Record fixtures for: a normal page, an empty result, an auth failure, a
   rate-limit response, a malformed body, and a record missing artist or title.
6. Run the conformance suite. Then break the provider on purpose and confirm each
   conformance test goes red.

---

## 9. Conformance suite

Every provider runs the same parametrized tests against its recorded fixtures.
None of them touch the network.

| Test | Asserts |
|---|---|
| `test_seed_emits_nothing` | `mode="seed"` returns `items == ()` and a non-null cursor |
| `test_boundary_is_inclusive` | two items sharing `loved_at == cursor.after` are both re-delivered, not one |
| `test_cursor_json_roundtrips` | `json.loads(json.dumps(cursor)) == cursor`, and feeding it back is accepted |
| `test_no_normalization` | a fixture title `Falling Down - Bonus Track` and an artist `Sigur Rós` survive byte-identical |
| `test_missing_fields_skipped` | a record with no artist yields `skipped == 1`, not a record with `artist=""` |
| `test_auth_body_not_status` | the 200-with-error-body fixture raises `SourceError(kind="auth")` |
| `test_rate_limit_carries_retry_after` | 429 fixture yields `retry_after` populated |
| `test_schema_error_on_garbage` | non-JSON body raises `kind="schema"`, not `transient` |
| `test_budget_respected` | `max_requests=1` issues exactly one request and returns `more=True` |
| `test_no_db_no_sleep` | monkeypatched `sqlite3.connect` and `time.sleep` are never called |
| `test_partial_failure_stops` | page 2 failing returns `partial=True` with page 1's items and page 1's cursor |
| `test_ids_never_invented` | a fixture with no mbid yields `recording_mbid is None`, not `""` |

**Each of these must be shown failing.** The suite ships with
`tests/broken_stub.py`, a provider deliberately wrong in twelve specific ways,
and a meta-test asserting that each conformance test fails against its
corresponding break. That meta-test exists because section 6 of the brief records
an audit that passed a real mismatch: the check was written in a form that could
not detect the thing it was checking for. The same failure mode is available to
every one of the tests above, and this is the only defence against it.

---

## 10. Open questions / risks

1. **Does Navidrome 0.63.2 populate `musicBrainzId` on `getStarred2` songs?**
   The server advertises `openSubsonic: true`, but I could not confirm the field
   is present without a valid credential, and the probe returned
   `error code 40`. If it does, Subsonic becomes a first-class MBID source and
   its precedence should rise. Needs one authenticated call to settle. Flagging
   rather than assuming.

2. **Does Deezer's `/user/me/tracks` actually return `time_add`?** The public
   documentation lists it on the user-tracks relation, but Deezer's API docs have
   been stale for years and the field has been reported missing on some accounts.
   If it is absent, Deezer degrades to `offset` cursoring against `total`, which
   is fragile under concurrent favouriting. Worth confirming before building.

3. **Deezer OAuth tokens historically do not expire and have no refresh token.**
   That makes revocation the only failure mode, but it also means a leaked token
   is permanent. This is `03-auth.md`'s problem; noting it here because it
   affects whether Deezer is worth shipping at all in v1.

4. **Last.fm `date.uts` semantics for bulk-imported loves.** If Last.fm ever
   backfills a user's loves with historical timestamps rather than import
   timestamps, a forward-only cursor will never see them, and the user's only
   recourse is a full backfill. I believe `uts` is the love time and this is
   safe, but I have not verified it against an account with imported loves.

5. **`still_loved` has no consumer yet.** I am specifying the column and the
   diff logic because throwing the information away is irreversible and cheap to
   keep, but nothing in v1 reads it. If Agent 6's UI has no place for it, it is
   dormant schema. That is a deliberate accepted cost, not an oversight.

6. **ListenBrainz reliability may force `metadata=false`.** 24 of 36 logged
   errors are read timeouts on the `metadata=true` variant. If raising the
   timeout to 45s and dropping to `count=25` does not fix it, the fallback is
   `metadata=false` plus a separate MBID lookup, which is more requests but each
   one cheap. Decide with data after the first week, not now.

7. **Bounded-payload retention.** Storing `raw` for every love is the difference
   between diagnosing a bad match and guessing. 4 KB per row across a few
   thousand rows is a few megabytes, which is fine, but I have not modelled a
   user with 20,000 loves who backfills everything. If that is a real profile,
   `raw` should be kept only for unresolved or low-confidence rows. Interacts
   directly with `04-identity.md`'s audit-trail design; worth reconciling between
   the two documents.

8. **No objection to any locked decision.** Adaptive polling in particular is the
   right call: I checked, and neither Last.fm nor ListenBrainz offers a push
   channel for loves. Worth noting for later that Subsonic-sourced loves are the
   one case where a Navidrome companion plugin could push instead of the app
   polling, which would make the 5-second active interval unnecessary. That is
   explicitly out of scope now and I am not arguing for it.
