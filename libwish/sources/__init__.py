"""Source providers: the places that know which tracks the user loved.

A source hands the runtime a page of `LovedTrack` records and the cursor it
would move to. It does not normalize text, open the database, publish events, or
sleep for pacing; those belong to the identity layer, the runtime and the
scheduler. Keeping them out is what makes a provider testable against a recorded
payload and what lets one be replaced without the runtime noticing.

Two rules in this module are load-bearing, and both exist because the shipped
poller lost loves:

`advance` calls the caller's `store` before it produces a cursor, so there is no
code path that yields a moved cursor without the items behind it having been
written. The previous poller wrote its high-water mark inside the fetch and left
the inserts to its caller, so anything that failed in between was lost silently.

The boundary is inclusive. `cursor_after` is the first second a poll returns,
not the last second it skips. Last.fm and ListenBrainz both timestamp to the
second, so an exclusive boundary drops every love that lands in the same second
as the cursor. Re-delivery is free: `track_sources` has a `(source_id,
source_item_id)` primary key and the insert is idempotent.

Adding a third source is a new module here plus one line at the bottom of this
file. The runtime only ever sees `SourceProvider`, `SourcePage` and an opaque
JSON cursor, so nothing else moves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterable, Literal

from ..errors import ConfigError, ProviderError, TransientError
from ..log import get
from ..models import (
    PROVIDER_ID_RE,
    ConfigStatus,
    LovedTrack,
    ProviderContext,
    ProviderInfo,
    SourcePage,
)

log = get("sources")

Mode = Literal["seed", "incremental", "backfill"]

#: `LovedTrack.raw` exists to explain a bad match months later, not to mirror the
#: API. A provider record that will not fit is kept as the leading bytes of its
#: JSON, because the first field of a payload is usually the one that changed.
RAW_LIMIT = 4096


@dataclass(frozen=True)
class PollPolicy:
    """How often the scheduler may call one source.

    Two tiers: `hot` while a browser is attached to the event stream, `cold`
    otherwise. `floor` is the fastest a manual refresh may drive this source,
    and it is the provider's own promise to the remote API rather than a user
    preference, which is why it lives beside the provider and not in `Settings`.
    """

    hot: int
    cold: int
    floor: int


#: Both shipping sources are third-party public APIs polled on the user's
#: behalf, so both take the same pair as `Settings.poll_hot_seconds` and
#: `Settings.poll_cold_seconds`. Neither offers a push channel for loves, which
#: is the only reason there is any polling at all.
DEFAULT_POLL = PollPolicy(hot=30, cold=600, floor=10)


@dataclass(frozen=True)
class PollResult:
    """What one completed poll leaves behind.

    Only `advance` builds this, and only after the items were stored, so holding
    one is proof that the cursor inside it is safe to persist.
    """

    cursor: dict | None
    page: SourcePage


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------


def cursor_after(cursor: dict | None) -> int | None:
    """The epoch second a poll resumes at, inclusive, or None to start at zero.

    Tolerant of a missing or unparseable value because the cursor is read back
    out of a database column that a user can edit: a cursor nobody can parse
    means "fetch what you can see", never a crash loop.
    """
    if not cursor:
        return None
    raw = cursor.get("after")
    try:
        after = int(raw)
    except (TypeError, ValueError):
        return None
    return after if after > 0 else None


def next_cursor(previous: dict | None, items: Iterable[LovedTrack]) -> dict | None:
    """The cursor covering `items`, given the cursor they were fetched with.

    Forward only. A source that briefly reports an older timestamp, or a page
    assembled out of order, cannot drag the position backwards and re-deliver
    the whole history behind it.
    """
    after = cursor_after(previous) or 0
    for item in items:
        if item.loved_at and item.loved_at > after:
            after = item.loved_at
    return {"after": after} if after else previous


def bounded_page(cursor: dict | None, collected: list[LovedTrack], *,
                 skipped: int = 0, total: int | None = None,
                 max_items: int = 500) -> SourcePage:
    """Assemble a page oldest first and cut it from the newest end.

    Both halves matter, and they are the reason this lives here rather than in
    each provider. Both APIs answer newest first, so keeping the newest
    `max_items` of an oversized window would produce a cursor that has moved
    past the older loves that were dropped, and nothing would ever ask for them
    again. Keeping the oldest instead means the cursor covers exactly what the
    page contains; the rest arrives on the next call, and `more` says so.

    A cut that lands inside a single second is safe for the same reason the
    boundary is inclusive: the next poll starts at that second and re-delivers
    the part of it that did not fit.
    """
    collected.sort(key=lambda item: (item.loved_at or 0, item.source_item_id))
    more = len(collected) > max_items
    items = tuple(collected[:max_items])
    return SourcePage(
        items=items,
        cursor=next_cursor(cursor, items),
        more=more,
        skipped=skipped,
        total=total,
    )


def clip_raw(record: object) -> dict | None:
    """The provider's own record, bounded, for the match audit trail."""
    if not isinstance(record, dict):
        return None
    try:
        encoded = json.dumps(record, default=str)
    except (TypeError, ValueError):
        return None
    if len(encoded) <= RAW_LIMIT:
        return record
    return {"truncated": encoded[:RAW_LIMIT]}


# ---------------------------------------------------------------------------
# The seam the runtime drives
# ---------------------------------------------------------------------------


def safe_poll(provider, cursor: dict | None, *, mode: Mode = "incremental",
              max_items: int = 500) -> SourcePage:
    """Call `provider.poll` with the error contract enforced around it.

    A provider is only permitted to raise `ProviderError`. Anything else is a
    bug in the provider, and letting it travel as itself would put a KeyError
    from one source's parser in front of a user and take the poll cycle down
    with it. It becomes a retryable failure with a traceback in the log and the
    original exception still attached as the cause.
    """
    try:
        return provider.poll(cursor, mode=mode, max_items=max_items)
    except ProviderError:
        raise
    except Exception as exc:
        info = getattr(provider, "info", None)
        provider_id = info.id if info is not None else ""
        log.exception("source poll raised %s", type(exc).__name__,
                      context={"source": provider_id, "mode": mode})
        raise TransientError(
            f"poll raised {type(exc).__name__}: {exc}",
            code="unexpected", provider_id=provider_id,
        ) from exc


def advance(provider, cursor: dict | None, *, store: Callable[[SourcePage], None],
            mode: Mode = "incremental", max_items: int = 500) -> PollResult:
    """Poll once, store what came back, and only then produce the new cursor.

    `store` must durably write the page and raise if it cannot. When it raises,
    this raises too and there is no result to persist, so the caller keeps the
    cursor it came in with and the next poll asks for the same window again. The
    repeat costs one ignored insert per item; the alternative, a cursor that
    moved past rows that were never written, costs the loves themselves.
    """
    page = safe_poll(provider, cursor, mode=mode, max_items=max_items)
    store(page)
    return PollResult(cursor=page.cursor, page=page)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class SourceBase:
    """Shared plumbing for a source provider.

    Deliberately thin. It holds the context, derives `check_config` from the
    declared `ConfigSpec` tuple so no provider writes that logic twice, and
    stops there: everything a source actually does differs per API.
    """

    info: ProviderInfo

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self.conf = ctx.conf
        self.http = ctx.http
        self.log = ctx.log

    def check_config(self) -> ConfigStatus:
        missing = tuple(
            spec.key for spec in self.info.config
            if spec.required and not (self.conf(spec.key) or spec.default)
        )
        if missing:
            prefix = f"LW_SOURCE_{self.info.id.upper()}_"
            named = ", ".join(prefix + key.upper() for key in missing)
            return ConfigStatus(ok=False, missing=missing, detail=f"set {named}")
        return ConfigStatus(ok=True)

    def _required(self, key: str) -> str:
        value = self.conf(key)
        if not value:
            raise ConfigError(
                f"LW_SOURCE_{self.info.id.upper()}_{key.upper()} is not set"
            )
        return value


REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    """Add one provider class to the registry. Used as a decorator.

    Rejects a bad or duplicate id here rather than at poll time, because the id
    is written into `track_sources` and a collision would silently merge two
    services' provenance into one.
    """
    info = getattr(cls, "info", None)
    if info is None or info.kind != "source":
        raise ConfigError(f"{cls.__name__} has no source ProviderInfo")
    if not PROVIDER_ID_RE.match(info.id):
        raise ConfigError(
            f"source id {info.id!r} must match {PROVIDER_ID_RE.pattern}; ids are "
            "stored in track_sources and cannot change once shipped"
        )
    existing = REGISTRY.get(info.id)
    if existing is not None and existing is not cls:
        raise ConfigError(
            f"source id {info.id!r} is claimed by both {existing.__name__} and {cls.__name__}"
        )
    REGISTRY[info.id] = cls
    return cls


def ids() -> tuple[str, ...]:
    return tuple(sorted(REGISTRY))


def infos() -> tuple[ProviderInfo, ...]:
    """Every source's self-description, for the settings screen."""
    return tuple(REGISTRY[key].info for key in ids())


def get_class(source_id: str) -> type:
    try:
        return REGISTRY[source_id]
    except KeyError:
        raise ConfigError(
            f"no source provider {source_id!r}; known: {', '.join(ids()) or 'none'}"
        ) from None


def create(source_id: str, ctx: ProviderContext):
    return get_class(source_id)(ctx)


def discover(ctx_for: Callable[[str], ProviderContext],
             configured: Iterable[str] | None = None) -> dict[str, object]:
    """Instantiate every registered source that has configuration present.

    `configured` is normally `settings.configured_provider_ids("source")`. A
    source with nothing set is absent rather than present and permanently
    failing, so an install that only uses Last.fm never sees a ListenBrainz
    error. An id with configuration but no provider is a typo in someone's
    compose file and is logged rather than raised, because one bad line should
    not stop the other sources from polling.
    """
    if configured is None:
        configured = ids()
    built: dict[str, object] = {}
    for source_id in sorted(set(configured)):
        cls = REGISTRY.get(source_id)
        if cls is None:
            log.warning("configuration for an unknown source", context={"source": source_id})
            continue
        built[source_id] = cls(ctx_for(source_id))
    return built


# Imported last: each module registers itself by decorating its class with
# `register`, which is defined above. A new source is one more line here.
from . import lastfm as lastfm  # noqa: E402,F401
from . import listenbrainz as listenbrainz  # noqa: E402,F401
