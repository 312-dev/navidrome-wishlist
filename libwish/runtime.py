"""Ingest and scheduling: the loop that turns loved tracks into queue rows.

The source providers deliberately stop at handing back a page and the cursor
they would advance to. Committing that cursor is here, and the order is the
whole point: rows are written first, the cursor moves second. Doing it the other
way loses loves permanently whenever anything fails in between, which is a bug
this application has already had.

Because the cursor boundary is inclusive, a failure means the next poll sees the
same items again. That is safe: ingest keys on track identity, so re-seeing a
love is a no-op. The asymmetry is deliberate. Duplicate work costs nothing,
losing a love costs something nobody can recover.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Iterable

from . import identity, sources
from .db import transaction
from .errors import ProviderError
from .log import get
from .models import LovedTrack, SourcePage
from .settings import configured_provider_ids

log = get("runtime")

# Statuses that mean the user has already decided about this track. A love
# arriving again must not drag it back into the queue: un-ignoring and
# re-buying are decisions for a person, not for a poller.
TERMINAL = ("purchased", "ignored", "owned")

# Cadence and batch size for filling in cover art and durations. Deliberately
# unhurried: the source is public and rate limited, and nothing about the queue
# is unusable while a row is still missing its artwork.
# Two speeds. A fresh install has every row to fill and a reader watching an
# empty grid, so it works through the backlog quickly; once caught up it drops
# to a cadence that will not bother an unauthenticated public API. Measured at
# roughly 0.4s per track, so a 164-row backlog clears in about three minutes.
ENRICH_INTERVAL_BUSY = 20
ENRICH_INTERVAL_IDLE = 900
ENRICH_BATCH = 25


class Ingest:
    """Writes a source page into the database, idempotently."""

    def __init__(self, svc: Any) -> None:
        self.svc = svc

    def __call__(self, source_id: str, page: SourcePage) -> int:
        added = 0
        for loved in page.items:
            if self._one(source_id, loved):
                added += 1
        return added

    def _one(self, source_id: str, loved: LovedTrack) -> bool:
        ident = identity.from_loved_track(loved)
        now = int(time.time())
        conn = self.svc.db()
        try:
            with transaction(conn):
                track_id = identity.find_existing(conn, ident)
                fresh = track_id is None
                if fresh:
                    cur = conn.execute(
                        "INSERT INTO tracks(artist, title, added_at, status,"
                        " artist_key, title_key, qualifier_key, fp_key,"
                        " identity_degraded, duration_ms)"
                        " VALUES(?,?,?,'queued',?,?,?,?,?,?)",
                        (loved.artist, loved.title, now,
                         ident.artist_key, ident.title.base,
                         identity.qualifier_key(ident), identity.fingerprint(ident),
                         int(bool(ident.identity_degraded)), ident.duration_ms),
                    )
                    track_id = cur.lastrowid
                conn.execute(
                    "INSERT OR IGNORE INTO track_sources(source_id, source_item_id, track_id,"
                    " loved_at, first_seen_at, last_seen_at, raw) VALUES(?,?,?,?,?,?,?)",
                    (source_id, getattr(loved, "source_item_id", None) or "", track_id,
                     getattr(loved, "loved_at", None), now, now,
                     json.dumps(sources.clip_raw(loved), default=str)),
                )
                conn.execute(
                    "UPDATE track_sources SET last_seen_at=? WHERE source_id=? AND track_id=?",
                    (now, source_id, track_id),
                )
        finally:
            conn.close()

        if fresh:
            self.svc.bus.publish("track.added", id=track_id, artist=loved.artist,
                                 title=loved.title, source=source_id)
        else:
            self.svc.bus.publish("track.source_added", id=track_id, source=source_id)
        return fresh


class Scheduler:
    """Polls each configured source on the interval its audience justifies."""

    def __init__(self, svc: Any, providers: dict[str, Any]) -> None:
        self.svc = svc
        self.providers = providers
        self.ingest = Ingest(svc)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # When each source last ran, not when it is next due. The interval is
        # read fresh on every tick and applied to this, so a reader opening the
        # page shortens the wait they are actually sitting through. Storing a
        # due time instead froze the cold interval into it: a source last polled
        # on the ten minute tier stayed ten minutes away however long someone
        # sat watching, and "loved it, opened the app, nothing there" was that.
        self._last: dict[str, float] = {sid: 0.0 for sid in providers}
        self._enrich_due = 0.0

    # ---- cursor state ----

    def _cursor(self, source_id: str) -> dict | None:
        conn = self.svc.db()
        try:
            row = conn.execute("SELECT cursor FROM source_state WHERE source_id=?",
                               (source_id,)).fetchone()
        finally:
            conn.close()
        if not row or not row["cursor"]:
            return None
        try:
            return json.loads(row["cursor"])
        except ValueError:
            log.warning("unreadable cursor, starting fresh", context={"source": source_id})
            return None

    def _save(self, source_id: str, cursor: dict | None, *, seen: int, added: int,
              ok: bool, error: str = "", kind: str = "", state: str = "") -> None:
        now = int(time.time())
        conn = self.svc.db()
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO source_state(source_id, enabled, mode, cursor, health,"
                    " last_attempt_at, items_seen, items_added) VALUES(?,1,'incremental',?,?,?,?,?)"
                    " ON CONFLICT(source_id) DO UPDATE SET"
                    "  cursor=COALESCE(excluded.cursor, source_state.cursor),"
                    "  health=excluded.health, last_attempt_at=excluded.last_attempt_at,"
                    "  items_seen=source_state.items_seen+excluded.items_seen,"
                    "  items_added=source_state.items_added+excluded.items_added",
                    (source_id, json.dumps(cursor) if cursor else None,
                     "ok" if ok else "error", now, seen, added),
                )
                if ok:
                    conn.execute(
                        "UPDATE source_state SET last_ok_at=?, consecutive_failures=0,"
                        " last_error=NULL, last_error_kind=NULL WHERE source_id=?", (now, source_id))
                else:
                    conn.execute(
                        "UPDATE source_state SET consecutive_failures=consecutive_failures+1,"
                        " last_error=?, last_error_kind=?, last_error_at=? WHERE source_id=?",
                        (error[:500], kind, now, source_id))
                conn.execute(
                    "INSERT INTO provider_state(kind, provider_id, state, detail, stale, updated_at)"
                    " VALUES('source',?,?,?,0,?)"
                    " ON CONFLICT(kind, provider_id) DO UPDATE SET"
                    "  state=excluded.state, detail=excluded.detail, updated_at=excluded.updated_at",
                    (source_id, state or ("ok" if ok else "error"), error[:200] or None, now),
                )
        finally:
            conn.close()
        self.svc.bus.publish("provider.state", provider=source_id, kind="source",
                             state=state or ("ok" if ok else "error"),
                             detail=error[:200])

    # ---- one pass ----

    def poll_once(self, source_id: str, *, mode: str = "incremental") -> int:
        provider = self.providers[source_id]

        # Missing settings are not a fault. The interface says "not set up" for
        # this, which is a different thing to tell someone than "this is
        # broken", and only one of the two names an action they can take.
        status = getattr(provider, "check_config", lambda: None)()
        if status is not None and not status.ok:
            detail = status.detail or f"needs {', '.join(status.missing)}"
            log.info("source not configured", context={"source": source_id, "detail": detail})
            self._save(source_id, None, seen=0, added=0, ok=False, error=detail,
                       kind="config", state="config")
            return 0

        cursor = self._cursor(source_id)
        added = 0
        seen = 0

        def store(page: SourcePage) -> None:
            nonlocal added, seen
            seen += len(page.items)
            added += self.ingest(source_id, page)

        try:
            result = sources.advance(provider, cursor, store=store, mode=mode)
        except ProviderError as exc:
            log.warning("source poll failed", context={"source": source_id, "err": str(exc)})
            self._save(source_id, None, seen=seen, added=added, ok=False,
                       error=str(exc), kind=type(exc).__name__)
            return added
        except Exception as exc:
            log.exception("unexpected source failure", context={"source": source_id})
            self._save(source_id, None, seen=seen, added=added, ok=False,
                       error=f"unexpected: {exc}", kind="unexpected")
            return added

        # Only now, with every row written, does the cursor move.
        self._save(source_id, result.cursor, seen=seen, added=added, ok=True)
        if added:
            log.info("queued new loves", context={"source": source_id, "added": added})
        return added

    def poll_all(self, *, mode: str = "incremental") -> int:
        return sum(self.poll_once(sid, mode=mode) for sid in list(self.providers))

    # ---- loop ----

    def _interval(self) -> int:
        s = self.svc.settings
        hot = self.svc.bus.someone_watching(s.presence_grace_seconds)
        return s.poll_hot_seconds if hot else s.poll_cold_seconds

    def _due_sources(self, now: float, interval: float) -> list[str]:
        """Which sources have gone `interval` without a poll.

        Measured backwards from the last poll rather than forwards to a due time
        decided when that poll finished. The difference is only visible when the
        interval changes underneath, which is exactly the case that matters:
        someone opens the page, the app goes hot, and the source they are
        waiting on is due now instead of at the end of a cold ten minutes.
        """
        return [sid for sid in list(self.providers)
                if now - self._last.get(sid, 0.0) >= interval]

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            interval = self._interval()
            for source_id in self._due_sources(now, interval):
                try:
                    self.poll_once(source_id)
                except Exception:
                    log.exception("scheduler swallowed a failure",
                                  context={"source": source_id})
                self._last[source_id] = time.time()
            if now >= self._enrich_due:
                queued = self._sweep_enrichment()
                self._enrich_due = time.time() + (
                    ENRICH_INTERVAL_BUSY if queued else ENRICH_INTERVAL_IDLE)
            self._stop.wait(timeout=min(5.0, interval))

    def _sweep_enrichment(self) -> int:
        """Queue lookups for rows still missing a cover or a duration.

        Small batches on a slow cadence because the metadata source is public,
        unauthenticated and rate limited. There is no deadline here: the queue
        is readable without any of it, and being throttled into a backoff would
        delay the polling that actually matters.
        """
        from . import enrich

        try:
            queued = enrich.sweep(self.svc, limit=ENRICH_BATCH)
        except Exception:
            log.exception("enrichment sweep failed")
            return 0
        if queued:
            log.info("queued metadata lookups", context={"tracks": len(queued)})
        return len(queued)

    def start(self) -> None:
        # Started even with no sources configured. Filling in metadata for rows
        # that are already queued is this loop's work too, and that is exactly
        # the state a fresh install is in.
        self._thread = threading.Thread(target=self._run, name="libwish-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started", context={"sources": sorted(self.providers)})

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None


def backfill_identity(svc: Any) -> int:
    """Fill identity columns on rows that predate them.

    Rows imported from the original database carry an artist and a title and
    nothing else, so they cannot be found by fingerprint until this has run, and
    a love arriving for one of them would insert a duplicate. Idempotent, so
    running it on every boot is cheap once there is nothing left to do.
    """
    conn = svc.db()
    try:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE fp_key IS NULL AND merged_into IS NULL"
        ).fetchone()["n"]
        if not pending:
            return 0
        log.info("computing identity for older rows", context={"rows": pending})
        collisions = identity.recompute_identity_columns(conn)
        if collisions:
            # Nothing is merged automatically. A wrong merge silently loses a
            # queue row, so these are reported and left for a person.
            log.warning("fingerprint collisions left unmerged",
                        context={"groups": len(collisions)})
        return pending
    finally:
        conn.close()


def wire(svc: Any, context_for: Callable[[str, str], Any]) -> tuple[Scheduler, dict]:
    """Build the providers and attach everything to the running application."""
    from . import claim, stores, sync

    backfill_identity(svc)

    # Only sources the user has actually configured. A provider built without
    # configuration would fail on every poll forever, filling the log and the
    # status page with a fault that is really just "you have not set this up".
    # An empty set means build nothing, so it is passed through as a set rather
    # than collapsed to None, which discover() reads as "every registered one".
    source_providers = sources.discover(lambda sid: context_for("source", sid),
                                        configured_provider_ids("source"))
    store_providers = stores.build(None, lambda sid: context_for("store", sid))
    claim.register(svc, store_providers)
    sync.register(svc, store_providers)
    from . import enrich
    svc.jobs.register("enrich", enrich.make_handler(svc))

    scheduler = Scheduler(svc, source_providers)
    log.info("wired", context={"sources": sorted(source_providers),
                               "stores": sorted(store_providers)})
    return scheduler, store_providers
