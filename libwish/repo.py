"""Track queries, in one place.

Routes do not write SQL. Keeping it here means the column set the interface
depends on is visible in one file, which matters because several columns are
scheduled for removal in migration 0007 and the compiler cannot find their
readers for us.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .db import transaction

# Columns the interface reads. Listed explicitly rather than SELECT *, so that
# adding a column to the table does not silently widen the JSON the browser
# receives, and so removing one fails here rather than in a template.
TRACK_FIELDS = (
    "id", "artist", "title", "status", "added_at",
    "preview_url", "cover_url", "chosen_source",
    "purchased_at", "purchased_via", "ignored_at",
    # Runtime of the recording, in milliseconds. Not only for display: the
    # matcher vetoes a candidate whose duration differs by more than 15
    # seconds, and that gate cannot fire on a row where this is NULL.
    "duration_ms",
    # A direct Bandcamp link resolved by an earlier version. Migration 0007
    # drops this column, so the reader here is what has to move first.
    "bandcamp_url",
)

# The store a retry has to go back to. There is no `store` column on `tracks`,
# because a track is not assigned to a shop until something tries to buy it
# there, so this is read off the jobs table instead: the most recent claim job
# against the track that named a provider at all. A row that has never been
# claimed gets NULL, which the reader treats as "no store picked yet" rather
# than as this shop in particular, and a row refused or failed at a named
# store stays pointed at that store rather than reopening the choice between
# every one configured.
LAST_CLAIM_STORE_SQL = (
    "(SELECT provider_id FROM jobs"
    " WHERE jobs.track_id = tracks.id AND jobs.kind = 'claim'"
    "   AND jobs.provider_id IS NOT NULL"
    " ORDER BY jobs.id DESC LIMIT 1) AS last_claim_store"
)

STATUSES = ("queued", "buying", "purchased", "ignored", "owned")

# Which statuses each tab of the interface is made of.
#
# `buying` is deliberately in none of them. Nothing sets it: a claim in flight
# leaves the track queued and shows its progress on the row, which is what
# keeps a purchase visible in the list it was started from. Were it ever set,
# a track would leave the wanted count without arriving in any other.
VIEWS: dict[str, tuple[str, ...]] = {
    "wanted": ("queued",),
    "owned": ("purchased", "owned"),
    "ignored": ("ignored",),
}


def search_clause(q: str) -> tuple[str, tuple]:
    """A WHERE fragment matching every word of `q`, and its arguments.

    Matching runs against the folded identity keys rather than the raw artist
    and title, so a search for "buble" finds "Michael Bublé" and one for "ac dc"
    finds "AC/DC". Those columns already exist because the matcher needs them;
    the same folding that stops a wrong purchase makes search forgiving.

    Words are ANDed and matched against artist and title joined together, so
    "chvrches lies" finds the track without the reader having to know which
    field holds which word. Word order does not matter.

    An empty or unfoldable query returns no clause at all, which means every
    row. That is the right answer for a cleared search box: some artists fold
    to nothing, and treating that as "match nothing" would empty the screen for
    what looks to the reader like an ordinary keystroke.
    """
    from .identity import fold

    words = [fold(word) for word in (q or "").split()]
    words = [w for w in words if w]
    if not words:
        return "", ()
    # COALESCE because a row queued before the identity backfill ran has NULL
    # keys, and NULL concatenates to NULL, which would silently hide it.
    term = "(COALESCE(artist_key,'') || ' ' || COALESCE(title_key,''))"
    clause = " AND ".join(f"{term} LIKE ?" for _ in words)
    return f" AND ({clause})", tuple(f"%{w}%" for w in words)


class TrackRepo:
    def __init__(self, db_factory: Callable[[], Any]) -> None:
        self._db = db_factory

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        conn = self._db()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def queued(self, q: str = "") -> list[dict]:
        cols = ", ".join(TRACK_FIELDS) + ", " + LAST_CLAIM_STORE_SQL
        where, args = search_clause(q)
        return self._rows(
            f"SELECT {cols} FROM tracks WHERE status='queued'{where}"
            " ORDER BY added_at DESC, id DESC", args
        )

    def ignored(self, q: str = "") -> list[dict]:
        cols = ", ".join(TRACK_FIELDS) + ", " + LAST_CLAIM_STORE_SQL
        where, args = search_clause(q)
        return self._rows(
            f"SELECT {cols} FROM tracks WHERE status='ignored'{where}"
            " ORDER BY ignored_at DESC", args
        )

    def owned(self, q: str = "") -> list[dict]:
        cols = ", ".join(TRACK_FIELDS) + ", " + LAST_CLAIM_STORE_SQL
        where, args = search_clause(q)
        return self._rows(
            f"SELECT {cols} FROM tracks WHERE status IN ('purchased','owned'){where}"
            " ORDER BY purchased_at DESC", args
        )

    def get(self, track_id: int) -> dict | None:
        cols = ", ".join(TRACK_FIELDS) + ", " + LAST_CLAIM_STORE_SQL
        rows = self._rows(f"SELECT {cols} FROM tracks WHERE id=?", (track_id,))
        return rows[0] if rows else None

    def counts(self) -> dict[str, int]:
        return {r["status"] or "unknown": r["n"] for r in
                self._rows("SELECT status, COUNT(*) AS n FROM tracks GROUP BY status")}

    def view_counts(self) -> dict[str, int]:
        """How many tracks each tab holds, under the names the tabs use.

        Read through VIEWS rather than counted per status at the call site, so
        a number on a tab and the rows found under it cannot come from two
        different ideas of what belongs there.
        """
        raw = self.counts()
        return {view: sum(raw.get(status, 0) for status in statuses)
                for view, statuses in VIEWS.items()}

    def set_status(self, track_id: int, status: str, **extra: Any) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown track status {status!r}")
        sets = ["status=?"]
        args: list[Any] = [status]
        if status == "ignored":
            sets.append("ignored_at=?")
            args.append(int(time.time()))
        elif status == "queued":
            sets.append("ignored_at=NULL")
        for key, value in extra.items():
            sets.append(f"{key}=?")
            args.append(value)
        args.append(track_id)
        conn = self._db()
        try:
            with transaction(conn):
                conn.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id=?", tuple(args))
        finally:
            conn.close()

    def mark_purchased(self, track_id: int, via: str) -> None:
        self.set_status(track_id, "purchased",
                        purchased_at=int(time.time()), purchased_via=via)
