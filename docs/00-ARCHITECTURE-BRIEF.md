# Library Wishlist - architecture planning brief

Shared context for the parallel planning agents. **Read this file in full before
starting.** You have no conversation history; everything you need is here or on
disk at the paths named below.

Written 2026-08-09.

---

## 1. What the product is

A self-hosted app that turns *tracks you loved* into *records you own*.

```
loved on Last.fm / ListenBrainz / your own Navidrome
        -> wishlist (buy links, preview, cover)
        -> you click Buy, pay at the store with your own account
        -> you click Claim
        -> app authenticates to the store, downloads the file you now own
        -> file lands in your music library, Navidrome picks it up
```

It is explicitly **not** a ripper and not a streaming client. The only files it
ever fetches are ones the user has actually purchased, from the user's own store
account, onto the user's own hardware.

## 2. Where it runs today

Live on a Mac Mini (on the LAN), at
`~/music-stack/`, as launchd job `com.example.love-queue` on
`0.0.0.0:8080`. Library at `/Volumes/Music/library`. Navidrome **0.63.2** scans
that directory (not a Docker container; installed directly).

The current directory is a personal junk drawer (beets, deemix, streamrip,
various sync scripts). The app itself is cleanly separable from it.

## 3. Current implementation

946 lines, 5 files, **one external dependency (Flask)**. Everything else is
stdlib (`sqlite3`, `urllib`, `json`, `re`, `html`).

| File | Lines | Role |
|---|---|---|
| `app.py` | 183 | Flask routes + the entire UI as one inline `PAGE` HTML string |
| `queue_lib.py` | 143 | SQLite queue, enrichment, purchase dispatch |
| `queue_ingest.py` | 87 | polls Last.fm + ListenBrainz loves into the queue |
| `qobuz_fetch.py` | 116 | cookie-auth Qobuz purchase enumeration + FLAC download |
| `cookie_broker.py` | 417 | browser-seeded cookie jar receiver + keepalive (new, 2026-08-02) |

### Schema (live)

```sql
CREATE TABLE tracks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedup_key TEXT UNIQUE,              -- artist.lower() + "\t" + title.lower()
  artist TEXT, title TEXT,
  source_platform TEXT,               -- see note below; NOT a clean source tag
  added_at INTEGER,
  status TEXT DEFAULT 'queued',       -- queued | buying | purchased | ignored
  resolved INTEGER DEFAULT 0,
  preview_url TEXT, cover_url TEXT, bandcamp_url TEXT, qobuz_url TEXT,
  chosen_source TEXT, purchased_at INTEGER, purchased_via TEXT, ignored_at INTEGER
);
```

Live data: 160 queued, 2 purchased, 2 ignored.

**Correction (verified 2026-08-09):** `source_platform` is not a clean source tag.
Actual distribution across the 164 rows is `deezer-unobtainable` 156, `lastfm` 6,
`listenbrainz` 2. The dominant value is a *provenance* tag written by a sibling
script, not a source name, so the column is already overloaded and cannot simply be
reused as the multi-source discriminator.

### `queue_lib.py` surface

`conn` `init` `dkey` `add_track(artist,title,platform)` `deezer_meta`
`bandcamp_search` `qobuz_search_url` `resolve(track_id)` `resolve_pending`
`mark_purchased(track_id,via)` `mark_ignored` `restore_track`
`fetch_purchase(track_id, via="qobuz")`

### Two seams already exist (formalize, don't invent)

- **Sources**: `tracks.source_platform` + `queue_ingest.py`'s
  `for src, fn in (("lastfm", lastfm_new), ("listenbrainz", lb_new))`
- **Stores**: `fetch_purchase(track_id, via="qobuz")` and the
  `bandcamp_url` / `qobuz_url` / `chosen_source` columns

High-water marks are currently flat files (`queue/lastfm_hw.txt`), not DB rows.

### The cookie broker (built, deployed, proven)

Repo: `github.com/312-dev/cookie-broker` (private), local `~/repos/cookie-broker`.

Chrome MV3 extension reads a site's **full cookie jar** (including `HttpOnly`,
which page JS cannot) and POSTs it to a receiver. The receiver owns keeping the
session alive: persists the jar, absorbs `Set-Cookie` on every request it makes,
and probes on a keepalive timer. Split is **extension seeds, receiver keeps**.

Verified end to end 2026-08-02 against live Qobuz: 11-cookie jar pushed,
`PROBE LIVE`, enumerated real purchases, and a Claim downloaded a purchased FLAC
12 seconds after the push. `/auth/ingest` + `/auth/status` are live on the Mini
behind a bearer token.

## 4. Locked decisions (do not relitigate)

1. **Self-hosted, one instance per user.** No SaaS, no multi-tenancy, no accounts,
   no central server. A hosted version would hold many users' purchase-capable
   session cookies and sit in the copyright file path; both are disqualifying.
2. **Not a Navidrome plugin.** Verified against 0.63.2's manifest schema: a plugin
   may declare only `artwork cache http kvstore library matcher scheduler
   subsonicapi taskqueue users websocket`. None accept inbound HTTP, `websocket`
   is for *establishing* outbound connections, `LibraryPermission.filesystem` is
   read-only, and every entry point has a hard 30s wazero timeout. An optional
   companion plugin for *presentation only* (`subsonicapi` + `scheduler`) may be
   revisited later; it is out of scope now.
3. **Stack**: Python 3 + Flask + Jinja + HTMX + Alpine + Tailwind (standalone CLI
   at build time, no Node in the runtime image) + [Basecoat](https://basecoatui.com)
   for components. Serve with **waitress**, not the Flask dev server.
4. **SQLite** in `/config`. No database server.
5. **Live refresh via SSE**, not WebSockets (one-way, survives any reverse proxy,
   `EventSource` auto-reconnects).
6. **Adaptive polling.** Neither Last.fm nor ListenBrainz offers webhooks or a
   documented push channel for loves/feedback. Poll fast (5-10s) while a browser
   is connected via SSE, back off to ~5min when nobody is watching.
7. **Distribution**: multi-arch Docker image (amd64 + arm64) on GHCR, `/config` +
   `/music` volumes, `PUID`/`PGID`/`TZ`, compose example beside Navidrome. Manual
   cookie **paste is the baseline path**; the extension is an optional upgrade
   tier, because an extension that reads `HttpOnly` cookies is structurally an
   infostealer and store review is a real risk.
8. **Config entirely via env.** Today there are 8 hardcoded absolute paths.
9. **Spotify is deferred.** Not in scope for this planning round.

## 5. What this planning round must produce

The user wants **pluggable sources and pluggable stores**, each with a documented
template so a new provider can be added by following a pattern, **including OAuth
support**. That is the core of this round.

## 6. Known hazards (real incidents, do not repeat)

- **The wrong-track download, 2026-08-02.** A sibling job resolved the loved track
  `CHVRCHES - Lies` to a Deezer search hit and downloaded
  `Such Great Heights (From "Tell Me Lies Season 3")`, because the query term
  appeared inside a soundtrack parenthetical. Soundtrack/remix/live suffixes inject
  arbitrary words into title fields. **A substring match is not a match.**
- **The audit that reproduced the bug.** A first pass checking `title in downloaded`
  passed this mismatch, for exactly the same reason. Verification logic must be
  written so it can fail.
- `dedup_key` is lowercased `artist\ttitle`. No MBID, no ISRC. This is the weakest
  point in the whole system.
- **The existing keys are not all in one dialect.** Verified 2026-08-09 against the
  164 live rows: only **123** reproduce from `lower(artist) + "\t" + lower(title)`.
  The other **41 do not**, and their exact formula has not been characterised (a
  punctuation-stripping variant reproduces just 1 of them). Migration must therefore
  recompute identity from the `artist` and `title` columns and must never parse,
  trust, or round-trip an existing `dedup_key`.
- **Unicode artists break naive normalisation.** In the live data, `Michael Bublé`
  loses the accented character and truncates, and `¥$` normalises to the **empty
  string**, so an artist whose whole name is non-ASCII symbols gets an empty key.
  Normalisation must use Unicode folding (NFKD plus compatibility mapping), not an
  ASCII-only character class.
- **`launchctl bootout` is not persistent.** A job disabled that way silently
  re-armed at next login and ran 1569 times while believed disabled.
- **Live data-loss bug: the high-water mark advances before the rows are written.**
  In `queue_ingest.py`, `lastfm_new()` writes the new watermark at line 49 and only
  then returns the tracks for the caller to insert. Any failure between the two loses
  those loves permanently, because the watermark has already moved past them. The
  ListenBrainz path (line 70) has the identical shape. The new design must advance a
  cursor only after the rows are durably committed.
- **Live bug: same-second loves are dropped.** Both paths filter with
  `uts(t) > hw` / `cr(f) > hw` against second-granular timestamps, so a sibling loved
  in the same second as the watermark is skipped forever. Cursors need a tiebreaker
  (a stable per-item id) rather than a bare timestamp comparison.
- **The same substring hazard is live in the PURCHASE path, not just the retired
  rip job.** `qobuz_fetch._matches` (line 84) accepts a row when the normalized
  artist appears anywhere in it and 70% of title tokens appear anywhere in it.
  Executed verbatim against the real purchases-page text, claiming
  `CHVRCHES - Lies` matches BOTH the correct row and the
  `Such Great Heights (From "Tell Me Lies Season 3")` row, each at 100%, because
  the single token `lies` occurs inside `Tell Me Lies`. `fetch_for()` takes the
  first match in enumeration order, so with both owned, the wrong file is selected
  by page ordering alone. Short, single-token titles are the worst case.
- **Library rescan has never fired.** `queue_lib.fetch_purchase` line 140 runs
  `docker exec navidrome navidrome scan`, but Navidrome on the Mini is installed
  directly and is not a container. Every successful download has silently skipped
  the rescan. The rescan must be an adapter per server type, not a hardcoded
  `docker exec`.
- `qobuz_fetch.enumerate_tracks` returns `[]` for both "no purchases" and "dead
  cookie", so a failure is indistinguishable from an empty result. It also has no
  pagination and never enumerates album purchases.
- Qobuz web login uses reCAPTCHA, so headless auto-login is out. Its mobile API
  needs a rotating app secret; also out. The session cookie path is the only clean
  one, hence the broker.
- The app currently binds `0.0.0.0` and ships the Flask dev server.

## 7. Output contract for every agent

- Write your deliverable to the path named in your brief, under
  `~/repos/library-wishlist/docs/architecture/`. Create it if absent.
- Markdown. Lead with a short **Decisions** list (what you are recommending and
  why), then detail, then a **Open questions / risks** section at the end.
- Be concrete: name interfaces, method signatures, table columns, file paths.
- Where you are uncertain, say so explicitly rather than inventing confidence.
- Respect the locked decisions in section 4. If you believe one is wrong, do not
  silently work around it: write your objection in Open questions.
- **Do not write any code into the live Mini or `~/music-stack`.** This round is
  planning only. Prototype snippets inside your markdown are fine.
- House style: no em dashes, plain ASCII punctuation, no Claude attribution.
- Return **a 3-line summary only** as your final message. The file is the artifact.

---

## 8. The agents

### Agent 1 - Source provider interface
**Output:** `docs/architecture/01-sources.md`

Design the contract every "loved tracks" source implements, and the template for
adding a new one. In scope: Last.fm loves, ListenBrainz feedback, Subsonic/Navidrome
`getStarred` (the user's own server, so it can be polled hard at no cost to anyone),
and Deezer favorites. Cover: the yielded record shape, incremental cursors and
high-water marks (currently flat files, should they move into SQLite), backfill vs
forward-only on first connect, per-source poll intervals under the adaptive-polling
rule, rate-limit and failure handling, and what happens when the same track arrives
from two sources. Do **not** design credential acquisition; that is Agent 3.
Produce a worked example: the full skeleton for adding a hypothetical new source.

### Agent 2 - Store provider interface
**Output:** `docs/architecture/02-stores.md`

Design the contract every purchase backend implements, and the template for adding
one. In scope: Qobuz (cookie session, working code in `qobuz_fetch.py`), Bandcamp
(essential; sanctioned downloads, indie catalog Qobuz lacks, `bandcampsync` exists
as prior art), and 7digital as a third to prove the abstraction. Cover: search/buy-link
generation, enumerating what the user owns, matching a wishlist item to a purchase,
downloading, verifying the file is what it claims (the current code checks the `fLaC`
magic bytes), where the file is written, and the **validate-before-remove** guarantee
already in `fetch_purchase` (a track only leaves the queue on a confirmed success).
Address stores that cannot enumerate purchases at all. Do **not** design credential
acquisition; that is Agent 3.

### Agent 3 - Credentials and OAuth
**Output:** `docs/architecture/03-auth.md`

The cross-cutting hard one. Design how a provider (source *or* store) obtains,
stores, refreshes and revokes credentials. The auth patterns in play are genuinely
different and all four must be supported: **OAuth 2** (Deezer, Tidal; note
7digital is **OAuth 1.0a**, not 2, and is partner-gated, so treat it as a
separate pattern. Its docs describe acquiring an access token *and secret*, which
is a 1.0a signature; public summaries disagree and it cannot be confirmed without
a partner account),
**Last.fm's own web-auth token-to-session-key flow** (not OAuth), **pasted user
tokens** (ListenBrainz), **username+password/token+salt** (Subsonic), and
**browser session cookies via the cookie broker** (Qobuz, Bandcamp).

The hard problem to solve properly: **OAuth redirect URIs for an app at an
unpredictable private address** (`http://192.168.1.42:8080`, a tailnet IP, a
hostname behind someone's reverse proxy). Evaluate loopback redirects, device
authorization grant, a manual code paste, and a public relay, with an explicit
recommendation. Also cover at-rest encryption of tokens in SQLite for a
single-user app (what key, stored where, and be honest about what it actually
protects against), refresh scheduling, and how a provider signals "my credential
died" to the UI. Read `~/repos/cookie-broker/receiver/cookie_broker.py` and its
READMEs first; the cookie path is built and must be integrated, not redesigned.

### Agent 4 - Track identity and matching
**Output:** `docs/architecture/04-identity.md`

The highest-risk area, and the one that already caused a real incident (section 6).
Design canonical track identity across sources and stores. Cover: MusicBrainz MBIDs
(ListenBrainz supplies them natively, Last.fm sometimes), ISRC, and what to do when
you have neither; a replacement for the `artist\ttitle` `dedup_key` including how
to migrate 164 existing rows; normalization rules for the suffixes that break naive
matching (`(Remastered)`, `(Live at ...)`, `(From "...")`, `feat.`, explicit/clean,
alternate capitalisation and punctuation); and a **confidence-scored match with an
explicit refusal threshold**, because the correct behaviour on a doubtful match is
to refuse and tell the user, never to download the wrong file. Specify how a match
decision gets logged so a wrong one is diagnosable afterwards.

### Agent 5 - App runtime, jobs and packaging
**Output:** `docs/architecture/05-runtime.md`

Design the skeleton everything else plugs into. Cover: package layout replacing the
current `sys.path.insert` hack; provider registration and discovery (entry points vs
a registry module vs directory scanning) for both sources and stores; the scheduler
implementing adaptive polling, including how it learns whether a browser is connected
via SSE; the SSE endpoint and event shapes; the job model for long operations
(a purchase download is slow and must not block a request); SQLite schema migrations
for an app users upgrade in place; the full env-var config surface; structured logging;
health/status endpoints; and the Dockerfile (multi-stage: Tailwind CLI at build,
Python + waitress at runtime, multi-arch, `PUID`/`PGID`, `/config` + `/music`) plus a
compose example beside Navidrome. Also specify how the library rescan is triggered
per server (Subsonic `startScan` for Navidrome/Airsonic/Gonic, and adapters for
Jellyfin, Emby, Plex) since the app is otherwise server-agnostic by writing files.

### Agent 6 - Visual direction and UI
**Output:** `docs/architecture/06-design.md`

Aesthetic direction before any markup. **Load the `frontend-design` skill first and
follow its two-pass process** (compact token system, then critique it against the
brief before proposing markup). Also consult `make-interfaces-feel-better` for the
polish pass.

The subject is buying records, and its vernacular is sleeves, catalog numbers, price
stickers, receipts, and quality tiers. The thing that makes this app different from a
streaming UI is **provenance**: a 24/96 hi-res purchase is not the same object as a
CD-quality one or an MP3, and the app exists because you own the file rather than rent
it. That deserves to be the signature element, not a colored status dot. The current
UI (one inline `PAGE` string in `app.py`) is competent Apple-Music-store pastiche and
is the baseline to beat.

Deliver: a token system (4-6 named hex values, a display/body/utility type pairing, a
scale), a layout concept with ASCII wireframes for the wishlist view, the one signature
element, and the empty/loading/error states. Basecoat is theme-compatible with shadcn,
so specify **which tokens to override** so the result does not read as another Tailwind
admin panel. Cover the live-refresh UX: what a track arriving via SSE should look and
feel like, and how a Claim reports progress and failure. Respect reduced-motion,
keyboard focus, and mobile width.
