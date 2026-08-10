# 07 - Integration roadmap

Written 2026-08-09, after reading `00-ARCHITECTURE-BRIEF.md` and all six architecture
documents (6,590 lines) plus the 164-row live export.

The six documents were written by agents who could not see each other's work. Each is
internally coherent. Together they are not: they use four package names, four error
taxonomies, four auth-kind vocabularies, two confidence scales, two duration columns and
two staging directories. This document is the integration pass.

**Read order for anyone building this:** section 1 (what disagrees), section 3 (the
contracts to freeze before parallel work), section 4 (milestones), section 6 (what not to
build).

### Note on repo state

While this was being written the working app was moved out of `_legacy/` to the repo root,
git-initialised, and given a `Dockerfile`, `pyproject.toml` and `docker-compose.yml`. That
in-flight work is a partial M1 and it uses **none** of the conventions in the six
documents: env prefix `WISHLIST_*` / `QOBUZ_*` (not `LW_`), config volume `/data` (not
`/config`), flat py-modules (not a `libwish` package), `requires-python = ">=3.10"` (not
3.11). It is counted as contradiction C25 below and M1 has to reconcile it.

---

## 1. Contradictions

Twenty-six. Ordered by how much damage they do if not settled before code is written.
"Winner" is a recommendation with a reason, not a vote count.

### The blocking five

These make it impossible for two people to write compatible code, and every one of them
touches more than two documents.

---

**C1. Four package names and three env prefixes.**

| Doc | Package | Env |
|---|---|---|
| `01-sources.md:83` | `lw/sources/types.py` | bare (`FUNKWHALE_URL`, `01-sources.md:700`) |
| `02-stores.md:102-113` | `libwish/stores/` | `LIBWISH_*` (`02-stores.md:924`) |
| `03-auth.md:972` | `wishlist.auth` | mixed: `PUBLIC_URL`, `LW_SECRET_KEY` (`03-auth.md:948-950`) |
| `04-identity.md:588` | `libwish/identity.py` | `MATCH_REMASTER_EQUIVALENT` (`04-identity.md:295`) |
| `05-runtime.md:81` | `libwish/` | `LW_*` throughout |
| repo today | flat modules | `WISHLIST_*` |

**Winner: `libwish` package, `LW_` prefix, everywhere, no exceptions.** 05 owns packaging
and its namespacing scheme (`LW_SOURCE_<ID>_<KEY>`, `05-runtime.md:252-258`) is the only
one that lets a new provider be configured without a code change. Concretely:
`LIBWISH_CLAIM_MIN_CONFIDENCE` -> `LW_CLAIM_AUTO_SCORE`, `PUBLIC_URL` -> `LW_BASE_URL`,
`MATCH_REMASTER_EQUIVALENT` -> `LW_MATCH_REMASTER_EQUIVALENT`.

---

**C2. The store provider owns `claim()` in one document and is forbidden from matching in
the other.**

`05-runtime.md:525`:

```python
def claim(self, jctx: JobContext, track) -> ClaimResult: ...   # Agent 2
```

`02-stores.md:20-23`:

> **Providers never decide whether a purchase matches a wishlist track.** [...]
> `qobuz_fetch._matches` is deleted, not ported. This is the direct fix for the
> 2026-08-02 wrong-track incident, which happened inside exactly this kind of
> per-provider substring check.

If a provider implements `claim()`, then enumerate/match/download/verify/commit live
inside each provider, which is precisely the shape that produced the incident. 05 wrote
this line without having read 02's pipeline.

**Winner: 02, decisively.** The store protocol is `check / buy_url / find_offers /
list_owned / expand / download` (`02-stores.md:212-253`) and nothing else. `claim()` is a
job handler in `libwish/claim.py` that calls providers. 05's `handle_claim`
(`05-runtime.md:821-827`) becomes a thin wrapper around `claim.claim(track_id, store_id)`.

---

**C3. Two confidence scales and two refusal bands.**

`02-stores.md:340-346`:

| score | action |
|---|---|
| `>= LIBWISH_CLAIM_MIN_CONFIDENCE` (0.90) | proceed |
| `>= LIBWISH_CLAIM_REVIEW_CONFIDENCE` (0.60) | refuse, `needs_review` |

`04-identity.md:443-448`:

| Score | Outcome |
|---|---|
| >= 90 | `auto` |
| 70 - 89 | `confirm` |
| < 70 | `refused` |

Floats 0..1 versus integers 0..100, and a review floor of 0.60 versus 70. `06-design.md:578`
then prints `Confidence 0.42, and a claim needs 0.90`, adopting 02's scale.

**Winner: 04.** It owns matching, its score is an integer sum of named contributions
(`04-identity.md:418-430`) and a float would be a lossy re-encoding of it. Freeze:
`LW_CLAIM_AUTO_SCORE=90`, `LW_CLAIM_CONFIRM_SCORE=70`, integers everywhere including the
wire and the UI. `06-design.md` prints `42 / 90`, not `0.42 / 0.90`.

---

**C4. Two duration columns, two units, two veto thresholds.**

- `02-stores.md:895`: `ALTER TABLE tracks ADD COLUMN duration_s REAL;` and a hard refusal
  at `max(3.0, 0.05 * expected)` seconds (`02-stores.md:349-352`).
- `04-identity.md:626`: `ALTER TABLE tracks ADD COLUMN duration_ms INTEGER;` and gate 5
  refuses at 15s (`04-identity.md:367`), with post-download quarantine also at 15s
  (`04-identity.md:820`).

Both are written as `ALTER TABLE tracks`. Applying both migrations gives one table two
duration columns that will drift.

**Winner: `duration_ms INTEGER` (04), one column.** On thresholds, both are right about
different things and the reconciliation is clean:

- **Match gate: 15s** (04). Store-reported durations round and truncate; a 3-second veto at
  match time refuses correct matches. 15s still stops the 08-02 incident (3:47 vs 4:22 is
  35s apart).
- **Post-download verify: 3s** (02). Here you have the file's own STREAMINFO duration and
  the matched item's, so the tolerance can be tight. Failure quarantines rather than
  refusing.

---

**C5. Two staging directories, and 05's startup check rejects 02's layout.**

`02-stores.md:38`:

> **Downloads land in staging under `/config`, then move atomically into `/music`.**
> [...] Cross-device staging is handled by a temp dir inside `/music` and `os.replace`.

`05-runtime.md:1044` and `05-runtime.md:1357-1358`:

> `LW_INCOMING_DIR` | `$LW_MUSIC_DIR/.libwish-incoming`
>
> If `LW_INCOMING_DIR` resolves to a different device than `LW_MUSIC_DIR`, startup
> fails with an explicit message rather than silently degrading to a non-atomic copy.

In the reference compose (`/srv/library-wishlist/config` and `/srv/music` as separate bind
mounts), 02's default **is** the cross-device case, so 05's validator would refuse to start
a correctly configured install.

**Winner: 05.** Staging on the music volume, dot-prefixed so every scanner skips it, so the
publish is always a rename. 02's real concern (unverified bytes in the scan path) is
answered by the dot prefix, which Navidrome, Jellyfin, Plex and Emby all honour. Delete
02's `/config/staging` and its cross-device copy branch. Keep `/config/staging/failed/`
for kept failures, since those are never published.

---

### The rest

---

**C6. Four error taxonomies.**

- `01-sources.md:179-181`: one class, `SourceError(kind="transient|rate_limited|auth|config|schema")`
- `02-stores.md:260-289`: `StoreError` + 7 subclasses including `StorePreparing`, `StoreParseError`
- `03-auth.md:783-789`: two classes, `TransportError` vs `AuthError(fatal=bool)`
- `05-runtime.md:372-399`: `ProviderError` + 6 subclasses

**Winner: 05's hierarchy as the trunk**, extended to cover what the others need:

```python
ProviderError(retryable=False, code, user_action)
  ConfigError            code="config"
  AuthExpired            code="auth_expired"     + fatal: bool     (from 03)
  RateLimited            code="rate_limited"     + retry_after
  TransientError         code="transient"
  SchemaError            code="schema"           (01 "schema" / 02 StoreParseError)
  Preparing              code="preparing"        retryable=True    (02 StorePreparing)
  NotFound               code="not_found"        (02 StoreNotOwned, StoreFormatUnavailable)
  RefusedAmbiguous       code="refused"
```

Non-negotiable rule carried from `03-auth.md:802-806`: **only `AuthExpired` may move a
credential out of `live`.** `TransientError` never does.

---

**C7. `HttpClient` auto-classifies 403 as auth; the auth document forbids that.**

`05-runtime.md:434-435`: "automatic mapping of `HTTPError` [...] 401/403 -> `AuthExpired`".

`03-auth.md:855-864`:

> A 403 can mean "credential rejected" or "WAF disliked your request". [...] Rule: a 403
> sets `status='error'` and schedules **one** re-probe after 90 seconds.

05's automatic mapping would mark Qobuz dead every time a WAF sneezes, which is the exact
scar `DEFAULT_UA` exists for in the broker.

**Winner: 03.** `HttpClient` maps 401 -> `AuthExpired(fatal=True)`, 403 ->
`AuthExpired(fatal=False)`, and the credential layer applies the one-re-probe rule before
transitioning.

---

**C8. Four auth-kind vocabularies, and two incompatible `AuthSpec` types.**

- `01-sources.md:160-161`: `none|api_key|lastfm_session|user_token|subsonic_password|oauth2|cookie_jar`
- `02-stores.md:216`: `"cookie" | "oauth2" | "none"`
- `03-auth.md:286-302`: `AuthSpec(method=..., label, setup_url, authorize_url, ..., pkce, redirect_modes)` with methods `oauth2|oauth1a|lastfm_web|token_paste|userpass|cookie_jar`
- `05-runtime.md:226`, `05-runtime.md:519`: `AuthSpec(kind="lastfm_web_auth")`, `AuthSpec(kind="cookie", site=...)`

Both 03 and 05 define a type called `AuthSpec` with a different first field (`method` vs
`kind`) and a different value set.

**Winner: 03's `AuthSpec` dataclass verbatim** (it is the only one carrying setup URLs,
scopes and redirect modes, which is what makes a provider addable without UI work), with
its method vocabulary. Providers reference a module-level constant
(`auth=LASTFM_AUTH`), never construct one inline as `05-runtime.md:226` shows.

---

**C9. Provider source-of-truth column vs join table.**

`05-runtime.md:238-239`:

> It is written into `tracks.source_provider`, `purchases.store_provider`,
> `provider_state.provider_id` [...]

`01-sources.md:59-60` and `01-sources.md:546-559` replace `tracks.source_platform` with a
`track_sources` join table, because a track can be loved on three services. There is also
no `purchases` table anywhere; 02 calls it `claims` + `files` (`02-stores.md:857-889`).

**Winner: 01.** `tracks.source_provider` does not exist. `purchases` does not exist.
05's provider-id-is-frozen rule (`05-runtime.md:237-245`) still stands, it just applies to
`track_sources.source_id`, `claims.store` and `provider_state.provider_id`.

---

**C10. Two cursor-state tables and two transaction shapes.**

- `01-sources.md:241-266`: typed `source_state` table with cursor, boundary_ids, backfill
  state, health, `next_due_at`, counters.
- `05-runtime.md:442-449`: generic `provider_state(provider_id, key, value)` KV plus an
  `advance()` context manager (`05-runtime.md:465-471`).

And the transaction shape differs. `01-sources.md:588-589`:

> Note the cursor update is a separate final transaction covering only committed items.

versus `05-runtime.md:472-474`:

> the cursor is committed **in the same transaction as the track inserts**, or not at all

Both fix the live loss bug. 01's is more forgiving of a mid-batch failure; 05's is stricter.

**Winner: a merge.** Adopt 01's typed `source_state` table (health and scheduling belong
next to the cursor) and 05's `advance()` mechanism, applied **per page** rather than per
batch: page items and the cursor commit together, a later page failing leaves the cursor at
the last good page. That is 01's `partial=True` semantics with 05's atomicity. 05's generic
`provider_state` KV survives only for runtime internals such as the cached Plex section id.

---

**C11. Poll intervals disagree in four cells.**

| Source | 01 active/idle/floor (`01-sources.md:476-482`) | 05 hot/warm/cold/floor (`05-runtime.md:580-585`) |
|---|---|---|
| subsonic | 5 / 60 / 2 | 5 / 30 / 60 / 2 |
| listenbrainz | **15** / 300 / 10 | **30** / 150 / 300 / 10 |
| lastfm | 30 / 300 / **5** | 30 / 150 / 300 / **10** |
| deezer | 60 / **900** / **10** | 60 / 300 / **600** / **15** |

01 has no `warm` tier at all.

**Winner: 05.** It owns the scheduler, its values are the more conservative ones, and 01's
15s on ListenBrainz is indefensible against an endpoint that already produced 24 read
timeouts in the live log (`01-sources.md:420-422`). See cut O13 for the `warm` tier itself.

---

**C12. Track status vocabulary.**

- live / brief: `queued | buying | purchased | ignored`
- `02-stores.md:902-911`: adds `owned` as terminal success, `purchased` demoted to "paid,
  no verified file"
- `05-runtime.md:1153`: `"tracks": {"queued": 160, "purchased": 2, "ignored": 2}` - no `owned`
- `01-sources.md:379-381`: refuses requeue for status in `('purchased','ignored')` - no `owned`
- `04-identity.md:628-629`: adds an orthogonal `match_status` column

**Winner: 02's five values.** `queued | buying | purchased | owned | ignored`. 01's rule
becomes "terminal statuses" = `purchased | owned | ignored`. 05's `/api/status` gains
`owned`. 04's `match_status` is orthogonal and stays.

---

**C13. Match audit stored twice.**

`02-stores.md:869`: `claims.match_decision_json TEXT -- Agent 4 trace, written BEFORE downloading`

`04-identity.md:738-770`: `match_decision` + `match_candidate` tables with
`matcher_version`, `lexicon_hash` and a `replay` CLI that reads them.

Two audit stores means `libwish audit replay` (`04-identity.md:793`) silently misses every
claim decision.

**Winner: 04's tables.** `claims.match_decision_id INTEGER REFERENCES match_decision(id)`
replaces the JSON blob.

---

**C14. SSE event names and envelope.**

`05-runtime.md:889-903` defines the namespace: `track.added`, `track.updated`,
`track.removed`, `job.started/progress/finished`, `provider.status`, `credential.updated`,
`scan.requested`, `poll.tier`, `heartbeat`, `resync`, `shutdown`. Every payload carries
`"v": 1` and is a JSON object.

Against that:

- `03-auth.md:905-912` emits `{"type": "credential", ...}` - different name, no `v`, and
  the type is in the data rather than the SSE `event:` field.
- `06-design.md:497-499` requires `claim.progress` and `claim.result`.
- `01-sources.md:586` emits `track.source_added`, which is not in 05's list.
- `02-stores.md:790-791` emits a `library.changed` event; 05 calls it `scan.requested`
  (`05-runtime.md:899`) and drives it from a job, not an event.

**Winner: 05's namespace and envelope.** Add `track.source_added`. `credential.updated`
carries 03's payload fields (`status`, `detail`, `action`, `since`) inside 05's envelope.
06's `claim.*` are `job.*` filtered on `type == "claim"`, with a `phase` field added to
`job.progress` so the UI can render stage names. `library.changed` is deleted.

Also: `02-stores.md:191-197` keys progress on `claim_id`; 05 keys on `job_id`. Carry both,
`job_id` primary.

---

**C15. Collision on publish: replace, or refuse.**

`02-stores.md:785-793`:

> Probe the existing file. If the incoming file is a strictly better tier [...] replace it
> [...] If equal or worse, keep the existing file and finish the claim as `committed`

`05-runtime.md:1350-1352`:

> if `dest` exists, `publish` raises `FileExists` and the job ends `failed` [...] It never
> silently overwrites a file the user already owns

**Winner: 05 for v1.** Overwriting a file in someone's library based on a probed quality
comparison is a large amount of trust to place in the tier ladder on day one. 02's upgrade
path is a good v2 feature behind an explicit user action ("replace my 16/44 with this
24/96"), not a silent default. Keep 02's "never write `Title (1).flac`" rule, which both
documents agree on.

---

**C16. Interrupted claims: `failed` or `interrupted`.**

`02-stores.md:393-395`: "any `claims` row in a non-terminal state is marked `failed` with
`error_code="interrupted"`".

`05-runtime.md:780-782`: "`interrupted` is a distinct state, not `failed`, because the user
should be told the truth: we do not know whether the purchase downloaded."

**Winner: 05.** The reasoning is better and the state is cheap.

---

**C17. Config file format contradicts a locked decision.**

Brief locked decision 8: "Config entirely via env." `05-runtime.md:986`: "Resolution order:
environment, then `/config/config.toml`, then the declared default." 05 does not flag this
as a deviation in its open questions.

**Winner: the brief.** Env only for v1. Keep the resolution function so a file source can
be added later without touching call sites.

---

**C18. `ProviderContext.identity` versus "providers must not normalize".**

`05-runtime.md:359` hands every provider an `identity: IdentityService`.
`01-sources.md:216` forbids it:

| Not allowed | Who owns it instead |
|---|---|
| Normalize or clean strings | `04-identity.md` |

and `01-sources.md:145-147` explains why: the identity layer needs the original bytes.

**Winner: 01.** Remove `identity` from `ProviderContext`. Identity is called by the runtime
on the ingest path and by `claim.py`, never by a provider. This also resolves
`05-runtime.md:1612-1616` (open question 7), because the split falls out naturally: cheap
`fold`/`parse_title`/`find_existing` inline, networked MBID promotion as a job.

---

**C19. The online MusicBrainz backfill is a migration step.**

`04-identity.md:706-707`, migration step 6:

> Online backfill, rate limited to 1 req/sec [...] ListenBrainz mapper then MB search per row.

`05-runtime.md:966-968` runs migrations inside `BEGIN IMMEDIATE` at startup and aborts
startup on failure. Three minutes of network calls inside a startup transaction, on a box
that may have no outbound internet (`06-design.md:742` assumes exactly that), is a
crashloop waiting to happen.

**Winner: 05's structure.** Migration steps 1-5 and 9 (pure, offline, idempotent) stay in
the migration. Step 6-8 becomes an `identity_backfill` job enqueued after first boot, whose
progress is visible in the UI.

---

**C20. Migration numbering and database filename.**

`04-identity.md:693` gates on `PRAGMA user_version = 1` and sets `user_version = 2`;
`05-runtime.md:104` assigns identity to `0004_identity.sql`. 04 snapshots with
`VACUUM INTO '/config/queue.db.pre-v2'`; 05 uses the sqlite3 online backup API to
`<db>.pre-<NNNN>.bak` and names the DB `$LW_CONFIG_DIR/library-wishlist.db`
(`05-runtime.md:996`).

**Winner: 05** on numbering, filename and backup mechanism. 04's procedure keeps its steps,
renumbered.

---

**C21. `CredentialHandle` is referenced by two documents and defined by none.**

`02-stores.md:218` (`creds: "CredentialHandle"`) and `05-runtime.md:354` (`creds:
CredentialHandle`) both depend on it. `03-auth.md:230-243` defines `CredentialStore`,
`Credential` and `CredentialStatus`, and never mentions a handle.

**Resolution:** 03 must add it. See contract C8 in section 3.

---

**C22. Credential status vocabulary vs provider state vocabulary.**

`03-auth.md:122-123`: `absent|pending|live|expiring|expired|revoked|error`, plus `unknown`
(`03-auth.md:936-937`).

`05-runtime.md:896`: `ok|auth_expired|config|error|disabled|suspended`.

Not strictly a contradiction (they describe different objects) but they reach one UI and
nobody wrote the mapping.

**Resolution:** keep both, publish the mapping table (section 3, C8).

---

**C23. 7digital is OAuth 1.0a, or it is a cookie scrape.**

`03-auth.md:14-19` adds a sixth auth method solely for it. `02-stores.md:560-587` concludes
the partner API is unobtainable by a self-hoster and ships the store as buy-link-only with
`auth kind: cookie (broker), optional` (`02-stores.md:307`).

**Winner: 02.** `oauth1a` would ship with no consumer. See cut O3.

---

**C24. The UI's signature element has no data source.**

`06-design.md:12-15` makes the provenance plate the signature, with wanted rows showing
`UP TO 24/96` and a price. `02-stores.md:1055-1058` (open question 3):

> Without it there is no catalogue search, so Qobuz buy links stay search pages rather
> than product pages, and `find_offers` returns nothing for Qobuz.

and `02-stores.md:302` records `deep_link: no` for Qobuz. Bandcamp does not commit to a
format until download (`06-design.md:762-763`). So in v1, for both shipping stores, there is
no promised tier and no price.

**Winner: reality.** 06's eighth state (`TIER UNKNOWN`) is the **default** for v1, not the
exception. The tier ladder is populated only on `owned` rows, where it is a fact about a
file. Grouping-by-tier (`06-design.md:221`) degrades to grouping owned rows only. This does
not kill the design: the *owned* plate, which is the payoff, still works and is still
unlike anything else in the category.

---

**C25. The shipped repo uses none of the agreed conventions.**

`pyproject.toml` at the root declares `py-modules = ["app", "queue_lib", "cookie_broker",
"qobuz_fetch", "queue_ingest"]` and `requires-python = ">=3.10"`. The `Dockerfile` sets
`WISHLIST_DB=/data/queue/queue.db`, `QOBUZ_DEST_DIR=/music`, `WISHLIST_HOST=0.0.0.0`.

**Winner: the docs, but do the rename once, in M1, deliberately.** `/data` -> `/config`,
`WISHLIST_*` -> `LW_*`, flat modules -> `libwish`, `>=3.10` -> `>=3.11`. Doing it later
means migrating a user's compose file.

---

**C26. Facts about the live deployment disagree with each other.**

Brief section 2: Navidrome "is not a Docker container; installed directly". Brief section 6:
"Library rescan has never fired [...] Every successful download has silently skipped the
rescan." `05-runtime.md:1592-1594` (open question 4):

> On the box today it is `deluan/navidrome:latest` in Docker, and
> `queue_lib.fetch_purchase` calls `docker exec navidrome`.

If 05 is right, the rescan **has** been firing and the brief's hazard list is wrong. This
does not change the design (the HTTP API is correct either way) but it changes the cutover
runbook and it means one recorded "hazard" is not one.

**Resolution:** verify on the Mini before writing the runbook. One `docker ps`.

---

## 2. Dependency graph

From actual interface references, not intuition. An arrow means "cannot be written without".

```
                    ┌──────────────────────────────────┐
                    │ SHELL (05)                       │
                    │ Settings · db+migrate · errors   │
                    │ registry · ProviderContext       │
                    │ HttpClient · jobs · EventBus     │
                    └───┬───────────┬──────────┬───────┘
                        │           │          │
            ┌───────────┘           │          └───────────┐
            ▼                       ▼                      ▼
   ┌─────────────────┐   ┌────────────────────┐  ┌──────────────────┐
   │ IDENTITY (04)   │   │ AUTH (03)          │  │ SSE + UI (05,06) │
   │ pure stdlib     │   │ CredentialStore    │  │ EventBus consumer│
   │ NO deps at all  │   │ CredentialHandle   │  │                  │
   └────────┬────────┘   │ cookie_jar adapter │  └──────────────────┘
            │            └────────┬───────────┘
            │                     │
            │       ┌─────────────┴───────────┐
            │       ▼                         ▼
            │  ┌──────────────┐      ┌──────────────────┐
            └─▶│ SOURCES (01) │      │ STORES (02)      │◀── needs IDENTITY
               │ ingest path  │      │ + claim pipeline │◀── needs JOBS
               └──────┬───────┘      │ + verify         │◀── needs staging
                      │              └────────┬─────────┘
                      └──────────┬────────────┘
                                 ▼
                        ┌──────────────────┐
                        │ UI beyond a list │
                        │ plate · refusal  │
                        └──────────────────┘
```

Evidence for each edge:

| Edge | Evidence |
|---|---|
| everything -> shell | `01-sources.md:213` ("the runtime"), `02-stores.md:806` ("Agent 5 owns the migration runner"), `03-auth.md:809` ("One job in the scheduler"), `04-identity.md:588` (lands as `libwish/identity.py`) |
| identity -> nothing | `04-identity.md:586-588`: "depends on `json`, `re`, `unicodedata`, `collections` and `difflib` only" |
| sources -> identity | `01-sources.md:574`: `track_id = identity.resolve(item)` |
| sources -> auth | `01-sources.md:700`: `cfg.secret("funkwhale", "token")` |
| stores -> identity | `02-stores.md:337-339`: "Feed every cached `OwnedItem` [...] to Agent 4's matcher" |
| stores -> auth | `02-stores.md:985`: `creds.http_client(...)` |
| stores -> jobs | `02-stores.md:529-530`: "the single reason `download()` must be a job" |
| UI -> stores | `06-design.md:766`: "can each store provider return a `promised_tier`" |
| UI -> identity | `06-design.md:768-771`: needs score and threshold together in the result |
| auth -> shell | `03-auth.md:894-899`: routes must not be unauthenticated, needs 05's auth mode |

**Two things this graph says that intuition does not:**

1. **Identity has zero dependencies and can be built first, alone, offline, against a
   fixture.** It is also the highest-risk subsystem. Build it first anyway.
2. **Auth is not a prerequisite for the first working version.** The live app authenticates
   Qobuz through the already-deployed cookie broker, and Last.fm / ListenBrainz through env
   vars. A full `CredentialStore` is only required when the *UI* has to acquire a credential,
   which is a v1.1 concern. This is the single biggest sequencing insight here: 03 is 1,142
   lines describing work that unblocks nothing in M1 through M4.

---

## 3. Shared core contracts

These must be frozen before parallel work starts. Everything below is a reconciliation, not
a new design; the source document is cited so the detail behind each one is findable.

### C1 - Naming

- Package `libwish`. Modules per `05-runtime.md:77-136`, with `libwish/claim.py` added
  (`02-stores.md:112`) and `libwish/identity.py` + `libwish/match.py` at top level
  (`04-identity.md:588`) rather than under `libwish/identity/`.
- Env prefix `LW_`. Provider namespace `LW_SOURCE_<ID>_<KEY>` / `LW_STORE_<ID>_<KEY>`
  (`05-runtime.md:252-258`).
- Provider `id` matches `^[a-z][a-z0-9_]{1,31}$`, unique across both kinds, frozen forever
  once shipped (`05-runtime.md:237-245`).
- Volumes `/config` and `/music`. DB at `$LW_CONFIG_DIR/library-wishlist.db`.

### C2 - `ProviderInfo`

`05-runtime.md:295-320` verbatim, with two changes:
- `auth: AuthSpec` is 03's dataclass (C8 below), referenced as a module constant.
- `capabilities` for stores is 02's typed `StoreCapabilities` (`02-stores.md:142-149`), not
  a `frozenset[str]`. 05's own open question 8 (`05-runtime.md:1618-1623`) doubts the
  stringly-typed choice; 02 already wrote the typed version. Sources keep the frozenset,
  with exactly two members the runtime branches on: `backfill`, `cursor`.

### C3 - `ProviderContext`

`05-runtime.md:347-360` minus `identity` (see C18), which leaves:

```python
@dataclass(frozen=True)
class ProviderContext:
    provider_id: str
    settings:    Settings
    log:         Logger                        # bound with provider=<id>
    conf:        Callable[[str], str | None]   # namespaced; providers never read os.environ
    creds:       CredentialHandle
    http:        HttpClient                    # the ONLY http path; routes via SessionKeeper for cookie auth
    state:       ProviderState
    db:          Callable[[], sqlite3.Connection]
    paths:       PathService
```

Absent by design: the event bus, the job queue, the identity service.

### C4 - Error taxonomy

Section 1, C6. One hierarchy in `libwish/errors.py`. The 403 rule from C7. Anything that is
not a `ProviderError` escaping a provider is logged with a traceback, treated as
`TransientError(code="unexpected")` and counted at `/api/status`
(`05-runtime.md:415-420`).

### C5 - The loved-track record

`01-sources.md:88-120` verbatim: `LovedTrack` and `TrackIds`. This one needs no
reconciliation; nothing else in the six documents defines a competing shape, and its
"raw means raw, no normalization" rule is what makes the identity layer possible.

The fetch protocol reduces to (see cut O6):

```python
@dataclass(frozen=True)
class SourcePage:
    items:   tuple[LovedTrack, ...]
    cursor:  dict | None          # provider-owned, JSON-serialisable, opaque elsewhere
    more:    bool = False
    skipped: int = 0
    total:   int | None = None

def poll(self, cursor: dict | None, *, mode: Literal["seed","incremental","backfill"],
         max_items: int = 500) -> SourcePage
```

Boundary comparison is `>=`, not `>` (`01-sources.md:38-44`). This is not negotiable and it
is one of the two live data-loss bugs.

### C6 - Track identity

`04-identity.md:466-482` (`NormalizedTitle`, including `__contains__` raising `TypeError`)
and `04-identity.md:502-525` (`TrackIdentity`, `MatchDecision`) verbatim. Public surface at
`04-identity.md:487-498`.

Frozen numbers: auto >= 90, confirm 70-89, refused < 70; string-only cap 84; paren-stripped
and degraded cap 74; artist gate 0.85; title fuzzy 0.92; duration gate 15s.

The three invariants (`04-identity.md:455-462`) are contractual: no substring anywhere, tie-in
text is never evidence, strings alone never auto-claim.

### C7 - Store contract

`02-stores.md:117-207` (value types) and `02-stores.md:212-253` (protocol) verbatim, with:
- `__init__(self, ctx: ProviderContext)` instead of `(cfg, creds)`; the credential handle is
  `ctx.creds`.
- No `claim()` method (C2).
- `duration_s: float | None` on `OwnedItem` stays as a float because it is a store-reported
  value; only the `tracks` column is `duration_ms` (C4). Convert at the boundary.

### C8 - Credentials

`03-auth.md:208-243` (`Credential`, `CredentialStatus`, `CredentialStore`) verbatim, plus the
missing type that C21 exposed:

```python
class CredentialHandle(Protocol):
    """Per-provider facade over CredentialStore. What a provider actually gets."""
    def require(self) -> Credential: ...            # raises AuthExpired if absent/expired
    def http_client(self, **kw) -> HttpClient: ...  # cookie providers get a SessionKeeper-backed one
    def mark_ok(self) -> None: ...
    def mark_failed(self, detail: str, *, fatal: bool) -> None: ...
```

`AuthSpec` is `03-auth.md:286-302` with methods `oauth2|oauth1a|lastfm_web|token_paste|userpass|cookie_jar|none`.

Status mapping (C22), published once so the UI has one branch:

| `credentials.status` | `provider.state` | UI |
|---|---|---|
| `absent`, `pending` | `config` | "not set up" |
| `live` | `ok` | normal |
| `expiring` | `ok` + warning banner | "reconnect soon" |
| `expired`, `revoked` | `auth_expired` | banner + per-row disable |
| `error` | `error` | "cannot reach X" |
| `unknown` | `ok` + `stale: true` | "unverified for Nh" |

### C9 - Event contract

`05-runtime.md:885-903` as the canonical list, with the C14 amendments: add
`track.source_added`; `job.progress` gains `phase`; `credential.updated` carries 03's
payload; delete `library.changed`. Every payload is a JSON object carrying `"v": 1`. Only
the runtime publishes (`05-runtime.md:912-918`).

The five claim phases, reconciled between `02-stores.md:24-28` and `06-design.md:523-529`,
because the UI prints "3 OF 5" and it has to be true:

| # | `phase` | UI label | Sub-line |
|---|---|---|---|
| 1 | `session` | `SESSION` | Checking your Qobuz session |
| 2 | `enumerate` | `FINDING` | Looking through your Qobuz purchases |
| 3 | `match` | `MATCHING` | Matching your purchases to "Lies" by CHVRCHES |
| 4 | `download` | `DOWNLOAD` | Downloading, 8.1 of 12.4 MB |
| 5 | `verify` | `VERIFY` | Checking the file is what it claims to be |

Commit is not a phase; it is the terminal transition to `job.finished`.

### C10 - Database

Table ownership, one owner each:

| Table | Owner | Source |
|---|---|---|
| `tracks` | shared, columns per owner | `04-identity.md:620-630`, `02-stores.md:895-899` |
| `track_sources` | 01 | `01-sources.md:546-559` |
| `source_state` | 01 mechanism, 05 transaction | C10 |
| `track_mbid`, `track_isrc` | 04 | `04-identity.md:632-651` |
| `match_decision` | 04 | `04-identity.md:738-758` |
| `track_store_offers`, `store_inventory`, `store_sync`, `claims`, `files` | 02 | `02-stores.md:808-889` |
| `credentials`, `auth_pending` | 03 | `03-auth.md:117-186` |
| `jobs`, `provider_state`, `provider_runs` | 05 | `05-runtime.md:691-714`, `442-449`, `653-664` |

Migration numbering, forward-only, `PRAGMA user_version`, online-backup before each apply,
adopt-at-1 for the live DB (`05-runtime.md:953-968`):

```
0001_baseline.sql     live schema exactly, including dedup_key   (adopt, never executed on the live DB)
0002_jobs.sql         jobs, provider_state, provider_runs
0003_identity.sql     04's DDL; offline backfill only
0004_sources.sql      track_sources, source_state; 164-row provenance import
0005_stores.sql       claims, files, store_inventory, offers, store_sync; status vocabulary
0006_credentials.sql  credentials, auth_pending
0007_contract.sql     drop dedup_key, source_platform, bandcamp_url, qobuz_url  (one release later)
```

`0007` is the release nobody scheduled. It is on the roadmap so it happens.

---

## 4. Milestones

Every milestone ends with a running application that is better than the one before it. None
of them is a half-wired refactor. Sizes are S (a day or two), M (about a week), L (two weeks
or more) at one person's pace.

---

### M1 - The same app, properly packaged (L)

**From:** `05-runtime.md` sections 2, 8, 9, 10, 11, 13, 15; C1, C25.

Build the shell and move the existing behaviour onto it, unchanged. `libwish` package with
`pyproject.toml`, `Settings` from `LW_*` env only, per-thread SQLite with WAL, the migration
runner with adopt-at-1, `libwish/errors.py`, the registry and `ProviderContext`, `HttpClient`,
the job queue and 2 workers, structured logging with redaction, `/healthz` and `/readyz`,
waitress with `LW_AUTH_MODE=token`, the single-instance lock (`05-runtime.md:1596-1603`), and
the multi-stage Dockerfile with `/config` + `/music` + PUID/PGID.

Existing behaviour ports as-is: the current `PAGE` string becomes one Jinja template with no
visual change, Last.fm and ListenBrainz ingest become two providers with their current
logic, Qobuz claim becomes a job handler wrapping today's `qobuz_fetch` code.

**Done means:** the Mini's `queue.db`, copied and unmodified, is served by the container
beside Navidrome; all 164 rows render; clicking Claim on a purchased Qobuz track downloads
the FLAC as a background job that survives a page reload; `docker logs` shows one structured
line per poll; the container refuses to start with `LW_AUTH_MODE=none` on `0.0.0.0`.

**Also fixed here, because they are one-liners in this context:** the `docker exec` rescan
becomes `LW_RESCAN_KIND=subsonic` calling `startScan` over HTTP; `Q.init()` runs at import
rather than under `if __name__` (`05-runtime.md:66`).

---

### M2 - Identity (M) - *parallel with M3, M5*

**From:** `04-identity.md` entire; C6.

`libwish/identity.py` and `libwish/match.py` from 04's prototype. Migration `0003`: the new
columns, offline recompute of `artist_key`/`title_key`/`qualifier_key`/`fp_key` from `artist`
and `title` (never from `dedup_key`, `04-identity.md:611-613`), the collision report before
the unique index, `identity_degraded` on the one `¥$` row. The `match_decision` table. The
positive and negative corpora, the naive-matcher meta-test, the invariant tests, the
live-164 fixture.

The MusicBrainz backfill is a **job**, not a migration step (C19), with the shared 1 req/s
token bucket.

**Done means:** `pytest` proves the negative corpus rejects `naive_match`; the live 164 rows
produce 164 distinct fingerprints; `"lies" in parse_title(...)` raises `TypeError`;
`libwish audit replay --since <ts>` runs offline and prints zero flips against a corpus
generated by the same version.

This is the highest-value milestone in the document. It is also entirely offline, entirely
stdlib, and testable without a single credential.

---

### M3 - Sources (M) - *parallel with M2, M5*

**From:** `01-sources.md`; C5, C10, C11.

The `SourceProvider` contract, `source_state`, `advance()` per page, the inclusive boundary,
the ingest algorithm with `track_sources` and the terminal-status no-requeue rule. Port
Last.fm and ListenBrainz properly (body-based error classification, `01-sources.md:505-511`),
add Subsonic/Navidrome. Migration `0004`, including the 164-row provenance import that maps
`deezer-unobtainable` to `import:deezer-unobtainable` (`01-sources.md:618-637`).

**Done means:** `kill -9` during a poll re-delivers the same window on restart instead of
losing it (this is the live bug at `queue_ingest.py` and the reason the milestone exists);
two loves in the same second both arrive; starring a track in Navidrome puts it on the
wishlist; a track loved on Last.fm and ListenBrainz is one row with two `track_sources`
entries.

---

### M4 - SSE and the real UI (L)

**From:** `05-runtime.md` section 7; `06-design.md` entire; C9, C14, C24.

`EventBus`, `GET /events`, the two-tier presence logic (see cut O13), the waitress thread
budget. Then the design: Tailwind standalone at build time, Basecoat with 06's override
block (`06-design.md:399-448`, which is load-bearing, not polish), the vendored Archivo, the rack layout, the provenance plate as a Jinja macro, the insertion policy that
never moves content under a reader, the refusal screen, the empty and error states, reduced
motion and keyboard support.

The plate ships four states in v1 per C24: `NO STORE`, `WANTED`, `WORKING`, `OWNED`, plus
`REFUSED`. The tier ladder is populated on `owned` rows only. `WANTED` draws nothing: it is
the state of every row on the front page, so a badge announcing it sat beside a tab and a
buy button already saying the same thing. The element stays in the markup, hidden, because
it is what the live connection swaps a starting claim into.

**Done means:** love a track in Navidrome, it appears in an open browser within the hot
interval with no reload and without scrolling the page; a claim shows five named stages; a
refusal prints the rejected candidate's title verbatim with `42 / 90` and downloads nothing;
`prefers-reduced-motion` removes the stamp press; the whole thing is usable at 375px.

---

### M5 - Credentials in the UI (M) - *parallel with M2, M3*

**From:** `03-auth.md` sections 1, 2, 5.3-5.6, 6, 7; C8, C21, C22.

`CredentialStore` with **plaintext 0600** storage (see cut O1), `CredentialHandle`, the
`cookie_jar` adapter over the deployed `SessionKeeper` with zero upstream changes, plus
`token_paste`, `userpass` and `lastfm_web`. The exception split, the janitor's verify sweep,
the `credential.updated` event, the dead-credential banner with its closed-set `action`.
Migration `0006`.

**Done means:** every source and Qobuz can be connected from the UI with no env vars set;
pasting a whitespace-padded ListenBrainz token fails at paste time with the reason, not at
the first poll; deleting the Qobuz jar produces the reseed banner within one janitor tick and
disables Claim on affected rows with "Qobuz session expired" inline; `/api/credentials`
returns no secret material.

No OAuth. Nothing in M1 through M5 needs it.

---

### M6 - Stores and the claim pipeline (L)

**From:** `02-stores.md` sections 2, 3, 4.1, 5, 6, 7; C2, C3, C4, C5, C13, C15.

The `StoreProvider` protocol, `libwish/claim.py` with the five phases and the single commit
point, `verify.py` (reduced per cut O8), staging and atomic publish, migration `0005` with
the `owned` status and the `/music` reconciler for the 2 existing purchased rows. Qobuz
rewritten: pagination, `/profile/downloads/album`, structured row parsing instead of an HTML
slice, `StoreAuthError` on a `/signin` redirect, `SchemaError` on a missing anchor, the
config-driven format preference.

**Done means:** a Qobuz album purchase is claimable; a deliberately corrupted cookie jar
produces "your Qobuz session expired" and not "you own nothing"; a saved Qobuz signin page
saved as `.flac` fails verification with code `html_error_page`, not merely "not ok"; the
match trace is in `match_decision` before any bytes are fetched; a failed claim leaves the
track on the wishlist and the staging directory swept.

---

### M7 - Bandcamp (M)

**From:** `02-stores.md:460-558`.

The fan-id and collection enumeration, the three-hop download with the `/statdownload/`
transform, `Preparing` retries, zip handling with zip-slip and ratio guards, `expand()` and
the second match pass inside the archive.

**Done means:** a Bandcamp album purchase downloads, the correct member is extracted after a
second CONFIRM, and the file lands with its cover. Bandcamp is permanently tier 3
(`04-identity.md:829-831`), so every Bandcamp claim goes through CONFIRM by design.

**Risk to test on day one:** `02-stores.md:1042-1049` flags possible TLS fingerprinting. If
plain `urllib` gets 403s a browser does not, Bandcamp costs a compiled dependency on both
arches. Test this before committing to the milestone, not during it.

---

### M8 - Contract migration and hardening (S)

Migration `0007` drops `dedup_key`, `source_platform`, `bandcamp_url` and `qobuz_url`. The
`buying` timeout maintenance job. Backup documentation and `libwish backup`. Pinned image
tags in the compose example. The cutover runbook (gap G1).

---

### Deliberately after v1

Deezer OAuth2 and the redirect ladder (M9); the identity confirm-queue UI if the backfill
produces a long one; the quality-upgrade replace path from C15; `LocalInbox`; 7digital;
Jellyfin/Emby/Plex rescan adapters; encryption at rest gated on `LW_SECRET_KEY`.

### Parallelism

```
M1 ────┬──▶ M2 (identity)  ─┐
       ├──▶ M3 (sources)   ─┼──▶ M4 (SSE + UI) ──┬──▶ M6 (stores) ──▶ M7 (bandcamp) ──▶ M8
       └──▶ M5 (credentials)┘                     │
                                                  └── M6 also needs M2
```

M2, M3 and M5 are genuinely independent once the section 3 contracts are frozen: M2 touches
no I/O, M3 touches no store code, M5 touches no track code. M4 needs M3 for something to
render. M6 needs M2 (scoring), M5 (the cookie handle) and M1 (jobs, staging).

---

## 5. What the six documents collectively forgot

### Load-bearing

**G1. The cutover runbook.** Nobody owns moving from the Mini to the container. It involves:
Python 3.9.6 to 3.11+ (`05-runtime.md:1583-1587`, so this is a cutover and not an in-place
upgrade), launchd to Docker, `/Volumes/Music/library` to `/music`, `queue/lastfm_hw.txt` and
`queue/lb_hw.txt` into `source_state` (`01-sources.md:281-285`), `queue/qobuz_jar.json` to
its new home, and the fact that the launchd job must be genuinely disabled rather than
`bootout`-ed (brief section 6, the job that ran 1569 times while believed disabled). Above
all: **the old and new must never both write the database**, which the single-instance lock
does not protect against because they are different processes with different lock files.
This is the most likely way to lose the 164 rows.

**G2. The 164-row migration is described three times by three owners with no sequence.**
`01-sources.md:618-637` (provenance), `02-stores.md:912-917` (status and the `/music`
reconciler), `04-identity.md:693-718` (identity). They interact: identity migration can merge
rows, which changes which `track_id` the provenance import should point at. Correct order is
identity, then provenance, then the owned reconciler, and it is written down here because it
is written down nowhere else. Note also that `_tracks-sample.json` carries only
`artist`/`title`/`source_platform`/`status`/`dedup_key`; it has no `id`, no `added_at` and
none of the URL columns, so it cannot exercise 01's or 02's migration SQL as a fixture.

**G3. First-run onboarding falls in the gap between two documents.**
`05-runtime.md:1625-1628` punts it to Agent 6. `06-design.md:788-790` designs the three-step
panel and then says the provider-connection screens are "not designed here" because they
depend on Agent 3. So the screen a new user hits second, the connect-a-source form, is
designed by nobody. It is renderable generically from `ConfigSpec` plus `AuthSpec`
(`05-runtime.md:264-267`, `03-auth.md:328-353`), which is the whole point of those types, but
somebody has to build it. Belongs in M5.

**G4. Nothing owns the resolve and enrich path.** The live app's `resolve()`,
`resolve_pending()`, `deezer_meta()`, `bandcamp_search()` and `qobuz_search_url()` populate
`preview_url`, `cover_url`, `bandcamp_url`, `qobuz_url` and `resolved`. 02 covers `buy_url`
and `find_offers`, which is the store half. The **unauthenticated Deezer lookup that supplies
the play button and the cover art** appears only in passing at `01-sources.md:465-468` and is
in no document's scope. The current UI's two most visible features have no home in the new
architecture. It belongs in M1 as a `resolve` job type.

**G5. Rate limiting is specified three times, incompatibly.** `04-identity.md:150-153` needs
one process-wide 1 req/s token bucket for MusicBrainz; `02-stores.md:456-457` and
`02-stores.md:948` want per-store `min_interval_s`; `05-runtime.md:427-428` has
`ctx.http.limit(host, rps)`. Three limiters means the MusicBrainz bucket is per-provider and
the IP gets blocked. **One registry keyed by host, owned by `HttpClient`, shared process-wide.**

**G6. Four error vocabularies reach one UI.** `05-runtime.md:377` `user_action`,
`03-auth.md:917` `action`, `02-stores.md:874` `error_code`, `04-identity.md:534-543` reason
codes. Nobody wrote the mapping from a reason code to a sentence a user reads. 06's failure
table (`06-design.md:557-561`) covers five cases out of roughly thirty.

**G7. Rollback.** `05-runtime.md:957-959` refuses to start on a downgrade, and the compose
example uses `:latest` (`05-runtime.md:1511`). Together those mean: pull latest, migrate,
hit a bug, cannot go back. Pin the tag in the example and document restoring
`<db>.pre-<NNNN>.bak`.

**G8. Backup.** `/config` holds the DB, the cookie jar and (if encryption ships) the key. The
only backup in the entire design is the pre-migration one. A `libwish backup` command and a
README paragraph is the whole fix, and its absence is the difference between "I lost my
config" and "I lost 164 loves I collected over a year".

**G9. Testing strategy above the unit level.** Each document invents its own "prove the test
can fail" mechanism: `01-sources.md:842-848` (a stub broken twelve ways plus a meta-test per
break), `02-stores.md:720-739` (fixtures per failure code), `03-auth.md:1020-1024` (write the
failing test first), `04-identity.md:878-895` (the naive-matcher meta-test). The instinct is
correct and it is the right lesson from the 08-02 audit. But there is no CI configuration, no
fixture-recording tool, no convention for testing a store provider without an account, and no
test at all of the seam between subsystems. Unify on 04's meta-test pattern, which is the
cheapest and the most convincing.

**G10. Two authentication systems on one app.** `LW_AUTH_TOKEN` gates the UI and API;
`COOKIE_BROKER_TOKEN` gates `/auth/ingest` for the extension (`03-auth.md:880-884`). Both are
correct in isolation. Nobody wrote down that there are two, which token goes where, or what
happens when a user sets one and not the other.

### Can wait

Disk-full mid-download (there is a pre-flight check at `05-runtime.md:1047`, nothing handles
ENOSPC at write time); timezone handling for `loved_at` display; the AGPL/legal paragraph in
the README that a copyright-adjacent app should probably have; retention policy for the three
different raw-payload stores (`track_sources.raw` at 4KB/90 days, `store_inventory.raw_json`
unbounded, `match_candidate.candidate_json` at 180 days); resumable downloads.

### Correctly and deliberately out of scope

Multi-user. `03-auth.md:1118-1124` names `(provider, account_id)` as the schema change most
likely to be regretted and leaves it out anyway. That is the right call and it should stay
out.

---

## 6. Over-engineering

The working application is 946 lines across 5 files with one dependency, serving one user.
These documents propose roughly 6,900 lines of design. A roadmap that adopts all of it
uncritically has failed. Nineteen cuts, blunt, with the smaller version named.

---

**O1. AES-256-GCM credential encryption, `cryptography`, key versioning, `LW_SECRET_KEY_OLD`
rotation and boot-time re-wrap.** `03-auth.md:531-551`.

03's own section 4.3 dismantles it (`03-auth.md:569-584`): it protects nothing against
anything running as the app user, and with the default `/config/secret.key` the key and the
ciphertext travel together so "the encryption buys exactly zero" for the default
configuration. It costs the project's first non-pure-Python dependency, which
`05-runtime.md:1440-1443` explicitly does not want.

**Smaller version:** `0600` files, one honest README paragraph. Add `cryptography` as an
optional extra that activates only when `LW_SECRET_KEY` is present in the environment, which
is the one configuration where it actually helps. This also deletes O2.

---

**O2. The `SessionKeeper.load_state`/`save_state` upstream hooks.** `03-auth.md:755-774`.

Exists solely to bring the cookie jar under O1. Cut with it. The broker then needs **zero**
changes, which is a better outcome than "one small additive change" against a component the
brief describes as built, deployed and proven.

---

**O3. `oauth1a` and RFC 5849 request signing.** `03-auth.md:637-658`.

Its only consumer is 7digital, which `02-stores.md:560-573` establishes cannot be automated
by a self-hoster at all. 03 concedes it would be "implemented against no live account"
(`03-auth.md:1084-1088`). That is a couple of hundred lines of percent-encoding a signature
base string, tested against a spec fixture, proving a shape nobody will use.

**Smaller version:** delete. Deezer's `oauth2` is the only OAuth in v1 and it is optional.

---

**O4. The user-hosted OAuth relay page and `OAUTH_REDIRECT_URL`.** `03-auth.md:491-495`,
`03-auth.md:1031-1071`.

03's own analysis (`03-auth.md:393-395`) finds that of the entire provider set exactly two
could need a redirect workaround and one of them documents that it does not. The relay is an
escape hatch, plus a hosted HTML page, plus a security note, plus a mode in
`resolve_redirect`, for a case that may never arise.

**Smaller version:** ship `same_origin` and `loopback_paste`. Two rungs. Add the third if
Tidal ever ships.

---

**O5. Four cursor kinds, `boundary_ids`, an independent backfill cursor with resumable
progress, snapshot diffing, and `still_loved`.** `01-sources.md:241-266`, `01-sources.md:355-370`.

For four sources of which three are timestamp-cursored. `01-sources.md:878-881` admits
`still_loved` has no consumer in v1 and calls it "dormant schema".

**Smaller version:** one opaque JSON cursor blob the provider owns, inclusive comparison, and
a `seen` id set for Subsonic. Forward-only on first connect (`01-sources.md:341-345` already
makes that the default) plus a `libwish backfill <source>` CLI command that walks history
once and exits. No `backfill_cursor`, no `backfill_seen`/`backfill_total`, no SSE progress
bar for backfill, no un-love detection.

---

**O6. `Budget(max_items, max_requests, deadline_ts)` with `more` and `partial` flags.**
`01-sources.md:163-178`.

A scheduling protocol between the provider and the scheduler, for a personal queue that gets
a handful of new loves a day.

**Smaller version:** `max_items` and `more`. That is `SourcePage` in C5.

---

**O7. Twelve parametrised conformance tests plus `broken_stub.py` deliberately wrong in twelve
specific ways plus a meta-test asserting each break turns each test red.**
`01-sources.md:824-848`.

The instinct is right and the incident justifies some of it, but this is a test suite for the
test suite, per provider, for four providers.

**Smaller version:** keep 04's naive-matcher meta-test (`04-identity.md:880-895`), which is
the same idea for one line of code and is the one that guards the actual incident. Keep five
conformance tests: no normalization, auth classified from the body not the status, missing
fields skipped, cursor JSON round-trips, no DB and no sleep. Drop the other seven and the
stub.

---

**O8. A hand-written stdlib header parser for FLAC, MP3, MP4/ALAC, Ogg, WAV and AIFF.**
`02-stores.md:653-682`.

02 estimates 150 lines and it is bit-twiddling across six container formats, five of which v1
will never download. Reading an 80-bit extended float out of an AIFF `COMM` chunk is not on
the critical path to buying records.

**Smaller version:** FLAC STREAMINFO (the only format where the duration check matters, and
02 is right that it is exact and free), plus a magic-byte sniff for everything else, plus the
`html_error_page` check which catches the actual observed failure. Or take `mutagen`, which
`02-stores.md:679-682` already concedes is legitimate. Either is a third of the code.

---

**O9. `LocalInbox` as a pseudo-store.** `02-stores.md:610-642`.

The argument for it is genuinely good and the interface is right. It is still a store for
purchases the app cannot see, shipped alongside two stores it can, on day one.

**Smaller version:** defer. Its 60 lines stay cheap precisely because the interface was
designed with it in mind, which is the actual value 02 delivered here.

---

**O10. 7digital as a third store.** `02-stores.md:560-608`.

No obtainable API, an unverified locker, a company mid-rebrand into B2B, and 02 itself
suggests Bleep or Presto would be better candidates (`02-stores.md:1050-1054`). It exists to
prove the abstraction, but the abstraction is already proven by Qobuz (per-track, cookie,
HTML scrape, synchronous) and Bandcamp (release-granular, JSON API, asynchronous, archives)
being genuinely different in every dimension the interface models.

**Smaller version:** delete. The buy-link stub is twenty lines whenever anyone wants it.

---

**O11. The full job machinery: priority levels, per-type `max_attempts` ladders, a stall
watchdog, retention pruning, coalesced progress persistence.** `05-runtime.md:691-810`.

For two workers and about five job types.

**Smaller version:** keep the table, `job_key` idempotency (which is what stops a
double-click starting two downloads to one path, and is the best idea in the section), crash
recovery to `interrupted`, and cooperative cancel. Cut `priority`, the retry ladders, the
stall watchdog and `LW_JOB_RETENTION_DAYS` for v1.

---

**O12. SSE `Last-Event-ID` replay ring, `resync`, `overflow`, per-subscriber queue caps.**
`05-runtime.md:864-877`.

One user with one or two tabs. This is designed for a fan-out problem that does not exist.

**Smaller version:** on reconnect, always refetch `/api/queue`. That is the `resync` path,
taken unconditionally, and it deletes the ring, the sequence table, the overflow branch and
the queue cap. Keep the `: ping` keepalive and the `LW_SSE_MAX_CLIENTS` cap, because the
waitress thread ceiling (`05-runtime.md:878-880`) is a real constraint.

---

**O13. Three poll tiers plus a grace period plus a hot budget plus `last_interaction`
tracking plus a `poll.tier` event.** `05-runtime.md:594-632`.

The concern is legitimate: a forgotten tab must not poll Last.fm every 30 seconds forever.
But the answer to that is a floor and a cold default, not a four-variable state machine with
a "presence is not attention" doctrine.

**Smaller version:** two tiers. `watchers > 0` is hot, otherwise cold, with the 90-second
grace to ride out an `EventSource` reconnect (which is the one part of this that is genuinely
load-bearing). Cut `warm`, `LW_HOT_MAX_MINUTES` and `last_interaction`. If someone leaves a
tab open for a week, they poll their own Navidrome every 5 seconds and Last.fm every 30, and
that is fine.

---

**O14. Rescan adapters for Jellyfin, Emby and Plex.** `05-runtime.md:1215-1234`.

Three untested code paths for three servers the author does not run, in an app whose entire
premise is sitting beside Navidrome.

**Smaller version:** `subsonic`, `command` and `none`. `command` covers every other server
including the three above and including `docker exec navidrome navidrome scan` for anyone who
wants it (`05-runtime.md:1236-1239` already says so).

---

**O15. `/metrics` Prometheus endpoint.** `05-runtime.md:1145`. Off by default, for one user,
duplicating `/api/status`. Delete.

---

**O16. `/config/config.toml` as a second configuration source.** `05-runtime.md:986`.

Contradicts locked decision 8 and doubles the number of places a value can come from, which
doubles the number of places a support question starts.

**Smaller version:** env only. Keep the resolver function so a file can be added later.

---

**O17. Entry-point plugin discovery and `LW_PLUGIN_ENTRYPOINTS`.** `05-runtime.md:18-21`,
`05-runtime.md:1077`.

There are no third-party providers and there will not be for a long time. 05's own argument
against directory scanning applies with almost equal force here: `importlib.metadata` is
fragile in an editable checkout and invisible in the source tree.

**Smaller version:** the explicit import list, which is the entire feature and which 05
already identifies as the trunk. Keep the quarantine behaviour (a bad provider should not
take down the app). Delete the entry-point group and its config flag.

---

**O18. The `match_candidate` table storing every losing candidate's raw payload, with 180-day
retention and a `VACUUM` job.** `04-identity.md:762-770`, `04-identity.md:803-805`.

04's reasoning for logging losers is excellent and I would not touch `match_decision`. But a
second table with its own retention policy and vacuum job, for a user who claims maybe five
tracks a week, is scaffolding.

**Smaller version:** keep the top three candidates as a JSON array in
`match_decision.candidates_json`. Same diagnostic value at the scale this app runs at, one
table, no pruning job.

---

**O19. Seven plate states and the two `UP TO` variants.** `06-design.md:240-249`.

Per C24, the data for `AVAILABLE`, `AWAITING` and both `UP TO` forms does not exist in v1 for
either shipping store. Building states that render fabricated or absent data is worse than
not building them.

**Smaller version:** five states in v1 (`NO STORE`, `WANTED`, `WORKING`, `REFUSED`, `OWNED`),
tier ladder on `OWNED` only. Add the promised-tier states when a store actually returns
formats at search time. Note that the *owned* plate, which is the design's real payoff and the
thing no other music software does, is unaffected.

---

**Not cut, and worth saying so.** The vendored Archivo (`06-design.md:135-138`, about
40KB) is the only place this design spends bytes, and it buys the entire difference between
"record shop" and "Tailwind admin panel". The refusal
screen (`06-design.md:564-596`) is the most expensive single UI state here and it is the one
that encodes the incident into the product permanently. `job_key` idempotency
(`05-runtime.md:722-736`), `advance()` (`05-runtime.md:459-476`) and the inclusive cursor
boundary (`01-sources.md:38-44`) each fix a live data-loss bug for almost nothing. 04's type-
level ban on substring containment (`04-identity.md:466-482`) is the best twelve lines in all
six documents.

---

## 7. Decisions needed before M1 starts

Five, each one blocking, each answerable in minutes.

1. **Encryption at rest: in or out for v1?** (O1). Determines whether the dependency count is
   3 or 4 and whether `credentials.secret_enc` is a BLOB or TEXT.
2. **Is Navidrome on the Mini a container or a direct install?** (C26). One `docker ps`.
   Determines whether "the rescan has never fired" is a real hazard and what the cutover
   runbook says.
3. **Confirm the 164-row export is not the whole story.** `_tracks-sample.json` lacks `id`,
   `added_at` and the URL columns. A full `.dump` is needed before the migrations in M2 and
   M3 can be tested.
4. **Test Bandcamp with plain `urllib` now, not in M7** (`02-stores.md:1042-1049`). If TLS
   fingerprinting is real, it changes the dependency story and the Docker build for both
   architectures, and Bandcamp is called essential.
5. **Accept the interval deviation from locked decision 6** (`05-runtime.md:1565-1573`).
   30s on Last.fm and ListenBrainz rather than the brief's 5-10s. 05 asked explicitly and
   nobody has answered.
