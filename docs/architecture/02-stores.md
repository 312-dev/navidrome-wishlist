# 02 - Store provider interface

Agent 2 deliverable. Written 2026-08-09.

Scope: the contract every purchase backend implements, the template for adding one,
and the claim pipeline that sits above them. Credential acquisition is Agent 3.
Track identity and match scoring are Agent 4. Package layout, scheduler, SSE and
rescan triggering are Agent 5; this document names the seams it needs from them.

---

## Decisions

1. **One base protocol plus three capability flags, not one fat interface.** Stores
   differ in what they can do, not just how. `search`, `enumerate_owned`, `download`
   and `release_granular` are declared per store, and the UI reads them: a store that
   cannot enumerate never shows a Claim button. This is what lets 7digital and a
   hypothetical Beatport live in the same registry as Qobuz.
2. **Providers never decide whether a purchase matches a wishlist track.** A provider
   returns `OwnedItem` records; the shared matcher (Agent 4) scores them and the claim
   pipeline applies the threshold. `qobuz_fetch._matches` is deleted, not ported. This
   is the direct fix for the 2026-08-02 wrong-track incident, which happened inside
   exactly this kind of per-provider substring check.
3. **Claim is a five-phase pipeline with a single commit point**:
   `enumerate -> match -> download (to staging) -> verify -> commit`. Nothing touches
   `/music` or mutates the track row until verification passes. This generalises the
   validate-before-remove guarantee that `fetch_purchase` already has for Qobuz and
   makes it structural rather than per-store discipline.
4. **Verification is a real probe with named failure codes, not one magic-byte `if`.**
   Container magic, sample rate, bit depth, duration, size floor, plus an explicit
   "this is an HTML error page" check. The verifier ships with fixtures that make it
   fail, because the audit that reproduced the 08-02 bug is the reason to distrust any
   check that has only ever been observed passing.
5. **Duration is the second independent signal.** Store text and wishlist text can
   both say "Lies"; a 3:47 track and a 4:22 track cannot both be right. Add
   `tracks.duration_s` (Deezer already returns it) and make a duration mismatch a hard
   refusal even at high text confidence.
6. **Downloads land in staging under `/config`, then move atomically into `/music`.**
   No partial file is ever visible to Navidrome. Cross-device staging is handled by a
   temp dir inside `/music` and `os.replace`.
7. **Bandcamp is release-granular and asynchronous.** Purchases are releases, files
   arrive as zips, and FLAC is encoded on demand so a download can legitimately answer
   "not ready yet". The interface returns an iterator/job, not a blocking call, and the
   pipeline matches twice: release first, then member inside the archive.
8. **7digital is buy-link-only by default.** Its documented API is partner-gated
   (OAuth 1.0a consumer key issued commercially), which a self-hoster cannot obtain, so
   the locker path is an optional cookie upgrade marked unverified. That is a feature
   for this round: it is the store that proves the abstraction survives a provider with
   no enumeration.
9. **Stores that cannot enumerate share the pipeline via a manual inbox.** A
   `LocalInbox` pseudo-store enumerates files the user dropped in `/config/inbox` and
   runs the same match/verify/commit path. One code path, three sources of bytes.
10. **All store HTTP goes through the cookie broker's session object**, not raw
    `urllib` per provider, because the broker's contract is that it absorbs `Set-Cookie`
    on every request *it* makes. A provider that bypasses it silently ages the session out.

---

## 1. What exists today, and what is wrong with it

Read from the live Mini: `~/music-stack/qobuz_fetch.py` (116 lines) and
`~/music-stack/queue_lib.py` (143 lines).

The Qobuz implementation genuinely works and is the reference for the flow. It also
contains six defects that the new interface must design out rather than port:

| # | Defect | Consequence |
|---|---|---|
| 1 | `_matches()` normalises to `[a-z0-9]`, requires `artist in row_text`, then 70% title-token overlap | This is the section 6 hazard verbatim. `Lies` scores 1.0 against a row containing `Tell Me Lies Season 3`. |
| 2 | `enumerate_tracks()` returns `[]` for both "no purchases" and "cookie dead" | A dead session reads to the user as "you own nothing yet". Only a `/signin` substring in the first 3000 bytes distinguishes them, and only sometimes. |
| 3 | No pagination on `/profile/downloads/track` | Purchases past the first page are invisible. Fine at 2 purchases, wrong at 200. |
| 4 | Only `/profile/downloads/track` is read; `/profile/downloads/album` is not | Album purchases cannot be claimed at all. |
| 5 | Format preference `(7, 6, 27, 5)` commented "best FLAC first" | If 27 is 24/192 and 7 is 24/96, this tries 24/96 before 24/192, so "best first" is not what it does. Needs confirming against a real hi-res purchase, but the comment and the order disagree either way. |
| 6 | `entry_text = _strip(html[prev:m.end()])[-500:]` | The match input is a positional slice of raw HTML. Any markup change silently shifts what is being matched against. |

And in `queue_lib.fetch_purchase`:

```python
subprocess.run(["/usr/local/bin/docker", "exec", "navidrome", "navidrome", "scan", ...])
```

wrapped in `except Exception: pass`. Per brief section 2, Navidrome on the Mini is
installed directly and is not a container, so **this call has never worked**; scans
have been relying on Navidrome's own periodic scan the whole time. Rescan moves to
Agent 5's server adapters.

The parts worth keeping verbatim:

- The staged verification instinct: `head != b"fLaC"` before writing, size floor,
  `os.remove` on failure, and returning `(ok, message)` such that only a real download
  returns `ok=True`. That is the validate-before-remove guarantee. Generalise it.
- `queue_lib.bandcamp_search`'s refusal to fall back to a loose match, and the comment
  explaining why (Bandcamp is full of covers whose band name is an unrelated uploader).
  That instinct becomes the matcher's `REFUSE` outcome, not a silent `None`.

---

## 2. The contract

Proposed files (final layout is Agent 5's call):

```
libwish/stores/__init__.py     registry + get(store_id)
libwish/stores/base.py         dataclasses, protocol, exceptions
libwish/stores/net.py          HttpClient bound to a credential handle
libwish/stores/verify.py       audio probe + verification gate
libwish/stores/staging.py      staging dir, atomic commit into /music
libwish/stores/qobuz.py
libwish/stores/bandcamp.py
libwish/stores/sevendigital.py
libwish/stores/local_inbox.py
libwish/claim.py               the pipeline (not a store)
```

### 2.1 Value types

```python
# libwish/stores/base.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Iterator, Protocol, Sequence, Callable

@dataclass(frozen=True)
class Identifiers:
    """Whatever a provider can give us. All optional. Agent 4 owns how they are used."""
    isrc: str | None = None
    mbid: str | None = None          # recording MBID
    upc: str | None = None           # release
    store_track_id: str | None = None
    store_release_id: str | None = None

@dataclass(frozen=True)
class TrackQuery:
    """What we are looking for. Built from a tracks row by the pipeline, never by a provider."""
    artist: str
    title: str
    album: str | None = None
    duration_s: float | None = None
    ids: Identifiers = field(default_factory=Identifiers)

@dataclass(frozen=True)
class StoreCapabilities:
    search: bool                     # can produce any buy link at all
    deep_link: bool                  # can produce a link to the exact product, not a search page
    enumerate_owned: bool
    download: bool
    release_granular: bool           # purchases are releases; downloads may be archives
    async_prepare: bool              # store may answer "encoding, retry later"
    formats: tuple[str, ...] = ()    # advertised, best first, e.g. ("flac-24","flac-16","mp3-320")

@dataclass(frozen=True)
class Offer:
    store: str
    kind: str                        # "product" | "search"
    url: str
    artist: str | None = None
    track_title: str | None = None
    release_title: str | None = None
    price_cents: int | None = None
    currency: str | None = None
    formats: tuple[str, ...] = ()
    ids: Identifiers = field(default_factory=Identifiers)
    raw: dict = field(default_factory=dict)

@dataclass(frozen=True)
class OwnedItem:
    store: str
    item_key: str                    # opaque, stable, unique within the store
    kind: str                        # "track" | "release"
    artist: str
    title: str                       # track title, or release title when kind="release"
    release_title: str | None = None
    parent_key: str | None = None    # release item_key for a track inside a release
    purchased_at: int | None = None
    duration_s: float | None = None
    track_number: int | None = None
    formats: tuple[str, ...] = ()
    ids: Identifiers = field(default_factory=Identifiers)
    raw: dict = field(default_factory=dict)   # verbatim provider record, persisted

@dataclass
class DownloadResult:
    path: Path                       # staged file. NOT in the library.
    requested_format: str
    bytes: int
    source_host: str
    is_archive: bool = False         # a zip/tar we still need to open
    notes: dict = field(default_factory=dict)

@dataclass
class ProgressEvent:
    claim_id: int
    phase: str                       # enumerate|match|download|verify|commit
    detail: str
    bytes_done: int | None = None
    bytes_total: int | None = None

ProgressFn = Callable[[ProgressEvent], None]

@dataclass
class StoreHealth:
    ok: bool
    authed: bool
    detail: str
    checked_at: int
    owned_count: int | None = None
```

### 2.2 The protocol

```python
class StoreProvider(Protocol):
    id: ClassVar[str]                        # "qobuz" - stable, used as a DB value
    name: ClassVar[str]                      # "Qobuz" - shown in the UI
    capabilities: ClassVar[StoreCapabilities]
    auth_kind: ClassVar[str]                 # "cookie" | "oauth2" | "none" - Agent 3 dispatches on this

    def __init__(self, cfg: "StoreConfig", creds: "CredentialHandle") -> None: ...

    # --- health -----------------------------------------------------------
    def check(self) -> StoreHealth:
        """Cheap liveness + auth probe. Must distinguish 'not authed' from 'store down'.
        Never raises; encodes failure in StoreHealth."""

    # --- discovery (usually unauthenticated) ------------------------------
    def buy_url(self, q: TrackQuery) -> str:
        """Always returns something clickable. A search URL is an acceptable answer."""

    def find_offers(self, q: TrackQuery, limit: int = 5) -> list[Offer]:
        """Candidate products. MUST NOT filter by its own notion of a match; return
        candidates with whatever metadata the store gave, let the matcher score them.
        Returns [] if the store has no queryable search."""

    # --- ownership (only if capabilities.enumerate_owned) -----------------
    def list_owned(self, since: int | None = None) -> Iterator[OwnedItem]:
        """Yield everything the user owns, newest first where the store allows it.
        `since` is a best-effort purchased_at cutoff; a store that cannot filter
        server-side ignores it and the pipeline stops consuming early.
        MUST raise StoreAuthError on a dead session. MUST NOT return [] for that case."""

    def expand(self, item: OwnedItem) -> Iterator[OwnedItem]:
        """For release_granular stores: the tracks inside a release item.
        Non-release stores return iter([item])."""

    # --- fetch (only if capabilities.download) ---------------------------
    def download(self, item: OwnedItem, dest_dir: Path,
                 prefer: Sequence[str], progress: ProgressFn) -> DownloadResult:
        """Fetch the best available format from `prefer` (best first) into dest_dir.
        dest_dir is a staging directory owned by the caller.
        Raises StorePreparing (retryable) if the store is still encoding.
        Raises StoreFormatUnavailable if nothing in `prefer` exists.
        Does NOT verify the file - that is the pipeline's job."""
```

`expand()` is what keeps the Bandcamp album case out of the pipeline's special-case
list: for Qobuz it is the identity, for Bandcamp it reads the release's track list.

### 2.3 Exceptions

```python
class StoreError(Exception):
    code = "store_error"
    retryable = False

class StoreAuthError(StoreError):        # session/token dead -> Agent 3's "credential died" signal
    code = "auth"

class StoreRateLimited(StoreError):
    code = "rate_limited"
    retryable = True
    def __init__(self, msg, retry_after: float = 60.0): ...

class StoreTemporaryError(StoreError):   # 5xx, timeout, connection reset
    code = "temporary"
    retryable = True

class StorePreparing(StoreError):        # Bandcamp encoding a FLAC on demand
    code = "preparing"
    retryable = True

class StoreNotOwned(StoreError):
    code = "not_owned"

class StoreFormatUnavailable(StoreError):
    code = "format_unavailable"

class StoreParseError(StoreError):       # the site changed shape under us
    code = "parse"
```

`StoreParseError` matters more than it looks. Two of the three stores here are HTML
scrapes; the failure mode when the markup changes is a silent empty list, which reads
as "you own nothing". Every scrape helper must raise `StoreParseError` when its anchor
element is missing, and the health check must surface it as degraded rather than as
"not authed".

### 2.4 Capability matrix

| | Qobuz | Bandcamp | 7digital | LocalInbox |
|---|---|---|---|---|
| `search` | yes | yes | yes | no |
| `deep_link` | **no** (search URL only) | yes (autocomplete gives item URLs) | search URL; deep link unverified | n/a |
| `enumerate_owned` | yes (HTML scrape) | yes (JSON API) | **off by default**, cookie upgrade | yes (directory listing) |
| `download` | yes (signed URL) | yes (statdownload -> signed URL) | off by default | n/a (already local) |
| `release_granular` | no (per-track entries) | **yes** | likely yes | no |
| `async_prepare` | no | **yes** | unknown | no |
| auth kind | cookie (broker) | cookie (broker) | cookie (broker), optional | none |

Qobuz lacking `deep_link` is a real product consequence: the Buy button sends you to a
search results page, not to the record. Fixing that needs the catalogue API, which
needs an `app_id` (see 4.1).

---

## 3. The claim pipeline

`libwish/claim.py`. This is where the guarantees live.

```python
def claim(track_id: int, store_id: str | None = None, *,
          force_refresh: bool = False) -> ClaimOutcome
```

If `store_id` is None, try enabled stores in `LIBWISH_STORES` order, stopping at the
first that produces a committed file. Every attempt writes a `claims` row whether it
succeeds or not.

### Phases

**1. enumerate.** Refresh `store_inventory` for the store if the cache is older than
`LIBWISH_INVENTORY_TTL_S` (default 300) or `force_refresh`. Refresh is
`list_owned(since=last_full_sync - 86400)` for incremental, full every
`LIBWISH_INVENTORY_FULL_S` (default 86400). `StoreAuthError` here ends the claim
immediately with `error_code="auth"` and fires the credential-dead signal; it must
never be swallowed into "not found".

**2. match.** Build a `TrackQuery` from the `tracks` row. Feed every cached
`OwnedItem` for that store to Agent 4's matcher, which returns
`[(item, score, trace)]`. Apply thresholds:

| score | action |
|---|---|
| `>= LIBWISH_CLAIM_MIN_CONFIDENCE` (0.90) | proceed |
| `>= LIBWISH_CLAIM_REVIEW_CONFIDENCE` (0.60) | **refuse**, state `needs_review`, surface top 3 candidates in the UI for a one-click human confirm |
| below | `not_owned` |

Two hard vetoes applied after scoring, regardless of score:

- **Duration.** If both `tracks.duration_s` and `OwnedItem.duration_s` are known and
  differ by more than `max(3.0, 0.05 * expected)` seconds, refuse with
  `error_code="duration_mismatch"`. This alone would have stopped the 08-02 incident.
- **Already claimed.** If a committed `claims` row already exists for
  `(store, item_key)` against a *different* track, refuse with
  `error_code="item_already_claimed"`. One purchase satisfying two wishlist rows is a
  match bug, not a bargain.

The full scoring trace is written to `claims.match_decision_json` before the download
starts, so a wrong match is diagnosable from the DB alone, with no log correlation.

**3. download.** For `release_granular` stores, `expand()` the matched release and
re-match to pick the member, recording a second trace. Then
`provider.download(item, staging, prefer, progress)` into
`/config/staging/claim-<claim_id>/`. `StorePreparing` and `StoreRateLimited` reschedule
the job with backoff (30s, 2m, 10m, 30m, then fail) rather than failing the claim.

**4. verify.** See section 5. On failure the staged file is deleted (or kept under
`/config/staging/failed/<claim_id>/` when `LIBWISH_KEEP_FAILED_DAYS > 0`) and the claim
fails. The track stays on the wishlist.

**5. commit.** Atomic move into `/music` (section 6), then a single SQLite transaction:
insert the `files` row, update `claims` to `committed`, set `tracks.status='owned'`
with `owned_store`, `owned_path`, `owned_quality`. Then, outside the transaction,
signal Agent 5 to trigger a library rescan.

### Ordering guarantee

The track row is mutated **only** in phase 5, after a verified file exists at its final
path. Every earlier failure leaves the wishlist untouched. That is the same promise
`fetch_purchase` makes today for Qobuz, made structural.

### Purchase-lag retries

When the user clicks "I bought it", the store often does not expose the order for
several minutes. Rather than showing "not found in your purchases yet" as the current
code does, schedule the claim with a retry ladder at 0s, 30s, 2m, 10m, 30m, 2h and
report "waiting for the store to register your purchase" between attempts. The track
sits in `purchased` (paid, unclaimed) meanwhile, visibly, so it can never quietly
vanish.

### Concurrency

One in-flight claim per store (`threading.Lock` keyed by store id), because these are
scrape-heavy and parallel requests are how you get rate limited. On startup, any
`claims` row in a non-terminal state is marked `failed` with `error_code="interrupted"`
and its staging directory removed; interrupted claims are safe to retry because nothing
was committed.

---

## 4. Per-store designs

### 4.1 Qobuz

Auth: session cookie jar via the broker. Web login uses reCAPTCHA and the mobile API
needs a rotating app secret, so this is the only clean path (brief section 6).

**Enumerate.** Ported from working code, with the defects from section 1 fixed.

```
GET /profile/downloads/track?page=N   (repeat until a page yields no new entry ids)
GET /profile/downloads/album?page=N   (new: album purchases)
```

Entry ids come from `/account/download/(\d+)/1`. Instead of slicing raw HTML for match
text, parse each row into fields with `html.parser` and populate `OwnedItem.artist` /
`.title` / `.release_title` properly; only fall back to the row's text content when the
structured parse fails, and mark that in `raw` so a bad match can be traced to a
degraded parse.

The per-entry options page gives the order id and clean metadata:

```
GET /account/download/{eid}/1
  -> /account/download/track/{eid}/1/(\d+)/     order id
  -> "track"/"title", "artistName"/"performer"
```

This is one request per entry, which is why the inventory cache matters. Cache the
order id in `store_inventory.raw` so a re-claim does not re-fetch it.

Auth detection: a `/signin` redirect, a login form in the response, or a 401/403 must
raise `StoreAuthError`. An HTTP 200 with zero entry ids *and* no recognisable
"downloads" container raises `StoreParseError`. Only a 200 with a recognisable empty
container yields an empty list.

**Download.** Unchanged in shape from the working code:

```
GET /account/download/track/{eid}/1/{order}/{fmt}   -> {"url": "https://..."}
GET <url>                                            -> the file
```

Format ids observed in the live code: `5` MP3, `6` FLAC 16/44, `7` FLAC 24/<=96,
`27` FLAC 24/192. Preference order becomes config-driven
(`LIBWISH_STORE_QOBUZ_FORMATS=flac-24-192,flac-24-96,flac-16,mp3-320`) mapped to those
ids, which also fixes the existing order/comment disagreement. Content-Type must be
`audio/*` or `application/octet-stream`; an HTML response is `StoreParseError`, not a
silently skipped format.

**Deep links and search.** `find_offers` currently cannot return `kind="product"`.
`/api.json/0.2/catalog/search` would fix it but needs an `app_id`, which Qobuz issues
per partner via api@qobuz.com. Some clients scrape the `app_id` out of the web player
bundle. **I am not recommending that**: it is fragile, it is a credential issued to
someone else, and the buy-link is a convenience rather than a correctness feature.
Ship the search URL and revisit if Qobuz ever issues a hobbyist key.

**Rate limit.** No published limit. Use `min_interval_s=1.0` with jitter, cap
concurrent requests at 1, honour `Retry-After`.

### 4.2 Bandcamp

Essential per the brief: sanctioned downloads, and an indie catalogue Qobuz does not
carry. Prior art: `meeb/bandcampsync`, `easlice/bandcamp-downloader`,
`Ezwen/bandcamp-collection-downloader`, `Ovyerus/bandsnatch`. All of them use the same
cookie-session flow, which is good evidence it is stable.

**Search / offers.** Already working in `queue_lib.bandcamp_search`:

```
POST https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic
body: {"search_text": "<artist> <title>", "search_filter": "t",
       "full_page": false, "fan_id": null}
-> auto.results[]  with band_name, name, item_url_path, item_type, art_id
```

Change from the current code: return all results as `Offer` candidates rather than
returning a single URL only on an exact double match. The refusal logic moves up into
the matcher, where it is shared, tested and logged. The reason the current code refuses
loose matches (covers uploaded under unrelated band names) is exactly the case the
matcher's confidence bands are for. `search_filter: "a"` gives albums, worth querying
when a track search comes back empty.

**Fan id.** Any authenticated page carries it:

```
GET https://bandcamp.com/<username>   (or /collection)
parse <div id="pagedata" data-blob="...">, html.unescape, json.loads
-> fan_data.fan_id
-> collection_data.last_token, collection_data.item_count
-> item_cache.collection{}, redownload_urls{}
```

**Enumerate.**

```
POST https://bandcamp.com/api/fancollection/1/collection_items
body: {"fan_id": <id>, "older_than_token": <token>, "count": 100}
-> {"items": [...], "redownload_urls": {"p<sale_item_id>": "https://bandcamp.com/download?..."},
    "last_token": "...", "more_available": bool}
```

Page by feeding `last_token` back as `older_than_token`. Also enumerate
`/api/fancollection/1/hidden_items` with the same payload shape, since hidden purchases
are still owned. The `redownload_urls` map is keyed by
`<sale_item_type><sale_item_id>` (`p123456` for a purchase); join it onto items by
those fields. `item_key` = `f"{sale_item_type}{sale_item_id}"`.

**Download.** Three hops, and the middle one is the part naive implementations get
wrong:

```
1. GET <redownload_url>
   -> pagedata data-blob -> digital_items[0].downloads["flac"].url
      NOTE: this is a *stat* URL, not a file URL. Fetching it directly fails.
2. transform  /download/ -> /statdownload/  and append  &.rand=<epoch_ms>  &.vrs=1
   GET it -> JSON(ish) {"result": "ok", "download_url": "https://...?fsig=&id=&ts=&token="}
   error shape: {"errortype": "...", "retry_url": "..."}  e.g. ExpiredFreeDownloadError
3. GET download_url -> the bytes (zip for a release, bare file for a single track)
```

Response 2 is not always clean JSON (it can arrive wrapped in JS), so parse
defensively and raise `StoreParseError` rather than guessing. A `retry_url` in the
response is a retry instruction, not a failure: follow it once, then treat it as
`StorePreparing`.

Format keys: `flac` (default), `mp3-320`, `mp3-v0`, `aac-hi`, `aiff-lossless`, `alac`,
`vorbis`, `wav`. Bandcamp encodes non-MP3 formats **on demand**, so a first request for
`flac` can come back not-ready. That is `StorePreparing`, and it is the single reason
`download()` must be a job rather than a request handler.

**Archives.** A release download is a zip. `staging.py` opens it with `zipfile`,
enumerates audio members, and hands each member's metadata back through
`expand()`/matcher to pick the one the wishlist wants. Rules:

- Reject any member whose normalised path escapes the extraction root (zip-slip), and
  any member with an absolute path or a symlink bit.
- Cap total uncompressed size at `LIBWISH_MAX_UNPACK_BYTES` (default 2 GiB) and refuse
  a compression ratio above 200:1.
- Extract only the members needed, unless
  `LIBWISH_STORE_BANDCAMP_COMMIT_WHOLE_RELEASE=1`, in which case commit every audio
  member and additionally mark any other wishlist track matched by a member as owned.
  That last behaviour is a genuine win: buying the album satisfies four wishlist rows
  at once.
- Keep the cover art member (`cover.jpg`/`folder.jpg`) alongside the audio when
  committing a whole release.

**Rate limit / TLS.** No published limit; `bandcampsync` jitters its schedule to avoid
dogpiling. Use `min_interval_s=2.0` with jitter and `count=100` pages.
`easlice/bandcamp-downloader` uses `curl_cffi` with `impersonate="chrome"`, which
suggests Bandcamp may fingerprint TLS. Start with stdlib `urllib` plus a realistic UA
and full cookie jar, and measure. If we see 403s that a browser does not get, add
`curl_cffi` as an **optional extra** (`pip install libwish[bandcamp-tls]`) with a graceful
degradation message, rather than as a base dependency. Flagged as a risk below.

**Cookies.** The jar the broker holds should include `identity` and `client_id` at
minimum; the extension pushes the full jar so this needs no special handling. Do not
hand-pick cookie names.

### 4.3 7digital

The honest one, and the reason it is in scope: it proves the abstraction handles a
store we cannot fully automate.

**What is true.** The consumer store is live at `us.7digital.com`, sells DRM-free
16-bit and 24-bit FLAC plus MP3, and has a "Your Music" locker section. The B2B side
was folded into MassiveMusic (Songtradr) in June 2025; the consumer store continues
and the banner points business users elsewhere.

**What blocks automation.** The documented REST API (v1.2, including the locker
endpoints) uses OAuth 1.0a signing and requires a partner `oauth_consumer_key` issued
commercially. There is no self-service developer signup a self-hoster can complete.
The Node and Python client libraries are unmaintained.

**Therefore:**

```python
class SevenDigitalStore:
    id = "sevendigital"
    name = "7digital"
    capabilities = StoreCapabilities(
        search=True, deep_link=False,
        enumerate_owned=False,      # flipped True only when a cookie jar is present
        download=False,
        release_granular=True, async_prepare=False,
        formats=("flac-24", "flac-16", "mp3-320"),
    )
```

`buy_url` returns `https://us.7digital.com/search?q=<artist>+<title>` (locale from
`LIBWISH_STORE_SEVENDIGITAL_LOCALE`, default `us`). With a cookie jar present, the
provider promotes itself to `enumerate_owned=True, download=True` and scrapes the
locker.

**The locker shape is unverified.** I have no 7digital account and did not log in, so I
am not going to invent endpoints. What ships instead is a discovery checklist in the
provider docstring for whoever implements it:

1. Log in, open "Your Music", capture the network tab.
2. Is the list server-rendered HTML or an XHR to a JSON endpoint? Record the URL, the
   pagination parameter, and whether it is per-track or per-release.
3. What does the download button hit? One-shot signed URL, or a stat/redirect hop like
   Bandcamp's?
4. Does the response carry ISRC or a stable release id? (Agent 4 wants these.)
5. Does a format choice appear in the request, or is it a per-purchase property?

Until that is filled in, 7digital is buy-link-only and the UI shows Buy plus "I bought
it", never Claim. That is the correct, honest behaviour, and it is what the capability
flags exist to express.

### 4.4 LocalInbox, and stores that cannot enumerate at all

Any store we cannot automate (Beatport, Bleep, an artist's own site, a Bandcamp
purchase made before the cookie existed) resolves through one shared path.

```python
class LocalInboxStore:
    id = "inbox"
    name = "Manual drop"
    capabilities = StoreCapabilities(search=False, deep_link=False,
                                     enumerate_owned=True, download=True,
                                     release_granular=False, async_prepare=False)
```

`list_owned()` walks `LIBWISH_INBOX_DIR` (default `/config/inbox`), probes each audio
file (section 5), and yields an `OwnedItem` built from the file's **tags**, using
`item_key = sha256(path + size + mtime)[:16]`. `download()` is a copy from the inbox
into staging. Everything downstream is identical: same matcher, same thresholds, same
duration veto, same verification, same atomic commit, same audit row.

That is the strongest argument for this shape of interface. "The store cannot be
automated" costs one 60-line provider, not a parallel code path with its own bugs.

The UI consequences for a non-enumerable store:

- Buy button: yes, from `buy_url`.
- "I bought it": marks `tracks.status='purchased'` with `purchased_via=<store>` and
  `verified=0`. The track leaves the wishlist view but appears in a **Purchased,
  unclaimed** bucket with a count badge. It never silently disappears, which is the
  failure mode to avoid.
- Claim button: hidden, replaced by "Drop the file in `/config/inbox`" with the path
  shown.

---

## 5. Verification

`libwish/stores/verify.py`. The rule from brief section 6 is that verification logic
must be written so it can fail. That means it needs named failure codes and fixtures
that trigger each one.

### The probe

Header parsing only, stdlib, no decode:

| Container | Detection | Fields recovered |
|---|---|---|
| FLAC | `fLaC` at 0 | STREAMINFO block: sample rate (20 bits), channels (3), bit depth (5), total samples (36) -> exact duration |
| MP3 | ID3v2 skip, then frame sync `0xFFE` | sample rate, bitrate, Xing/VBRI frame count -> duration |
| MP4/M4A/ALAC | `ftyp` at 4 | `mvhd` timescale + duration; `alac`/`mp4a` codec box |
| Ogg | `OggS` at 0, then `\x01vorbis` or `OpusHead` | sample rate, channels; duration from the last page's granule position |
| WAV | `RIFF` + `WAVE` + `fmt ` | sample rate, bit depth, channels; duration from the `data` chunk size |
| AIFF | `FORM` + `AIFF` + `COMM` | sample rate (80-bit float), bit depth, frame count |

```python
@dataclass(frozen=True)
class Probe:
    container: str            # flac|mp3|m4a|ogg|wav|aiff|zip|html|unknown
    codec: str | None
    sample_rate: int | None
    bit_depth: int | None
    channels: int | None
    duration_s: float | None
    bytes: int

def probe(path: Path) -> Probe: ...
```

FLAC's STREAMINFO gives an exact duration from the header alone, which is what makes
the duration veto cheap and reliable for the format we care most about. `mutagen` would
be a legitimate dependency here and is worth adding if we later want tag rewriting, but
for verification alone the stdlib parse is roughly 150 lines and keeps the dependency
count at Flask plus waitress.

### The gate

```python
@dataclass(frozen=True)
class Expectation:
    allowed_containers: frozenset[str]
    min_bytes: int = 131072
    expect_duration_s: float | None = None
    duration_tolerance_s: float = 3.0
    min_bitrate_kbps: float = 32.0

@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    code: str            # "ok" or a specific failure code
    detail: str
    probe: Probe

def verify(path: Path, expect: Expectation) -> VerifyResult
```

Failure codes, each with its own fixture:

| code | trigger |
|---|---|
| `empty` | zero bytes |
| `too_small` | below `min_bytes` |
| `html_error_page` | starts with `<!DOCTYPE`, `<html`, or `{"error"` - the store served an error with a 200 |
| `unexpected_archive` | a zip where a bare file was expected |
| `unknown_container` | no magic matched |
| `container_mismatch` | probed container not in `allowed_containers` (an MP3 renamed `.flac`) |
| `truncated` | header says N samples, file size cannot hold them |
| `duration_mismatch` | probed duration outside tolerance of the expected duration |
| `implausible_bitrate` | bytes/duration below `min_bitrate_kbps` (silence padding, a stub) |

### Proving it can fail

`tests/test_verify.py` ships fixtures under `tests/fixtures/verify/`:

- `good_16_44.flac` - passes
- `good_24_96.flac` - passes, and asserts `bit_depth == 24` so the quality tier the UI
  displays is the one the file actually is
- `mp3_renamed.flac` - asserts `container_mismatch`, not merely `not ok`
- `error_page.flac` - a saved Qobuz signin page - asserts `html_error_page`
- `truncated.flac` - first 64 KiB of a real file - asserts `truncated`
- `album.zip` - asserts `unexpected_archive`
- `wrong_track.flac` - a 4:22 file verified against a 3:47 expectation - asserts
  `duration_mismatch`

Each test asserts the **specific code**. A test that only asserts `not result.ok` would
pass for the wrong reason, which is precisely how the 08-02 audit reproduced the bug it
was auditing.

Add a `libwish selftest` CLI subcommand that runs the verifier against the fixtures at
runtime, so a packaging change that drops the fixtures or breaks a parser is caught on
the user's machine and not only in CI.

---

## 6. Where files are written

### Staging

```
/config/staging/claim-<claim_id>/            in flight
/config/staging/failed/<claim_id>/           kept LIBWISH_KEEP_FAILED_DAYS, default 7
```

Staging under `/config` rather than `/music` keeps unverified bytes out of Navidrome's
scan path entirely. Purge on startup and on a daily job.

### Commit

```python
def commit(staged: Path, item: OwnedItem, probe: Probe, cfg) -> Path
```

1. Build the destination from **the store's metadata**, not the wishlist's, because the
   file's own tags came from the store and a split between path and tags confuses every
   library scanner:

   ```
   LIBWISH_PATH_TEMPLATE="{album_artist}/{album}/{track:02d} {title}.{ext}"
   fallbacks: album_artist -> artist -> "Unknown Artist"
              album        -> "Singles"
              track        -> omit the number segment entirely
   ```

   Sanitise each component independently: strip `/`, NUL and control characters,
   replace `:` `?` `*` `"` `<` `>` `|` with `-`, strip trailing dots and spaces
   (Windows/SMB shares), truncate to 200 bytes UTF-8 preserving the extension. Never
   let a component become empty or `.`/`..`.

2. If `/config` and `/music` are different devices (`st_dev` differs), copy into
   `/music/.libwish-tmp/<claim_id>` first. Otherwise hardlink or rename into it.
3. `fsync` the file, then `fsync` the destination directory.
4. `os.replace(tmp, final)` - atomic within the filesystem, so Navidrome never sees a
   partial file even mid-scan.
5. Apply `PUID`/`PGID` ownership and `LIBWISH_FILE_MODE` (default `0644`,
   directories `0755`).

**Collision policy** when the destination already exists:

- Probe the existing file. If the incoming file is a strictly better tier (higher bit
  depth, then higher sample rate, then lossless over lossy, then larger), replace it and
  record the displaced file's path in `claims.notes`.
- If equal or worse, keep the existing file and finish the claim as
  `committed` with `notes.dedup="kept_existing"`. The user owns it either way; the
  point of the app is satisfied.
- Never write `Title (1).flac`. Silent duplicates are how libraries rot.

### Rescan

`commit()` emits a `library.changed` event with the touched directories. Agent 5 owns
the per-server adapters (Subsonic `startScan` for Navidrome/Airsonic/Gonic, and
Jellyfin/Emby/Plex equivalents). The current hardcoded
`docker exec navidrome navidrome scan` is deleted; per section 1 it never ran.

---

## 7. Schema

Additive migrations. Agent 5 owns the migration runner.

```sql
-- Buy links, one row per (track, store, kind). Replaces tracks.bandcamp_url / qobuz_url.
CREATE TABLE track_store_offers(
  id            INTEGER PRIMARY KEY,
  track_id      INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  store         TEXT    NOT NULL,
  kind          TEXT    NOT NULL,          -- 'product' | 'search'
  url           TEXT    NOT NULL,
  artist        TEXT, track_title TEXT, release_title TEXT,
  price_cents   INTEGER, currency TEXT,
  formats_json  TEXT,
  confidence    REAL,                      -- matcher score; NULL for kind='search'
  isrc TEXT, mbid TEXT, upc TEXT,
  checked_at    INTEGER NOT NULL,
  UNIQUE(track_id, store, kind)
);
CREATE INDEX ix_offers_track ON track_store_offers(track_id);

-- Cached ownership inventory. One row per purchasable item the user owns.
CREATE TABLE store_inventory(
  id            INTEGER PRIMARY KEY,
  store         TEXT    NOT NULL,
  item_key      TEXT    NOT NULL,
  parent_key    TEXT,
  kind          TEXT    NOT NULL,          -- 'track' | 'release'
  artist        TEXT, title TEXT, release_title TEXT,
  track_number  INTEGER,
  duration_s    REAL,
  isrc TEXT, mbid TEXT, upc TEXT,
  purchased_at  INTEGER,
  formats_json  TEXT,
  raw_json      TEXT    NOT NULL,          -- verbatim provider record, for diagnosis
  first_seen_at INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  UNIQUE(store, item_key)
);
CREATE INDEX ix_inv_store_seen ON store_inventory(store, last_seen_at);

-- Sync bookkeeping per store.
CREATE TABLE store_sync(
  store             TEXT PRIMARY KEY,
  last_incremental  INTEGER,
  last_full         INTEGER,
  cursor            TEXT,                  -- e.g. Bandcamp older_than_token
  owned_count       INTEGER,
  last_error_code   TEXT, last_error_at INTEGER, last_error_message TEXT
);

-- Audit row for every claim attempt. Never deleted.
CREATE TABLE claims(
  id                  INTEGER PRIMARY KEY,
  track_id            INTEGER NOT NULL REFERENCES tracks(id),
  store               TEXT    NOT NULL,
  state               TEXT    NOT NULL,    -- pending|enumerating|matching|downloading|
                                           -- verifying|committed|failed|refused|needs_review
  attempt             INTEGER NOT NULL DEFAULT 1,
  started_at          INTEGER NOT NULL,
  finished_at         INTEGER,
  next_retry_at       INTEGER,
  matched_item_key    TEXT,
  match_confidence    REAL,
  match_decision_json TEXT,                -- Agent 4 trace, written BEFORE downloading
  requested_format    TEXT, actual_container TEXT,
  sample_rate INTEGER, bit_depth INTEGER, bytes INTEGER, duration_s REAL,
  staged_path         TEXT, final_path TEXT,
  verify_code         TEXT,
  error_code          TEXT, error_message TEXT,
  notes_json          TEXT
);
CREATE INDEX ix_claims_track ON claims(track_id, started_at DESC);
CREATE INDEX ix_claims_retry ON claims(next_retry_at) WHERE next_retry_at IS NOT NULL;

-- Files we put in the library. Lets us detect an external deletion and re-offer a claim.
CREATE TABLE files(
  id          INTEGER PRIMARY KEY,
  track_id    INTEGER REFERENCES tracks(id),
  claim_id    INTEGER REFERENCES claims(id),
  path        TEXT NOT NULL UNIQUE,
  bytes       INTEGER, sha256 TEXT,
  container   TEXT, sample_rate INTEGER, bit_depth INTEGER,
  committed_at INTEGER NOT NULL
);
```

Columns added to `tracks`:

```sql
ALTER TABLE tracks ADD COLUMN duration_s REAL;        -- from Deezer; feeds the duration veto
ALTER TABLE tracks ADD COLUMN owned_store TEXT;
ALTER TABLE tracks ADD COLUMN owned_path TEXT;
ALTER TABLE tracks ADD COLUMN owned_quality TEXT;     -- "FLAC 24/96" - the signature UI element
ALTER TABLE tracks ADD COLUMN purchase_verified INTEGER DEFAULT 0;
```

**Status values.** Today: `queued | buying | purchased | ignored`. Proposed:

| status | meaning |
|---|---|
| `queued` | on the wishlist |
| `buying` | user clicked Buy and left for the store; times out back to `queued` after `LIBWISH_BUYING_TIMEOUT_DAYS` (default 14) |
| `purchased` | user says they paid; no verified file. `purchase_verified=0`. Shown in the "Purchased, unclaimed" bucket |
| `owned` | a verified file exists at `owned_path`. Terminal success |
| `ignored` | unchanged |

Migration of the 164 live rows: the 2 existing `purchased` rows keep `purchased`, and a
one-shot reconciler probes `/music` for a matching file (via the same matcher) and
promotes them to `owned` where it finds one at high confidence. Migrating existing
`bandcamp_url`/`qobuz_url` into `track_store_offers` as `kind='product'` and
`kind='search'` respectively is mechanical; the old columns stay in place, unread, for
one release before being dropped.

---

## 8. Configuration surface (store-side)

```
LIBWISH_STORES=qobuz,bandcamp,inbox            # enabled, in claim preference order
LIBWISH_MUSIC_DIR=/music
LIBWISH_STAGING_DIR=/config/staging
LIBWISH_INBOX_DIR=/config/inbox
LIBWISH_PATH_TEMPLATE={album_artist}/{album}/{track:02d} {title}.{ext}
LIBWISH_FILE_MODE=0644
LIBWISH_KEEP_FAILED_DAYS=7
LIBWISH_MAX_UNPACK_BYTES=2147483648

LIBWISH_CLAIM_MIN_CONFIDENCE=0.90
LIBWISH_CLAIM_REVIEW_CONFIDENCE=0.60
LIBWISH_CLAIM_DURATION_TOLERANCE_S=3.0
LIBWISH_INVENTORY_TTL_S=300
LIBWISH_INVENTORY_FULL_S=86400

LIBWISH_STORE_QOBUZ_FORMATS=flac-24-192,flac-24-96,flac-16,mp3-320
LIBWISH_STORE_QOBUZ_MIN_INTERVAL_S=1.0
LIBWISH_STORE_BANDCAMP_FORMAT=flac
LIBWISH_STORE_BANDCAMP_COMMIT_WHOLE_RELEASE=0
LIBWISH_STORE_BANDCAMP_MIN_INTERVAL_S=2.0
LIBWISH_STORE_SEVENDIGITAL_LOCALE=us
LIBWISH_STORE_SEVENDIGITAL_ENABLE_LOCKER=0     # unverified, opt in
```

Every store gets `LIBWISH_STORE_<ID>_MIN_INTERVAL_S` and `_ENABLED` for free from the
base config loader.

---

## 9. Template: adding a store

Everything a new provider needs, in one file plus one test file.

```python
# libwish/stores/examplestore.py
"""Example Records (https://example.tld) - <one line on what it sells and in what formats>.

Auth: <cookie via broker | oauth2 | none>.
Enumeration: <endpoint or page, and whether it paginates>.
Download: <how many hops, whether links expire>.
Verified against a live account on <date> by <who>. If unverified, SAY SO HERE.
"""
from libwish.stores.base import (
    StoreCapabilities, TrackQuery, Offer, OwnedItem, DownloadResult, StoreHealth,
    StoreAuthError, StoreParseError, StoreNotOwned, StorePreparing,
)

class ExampleStore:
    id = "example"
    name = "Example Records"
    auth_kind = "cookie"
    capabilities = StoreCapabilities(
        search=True, deep_link=True, enumerate_owned=True, download=True,
        release_granular=False, async_prepare=False,
        formats=("flac-16", "mp3-320"),
    )

    def __init__(self, cfg, creds):
        self.cfg = cfg
        # http applies the cookie jar, UA, rate limit, retries, and feeds Set-Cookie
        # back to the broker so the session stays alive. Do not use urllib directly.
        self.http = creds.http_client(min_interval_s=cfg.min_interval_s)

    # 1. Liveness. Must separate "not logged in" from "site is down".
    def check(self) -> StoreHealth: ...

    # 2. A clickable buy link. A search URL is fine.
    def buy_url(self, q: TrackQuery) -> str: ...

    # 3. Candidates. Return everything plausible; DO NOT decide what matches.
    def find_offers(self, q: TrackQuery, limit: int = 5) -> list[Offer]: ...

    # 4. Ownership. Raise StoreAuthError on a dead session - never return [].
    #    Raise StoreParseError when the anchor element is missing.
    def list_owned(self, since=None): ...

    # 5. Release -> tracks. Identity for non-release stores.
    def expand(self, item: OwnedItem): 
        yield item

    # 6. Bytes into dest_dir. Do not verify, do not touch /music, do not touch the DB.
    def download(self, item, dest_dir, prefer, progress) -> DownloadResult: ...
```

Register it (mechanism is Agent 5's; the store side only needs the class):

```python
# libwish/stores/__init__.py
from .examplestore import ExampleStore
REGISTRY = {c.id: c for c in (QobuzStore, BandcampStore, SevenDigitalStore,
                              LocalInboxStore, ExampleStore)}
```

### Checklist before a store is considered done

- [ ] `check()` returns `authed=False` (not an exception, not `ok=True`) when the jar is empty
- [ ] `list_owned()` raises `StoreAuthError` against a deliberately corrupted cookie jar
- [ ] `list_owned()` raises `StoreParseError` against a saved HTML page with the anchor element removed
- [ ] `list_owned()` paginates: verified against an account with more items than one page
- [ ] Every `OwnedItem.raw` round-trips through `json.dumps` (it is persisted)
- [ ] `item_key` is stable across two syncs a day apart
- [ ] `download()` writes only inside `dest_dir`
- [ ] `download()` raises rather than writing an HTML error page as audio
- [ ] Rate limiter honours `Retry-After`; a 429 does not fail the claim
- [ ] No matching logic anywhere in the provider (grep it for `in title`, `.lower()` comparisons)
- [ ] A recorded-fixture test for `find_offers` and `list_owned` so CI does not hit the live store
- [ ] Docstring states whether it was verified against a live account, and when

### Fixture tests

Each provider ships `tests/stores/test_<id>.py` driven by saved HTTP responses in
`tests/fixtures/<id>/`, so the suite runs offline and a site change shows up as a
failing parse test rather than as a user seeing "you own nothing".

---

## 10. Open questions and risks

1. **Bandcamp TLS fingerprinting.** `easlice/bandcamp-downloader` uses `curl_cffi` with
   `impersonate="chrome"`, which is a strong hint that plain `urllib` may be blocked at
   least some of the time. `meeb/bandcampsync` appears not to, so it may only matter on
   certain endpoints or from certain IPs. **Unverified.** Mitigation is an optional
   extra rather than a base dependency, but if it turns out to be required for
   `statdownload`, Bandcamp effectively costs us a compiled dependency and the Docker
   image gets bigger on both arches. Worth testing early since Bandcamp is called
   essential.
2. **7digital's locker is entirely unverified.** I did not log in and will not invent
   endpoints. The provider ships buy-link-only with a discovery checklist. If the
   abstraction needs a third *fully implemented* store to be convincing, Bleep or
   Presto Music may be better candidates than 7digital; both sell FLAC to consumers and
   neither is mid-rebrand into a B2B company.
3. **Qobuz `app_id`.** Without it there is no catalogue search, so Qobuz buy links stay
   search pages rather than product pages, and `find_offers` returns nothing for Qobuz.
   Scraping the web player's `app_id` is what other clients do; I am recommending
   against it (it is someone else's issued credential and it breaks on every bundle
   rebuild), but it is a real product cost and the decision should be conscious.
4. **Qobuz format ids.** `5/6/7/27` are taken from the working code's own comment. The
   mapping to actual quality tiers, and therefore the correct preference order, should
   be confirmed against a real hi-res purchase before the `owned_quality` string is
   shown in the UI. Displaying "24/192" for a file that is 24/96 would undermine the
   exact thing the UI is meant to make trustworthy.
5. **Duration is not always available.** Deezer supplies it for the wishlist side, but
   `deezer_meta` currently discards it, and the Qobuz downloads page may not expose a
   duration per row. Where either side is unknown the veto cannot fire and text
   confidence is all we have. The probe gives us the *downloaded* file's duration for
   free, so a fallback is to verify duration post-download and refuse at the verify
   gate instead of the match gate. That wastes a download but still never commits the
   wrong file, which is the property that matters.
6. **`raw_json` retention.** Storing the verbatim provider record per owned item is
   what makes a bad match diagnosable, but Bandcamp collection items are fat. At a few
   thousand items this is single-digit MB, which is fine; if it becomes a problem, store
   a pruned subset rather than dropping it, because the alternative is debugging a match
   failure with no evidence.
7. **Claiming the same purchase for two wishlist tracks** is blocked by the
   `item_already_claimed` veto, but there is a legitimate case: two wishlist rows that
   are genuinely the same recording under different titles. The right resolution is that
   Agent 4's identity work merges them upstream, and this veto is the alarm that says
   it did not. Flagging so the two designs agree on which layer owns it.
8. **Bandcamp whole-release commit changes wishlist semantics.** If buying an album
   satisfies four wishlist rows, four tracks disappear from the wishlist in one action.
   That is desirable but surprising, so it needs UI acknowledgement (Agent 6) rather
   than happening silently. Defaulted off.
9. **No objection to any locked decision.** The single-instance, self-hosted constraint
   is what makes the cookie-session store path defensible at all; a hosted version
   holding many users' purchase-capable sessions would make this document's Qobuz and
   Bandcamp sections irresponsible rather than merely fiddly.

---

## Sources

- [meeb/bandcampsync](https://github.com/meeb/bandcampsync)
- [easlice/bandcamp-downloader](https://github.com/easlice/bandcamp-downloader)
- [Reverse engineering Bandcamp downloads](https://torunar.github.io/en/2024/06/24/bandcamp-downloads/)
- [Ezwen/bandcamp-collection-downloader](https://github.com/Ezwen/bandcamp-collection-downloader)
- [7digital United States](https://us.7digital.com/)
- [Songtradr unites 7digital and others under MassiveMusic](https://www.digitalmusicnews.com/2025/06/11/massivemusic-reorganization-june-2025/)
- [Qobuz official API documentation mirror](https://github.com/csngoh/api-documentation)
