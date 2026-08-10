# 05 - App runtime, jobs and packaging

Agent 5. Written 2026-08-09.

This is the skeleton every other agent plugs into. Sections 3 (registration) and 4
(lifecycle) are the contract Agent 1 and Agent 2 implement against; they are written
to be sufficient on their own.

---

## Decisions

1. **Package `libwish`, installed, not path-hacked.** A real `pyproject.toml`
   package replaces `sys.path.insert(0, "~/music-stack")`. The app is
   started as `python -m libwish serve`, and the WSGI callable comes from
   `libwish.web:create_app()`.
2. **Provider discovery: explicit registry first, entry points second.** Built-ins
   are imported by an explicit list in `libwish/providers/sources/__init__.py` and
   `.../stores/__init__.py`. Third-party providers are discovered via
   `importlib.metadata.entry_points(group="libwish.sources" | "libwish.stores")`.
   No directory scanning. Reasons in section 3.
3. **Providers declare, they do not spawn.** A provider never starts a thread, never
   sleeps, never decides its own cadence, and never writes to the event bus. It
   returns values or raises from the shared error taxonomy. The scheduler owns
   cadence; the job runner owns concurrency; the runtime owns events. This is what
   makes a new provider a small file rather than a new subsystem.
4. **One scheduler thread, a bounded job worker pool, jobs persisted in SQLite.**
   No Celery, no Redis, no second process. A job that was running when the process
   died is visible as `interrupted` on next boot rather than lost.
5. **Adaptive polling is driven by SSE subscriber count plus a grace period plus a
   hot budget.** Naive "poll fast while a browser is connected" degrades into
   "poll fast forever" the moment somebody leaves a tab open. Section 5.
6. **Hot tier is per provider, and third parties do not get 5 seconds.** The user's
   own Subsonic server gets hot=5s. Last.fm and ListenBrainz get hot=30s. See
   Open questions, item 1, since this is a partial deviation from the brief.
7. **Migrations: numbered `.sql` files against `PRAGMA user_version`, with an
   automatic pre-migration backup.** No Alembic. Refuse to start on a downgrade.
   The live 164-row DB adopts as baseline without a rewrite.
8. **Bind `127.0.0.1` by default in code; the Docker image sets `0.0.0.0`
   explicitly**, and the app refuses to start unauthenticated on a non-loopback
   bind. This app holds purchase-capable session cookies and can spend money by
   proxy; open-on-the-LAN is not an acceptable default.
9. **Library rescan goes over the media server's HTTP API, never `docker exec`.**
   The current `docker exec navidrome navidrome scan` requires the docker socket,
   which is root-equivalent, in the one container that holds store session cookies.
   Subsonic `startScan` for Navidrome/Airsonic/Gonic, plus Jellyfin, Emby, Plex and
   `command` adapters.
10. **Downloads land in a hidden staging dir on the music volume and are moved into
    place with `os.replace`.** A half-written FLAC visible to a scanner gets indexed
    as a corrupt track and cached as such.
11. **Multi-stage Dockerfile, Tailwind standalone CLI pinned to `$BUILDPLATFORM`,
    `python:3.12-slim` runtime with waitress**, non-root with `PUID`/`PGID`,
    `/config` + `/music`. `/config` is chowned on boot; `/music` never is.

---

## 1. What is wrong with the current runtime

Concrete, from the live code, because each item motivates a decision below.

| Problem | Where | Consequence |
|---|---|---|
| `sys.path.insert` + absolute paths | `app.py:6`, `queue_lib.py:8`, `queue_ingest.py:6` | Cannot be installed, tested, or containerised. 8 hardcoded paths. |
| Claim runs inside the request handler | `app.py` `api_fetch` -> `Q.fetch_purchase` | The Qobuz download has a 240s timeout. Any proxy or browser gives up first and shows "network error" while the download is in fact succeeding. |
| High-water mark written before the tracks are inserted | `queue_ingest.py` `lastfm_new`/`lb_new` write `*_hw.txt`, then the caller loops `add_track` | A crash between the two silently loses those loves **permanently**. There is no way to notice and no way to recover short of `--backfill`. |
| High-water marks are flat files | `queue/lastfm_hw.txt` | Not in the DB backup, not transactional with the insert, not portable across a volume layout change. |
| `Q.init()` only under `if __name__ == "__main__"` | `app.py` | Under any WSGI server the module is imported, not executed. Schema init would never run. |
| Werkzeug dev server, `host="0.0.0.0"` | `app.py` last line | Single-threaded-ish, not for production, and reachable by anything on the network with no auth in front of `/api/fetch`. |
| Rescan by `docker exec` | `queue_lib.fetch_purchase` | Needs the docker socket and co-location. Also: the brief says Navidrome is installed directly, but on the Mini today it is the `deluan/navidrome:latest` container. Neither assumption should be baked in. |
| Front-end polls `/api/queue` every 60s and re-renders the whole list | `app.py` `PAGE` | The reason SSE is a locked decision. |
| `COOKIE_BROKER_TOKEN` stored inline in the launchd plist | `com.example.love-queue.plist` | Any process that can read the plist reads the token. See the note at the end of this document. |

---

## 2. Package layout

```
library-wishlist/
  pyproject.toml
  README.md
  libwish/
    __init__.py            # __version__
    __main__.py            # CLI: serve | migrate | doctor | probe | poll-once | job
    config.py              # env -> frozen Settings dataclass; single source of truth
    log.py                 # structured logging, context binding, secret redaction
    errors.py              # the shared ProviderError taxonomy (section 4.4)
    context.py             # ProviderContext, JobContext
    registry.py            # register_source/register_store, discovery, LoadReport
    events.py              # EventBus, Event, subscriber accounting, replay ring
    scheduler.py           # tick loop, tiers, backoff, suspension
    library.py             # rescan adapters
    paths.py               # staging dir, atomic publish, mode/umask handling
    db/
      __init__.py          # per-thread connections, pragmas, backup
      migrate.py
      migrations/
        0001_baseline.sql
        0002_jobs.sql
        0003_provider_state.sql
        0004_identity.sql      # Agent 4
        0005_credentials.sql   # Agent 3
    jobs/
      __init__.py
      queue.py             # JobQueue: claim, heartbeat, retry, recovery
      worker.py            # worker pool, per-store semaphores
      handlers.py          # JOB_HANDLERS registry
    providers/
      __init__.py
      base.py              # Provider protocol, ProviderInfo, ConfigSpec, ProbeResult
      sources/
        __init__.py        # BUILTIN = [...] explicit imports
        base.py            # SourceProvider  (Agent 1 owns the method bodies)
        lastfm.py  listenbrainz.py  subsonic.py  deezer.py
      stores/
        __init__.py
        base.py            # StoreProvider   (Agent 2 owns the method bodies)
        qobuz.py  bandcamp.py  sevendigital.py
    auth/                  # Agent 3
      __init__.py  credentials.py  oauth.py  cookies.py
    identity/              # Agent 4
      __init__.py  normalize.py  match.py
    web/
      __init__.py          # create_app() -> Flask
      security.py          # auth mode enforcement, proxy trust
      routes_ui.py  routes_api.py  routes_sse.py  routes_admin.py
      templates/           # Jinja + HTMX
      static/css/app.css   # build artefact, gitignored
  tailwind/
    input.css
    tailwind.config.js
  docker/
    Dockerfile
    entrypoint.sh
    docker-compose.example.yml
  tests/
```

`pyproject.toml` essentials:

```toml
[project]
name = "library-wishlist"
requires-python = ">=3.11"
dependencies = ["Flask>=3.0", "waitress>=3.0", "Jinja2>=3.1"]

[project.scripts]
libwish = "libwish.__main__:main"

[project.entry-points."libwish.sources"]
# built-ins are NOT listed here; this group is for third-party packages only

[project.entry-points."libwish.stores"]
```

`requires-python = ">=3.11"` for `ExceptionGroup`, `tomllib`, `typing.Self` and
`datetime.UTC`. Note the Mini's venv is on **Python 3.9.6**, so the container is the
supported runtime and a bare-metal install needs a newer interpreter.

Runtime dependency count stays at three, all pure Python, which is what keeps
multi-arch builds trivial (no compiler, no manylinux wheel matrix).

---

## 3. Provider registration and discovery

### 3.1 Why explicit registry over entry points over scanning

**Directory scanning is rejected.** It imports whatever happens to be in a folder,
in filesystem order, so a stray `qobuz.py.bak` becomes a live provider. Import errors
surface as "provider missing" rather than as an error. It also breaks under zipapp
and under any packaging that does not materialise `__file__`.

**Entry points alone are rejected as the trunk.** They depend on installed
distribution metadata, which is fragile in a `pip install -e` checkout and in a
container built by copying source. They also make the built-in set invisible in the
source tree: you cannot answer "what sources exist" by reading a file.

**Chosen: explicit list + entry points for extension.**

```python
# libwish/providers/sources/__init__.py
from . import lastfm, listenbrainz, subsonic, deezer   # noqa: F401  (import registers)

BUILTIN = ("lastfm", "listenbrainz", "subsonic", "deezer")
```

Adding a built-in source is a one-line edit here plus the new module. Deterministic,
greppable, and an import error is an import error.

### 3.2 The registry

```python
# libwish/registry.py

_SOURCES: dict[str, type[SourceProvider]] = {}
_STORES:  dict[str, type[StoreProvider]]  = {}
_QUARANTINED: dict[str, str] = {}          # provider id -> why it failed to load

def register_source(cls: type[SourceProvider]) -> type[SourceProvider]:
    """Class decorator. Records the class. Performs no I/O."""

def register_store(cls: type[StoreProvider]) -> type[StoreProvider]: ...

def load_providers(settings: Settings) -> LoadReport:
    """Import built-ins, then entry-point groups. Never raises for one bad provider:
    it is recorded in LoadReport.quarantined and reported at /api/status."""

def instantiate(settings, ctx_factory) -> tuple[dict[str, SourceProvider],
                                                dict[str, StoreProvider]]
```

Usage in a provider module:

```python
from libwish.registry import register_source
from libwish.providers.sources.base import SourceProvider
from libwish.providers.base import ProviderInfo, ConfigSpec, PollPolicy

@register_source
class LastfmSource(SourceProvider):
    info = ProviderInfo(
        id="lastfm",
        kind="source",
        display_name="Last.fm",
        homepage="https://www.last.fm",
        auth=AuthSpec(kind="lastfm_web_auth"),        # Agent 3's vocabulary
        config=(
            ConfigSpec("api_key",  required=True,  secret=True),
            ConfigSpec("username", required=True,  secret=False),
        ),
        poll=PollPolicy(hot=30, cold=300, floor=10),
        capabilities=frozenset({"backfill", "cursor"}),
    )
```

### 3.3 Provider identity rules (binding on Agents 1 and 2)

- `info.id` matches `^[a-z][a-z0-9_]{1,31}$` and is **frozen forever** once shipped.
  It is written into `tracks.source_provider`, `purchases.store_provider`,
  `provider_state.provider_id` and the env namespace. Renaming it orphans user data.
- `id` must be unique across *both* kinds. A source and a store may not both be
  `deezer`. If a provider is genuinely both (Bandcamp: it is a store, and your
  collection is arguably a source), it registers twice with distinct ids
  (`bandcamp` store, `bandcamp_collection` source) and shares a module.
- The registry rejects a duplicate id at load time and quarantines the second one.
- `display_name` is what the UI shows and may change freely.

### 3.4 Config namespacing

Every provider gets a private env namespace for free:

```
LW_SOURCE_<ID_UPPER>_<KEY_UPPER>      LW_SOURCE_LASTFM_API_KEY
LW_STORE_<ID_UPPER>_<KEY_UPPER>       LW_STORE_QOBUZ_DEST_SUBDIR
LW_SOURCE_<ID_UPPER>_ENABLED          true | false
LW_SOURCE_<ID_UPPER>_POLL_HOT_SECONDS
LW_SOURCE_<ID_UPPER>_POLL_COLD_SECONDS
```

Providers read via `ctx.conf("api_key")`, never `os.environ` directly, so the
resolution order (env -> `/config/config.toml` -> `ConfigSpec.default`) is uniform
and `/api/providers` can render a settings page generically. `ConfigSpec(secret=True)`
values are registered with the log redactor at load and are never returned by any
endpoint; `/api/providers` reports `"set"` or `"unset"` only.

**A new provider must not require any UI change to be configurable.** That is the
test for whether `ConfigSpec` is expressive enough.

---

## 4. Provider lifecycle contract

### 4.1 Phases

| # | Phase | Thread | Network allowed | May raise |
|---|---|---|---|---|
| 1 | import + `@register_*` | main, at startup | no | quarantines this provider only |
| 2 | `__init__(ctx)` | main | no | quarantines this provider only |
| 3 | `check_config()` | main | **no** | no; returns a status |
| 4 | `start()` | main | no | quarantines |
| 5 | `probe()` | worker | yes | yes, taxonomy only |
| 6 | work (`poll`, store methods) | worker | yes | yes, taxonomy only |
| 7 | `stop()` | main, on shutdown | no | swallowed |

Phase 3 is deliberately network-free so that "is this configured" is instant and
answerable offline. Phase 5 is the only thing that knows whether a credential works,
and it is always a live call (the cookie broker's rule: a status endpoint that echoes
what it was handed will happily claim a dead session is fine).

### 4.2 The base protocol

```python
# libwish/providers/base.py

@dataclass(frozen=True)
class ProviderInfo:
    id: str
    kind: Literal["source", "store"]
    display_name: str
    homepage: str
    auth: "AuthSpec"                       # defined by Agent 3
    config: tuple[ConfigSpec, ...] = ()
    capabilities: frozenset[str] = frozenset()
    poll: PollPolicy | None = None         # sources only

@dataclass(frozen=True)
class ConfigSpec:
    key: str
    required: bool = False
    secret: bool = False
    default: str | None = None
    help: str = ""
    choices: tuple[str, ...] | None = None

@dataclass(frozen=True)
class ConfigStatus:
    ok: bool
    missing: tuple[str, ...] = ()
    detail: str = ""

@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str                            # short, user-facing
    account: str | None = None             # e.g. the logged-in username, for the UI
    checked_at: float = field(default_factory=time.time)

class Provider(Protocol):
    info: ClassVar[ProviderInfo]
    def __init__(self, ctx: ProviderContext) -> None: ...
    def check_config(self) -> ConfigStatus: ...
    def probe(self) -> ProbeResult: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

Sensible defaults live in an ABC (`check_config` derived from `ConfigSpec` alone,
`start`/`stop` no-ops), so a minimal provider only writes `probe` plus its own kind's
methods.

### 4.3 `ProviderContext`

The only handle a provider gets to the rest of the app. Nothing else is importable
from provider code by convention, and that convention is what keeps providers
swappable.

```python
@dataclass(frozen=True)
class ProviderContext:
    provider_id: str
    settings: Settings
    log: Logger                                   # pre-bound with provider=<id>
    conf: Callable[[str], str | None]             # namespaced config lookup
    creds: CredentialHandle                       # Agent 3
    http: HttpClient                              # section 4.5
    state: ProviderState                          # section 4.6
    db: Callable[[], sqlite3.Connection]          # per-thread connection
    identity: IdentityService                     # Agent 4: normalize + match
    paths: PathService                            # staging + atomic publish
```

Deliberately absent: the event bus and the job queue. Providers do not emit events
and do not enqueue jobs. If a provider needs long work split up, it yields
incrementally (sources) or accepts a `JobContext` (stores) and the runtime does the
splitting. This keeps the event vocabulary closed.

### 4.4 Error taxonomy (`libwish/errors.py`)

This is the single most load-bearing part of the contract, because the scheduler,
the job runner and the UI all branch on it. Providers raise **only** these.

```python
class ProviderError(Exception):
    retryable: bool = False
    code: str = "provider_error"
    user_action: str | None = None      # rendered verbatim in the UI when set

class ConfigError(ProviderError):       # missing/invalid config
    retryable = False; code = "config"
    # user_action e.g. "Set LW_SOURCE_LASTFM_API_KEY."

class AuthExpired(ProviderError):       # credential died; the user must act
    retryable = False; code = "auth_expired"
    # user_action e.g. "Open Qobuz in your browser to reseed the session."

class RateLimited(ProviderError):       # back off, do not alarm the user
    retryable = True; code = "rate_limited"
    retry_after: float                  # seconds; honoured exactly by the scheduler

class TransientError(ProviderError):    # network blip, 5xx, timeout
    retryable = True; code = "transient"

class NotFound(ProviderError):          # asked for something the provider does not have
    retryable = False; code = "not_found"

class RefusedAmbiguous(ProviderError):  # Agent 4's confidence threshold said no
    retryable = False; code = "refused"
    # carries the candidate list so the UI can offer a manual choice
```

Behaviour, by class:

- `TransientError` / `RateLimited`: exponential backoff, no user-visible alarm until
  `consecutive_failures >= LW_FAILURE_ALERT_THRESHOLD` (default 3).
- `AuthExpired`: the provider is **suspended** immediately. It is not polled at all
  until Agent 3's layer emits `credential.updated` for that provider id. A
  `provider.status` SSE event fires once, on the transition, not every tick. This is
  lifted directly from `SessionKeeper.touch()`, which alerts only on the live->dead
  edge for exactly this reason.
- `ConfigError`: suspended, shown in settings, never retried.
- `RefusedAmbiguous`: **never retried, and never counted as a failure.** A refusal is
  the system working. Retrying a refusal is how you eventually download the wrong
  file. The job ends in state `refused`, distinct from `failed`.
- Any non-`ProviderError` escaping a provider is caught, logged with a full
  traceback, and treated as `TransientError` with `code="unexpected"`, *and* it
  increments a `provider_unexpected_errors` counter surfaced at `/api/status`. An
  unexpected exception is a bug, so it must be visible rather than absorbed. The
  existing code's `except Exception: pass` in `bandcamp_search` is the anti-pattern:
  it turned a broken resolver into "not on Bandcamp" for every single row.

### 4.5 `HttpClient`

Providers must not call `urllib.request.urlopen` directly. `ctx.http` gives:

- a per-provider User-Agent (`library-wishlist/<version> (+homepage)` by default; a
  provider may override to a browser UA where a WAF requires it, as the cookie broker
  already does and documents),
- a per-host token-bucket rate limiter (`ctx.http.limit(host, rps)`), so the floor in
  `PollPolicy` is not the only defence,
- timeouts that are always set (default connect 10s, read 30s; downloads override),
- automatic mapping of `HTTPError` 429 -> `RateLimited(retry_after=...)`,
  401/403 -> `AuthExpired`, 5xx -> `TransientError`, so providers rarely write
  `except` blocks at all,
- optional routing through Agent 3's `SessionKeeper.open()` when the provider's
  `AuthSpec` is cookie-based, which is what preserves the "absorb rotation on every
  request" property the broker already implements.

### 4.6 `ProviderState`: cursors and high-water marks

Runtime-owned mechanism; the semantics are Agent 1's. Replaces `queue/lastfm_hw.txt`.

```sql
CREATE TABLE provider_state(
  provider_id TEXT NOT NULL,
  key         TEXT NOT NULL,
  value       TEXT,                 -- json
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (provider_id, key)
);
```

```python
class ProviderState(Protocol):
    def get(self, key: str, default=None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
    @contextmanager
    def advance(self, key: str) -> Iterator[Cursor]: ...
```

`advance()` is the important one and it fixes the live data-loss bug. It opens a
single write transaction; the cursor is committed **in the same transaction as the
track inserts**, or not at all:

```python
with ctx.state.advance("loved_hw") as cur:
    for record in provider.poll(cur.value):
        insert_track(record)          # same txn
        cur.value = record.cursor     # staged, not yet committed
# commit happens here, atomically, or everything rolls back
```

Today the high-water file is written *before* the inserts, so a crash in between
silently and permanently drops those loves. With `advance()` a crash re-polls the
same window; duplicates are absorbed by the dedup key, which is the safe direction to
fail.

### 4.7 Worked skeleton: the minimum a provider must supply

Method bodies for `poll` and the store methods belong to Agents 1 and 2. This shows
only the registration and lifecycle surface they attach to.

```python
# libwish/providers/sources/example.py
from libwish.registry import register_source
from libwish.providers.base import ProviderInfo, ConfigSpec, PollPolicy, ProbeResult
from libwish.providers.sources.base import SourceProvider
from libwish.errors import AuthExpired

@register_source
class ExampleSource(SourceProvider):
    info = ProviderInfo(
        id="example", kind="source", display_name="Example",
        homepage="https://example.com",
        auth=AuthSpec(kind="token"),
        config=(ConfigSpec("token", required=True, secret=True),
                ConfigSpec("username", required=True)),
        poll=PollPolicy(hot=30, cold=300, floor=10),
        capabilities=frozenset({"backfill", "cursor"}),
    )

    def __init__(self, ctx):
        self.ctx = ctx                       # no I/O here

    def probe(self) -> ProbeResult:
        r = self.ctx.http.get_json("https://example.com/api/me")   # raises taxonomy
        return ProbeResult(ok=True, detail="ok", account=r["name"])

    def poll(self, cursor):                  # Agent 1 defines the record shape
        ...
```

```python
# libwish/providers/stores/example.py
@register_store
class ExampleStore(StoreProvider):
    info = ProviderInfo(
        id="example_store", kind="store", display_name="Example Store",
        homepage="https://store.example.com",
        auth=AuthSpec(kind="cookie", site="store.example.com"),
        config=(ConfigSpec("region", default="us"),),
        capabilities=frozenset({"buy_link", "enumerate_purchases", "download"}),
    )
    def probe(self) -> ProbeResult: ...
    def buy_url(self, track) -> str | None: ...          # Agent 2
    def claim(self, jctx: JobContext, track) -> ClaimResult: ...   # Agent 2
```

`capabilities` is how the runtime and the UI adapt without special-casing. The two
that the runtime itself branches on:

- `enumerate_purchases` absent -> the Claim flow cannot verify ownership by
  enumeration. The runtime then routes to the store's manual path (Agent 2 designs
  it) and the UI labels the button differently. Bandcamp and 7digital differ here.
- `backfill` absent -> first connect is forward-only; no "import everything" offer.

Registering the same capability string is the entire coupling between a provider and
the shell. New capability strings require a runtime change and should be rare;
`libwish/providers/base.py` holds the canonical list and the registry warns on an
unknown one rather than failing.

---

## 5. Scheduler and adaptive polling

### 5.1 Shape

One `Scheduler` thread, a `heapq` of `ScheduledTask`, a `threading.Condition` for
sleeping and for early wake-up. Not a thread per provider: a handful of providers do
not justify N threads, and shutdown with N threads is where bugs live.

```python
@dataclass(order=True)
class ScheduledTask:
    due_at: float
    seq: int                                  # tiebreak, keeps the heap total-ordered
    task_id: str = field(compare=False)       # "source:lastfm:poll"
    kind: str = field(compare=False)          # poll | probe | rescan | maintenance
    policy: PollPolicy | None = field(compare=False, default=None)
```

The scheduler thread **only dispatches**: it enqueues a job and immediately
reschedules. It never performs network work, so one slow source cannot delay every
other source's tick. Actual work runs on the job worker pool (section 6).

### 5.2 Tiers

```python
@dataclass(frozen=True)
class PollPolicy:
    hot: int          # a browser is watching
    cold: int         # nobody watching
    floor: int        # hard minimum between two calls, never violated
    warm: int | None = None   # defaults to cold // 2
    jitter: float = 0.15
```

Defaults per provider, and the reasoning:

| Provider | hot | warm | cold | floor | Why |
|---|---|---|---|---|---|
| `subsonic` | 5 | 30 | 60 | 2 | The user's own server on the LAN. Free to hammer. This is where the brief's 5-10s belongs. |
| `listenbrainz` | 30 | 150 | 300 | 10 | Third party, non-commercial, no documented rate limit but courtesy applies. |
| `lastfm` | 30 | 150 | 300 | 10 | Third party. Their terms ask for restraint and revoke keys. |
| `deezer` | 60 | 300 | 600 | 15 | Third party, unofficial API surface, aggressive rate limiting observed. |

All four overridable via `LW_SOURCE_<ID>_POLL_HOT_SECONDS` / `_COLD_SECONDS`, floored
at `floor`. A user who wants 5s on Last.fm can have it and owns the consequence.

### 5.3 Learning whether a browser is connected

The `EventBus` is the authority, because it is the thing that actually holds the
connections:

```python
class EventBus:
    def subscribe(self) -> Subscription      # increments watcher count
    def subscriber_count(self) -> int
    def last_subscriber_gone_at(self) -> float | None
```

Tier selection, evaluated on every scheduler tick:

```python
def tier(now: float) -> str:
    if bus.subscriber_count() > 0:
        if now - hot_since > LW_HOT_MAX_MINUTES * 60 and now - last_interaction > LW_HOT_MAX_MINUTES * 60:
            return "warm"                    # forgotten tab
        return "hot"
    if now - (bus.last_subscriber_gone_at() or 0) < LW_HOT_GRACE_SECONDS:
        return "hot"                         # ride out an SSE reconnect
    return "cold"
```

Three things that a naive implementation gets wrong:

1. **Grace period (`LW_HOT_GRACE_SECONDS`, default 90).** `EventSource` reconnects
   constantly: proxy idle timeouts, mobile tab suspension, laptop sleep. Without
   grace, the tier flaps hot/cold every reconnect and the poll interval is
   effectively random.
2. **Hot budget (`LW_HOT_MAX_MINUTES`, default 30).** A tab left open in another room
   would otherwise poll Last.fm every 30s forever. After the budget, presence alone
   drops to `warm`; any *real* interaction (any authenticated non-SSE request) resets
   `last_interaction` and restores `hot`. Presence is not attention.
3. **Wake on tier change.** When the first subscriber connects, the scheduler thread
   is notified so it recomputes due times immediately. Without this, connecting a
   browser does nothing for up to `cold` seconds, and the whole feature looks broken.
   Reschedule is `new_due = max(last_run_at + new_interval, now + floor)`, so going
   hot pulls the next poll forward without ever breaching the floor.

The tier is broadcast as a `poll.tier` event so the UI can honestly say "checking
every 30s" instead of implying real-time.

### 5.4 Failure handling

```
interval = policy[tier] * (1 + uniform(-jitter, +jitter))
on TransientError/unexpected: delay = min(interval * 2**consecutive_failures, LW_POLL_MAX_BACKOFF)
on RateLimited:               delay = max(retry_after, floor)          # exact, no backoff multiply
on AuthExpired/ConfigError:   suspended; no scheduling at all
on success:                   consecutive_failures = 0
```

`LW_POLL_MAX_BACKOFF` default 3600. A source that has been failing for an hour is not
going to be fixed by asking again in 30 seconds, and the backoff is what keeps a dead
third-party API from filling the log at 2 requests a minute for a week.

### 5.5 Observability, and the launchd lesson

Section 6 of the brief: `launchctl bootout` was not persistent; a job believed
disabled ran 1569 times. The design rule that follows is **the UI must report
observed behaviour, not configured intent.**

```sql
CREATE TABLE provider_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider_id TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  outcome TEXT,                    -- ok | transient | rate_limited | auth_expired | config | unexpected
  items INTEGER DEFAULT 0,         -- records yielded
  added INTEGER DEFAULT 0,         -- rows actually inserted
  detail TEXT
);
CREATE INDEX provider_runs_pid_started ON provider_runs(provider_id, started_at DESC);
```

`/api/status` reports per provider: `enabled` (intent), `last_run_at` and
`runs_last_hour` (fact). If those disagree, the disagreement is on screen. Rows are
pruned past `LW_RUN_RETENTION_DAYS` (default 30).

---

## 6. Job model

### 6.1 Why persisted jobs

The claim path is the motivating case. `qobuz_fetch._download_signed` uses a 240s
read timeout on a request made from inside a Flask handler. A browser or reverse
proxy will time out first, the user sees "network error", and the download completes
anyway. Backfill on first connect can be thousands of records. Both must be
detached from the request, must survive a page reload, and must be inspectable after
a crash.

Not Celery/RQ: a broker process contradicts the single-container packaging and
"SQLite, no database server". Not bare threads: a thread that dies with the process
leaves a spinner in the UI forever with no record of what happened.

### 6.2 Schema

```sql
CREATE TABLE jobs(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key       TEXT UNIQUE,          -- idempotency, see 6.3
  type          TEXT NOT NULL,        -- claim|poll|resolve|backfill|rescan|probe|maintenance
  payload       TEXT NOT NULL,        -- json
  state         TEXT NOT NULL,        -- queued|running|succeeded|failed|refused|cancelled|interrupted
  priority      INTEGER NOT NULL DEFAULT 100,   -- lower first; user-initiated = 10
  progress      REAL,                 -- 0..1, NULL = indeterminate
  progress_text TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 1,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  error_code    TEXT,
  error_text    TEXT,
  user_action   TEXT,
  result        TEXT,                 -- json
  created_at    INTEGER NOT NULL,
  started_at    INTEGER,
  finished_at   INTEGER,
  heartbeat_at  INTEGER,
  owner         TEXT                  -- process instance id
);
CREATE INDEX jobs_ready  ON jobs(state, priority, id);
CREATE INDEX jobs_recent ON jobs(created_at DESC);
```

`max_attempts` defaults to **1**. Automatic retry is opt-in per job type, because
retrying a partially completed download by default is how you get duplicate files.
`poll` and `probe` set it higher; `claim` does not.

### 6.3 Idempotency

`job_key UNIQUE` is the whole deduplication mechanism.

- claim: `claim:track=<id>:store=<store_id>`
- poll: `poll:<provider_id>`
- rescan: `rescan` (a single global key; see debounce below)
- backfill: `backfill:<provider_id>`

Enqueue is `INSERT ... ON CONFLICT(job_key) DO NOTHING RETURNING id`; on conflict the
existing queued/running job id is returned instead. Double-clicking Claim therefore
returns the in-flight job rather than starting a second download to the same
destination path, which would corrupt the file. The key is cleared (set to NULL) when
the job reaches a terminal state, so the same claim can be retried later.

The single `rescan` key gives trailing-edge debounce for free: claiming ten tracks
in a minute produces one scan, not ten.

### 6.4 Workers

`LW_JOB_WORKERS`, default **2**. Not 8: downloads are I/O bound but every store
rate-limits, and Agent 3's `SessionKeeper._lock` serialises requests per site anyway,
so extra workers just queue on a mutex.

On top of that, a **per-store semaphore of 1** around any job that uses a store
session. From `cookie_broker.py`:

> A rolling session cannot tolerate two in-flight consumers: both send the
> pre-rotation value, the site rotates for the first, and the second 401s and takes
> the session down with it.

Two concurrent Qobuz claims would therefore kill the session and look like
"cookie expired". The semaphore is not an optimisation, it is a correctness
requirement.

Claiming a job:

```sql
BEGIN IMMEDIATE;
SELECT id FROM jobs WHERE state='queued' ORDER BY priority, id LIMIT 1;
UPDATE jobs SET state='running', owner=?, started_at=?, heartbeat_at=?, attempts=attempts+1
 WHERE id=? AND state='queued';
COMMIT;
```

`BEGIN IMMEDIATE` plus the `state='queued'` guard in the UPDATE makes the claim
atomic across workers without a separate lock.

### 6.5 Crash recovery

At startup, before the pool starts:

```sql
UPDATE jobs SET state='interrupted', finished_at=?, error_code='interrupted',
                error_text='process restarted while this job was running'
 WHERE state='running' AND (owner IS NULL OR owner <> :instance_id);
```

`interrupted` is a distinct state, not `failed`, because the user should be told the
truth: we do not know whether the purchase downloaded. The staging directory is then
swept: any `.libwish-incoming/<job_id>/` with no live job is deleted. A partial FLAC
is never promoted, and it is never left where a scanner can see it.

### 6.6 `JobContext`

```python
class JobContext(Protocol):
    job_id: int
    log: Logger                                   # bound with job_id and type
    def progress(self, frac: float | None, text: str) -> None
    def check_cancelled(self) -> None             # raises JobCancelled
    def heartbeat(self) -> None
    @property
    def staging_dir(self) -> Path                 # created lazily, per job
    def publish(self, src: Path, rel_dest: str) -> Path   # atomic move into /music
```

`progress()` persists and emits `job.progress`, coalesced to at most one write and
one event per second (a download calling it per 1MB chunk would otherwise generate
hundreds of writes per file).

Cancellation is cooperative only. `POST /api/jobs/<id>/cancel` sets
`cancel_requested=1`; the worker notices at the next `check_cancelled()`. Download
loops call it once per chunk. No thread is ever killed.

Watchdog: a job whose `heartbeat_at` is older than `LW_JOB_STALL_SECONDS` (default
900) is reported as stalled at `/api/status`. It is **not** auto-killed, because
there is no safe way to interrupt an in-flight write; it is surfaced so the user can
cancel it.

### 6.7 Handler registration

```python
# libwish/jobs/handlers.py
JOB_HANDLERS: dict[str, JobHandler] = {}

def job_handler(type_: str, *, max_attempts: int = 1, priority: int = 100):
    def deco(fn: Callable[[JobContext, dict], dict]) -> ...
    return deco

@job_handler("claim", max_attempts=1, priority=10)
def handle_claim(ctx: JobContext, payload: dict) -> dict:
    store = registry.get_store(payload["store"])
    track = load_track(payload["track_id"])
    result = store.claim(ctx, track)              # Agent 2 owns this
    ...
```

Return value is JSON-serialisable and lands in `jobs.result`. Raising a
`ProviderError` maps to `failed` (or `refused` for `RefusedAmbiguous`) with
`error_code`, `error_text` and `user_action` recorded, so the UI renders a real
instruction rather than a stack trace.

### 6.8 The validate-before-remove guarantee, restated for jobs

`fetch_purchase` today only returns `ok=True` on a confirmed download, and only then
does the caller `mark_purchased`. That guarantee moves into the job handler and is
strengthened:

1. Match decision is written to the audit table **before** any bytes are fetched
   (Agent 4 owns the columns). If the process dies mid-download, why we chose that
   file is still on disk.
2. File is downloaded into `staging_dir`.
3. File is verified (magic bytes, minimum size, and whatever Agent 2 specifies).
4. `ctx.publish()` atomically renames it into `/music`.
5. Only after a successful rename does the track leave the queue, in the same
   transaction that records the purchase.
6. Rescan is enqueued last.

Any failure at any step leaves the track queued and the staging directory swept. The
track never leaves the queue on an unconfirmed success.

---

## 7. SSE

### 7.1 Endpoint

`GET /events` (authenticated), `Content-Type: text/event-stream`, plus
`Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no` (nginx
buffers SSE into uselessness otherwise).

Each connection gets a `queue.SimpleQueue`; the bus fans out non-blocking. A
subscriber whose queue exceeds `LW_SSE_QUEUE_MAX` (default 500) is dropped rather
than allowed to apply backpressure to the publisher, and gets a final `overflow`
event so the client does a full reload.

Keepalive comment `: ping` every `LW_SSE_PING_SECONDS` (default 20) to defeat proxy
idle timeouts and to detect a dead peer (the write raises).

`Last-Event-ID` is honoured against a bounded in-memory ring
(`LW_SSE_REPLAY`, default 200). A reconnect after a blip replays what it missed. Once
the ring has rolled past the requested id, the server sends `resync` and the client
does a full `/api/queue` fetch. Without this, a track that arrives during a 3-second
reconnect is invisible until the next full poll.

**Waitress trap, worth stating loudly:** SSE holds a worker thread for the life of
the connection. Waitress defaults to `threads=4`, so four open tabs would deadlock
the entire app. Set `threads=LW_HTTP_THREADS` (default 16) and cap concurrent
subscribers at `LW_SSE_MAX_CLIENTS` (default 8), returning 503 beyond that.
16 = 8 SSE + 8 for ordinary requests, with headroom.

### 7.2 Event shapes

Every event has `id:` (monotonic int), `event:` (name below) and `data:` (always a
JSON **object**, never a bare string, so client parsing is uniform). Every payload
carries `"v": 1`.

```
track.added       {v, id, artist, title, source, added_at, cover_url, resolved}
track.updated     {v, id, fields: {...changed keys only...}}
track.removed     {v, id, reason: "purchased"|"ignored"|"merged"}
job.started       {v, job_id, type, track_id?, store?}
job.progress      {v, job_id, progress: 0..1|null, text}
job.finished      {v, job_id, state, result?, error_code?, error_text?, user_action?}
provider.status   {v, kind, id, state: "ok"|"auth_expired"|"config"|"error"|"disabled"|"suspended",
                   detail, last_run_at, next_due_at, consecutive_failures}
credential.updated {v, provider_id}                      # Agent 3 emits, scheduler consumes
scan.requested    {v, target, ok, detail}
poll.tier         {v, tier: "hot"|"warm"|"cold", watchers, next_due_at}
heartbeat         {v, ts, queued, running, watchers}     # every 30s
resync            {v, reason}
shutdown          {v}                                    # sent on SIGTERM
```

`heartbeat` exists so the UI can distinguish "nothing is happening" from "the
connection is dead", which `EventSource` alone does not tell you reliably.

`shutdown` on SIGTERM makes browsers reconnect promptly on restart instead of sitting
on a half-open socket until a TCP timeout.

### 7.3 Who may publish

Only the runtime: scheduler, job runner, web handlers, and Agent 3's credential
layer (`credential.updated` only). Providers publish nothing directly; `ctx.progress`
is their only path to the bus and it emits exactly one event name. Adding an event
name is a runtime change, reviewed against the client. This is what stops the
vocabulary from drifting per provider.

---

## 8. Database and migrations

### 8.1 Connections and pragmas

```python
def connect() -> sqlite3.Connection:      # cached in threading.local()
    c = sqlite3.connect(settings.db_path, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA synchronous=NORMAL")
    return c
```

One connection per thread; sqlite3 objects are not safely shared across threads.
WAL matters here specifically: the SSE and status read path must not block behind a
download's writes.

### 8.2 Migration runner

Numbered SQL files against `PRAGMA user_version`. No Alembic: a migration graph and
a SQLAlchemy model layer are disproportionate for one single-file DB, and
autogenerate would need models this app does not otherwise have.

```
libwish/db/migrations/0001_baseline.sql
                      0002_jobs.sql
                      ...
```

Runner rules:

1. `current = PRAGMA user_version`.
2. If `current > max(known)`: **refuse to start** with an explicit message
   ("database was written by version X, this build understands up to Y"). Running an
   older binary against a newer schema silently mangles data; a container that
   crashloops with a clear reason is strictly better.
3. **Adoption**: if `current == 0` and a `tracks` table already exists, stamp
   `user_version=1` without executing `0001_baseline.sql`. This is how the live
   164-row `queue.db` upgrades in place without a rewrite.
4. Before applying anything, take a backup with the sqlite3 online backup API (not
   `cp`, which is unsafe with WAL) to `<db>.pre-<NNNN>.bak`. Keep
   `LW_DB_BACKUP_KEEP` (default 3).
5. Each file runs in `BEGIN IMMEDIATE; ...; PRAGMA user_version=N; COMMIT`. A failure
   rolls back that file and aborts startup.
6. Migrations are forward-only. No `downgrade()`. The backup is the undo.

`0001_baseline.sql` reproduces the current live schema **exactly**, including the
`dedup_key` column, so adoption is a no-op. Agent 4's identity replacement is
`0004_*`, expand-then-contract: add new columns, backfill in a maintenance job,
drop the old column in a later release rather than the same one. That way a user who
rolls the image tag back one version still has a working DB.

### 8.3 Runtime-owned tables

`jobs`, `provider_state`, `provider_runs`, `events_seq`. Everything track-shaped is
Agent 1 and Agent 4's; credentials are Agent 3's; purchase records are Agent 2's.
Runtime provides the migration slot and the transaction discipline, not the columns.

---

## 9. Configuration surface

All `LW_` prefixed. Resolution order: environment, then `/config/config.toml`, then
the declared default. `Settings` is a frozen dataclass built once at startup;
nothing reads `os.environ` after that.

### Core

| Var | Default | Notes |
|---|---|---|
| `LW_CONFIG_DIR` | `/config` | db, cookie jars, logs, instance token |
| `LW_MUSIC_DIR` | `/music` | download destination root |
| `LW_DB_PATH` | `$LW_CONFIG_DIR/library-wishlist.db` | |
| `LW_BIND_HOST` | `127.0.0.1` | image overrides to `0.0.0.0` |
| `LW_BIND_PORT` | `8080` | |
| `LW_BASE_URL` | unset | external URL; Agent 3 needs it for OAuth redirects |
| `LW_AUTH_MODE` | `token` | `none` \| `token` \| `basic` |
| `LW_AUTH_TOKEN` | generated | written to `$LW_CONFIG_DIR/instance_token`, mode 0600, printed once at first boot |
| `LW_BASIC_USER` / `LW_BASIC_PASSWORD` | unset | for `basic` |
| `LW_TRUSTED_PROXIES` | unset | `X-Forwarded-*` is ignored unless set |
| `LW_HTTP_THREADS` | `16` | waitress threads; SSE consumes one each |
| `LW_SHUTDOWN_GRACE` | `20` | seconds |
| `TZ` | `UTC` | |
| `PUID` / `PGID` / `UMASK` | `1000` / `1000` / `022` | entrypoint only, not read by the app |

### Scheduler

| Var | Default |
|---|---|
| `LW_POLL_ENABLED` | `true` |
| `LW_HOT_GRACE_SECONDS` | `90` |
| `LW_HOT_MAX_MINUTES` | `30` |
| `LW_POLL_MAX_BACKOFF` | `3600` |
| `LW_FAILURE_ALERT_THRESHOLD` | `3` |
| `LW_RUN_RETENTION_DAYS` | `30` |
| `LW_SOURCE_<ID>_ENABLED` | `true` if configured |
| `LW_SOURCE_<ID>_POLL_HOT_SECONDS` / `_POLL_COLD_SECONDS` | provider default |

### Jobs

| Var | Default |
|---|---|
| `LW_JOB_WORKERS` | `2` |
| `LW_JOB_RETENTION_DAYS` | `14` |
| `LW_JOB_STALL_SECONDS` | `900` |
| `LW_CLAIM_TIMEOUT` | `1800` |

### SSE

| Var | Default |
|---|---|
| `LW_SSE_PING_SECONDS` | `20` |
| `LW_SSE_REPLAY` | `200` |
| `LW_SSE_MAX_CLIENTS` | `8` |
| `LW_SSE_QUEUE_MAX` | `500` |

### Files

| Var | Default |
|---|---|
| `LW_INCOMING_DIR` | `$LW_MUSIC_DIR/.libwish-incoming` |
| `LW_FILE_MODE` | `0644` |
| `LW_DIR_MODE` | `0755` |
| `LW_MIN_FREE_MB` | `500` (refuse to start a claim below this) |

### Rescan

| Var | Default |
|---|---|
| `LW_RESCAN_KIND` | `none` (`subsonic`\|`jellyfin`\|`emby`\|`plex`\|`command`) |
| `LW_RESCAN_URL` | unset |
| `LW_RESCAN_USER` / `LW_RESCAN_PASSWORD` | unset (Subsonic) |
| `LW_RESCAN_TOKEN` | unset (Jellyfin/Emby/Plex) |
| `LW_RESCAN_SECTION` | unset (Plex section key) |
| `LW_RESCAN_COMMAND` | unset (`command` kind, `{path}` substituted) |
| `LW_RESCAN_DEBOUNCE` | `30` |
| `LW_RESCAN_TIMEOUT` | `120` |

### Logging

| Var | Default |
|---|---|
| `LW_LOG_LEVEL` | `info` |
| `LW_LOG_FORMAT` | `console` (`json`) |
| `LW_LOG_FILE` | unset (`$LW_CONFIG_DIR/logs/app.log` when set to a path) |
| `LW_LOG_HTTP` | `false` (access log) |

### Providers

| Var | Default |
|---|---|
| `LW_SOURCES_ENABLED` | unset = all configured |
| `LW_STORES_ENABLED` | unset = all configured |
| `LW_PLUGIN_ENTRYPOINTS` | `true` |

### Startup validation

`Settings` construction fails loudly rather than defaulting quietly when:

- `LW_AUTH_MODE=none` and `LW_BIND_HOST` is not a loopback address. The app can spend
  money by proxy and holds live store sessions; an open bind is not a default it may
  choose for the user. Same posture as `make_blueprint` refusing to start without
  `COOKIE_BROKER_TOKEN`.
- `LW_MUSIC_DIR` does not exist or is not writable.
- `LW_CONFIG_DIR` is not writable.
- A provider enabled by `LW_SOURCES_ENABLED` does not exist.

---

## 10. Structured logging

Stdlib `logging`, two formatters: `console` (human, for `docker logs`) and `json`.
Default `console`, because the first thing a self-hoster does is read the container
log.

Context is bound with `contextvars` and injected by a `logging.Filter`, so a
provider's `self.ctx.log.info("polled")` carries `provider`, and inside a job it also
carries `job_id` and `track_id`, without the provider passing anything:

```json
{"ts":"2026-08-09T18:41:02Z","level":"info","logger":"libwish.jobs.claim",
 "msg":"published file","job_id":412,"provider":"qobuz","track_id":41,
 "dest":"/music/Audioslave - Shadow on the Sun.flac","bytes":41238811,"quality":"hi-res"}
```

Rules:

1. **A secret is never logged.** Agent 3's credential layer registers every live
   secret value with a `RedactingFilter` at load; the filter replaces exact matches
   in any formatted message with `***`. Regex backstops for `Cookie:`,
   `Authorization: Bearer`, and `?token=`/`&t=` query params.
2. **No endpoint returns a secret value.** `/api/providers` reports `set`/`unset`.
   There is no config dump endpoint.
3. **No bare `except: pass` anywhere in the codebase.** The live
   `bandcamp_search` swallowed a total resolver failure and reported it as "not on
   Bandcamp" for every row. Every handler either re-raises as a taxonomy error or
   logs at `warning` with the exception attached. Enforce with a ruff rule
   (`BLE001`, `S110`) in CI.
4. **Every provider call logs one line at `info` on completion** with outcome and
   counts, so `docker logs | grep provider=lastfm` reconstructs history.
5. Rotation: if `LW_LOG_FILE` is set, `RotatingFileHandler` 10MB x 3 **in addition
   to** stdout. The live app's `app.err` is 35KB of unrotated tracebacks and
   `rip-sync.log` is 11MB; unbounded log files on a `/config` volume are a real
   failure mode.
6. The match-decision audit (Agent 4) is a **database table**, not a log line, so it
   survives log rotation and is queryable. A wrong download has to be diagnosable
   months later.

---

## 11. Health and status endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness. `SELECT 1` only. `{"status":"ok","version":"..."}`. Docker HEALTHCHECK target. |
| `GET /readyz` | none | 200 only when migrations are applied, the scheduler thread is alive, and the worker pool is up. 503 with a reason otherwise. Kept separate from `/healthz` so a crashloop is diagnosable. |
| `GET /api/status` | yes | The dashboard payload. |
| `GET /api/providers` | yes | Static descriptors: id, kind, display_name, auth spec, capabilities, config specs with `required`/`secret`. Lets the settings UI render a provider it has never heard of. |
| `POST /api/providers/<id>/probe` | yes | Forces a live probe. Enqueues a `probe` job, returns the job id. |
| `GET /api/jobs` / `GET /api/jobs/<id>` | yes | Job list and detail. |
| `POST /api/jobs/<id>/cancel` | yes | Cooperative cancel. |
| `GET /metrics` | optional | Prometheus text. `LW_METRICS_ENABLED=false` by default. |

`/api/status` shape:

```json
{
  "version": "0.4.1", "schema": 5, "uptime_s": 91422, "instance": "b3f1...",
  "poll": {"tier": "cold", "watchers": 0, "hot_grace_until": null},
  "tracks": {"queued": 160, "purchased": 2, "ignored": 2},
  "jobs": {"queued": 0, "running": 1, "failed_24h": 0, "refused_24h": 3, "stalled": 0},
  "sources": [{"id": "lastfm", "enabled": true, "config_ok": true,
               "state": "ok", "detail": "ok", "account": "a-listener",
               "last_probe_at": 1786310000, "probe_age_s": 8400, "stale": true,
               "last_run_at": 1786318000, "runs_last_hour": 12,
               "next_due_at": 1786318300, "consecutive_failures": 0}],
  "stores": [{"id": "qobuz", "state": "auth_expired",
              "user_action": "Open Qobuz in your browser to reseed the session."}],
  "quarantined": [],
  "library": {"path": "/music", "writable": true, "free_mb": 412300},
  "rescan": {"kind": "subsonic", "last_at": 1786317000, "last_ok": true}
}
```

Two deliberate choices:

- **Cached probes with an explicit age.** A live probe on every status hit would burn
  the provider's rate limit and make the dashboard slow. So the cached result is
  returned with `probe_age_s` and a `stale` flag past `LW_PROBE_STALE_SECONDS`
  (default 6h). The UI says "unverified for 6h", not "ok". Forcing a live check is an
  explicit action. This preserves the cookie broker's principle (never report a
  stored opinion as fact) while keeping it affordable.
- **`runs_last_hour` next to `enabled`.** Intent and behaviour side by side, so the
  launchd class of bug is visible rather than believed away.

---

## 12. Library rescan

The app is server-agnostic because it writes files. Rescan is the one place it has to
know what the user runs, so it is an adapter with a `none` default.

```python
class LibraryNotifier(Protocol):
    id: str
    def rescan(self, paths: list[str]) -> RescanResult: ...

@dataclass(frozen=True)
class RescanResult:
    ok: bool
    detail: str
    scanned: int | None = None
```

`paths` are the files just published. Most servers ignore them (full-library scan);
Plex is the exception and can scope by path, so the interface carries them.

### Adapters

**`subsonic`** (Navidrome, Airsonic-Advanced, Gonic)

```
GET {url}/rest/startScan?u={user}&t={md5(password+salt)}&s={salt}&v=1.16.1&c=libwish&f=json
GET {url}/rest/getScanStatus?...        # poll until scanning=false or LW_RESCAN_TIMEOUT
```

Token+salt only; never send `p=` (plaintext or hex-encoded password) even though the
spec allows it. Navidrome ignores path hints and does a full scan; at a personal
library's scale that is acceptable, and `getScanStatus` gives an honest completion
signal rather than fire-and-forget.

**`jellyfin`** / **`emby`**

```
POST {url}/Library/Refresh
Header: X-Emby-Token: {token}      (Jellyfin also accepts X-MediaBrowser-Token)
```

Library-wide. A folder-scoped `POST /Items/{id}/Refresh` exists but needs the library
item id, which means an extra discovery step; deferred to a later version. Neither
server reports scan completion usefully, so `RescanResult.scanned` is `None`.

**`plex`**

```
GET {url}/library/sections/{LW_RESCAN_SECTION}/refresh?path={urlencoded_dir}&X-Plex-Token={token}
```

Plex is the only one of the four with a genuine partial, path-scoped refresh, so use
it and fall back to section-wide when `path` is rejected. If `LW_RESCAN_SECTION` is
unset, discover it once via `GET /library/sections` and cache it in `provider_state`.

**`command`**: run `LW_RESCAN_COMMAND` via `subprocess` with `{path}` substituted,
timeout `LW_RESCAN_TIMEOUT`, output captured into the job result. This is the escape
hatch for anything not covered, including someone who really does want
`docker exec navidrome navidrome scan`.

**`none`**: no-op. Correct for Navidrome with `ND_SCANSCHEDULE`, or Plex with
automatic scanning. Explicitly the default, because guessing wrong here means either
nothing happens or the app hammers a scan endpoint.

### Why not `docker exec`

The current `queue_lib.fetch_purchase` shells out to
`docker exec navidrome navidrome scan`. That requires `/var/run/docker.sock` inside
the container, which is root-equivalent on the host, in the one container that also
holds purchase-capable session cookies. It also only works when the app and the media
server are on the same host, and it depends on the container being named `navidrome`.
The HTTP API works in every deployment shape, including a Navidrome installed
directly on the host (which the brief describes) and a Navidrome in Docker (which is
what is actually running on the Mini today).

### Scheduling

Rescan is a job with fixed `job_key='rescan'`, enqueued with a `LW_RESCAN_DEBOUNCE`
(default 30s) delay after a publish. The unique key coalesces a burst of claims into
one scan. Failure is logged and surfaced but never fails the claim: the file is
already correctly on disk, and the server will find it on its own schedule.

---

## 13. Process model, startup and shutdown

### Threads at steady state

| Thread | Count | Owner |
|---|---|---|
| waitress request threads | `LW_HTTP_THREADS` (16) | waitress |
| scheduler | 1 | `libwish.scheduler` |
| job workers | `LW_JOB_WORKERS` (2) | `libwish.jobs.worker` |
| cookie keepalive | 1 per cookie-auth site | Agent 3, via `SessionKeeper.start_keepalive()` |
| SSE writers | 1 per connection, drawn from the waitress pool | web |

The `SessionKeeper` daemon-thread pattern already in `cookie_broker.py` is the
precedent and stays as-is, including its startup jitter, its 0.9-1.1x interval
randomisation, and its live->dead edge alerting. The runtime's only addition is that
`on_dead` now publishes a `provider.status` SSE event in addition to whatever
notifier is configured.

### Startup order

```
1. parse Settings (fail fast on invalid config)
2. configure logging + redaction
3. open DB, apply pragmas
4. back up, migrate, verify user_version
5. recover interrupted jobs, sweep the staging dir
6. load_providers() -> LoadReport (quarantine failures, do not die)
7. instantiate providers, check_config(), start()
8. start Agent 3's credential layer + cookie keepalives
9. start job worker pool
10. start scheduler (initial tier = cold; every enabled source due immediately+jitter)
11. serve (waitress)
```

Migrations run before providers so a provider never sees a schema it does not expect.
Job recovery runs before the pool starts so a stale `running` row cannot be observed.

### Shutdown

SIGTERM/SIGINT:

1. Set `shutdown_event`. Waitress stops accepting.
2. Broadcast `shutdown` SSE, close subscriptions. Browsers reconnect promptly on
   restart instead of hanging on a half-open socket.
3. Scheduler exits at its next wake.
4. Workers finish the current job if it is within `LW_SHUTDOWN_GRACE`; otherwise
   `cancel_requested=1` and the job lands as `cancelled` at its next
   `check_cancelled()`. Staging is swept.
5. Provider `stop()`, best effort, swallowed exceptions.
6. Checkpoint WAL, close.
7. Hard exit after grace.

Compose sets `stop_grace_period: 30s`, above `LW_SHUTDOWN_GRACE` of 20.

### CLI

`python -m libwish <cmd>`, all of which the current app cannot do:

```
serve                     # the daemon
migrate [--dry-run]       # apply migrations and exit
doctor                    # config validation + a live probe of every provider
probe <provider>          # one live probe, exit non-zero on failure
poll-once <source>        # one poll cycle, no web server, verbose
job list|show|cancel|retry
```

`doctor` is the thing a user runs before filing a bug, and it is what makes
"is my Last.fm key right" answerable without reading the UI.

---

## 14. File placement and atomicity

Where inside `/music` a file goes is Agent 2's decision. The runtime guarantees the
mechanics, because they interact with the scanner:

1. Downloads write to `LW_INCOMING_DIR/<job_id>/` which defaults to
   `$LW_MUSIC_DIR/.libwish-incoming`. **Same filesystem as `/music`**, so the final
   move is a rename and not a copy; and dot-prefixed, so Navidrome, Jellyfin, Plex
   and Emby all skip it.
2. `ctx.publish(src, rel_dest)` does: `os.makedirs(dirname, mode=LW_DIR_MODE)`,
   `os.chmod(src, LW_FILE_MODE)`, then `os.replace(src, dest)`. Atomic within the
   mount. A scanner never sees a partial file, so it never indexes and caches a
   corrupt track.
3. Collision policy: if `dest` exists, `publish` raises `FileExists` and the job ends
   `failed` with `user_action` explaining it. It never silently overwrites a file the
   user already owns, and it never appends ` (1)`.
4. Free space is checked against `LW_MIN_FREE_MB` before a claim starts and the job is
   refused with a clear message rather than filling the volume.
5. Staging is swept on startup and after every terminal job.

If `LW_INCOMING_DIR` resolves to a different device than `LW_MUSIC_DIR`, startup
fails with an explicit message rather than silently degrading to a non-atomic copy.

---

## 15. Packaging

### 15.1 Dockerfile

```dockerfile
# syntax=docker/dockerfile:1.7

# ---- CSS: runs on the BUILD platform. Tailwind output is arch-independent, so
# ---- emulating arm64 under QEMU just to build a stylesheet is wasted minutes.
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS css
ARG TAILWIND_VERSION=v3.4.17
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64" \
 && chmod +x /usr/local/bin/tailwindcss
WORKDIR /src
COPY tailwind/ tailwind/
COPY libwish/web/templates/ libwish/web/templates/
RUN tailwindcss -c tailwind/tailwind.config.js \
      -i tailwind/input.css -o /out/app.css --minify

# ---- Python deps
FROM python:3.12-slim AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml README.md ./
COPY libwish/ ./libwish/
RUN pip install .

# ---- Runtime
FROM python:3.12-slim AS runtime
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/312-dev/library-wishlist" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

RUN apt-get update && apt-get install -y --no-install-recommends gosu tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 libwish && useradd -u 1000 -g 1000 -d /app -s /usr/sbin/nologin libwish

COPY --from=deps /opt/venv /opt/venv
COPY --from=css  /out/app.css /opt/venv/lib/python3.12/site-packages/libwish/web/static/css/app.css
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LW_CONFIG_DIR=/config \
    LW_MUSIC_DIR=/music \
    LW_BIND_HOST=0.0.0.0 \
    LW_BIND_PORT=8080 \
    PUID=1000 PGID=1000 UMASK=022 TZ=UTC

VOLUME ["/config", "/music"]
EXPOSE 8080
WORKDIR /app

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]
```

Notes on the choices:

- `--platform=$BUILDPLATFORM` on the CSS stage is the important multi-arch detail.
  Without it, `docker buildx build --platform linux/amd64,linux/arm64` runs the
  Tailwind binary under QEMU for the arm64 leg, which is slow and needs the arm64
  binary. CSS is identical for both, so build it once natively and `COPY --from`.
- Tailwind **standalone** binary, so there is no Node in the runtime image and none
  in the final layers. Version pinned via `ARG`.
- `gosu`, not `su-exec` (Debian base) and not running as root.
- Only three pure-Python runtime dependencies, so no compiler and no manylinux wheel
  matrix. If Agent 3 chooses `cryptography` for token encryption, arm64 wheels do
  exist but the dependency surface grows; a stdlib-only construction is preferred if
  it is defensible.
- `HEALTHCHECK` via `python`, since `slim` has no `curl`.

Build:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg VERSION=$(git describe --tags --always) \
  --build-arg REVISION=$(git rev-parse --short HEAD) \
  -t ghcr.io/312-dev/library-wishlist:latest \
  -t ghcr.io/312-dev/library-wishlist:$(git describe --tags --always) \
  --push -f docker/Dockerfile .
```

### 15.2 `entrypoint.sh`

```sh
#!/bin/sh
set -eu
umask "${UMASK:-022}"

if [ "$(id -u)" = "0" ]; then
  groupmod -o -g "${PGID:-1000}" libwish
  usermod  -o -u "${PUID:-1000}" libwish >/dev/null

  # /config is ours: create it and take ownership.
  mkdir -p "${LW_CONFIG_DIR}/logs"
  chown -R "${PUID:-1000}:${PGID:-1000}" "${LW_CONFIG_DIR}"

  # /music is NOT ours. It may be terabytes and it may be shared with other
  # software. Check writability and report; never recursively chown it.
  if ! gosu libwish test -w "${LW_MUSIC_DIR}"; then
    echo "FATAL: ${LW_MUSIC_DIR} is not writable by ${PUID:-1000}:${PGID:-1000}." >&2
    echo "Set PUID/PGID to match the owner of your library." >&2
    exit 1
  fi

  exec gosu libwish python -m libwish "$@"
fi
exec python -m libwish "$@"
```

Not recursively chowning `/music` is deliberate: on a large library it takes many
minutes on every container start, and it rewrites ownership the user never asked to
change, which breaks other tools sharing that directory. Checking and failing with an
actionable message is better than "fixing" it.

### 15.3 Compose example, beside Navidrome

```yaml
# docker/docker-compose.example.yml
services:
  navidrome:
    image: deluan/navidrome:0.63.2
    container_name: navidrome
    restart: unless-stopped
    user: "1000:1000"
    ports: ["4533:4533"]
    environment:
      ND_MUSICFOLDER: /music
      ND_DATAFOLDER: /data
      ND_SCANSCHEDULE: 24h        # library-wishlist triggers scans on demand
      ND_LOGLEVEL: info
    volumes:
      - /srv/music:/music
      - navidrome-data:/data

  library-wishlist:
    image: ghcr.io/312-dev/library-wishlist:latest
    container_name: library-wishlist
    restart: unless-stopped
    depends_on: [navidrome]
    ports:
      - "127.0.0.1:8080:8080"     # loopback only; put a reverse proxy or a tailnet in front
    environment:
      PUID: "1000"
      PGID: "1000"
      TZ: "America/Chicago"

      LW_BASE_URL: "https://wishlist.example.net"    # OAuth redirects need this

      # Sources
      LW_SOURCE_LASTFM_USERNAME: "yourname"
      LW_SOURCE_LASTFM_API_KEY:  "${LASTFM_API_KEY}"
      LW_SOURCE_LISTENBRAINZ_USERNAME: "yourname"
      LW_SOURCE_LISTENBRAINZ_TOKEN:    "${LISTENBRAINZ_TOKEN}"
      LW_SOURCE_SUBSONIC_URL:      "http://navidrome:4533"
      LW_SOURCE_SUBSONIC_USERNAME: "yourname"
      LW_SOURCE_SUBSONIC_PASSWORD: "${NAVIDROME_PASSWORD}"

      # Rescan: same Navidrome, over its API
      LW_RESCAN_KIND:     "subsonic"
      LW_RESCAN_URL:      "http://navidrome:4533"
      LW_RESCAN_USER:     "yourname"
      LW_RESCAN_PASSWORD: "${NAVIDROME_PASSWORD}"
    volumes:
      - /srv/library-wishlist/config:/config
      - /srv/music:/music          # MUST be the same host path Navidrome mounts
    stop_grace_period: 30s

volumes:
  navidrome-data:
```

Three things the example is teaching, and the docs should say so explicitly:

1. **The same host path is mounted into both containers.** If they differ, the app
   writes into a directory Navidrome never scans and nothing appears. This is the
   single most common self-hosting mistake with a pair like this.
2. **`PUID`/`PGID` must match on both**, or Navidrome cannot read what the app wrote.
3. **Port is published on `127.0.0.1`.** The app holds live store sessions; it should
   sit behind a reverse proxy, a tailnet, or Cloudflare Access, not on the LAN.
   `LW_AUTH_MODE=token` is still on by default even so.

`ND_SCANSCHEDULE: 24h` rather than `1m`: with on-demand `startScan` the periodic scan
is a backstop, not the mechanism, and a 1-minute scan interval on a large library is
constant disk churn.

---

## Open questions / risks

1. **Hot poll interval for third parties (partial deviation from locked decision 6).**
   The brief says poll every 5-10s while a browser is connected. I have applied that
   only to the user's own Subsonic server, and set 30s for Last.fm, ListenBrainz and
   60s for Deezer. Polling a third party's API 12 times a minute for one user's loved
   tracks, for as long as a tab is open, is antisocial and is how API keys get
   revoked; and a love takes seconds to reach Last.fm anyway, so 5s buys almost
   nothing over 30s in perceived latency. Both are env-overridable. **If you want the
   literal 5-10s on Last.fm, change one default and I will not argue further**, but I
   did not want to ship it silently.

2. **Waitress and SSE.** Waitress is thread-per-request, so every SSE connection
   occupies a worker for its lifetime. 16 threads and an 8-client cap is workable for
   a single-user app, but it is a hard ceiling, not an elegant one. The alternatives
   are gunicorn+gevent (a much larger dependency and monkeypatching) or moving SSE to
   a separate ASGI process (two servers in one container). I recommend staying with
   waitress and the cap. If SSE clients ever need to scale past ~8, revisit rather
   than raising the thread count indefinitely.

3. **Python version floor.** I have specified 3.11+. The Mini's current venv is
   Python 3.9.6, so the existing deployment cannot run this without a new
   interpreter. That is fine if Docker is the supported path, but it means the
   migration off the Mini's launchd job is a container cutover rather than an
   in-place upgrade of the same tree.

4. **Navidrome deployment assumption.** The brief states Navidrome is installed
   directly on the Mini, not in Docker. On the box today it is
   `deluan/navidrome:latest` in Docker, and `queue_lib.fetch_purchase` calls
   `docker exec navidrome`. The rescan design does not depend on either, but somebody
   should reconcile the brief with reality, since it affects what the migration
   runbook says.

5. **Whether jobs need multi-process safety.** Everything here assumes exactly one
   process owns the DB. If someone runs `libwish migrate` while `serve` is running,
   or scales the container to 2 replicas, job claiming is still safe (the
   `BEGIN IMMEDIATE` + guarded UPDATE) but the scheduler would double-poll and the
   cookie session would get two in-flight consumers, which the broker's own comment
   says kills it. **Recommendation: an exclusive advisory lock on a
   `/config/.libwish.lock` file at startup; refuse to start a second instance.** I
   have not specified the lock mechanics in detail and it should be nailed down.

6. **Where the app's own auth token lives.** I specified generation into
   `/config/instance_token`. Agent 3 owns credential storage generally, and the
   instance token may belong in whatever they design instead. There is an ordering
   dependency: the token must exist before the web server binds, which is before
   Agent 3's layer would normally start. Worth a five-minute conversation between
   sections 03 and 05.

7. **`ctx.identity` in `ProviderContext` presumes Agent 4's service is synchronous
   and cheap.** If matching turns out to need network calls to MusicBrainz, it cannot
   sit on the ingest path the way I have drawn it, and normalization would have to
   split from resolution (fast normalize inline, slow resolve as a job). I have left
   room for that but not designed it.

8. **Capability strings are a soft contract.** `enumerate_purchases`, `backfill`,
   `cursor`, `buy_link`, `download` are the ones the runtime branches on. This is a
   stringly-typed interface and it will accumulate cruft. An `Enum` is stricter but
   makes third-party providers unable to declare anything the core does not already
   know. I chose strings plus a warning on unknown values; I am not certain that is
   the right trade.

9. **First-boot experience is not designed here.** With zero providers configured the
   app starts, serves an empty wishlist, and shows a settings page. Whether there is
   a setup wizard, and whether `LW_AUTH_TOKEN` being printed once to the container log
   is a good enough onboarding, belongs with Agent 6.

---

## Note on a credential encountered while researching this document

`~/Library/LaunchAgents/com.example.love-queue.plist` on the Mini stores
`COOKIE_BROKER_TOKEN` as a literal string inside its `EnvironmentVariables` dict, so
reading the plist returns the live bearer token in the clear. It was reported through
sandbroker as alert `1786318486-d4d227c1`, which will keep firing until it is
acknowledged on the host.

Two things follow for this design, beyond rotating that token:

- The packaged app must never read a secret from a world-readable location. Compose
  environment values have the same weakness (`docker inspect` shows them), so the
  documented path should be `env_file:` with a mode-0600 file, or Docker secrets, and
  `/config/instance_token` is written mode 0600 by the app itself.
- `/api/providers` and `/api/status` return `set`/`unset` for secret config keys and
  never the value, and there is no config-dump endpoint at all.
