"""Background work, recorded as rows rather than held in memory.

Persisting jobs is what lets a restart tell the difference between work that
finished, work that failed, and work that was cut off partway. That third case
gets its own state because it is not a failure: a claim interrupted by a restart
may already have spent money, and presenting it as "failed" invites a user to
buy the same track twice. The interface reports it as interrupted and asks
rather than retrying on its own.

Only this layer publishes job events. Handlers report progress through the
callable they are given, so a handler never needs to know whether anyone is
watching, and cannot emit an event for a job it is not running.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .db import transaction
from .errors import LibwishError, ProviderError, TransientError
from .events import EventBus
from .log import get

log = get("jobs")

# Terminal states. Anything else is still in flight.
DONE = ("finished", "failed", "interrupted")

# The claim phases, in the order the interface prints them. The UI shows
# "3 OF 5", so this list is the authority for that denominator and adding a
# phase here changes what the user is told.
CLAIM_PHASES = ("session", "enumerate", "match", "download", "verify")

# A sweep of every shop's purchases. Same shape, different work: it reads each
# shop once and hands what it recognises to the claim queue.
SYNC_PHASES = ("session", "enumerate", "match", "queue")

# Kept here rather than beside each pipeline so the queue can answer "how many
# steps does this kind have" without importing the code that runs it.
PHASES = {"claim": CLAIM_PHASES, "sync": SYNC_PHASES}


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    state: str
    track_id: int | None
    provider_id: str | None
    phase: str | None
    attempts: int

    @classmethod
    def from_row(cls, row: Any) -> "Job":
        return cls(
            id=row["id"], kind=row["kind"], state=row["state"],
            track_id=row["track_id"], provider_id=row["provider_id"],
            phase=row["phase"], attempts=row["attempts"],
        )


ProgressFn = Callable[..., None]
Handler = Callable[[Job, ProgressFn], Any]


class JobQueue:
    """A small persistent work queue with a fixed pool of worker threads."""

    def __init__(self, db_factory: Callable[[], Any], bus: EventBus,
                 *, max_attempts: int = 3) -> None:
        self._db = db_factory
        self._bus = bus
        self._handlers: dict[str, Handler] = {}
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.max_attempts = max_attempts

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    # ---- producing ----

    def enqueue(self, kind: str, *, track_id: int | None = None,
                provider_id: str | None = None, dedupe: bool = True) -> int:
        """Add a job. Returns the id, or the id of an equivalent job already queued.

        Deduping matters because the interface enqueues from a button a user can
        press twice. Two claims for one track would race to download the same
        file into the same path.
        """
        conn = self._db()
        try:
            if dedupe:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE kind=? AND state IN ('queued','running')"
                    " AND track_id IS ? AND provider_id IS ?",
                    (kind, track_id, provider_id),
                ).fetchone()
                if row:
                    return row["id"]
            cur = conn.execute(
                "INSERT INTO jobs(kind, state, track_id, provider_id, created_at)"
                " VALUES(?,'queued',?,?,?)",
                (kind, track_id, provider_id, int(time.time())),
            )
            job_id = cur.lastrowid
        finally:
            conn.close()
        self._wake.set()
        return int(job_id)

    def reap_interrupted(self) -> int:
        """Mark jobs left running by a dead process. Call once at startup.

        Nothing is retried automatically. A job that died halfway through may
        have had external effects this process cannot see.
        """
        conn = self._db()
        try:
            with transaction(conn):
                cur = conn.execute(
                    "UPDATE jobs SET state='interrupted', finished_at=?,"
                    " error='process exited while this job was running'"
                    " WHERE state='running'",
                    (int(time.time()),),
                )
            count = cur.rowcount or 0
        finally:
            conn.close()
        if count:
            log.warning("marked interrupted jobs from a previous run", context={"count": count})
        return count

    # ---- consuming ----

    def _take(self) -> Job | None:
        """Atomically claim the oldest queued job."""
        conn = self._db()
        try:
            with transaction(conn):
                row = conn.execute(
                    "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at, id LIMIT 1"
                ).fetchone()
                if not row:
                    return None
                conn.execute(
                    "UPDATE jobs SET state='running', started_at=?, attempts=attempts+1"
                    " WHERE id=? AND state='queued'",
                    (int(time.time()), row["id"]),
                )
                fresh = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return Job.from_row(fresh)
        finally:
            conn.close()

    def _finish(self, job: Job, state: str, *, error: str = "", code: str = "") -> None:
        conn = self._db()
        try:
            conn.execute(
                "UPDATE jobs SET state=?, finished_at=?, error=?, error_code=? WHERE id=?",
                (state, int(time.time()), error or None, code or None, job.id),
            )
        finally:
            conn.close()

    def _progress_fn(self, job: Job) -> ProgressFn:
        def progress(phase: str, **data: Any) -> None:
            known = PHASES.get(job.kind)
            if known is not None and phase not in known:
                raise ValueError(f"unknown {job.kind} phase {phase!r}; expected one of {known}")
            conn = self._db()
            try:
                conn.execute(
                    "UPDATE jobs SET phase=?, progress=? WHERE id=?",
                    (phase, json.dumps(data, default=str), job.id),
                )
            finally:
                conn.close()
            known = PHASES.get(job.kind)
            step = known.index(phase) + 1 if known else None
            # Built as one dict rather than splatted, because a handler passing
            # a key this already sets would otherwise be a TypeError raised from
            # inside the queue, killing a job that was working. The queue's own
            # facts about the job win; a handler cannot restate them wrongly.
            payload = {
                "job_id": job.id, "kind": job.kind, "phase": phase,
                "step": step, "of": len(known) if step else None,
                "track_id": job.track_id,
            }
            for key, value in data.items():
                if key not in payload:
                    payload[key] = value
            self._bus.publish("job.progress", **payload)
        return progress

    def _run_one(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._finish(job, "failed", error=f"no handler registered for {job.kind!r}",
                         code="no_handler")
            self._bus.publish("job.failed", job_id=job.id, kind=job.kind,
                              track_id=job.track_id, error="not runnable")
            return

        self._bus.publish("job.started", job_id=job.id, kind=job.kind, track_id=job.track_id)
        try:
            handler(job, self._progress_fn(job))
        except TransientError as exc:
            if job.attempts < self.max_attempts:
                conn = self._db()
                try:
                    conn.execute("UPDATE jobs SET state='queued', started_at=NULL WHERE id=?",
                                 (job.id,))
                finally:
                    conn.close()
                log.warning("job will retry", context={"job": job.id, "attempt": job.attempts,
                                                       "err": str(exc)})
                return
            self._finish(job, "failed", error=str(exc), code=exc.code or "transient")
            self._bus.publish("job.failed", job_id=job.id, kind=job.kind,
                              track_id=job.track_id, error=str(exc))
        except (ProviderError, LibwishError) as exc:
            code = getattr(exc, "code", "") or type(exc).__name__
            self._finish(job, "failed", error=str(exc), code=code)
            self._bus.publish("job.failed", job_id=job.id, kind=job.kind,
                              track_id=job.track_id, error=str(exc))
        except Exception as exc:
            # A handler is expected to raise LibwishError. Anything else is a bug
            # here rather than a condition out in the world, so it is logged with
            # a traceback and reported without pretending to be diagnosable.
            log.error("unexpected handler failure\n%s", traceback.format_exc(),
                      context={"job": job.id, "kind": job.kind})
            self._finish(job, "failed", error=f"unexpected: {exc}", code="unexpected")
            self._bus.publish("job.failed", job_id=job.id, kind=job.kind,
                              track_id=job.track_id, error="unexpected internal error")
        else:
            self._finish(job, "finished")
            self._bus.publish("job.finished", job_id=job.id, kind=job.kind,
                              track_id=job.track_id)

    def _worker(self, name: str) -> None:
        while not self._stop.is_set():
            job = self._take()
            if job is None:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            log.info("running job", context={"job": job.id, "kind": job.kind, "worker": name})
            self._run_one(job)

    def start(self, workers: int = 2) -> None:
        self._stop.clear()
        for i in range(workers):
            t = threading.Thread(target=self._worker, args=(f"w{i}",),
                                 name=f"libwish-job-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("job workers started", context={"workers": workers})

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()

    # ---- reading ----

    def recent(self, limit: int = 50) -> Iterable[dict]:
        conn = self._db()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get(self, job_id: int) -> dict | None:
        conn = self._db()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
