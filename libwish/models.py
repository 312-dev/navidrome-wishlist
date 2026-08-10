"""Frozen value types and protocols shared by every libwish workstream.

This module is the bottom of the dependency graph: it imports nothing from the
rest of the package (there is nothing else to import yet) and nothing else in
this file performs I/O or holds behaviour. Everything here is a dataclass, a
Protocol, an enum, or a named constant, reconciled from the architecture docs
under docs/architecture/ per docs/07-ROADMAP.md section 3 ("Shared core
contracts", C1-C10). Each section below cites the contract it freezes.

Several fields reference types owned by other modules that do not exist yet
(Settings, Logger, HttpClient, ProviderState, PathService, PollPolicy,
AuthContext, sqlite3.Connection, pathlib.Path). `from __future__ import
annotations` makes every annotation in this file a string that is never
evaluated at import time, so those forward references are safe without
importing the modules that will eventually define them. Only dataclasses,
typing, enum and re are imported here; every other name in a type hint below
is a forward reference by design, not an oversight.

Exceptions are named in docstrings/comments where a source document names
them (e.g. "raises AuthExpired"), never imported: the error hierarchy lives
in libwish/errors.py, written separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, ClassVar, Iterator, Literal, Protocol, Sequence

# ---------------------------------------------------------------------------
# C1 - provider naming
# ---------------------------------------------------------------------------

#: A provider `id` is unique across sources and stores and, once shipped, is
#: frozen forever (05-runtime.md:237-245): lowercase, starts with a letter,
#: 2-32 characters total, `[a-z0-9_]` after the first character.
PROVIDER_ID_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# ---------------------------------------------------------------------------
# C6 - match thresholds (04-identity.md section 4, roadmap C6)
#
# Named here instead of as magic numbers because six workstreams read them
# independently. Every one of them is load-bearing: changing a value changes
# what counts as the same recording.
# ---------------------------------------------------------------------------

#: score >= this is outcome "auto". Reachable only with a validated recording
#: MBID shared by both sides or a shared ISRC; strings alone cannot reach it
#: (invariant I3, see MATCH_STRING_ONLY_CAP below).
MATCH_AUTO_MIN = 90

#: score >= this (and below MATCH_AUTO_MIN) is outcome "confirm": show both
#: sides and require a click, never download unattended. Below this the
#: outcome is "refused".
MATCH_CONFIRM_MIN = 70

#: Ceiling on the score when the match rests on strings alone, with no
#: validated MBID or shared ISRC as corroborating evidence. Keeps
#: MATCH_AUTO_MIN out of reach for a string-only match (invariant I3).
MATCH_STRING_ONLY_CAP = 84

#: Ceiling on the score for a match reached only via the paren-stripped title
#: form (`base_alt`), or for a degraded identity row. Both cases are weaker
#: evidence than an exact or token-set title match, so both are capped below
#: MATCH_CONFIRM_MIN's neighbourhood and forced into "confirm" at best.
MATCH_DEGRADED_CAP = 74

#: Minimum `artist_score` (0..1) to pass gate 1; below this the match is
#: refused with reason ARTIST_MISMATCH before scoring runs.
MATCH_ARTIST_GATE = 0.85

#: Minimum `difflib.SequenceMatcher` ratio (0..1) on normalized title bases
#: for the fuzzy title rung to pass. Below this and off the exact/token-set
#: rungs, the title gate fails with reason TITLE_MISMATCH.
MATCH_TITLE_FUZZY_GATE = 0.92

#: Maximum allowed difference, in seconds, between two known durations before
#: gate 5 fails with reason DURATION_MISMATCH. Only applies when both sides
#: report a duration; an unknown duration on either side does not gate.
MATCH_DURATION_GATE_S = 15

# ---------------------------------------------------------------------------
# C5 - sources: the loved-track record and fetch protocol
# (01-sources.md:88-120 verbatim; SourcePage/poll per roadmap C5, replacing
# the cut FetchPage/Budget/fetch() shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackIds:
    """Every identifier the source supplied. All optional. Never invented."""

    recording_mbid: str | None = None  # MusicBrainz recording
    release_mbid: str | None = None
    artist_mbids: tuple[str, ...] = ()
    isrc: str | None = None
    native_id: str | None = None  # the source's own track id
    native_url: str | None = None  # canonical page on the source


@dataclass(frozen=True, slots=True)
class LovedTrack:
    """One loved/favourited track exactly as a source reported it.

    `artist` and `title` are raw display strings, verbatim from the source
    and never cleaned: normalization is the identity layer's job, not the
    source's, so this record can be trusted as ground truth for what the
    source actually said.
    """

    # --- provenance ---
    source_id: str  # "lastfm", "listenbrainz", "subsonic", "deezer"
    source_item_id: str  # stable id of THIS love, within this source
    loved_at: int | None  # epoch seconds, None if the source has no timestamp

    # --- raw display strings, verbatim from the source, never cleaned ---
    artist: str
    title: str
    album: str | None = None
    artists: tuple[str, ...] = ()  # when the source splits them; else ()

    # --- identifiers ---
    ids: TrackIds = field(default_factory=TrackIds)

    # --- optional enrichment the source got for free ---
    duration_s: int | None = None
    cover_url: str | None = None

    # --- diagnosis ---
    raw: dict | None = None  # the provider's own record, truncated


@dataclass(frozen=True)
class SourcePage:
    """One page of loves returned by a source's `poll()`.

    Cursor comparison at the call site is `>=`, not `>` (01-sources.md:38-44):
    a source must be able to re-request its own last-seen position without
    losing the item that sits exactly on the boundary.
    """

    items: tuple[LovedTrack, ...]
    cursor: dict | None  # provider-owned, JSON-serialisable, opaque elsewhere
    more: bool = False
    skipped: int = 0
    total: int | None = None


class SourceProvider(Protocol):
    """Fetch side of a source. Lifecycle (`__init__`, `check_config`, `probe`,
    `start`, `stop`) comes from the shared Provider protocol in
    providers/base.py and is not repeated here; this is only the C5 fetch
    contract that replaced the older fetch()/FetchPage()/Budget shape.
    """

    def poll(
        self,
        cursor: dict | None,
        *,
        mode: Literal["seed", "incremental", "backfill"],
        max_items: int = 500,
    ) -> SourcePage: ...


# ---------------------------------------------------------------------------
# C6 - track identity (04-identity.md:466-482, 502-525 verbatim)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedTitle:
    """A parsed, classified title, produced by `identity.parse_title`.

    `__contains__` raises TypeError on purpose: invariant I1 ("no substring")
    is enforced as a type error rather than a convention, because a `in`
    comparison here would silently reintroduce the class of bug the identity
    layer exists to prevent (one title looking like a match because it is a
    textual fragment of the other). Callers must compare `base`, `base_alt`,
    or `tokens` explicitly instead.
    """

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


@dataclass(frozen=True)
class TrackIdentity:
    """A normalized track identity: one side of a match comparison."""

    artist_raw: str
    title_raw: str
    artists: tuple[str, ...]  # whole string first, then guard-passing fragments
    artist_key: str
    title: NormalizedTitle
    recording_mbids: frozenset[str]  # validated only
    isrcs: frozenset[str]
    duration_ms: int | None
    identity_degraded: bool = False
    store: str | None = None
    store_id: str | None = None
    raw: dict | None = None  # the untouched payload, carried for the audit log


@dataclass(frozen=True)
class MatchDecision:
    """The outcome of scoring one `TrackIdentity` against a candidate.

    `best_match` (in match.py) returns `refused` if the top two surviving
    candidates score within 5 points of each other (reason
    AMBIGUOUS_TOP_CANDIDATES): two near-identical candidates usually means
    two masters of the same song, and this type does not encode a way to
    prefer one silently.
    """

    outcome: Literal["auto", "confirm", "refused"]
    score: int
    gate_failed: str | None
    matched_via: str  # base_exact | token_set_equal | fuzzy | paren_stripped
    reasons: tuple[str, ...]
    capped: bool


# ---------------------------------------------------------------------------
# C7 - store contract (02-stores.md:117-207 value types, 212-253 protocol,
# verbatim except: __init__ takes ProviderContext instead of (cfg, creds),
# there is no claim() method, and OwnedItem.duration_s stays a float because
# it is a store-reported value)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identifiers:
    """Whatever a provider can give us. All optional. The claim pipeline
    owns how they are used, not the store that supplies them.
    """

    isrc: str | None = None
    mbid: str | None = None  # recording MBID
    upc: str | None = None  # release
    store_track_id: str | None = None
    store_release_id: str | None = None


@dataclass(frozen=True)
class TrackQuery:
    """What we are looking for. Built from a tracks row by the claim
    pipeline, never constructed by a provider itself.
    """

    artist: str
    title: str
    album: str | None = None
    duration_s: float | None = None
    ids: Identifiers = field(default_factory=Identifiers)


@dataclass(frozen=True)
class StoreCapabilities:
    """What one store provider can do. Read by the runtime to decide which
    StoreProvider methods it is safe to call.
    """

    search: bool  # can produce any buy link at all
    deep_link: bool  # can produce a link to the exact product, not a search page
    enumerate_owned: bool
    download: bool
    release_granular: bool  # purchases are releases; downloads may be archives
    async_prepare: bool  # store may answer "encoding, retry later"
    formats: tuple[str, ...] = ()  # advertised, best first, e.g. ("flac-24","flac-16","mp3-320")


@dataclass(frozen=True)
class Offer:
    """One candidate product a store returned for a query. A store must not
    filter these by its own notion of a match; the identity layer scores
    candidates, the store only supplies them.
    """

    store: str
    kind: str  # "product" | "search"
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
    """One item the user already owns at a store, as enumerated by
    `StoreProvider.list_owned`/`expand`.

    `duration_s` is a float because it is what the store reported, not what
    was downloaded and decoded; the `tracks` table's `duration_ms` column is
    an integer of the file actually written, a separate value populated
    later in the claim pipeline. Convert at that boundary, not here.
    """

    store: str
    item_key: str  # opaque, stable, unique within the store
    kind: str  # "track" | "release"
    artist: str
    title: str  # track title, or release title when kind="release"
    release_title: str | None = None
    parent_key: str | None = None  # release item_key for a track inside a release
    purchased_at: int | None = None
    duration_s: float | None = None
    track_number: int | None = None
    formats: tuple[str, ...] = ()
    ids: Identifiers = field(default_factory=Identifiers)
    raw: dict = field(default_factory=dict)  # verbatim provider record, persisted


@dataclass
class DownloadResult:
    """Result of a `StoreProvider.download` call. Not frozen: the pipeline
    stage after download may still need to correct `is_archive` once it
    looks at the file, per 02-stores.md.
    """

    path: Path  # staged file. NOT in the library.
    requested_format: str
    bytes: int
    source_host: str
    is_archive: bool = False  # a zip/tar we still need to open
    notes: dict = field(default_factory=dict)


@dataclass
class ProgressEvent:
    """One progress tick during a claim. Not frozen: instances are built
    incrementally by the store as a download proceeds.
    """

    claim_id: int
    phase: str  # enumerate|match|download|verify|commit
    detail: str
    bytes_done: int | None = None
    bytes_total: int | None = None


ProgressFn = Callable[[ProgressEvent], None]


@dataclass
class StoreHealth:
    """Result of `StoreProvider.check`. Not frozen: `owned_count` may be
    filled in lazily after the cheap liveness probe returns.
    """

    ok: bool
    authed: bool
    detail: str
    checked_at: int
    owned_count: int | None = None


class StoreProvider(Protocol):
    """A store: somewhere the user can buy, or already owns, a track.

    `__init__` takes the same `ProviderContext` every provider gets (C3); the
    credential handle is reached via `ctx.creds`, so there is no separate
    `creds` constructor argument. There is deliberately no `claim()` method
    here: claiming is the pipeline's job (libwish/claim.py), orchestrated
    across several stores and never owned by one.
    """

    id: ClassVar[str]  # "qobuz" - stable, used as a DB value
    name: ClassVar[str]  # "Qobuz" - shown in the UI
    capabilities: ClassVar[StoreCapabilities]
    auth_kind: ClassVar[str]  # "cookie" | "oauth2" | "none" - dispatches auth setup

    def __init__(self, ctx: ProviderContext) -> None: ...

    # --- health -----------------------------------------------------------
    def check(self) -> StoreHealth:
        """Cheap liveness + auth probe. Must distinguish 'not authed' from
        'store down'. Never raises; encodes failure in StoreHealth."""

    # --- discovery (usually unauthenticated) ------------------------------
    def buy_url(self, q: TrackQuery) -> str:
        """Always returns something clickable. A search URL is an acceptable
        answer."""

    def find_offers(self, q: TrackQuery, limit: int = 5) -> list[Offer]:
        """Candidate products. Must not filter by its own notion of a match;
        return candidates with whatever metadata the store gave, let the
        matcher score them. Returns [] if the store has no queryable
        search."""

    # --- ownership (only if capabilities.enumerate_owned) -----------------
    def list_owned(self, since: int | None = None) -> Iterator[OwnedItem]:
        """Yield everything the user owns, newest first where the store
        allows it. `since` is a best-effort purchased_at cutoff; a store
        that cannot filter server-side ignores it and the pipeline stops
        consuming early. Must raise StoreAuthError on a dead session; must
        not return [] for that case."""

    def expand(self, item: OwnedItem) -> Iterator[OwnedItem]:
        """For release_granular stores: the tracks inside a release item.
        Non-release stores return iter([item])."""

    # --- fetch (only if capabilities.download) ---------------------------
    def download(
        self,
        item: OwnedItem,
        dest_dir: Path,
        prefer: Sequence[str],
        progress: ProgressFn,
    ) -> DownloadResult:
        """Fetch the best available format from `prefer` (best first) into
        dest_dir. dest_dir is a staging directory owned by the caller.
        Raises StorePreparing (retryable) if the store is still encoding.
        Raises StoreFormatUnavailable if nothing in `prefer` exists. Does
        not verify the file: that is the pipeline's job."""


# ---------------------------------------------------------------------------
# C8 - credentials (03-auth.md:208-243, 286-302 verbatim, plus the
# CredentialHandle Protocol added by roadmap C8/C21)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    """The full stored credential. `secret` is decrypted; it is never
    logged and never serialized to an API response.
    """

    provider: str
    method: str
    status: str
    secret: dict
    public_meta: dict
    expires_at: int | None


@dataclass(frozen=True)
class CredentialStatus:
    """The safe projection of a Credential: what the API and SSE emit."""

    provider: str
    method: str
    status: str
    account: str | None
    detail: str | None
    action: str  # none|reauthorize|reseed_cookie|paste_token
    expires_at: int | None
    last_ok_at: int | None
    refreshable: bool


class CredentialStore:
    """The only code that touches `secret_enc`. Everything else deals in
    Credential/CredentialStatus dataclasses, never the encrypted column.
    """

    def get(self, provider: str) -> Credential | None: ...
    def require(self, provider: str) -> Credential: ...  # raises AuthError if absent/expired
    def put(
        self,
        provider: str,
        method: str,
        secret: dict,
        *,
        public_meta: dict | None = None,
        expires_at: int | None = None,
        status: str = "live",
    ) -> None: ...
    def patch_secret(self, provider: str, **fields) -> None:  # shallow-merge, for refresh
        ...
    def mark_ok(self, provider: str) -> None: ...
    def mark_failed(self, provider: str, detail: str, *, fatal: bool) -> None: ...
    def revoke(self, provider: str) -> None: ...  # provider revoke + wipe row
    def status(self, provider: str) -> CredentialStatus: ...
    def all_status(self) -> list[CredentialStatus]: ...


@dataclass(frozen=True)
class AuthSpec:
    """What a provider declares about how it authenticates. A provider does
    not implement an AuthMethod itself; it publishes this spec and the
    matching AuthMethod (picked by `method`) does the work.
    """

    method: str  # picks the AuthMethod
    label: str  # "Deezer"
    setup_url: str | None  # where the user registers an app
    setup_help: str | None  # one paragraph, rendered above the form
    # oauth2 / oauth1a
    authorize_url: str | None = None
    token_url: str | None = None
    revoke_url: str | None = None
    device_url: str | None = None  # presence enables rung 1 of the ladder
    scopes: tuple[str, ...] = ()
    pkce: bool = True
    redirect_modes: tuple[str, ...] = ("same_origin", "loopback_paste")
    # everything
    verify: Callable[[AuthContext, Credential], tuple[bool, str]] | None = None


class CredentialHandle(Protocol):
    """Per-provider facade over CredentialStore. What a provider actually
    gets via `ProviderContext.creds`, scoped to its own provider id so it
    cannot read or touch another provider's credential.
    """

    def require(self) -> Credential: ...  # raises AuthExpired if absent/expired
    def http_client(self, **kw) -> HttpClient: ...  # cookie providers get a SessionKeeper-backed one
    def mark_ok(self) -> None: ...
    def mark_failed(self, detail: str, *, fatal: bool) -> None: ...


# ---------------------------------------------------------------------------
# C2/C3 - provider runtime shape (05-runtime.md:295-320, 347-360, with the
# ProviderInfo.auth/capabilities changes from C2 and the ProviderContext
# field list from C3, which drops the `identity` field the runtime doc had)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigSpec:
    """One config key a provider declares, used to derive `check_config`
    without a provider having to write that logic itself.
    """

    key: str
    required: bool = False
    secret: bool = False
    default: str | None = None
    help: str = ""
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ConfigStatus:
    """Result of a provider's config check: no network, just "is this
    filled in".
    """

    ok: bool
    missing: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ProviderInfo:
    """Static self-description of one provider (source or store), read by
    the UI and the scheduler.

    `auth` is always an AuthSpec (C2): even a provider with no login
    (`method="none"`) declares one, so the runtime never has to special-case
    a missing auth block. `capabilities` differs by kind (C2): for a store
    it is the typed StoreCapabilities; for a source it stays the plain
    frozenset[str] the sources doc used, with exactly two members the
    runtime branches on, "backfill" and "cursor". `poll` is a PollPolicy,
    owned by the scheduler (05-runtime.md section 5.2) and referenced here
    only by forward-declared name; it applies to sources only.
    """

    id: str
    kind: Literal["source", "store"]
    display_name: str
    homepage: str
    auth: AuthSpec
    config: tuple[ConfigSpec, ...] = ()
    capabilities: frozenset[str] | StoreCapabilities = frozenset()
    poll: PollPolicy | None = None  # sources only


@dataclass(frozen=True)
class ProviderContext:
    """The only handle a provider gets to the rest of the app. Nothing else
    is importable from provider code by convention, which is what keeps
    providers swappable.

    This is 05-runtime.md's ProviderContext minus the `identity` field
    (roadmap C3/C18): identity normalization and matching are called by the
    claim pipeline and the source-ingest path around a provider, not handed
    to the provider itself, so a provider cannot reach past its own
    fetch/list/download surface into the matcher.
    """

    provider_id: str
    settings: Settings
    log: Logger  # bound with provider=<id>
    conf: Callable[[str], str | None]  # namespaced; providers never read os.environ
    creds: CredentialHandle
    http: HttpClient  # the ONLY http path; routes via SessionKeeper for cookie auth
    state: ProviderState
    db: Callable[[], sqlite3.Connection]
    paths: PathService


# ---------------------------------------------------------------------------
# C9 - event contract (05-runtime.md:885-903, with the C14 amendments:
# track.source_added added, job.progress gains `phase`, credential.updated
# carries the CredentialStatus payload from C8, library.changed deleted)
# ---------------------------------------------------------------------------


class EventName(str, Enum):
    """Every SSE event name the runtime may publish. Only the runtime
    publishes events (05-runtime.md:912-918); providers, the claim pipeline
    and auth code never touch the event bus directly. Every payload carries
    `"v": 1`; the shape referenced in each member's comment is the value of
    the event's `data:` field.
    """

    TRACK_ADDED = "track.added"  # {v, id, artist, title, source, added_at, cover_url, resolved}
    TRACK_UPDATED = "track.updated"  # {v, id, fields: {...changed keys only...}}
    TRACK_REMOVED = "track.removed"  # {v, id, reason: "purchased"|"ignored"|"merged"}
    TRACK_SOURCE_ADDED = "track.source_added"  # C14: a second source loved an existing track
    JOB_STARTED = "job.started"  # {v, job_id, type, track_id?, store?}
    JOB_PROGRESS = "job.progress"  # {v, job_id, phase, progress: 0..1|null, text}
    JOB_FINISHED = "job.finished"  # {v, job_id, state, result?, error_code?, error_text?, user_action?}
    PROVIDER_STATUS = "provider.status"  # {v, kind, id, state, detail, last_run_at, next_due_at, consecutive_failures}
    CREDENTIAL_UPDATED = "credential.updated"  # {v, provider_id} plus the CredentialStatus payload (C8)
    SCAN_REQUESTED = "scan.requested"  # {v, target, ok, detail}
    POLL_TIER = "poll.tier"  # {v, tier: "hot"|"warm"|"cold", watchers, next_due_at}
    HEARTBEAT = "heartbeat"  # {v, ts, queued, running, watchers} - every 30s
    RESYNC = "resync"  # {v, reason}
    SHUTDOWN = "shutdown"  # {v} - sent on SIGTERM
