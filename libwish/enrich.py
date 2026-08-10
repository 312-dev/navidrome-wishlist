"""Filling in the two things a queue row arrives without: a runtime and a cover.

Every row in the live queue has `duration_ms` NULL, and the matcher's duration
veto cannot fire on a NULL. So the safety gate that refuses a candidate running
more than 15 seconds away from the claim is, on today's data, not running at
all. One Deezer search response carries both the runtime and the cover art, so
one request per track closes that gap and fills the local cover cache at the
same time.

The load-bearing decision here is that Deezer's ordering is not evidence. A live
search for `artist:"CHVRCHES" track:"Lies"` answers with their
`Such Great Heights (From "Tell Me Lies Season 3")` first and the actual song
second, which is precisely the wrong-track confusion this application exists to
refuse. So a result is put through `identity` and `match` like any other
candidate and is only believed if it passes on its own merits, on an exact form
of the title. A wrong duration is worse than no duration: it would make the
matcher veto the correct purchase, turning a safety gate into a fault. Nothing
that fails to confirm is written, and nothing is cached for it either.

An inconclusive lookup is recorded as a `match_decision` row with phase
`enrich`, so the sweep can leave it alone for a month rather than asking Deezer
the same unanswerable question on every pass.

Deezer is unauthenticated, shared and rate limited, so every call goes through
one process-wide throttle, and a refusal arms a cooldown that the next caller
waits out instead of walking straight back into it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from typing import Any, Callable, Sequence

from . import identity, match
from .errors import LibwishError, PermanentError, ProviderError, RateLimited, VerificationFailed
from .http import HttpClient
from .log import get
from .match import MATCHER_VERSION
from .media import CoverCache
from .models import MATCH_CONFIRM_MIN, MATCH_DURATION_GATE_S, TrackIdentity

log = get("enrich")

#: One job per track, so a rate-limited or unreachable Deezer costs one row
#: rather than the whole sweep.
JOB_KIND = "enrich"

DEEZER_SEARCH = "https://api.deezer.com/search/track"

#: Deezer allows bursts well above this. Staying near three requests a second
#: keeps a 164-row sweep polite on an endpoint nobody is paying for.
MIN_INTERVAL_S = 0.35

#: Applied when Deezer refuses without saying for how long.
COOLDOWN_S = 30.0

#: Results considered from one response. The right answer is never at rank 20,
#: and scoring the tail only widens the surface for a lucky wrong match.
MAX_CANDIDATES = 10

#: The floor a Deezer hit has to clear to have its duration believed. A search
#: response carries no ISRC and no MBID, so `MATCH_STRING_ONLY_CAP` puts the
#: auto threshold permanently out of reach and requiring it would enrich
#: nothing. The confirm floor plus an exact title is what is actually available.
MIN_SCORE = MATCH_CONFIRM_MIN

#: Title rungs that are whole-field equality on some form of the title.
#: `fuzzy` is a similarity ratio and `paren_stripped` is equality after
#: discarding text nobody classified, and neither is worth writing a duration
#: on when the cost of being wrong is a vetoed purchase.
EXACT_RUNGS = frozenset({"base_exact", "token_set_equal"})

#: How long an inconclusive lookup stands. Long enough that a sweep does not
#: re-ask every day, short enough that a lexicon change eventually gets a second
#: opinion on the rows it would now parse differently.
RETRY_AFTER_S = 30 * 86_400

#: Cover sizes Deezer offers, largest first that is still reasonable to store.
COVER_FIELDS = ("cover_big", "cover_medium", "cover_xl", "cover")


class Throttle:
    """Spacing for one shared endpoint, plus a cooldown after a refusal.

    Process-wide rather than per-job because the job queue runs several workers
    and Deezer counts requests, not threads. `sleep` is injectable so a test can
    prove a backoff happened without waiting it out.
    """

    def __init__(self, min_interval: float = MIN_INTERVAL_S,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = min_interval
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> float:
        """Block until the endpoint may be called again. Returns seconds slept."""
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.min_interval
        if delay > 0:
            self._sleep(delay)
        return delay

    def penalise(self, seconds: float | None) -> float:
        """Push the next allowed call out after a refusal. Returns the new delay.

        Taken from the server's own `Retry-After` when it gave one, because a
        number we invented is a guess about someone else's budget.
        """
        delay = COOLDOWN_S if not seconds or seconds <= 0 else float(seconds)
        with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + delay)
        return delay

    def blocked_for(self) -> float:
        """Seconds the next call would have to wait. Zero when clear."""
        with self._lock:
            return max(0.0, self._next_at - time.monotonic())


#: The one shared throttle. A caller may pass its own, which is what tests do.
DEEZER = Throttle()


def _client(svc: Any) -> HttpClient:
    settings = getattr(svc, "settings", None)
    return HttpClient(
        user_agent=getattr(settings, "http_user_agent", "library-wishlist/1.0 (+self-hosted)"),
        timeout=getattr(settings, "http_timeout_seconds", 30),
        provider_id="deezer",
    )


def _covers(svc: Any) -> CoverCache:
    settings = getattr(svc, "settings", None)
    return CoverCache(
        settings.config_dir,
        user_agent=getattr(settings, "http_user_agent", "library-wishlist/1.0 (+self-hosted)"),
        timeout=getattr(settings, "http_timeout_seconds", 30),
    )


def query_for(artist: str, title: str) -> str:
    """Deezer's field-scoped search syntax for one track.

    Scoped rather than a bare string so the artist is a constraint instead of
    another bag of words the ranker may ignore. It narrows the field; it does
    not decide anything, which is what `match` is for.
    """
    return f'artist:"{artist}" track:"{title}"'


def search(http: HttpClient, artist: str, title: str, *,
           throttle: Throttle | None = None) -> list[dict]:
    """Raw Deezer results for one track, in the order Deezer returned them.

    The scoped query is asked first and a plain one only if it comes back empty.
    Deezer's field scoping is stricter than its ranking: `artist:"The Drifters"
    track:"Some Kind Of Wonderful"` returns nothing at all for a track Deezer
    carries several times over. That is worth a second request on the minority
    of rows that need it, and it loosens only the question, not the answer,
    since every result still has to get past the matcher.
    """
    rows = _one_search(http, query_for(artist, title), throttle=throttle)
    if rows:
        return rows
    return _one_search(http, f"{artist} {title}".strip(), throttle=throttle)


def _one_search(http: HttpClient, query: str, *,
                throttle: Throttle | None = None) -> list[dict]:
    """One call to Deezer's track search.

    Deezer answers HTTP 200 with an `error` object when the shared quota is
    spent, so the body has to be inspected. Reading only the status code would
    turn a rate limit into "this track does not exist" and record an
    inconclusive lookup that was never actually made.
    """
    tl = throttle or DEEZER
    tl.wait()
    url = f"{DEEZER_SEARCH}?{urllib.parse.urlencode({'q': query})}"
    try:
        payload = http.get(url).json()
    except RateLimited as exc:
        waited = tl.penalise(getattr(exc, "retry_after", None))
        log.warning("deezer rate limited, backing off",
                    context={"seconds": round(waited, 1), "query": query})
        raise

    err = payload.get("error") if isinstance(payload, dict) else None
    if err:
        message = err.get("message") or str(err) if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        if code == 4 or "quota" in str(message).lower():
            waited = tl.penalise(None)
            log.warning("deezer quota exhausted, backing off",
                        context={"seconds": round(waited, 1)})
            raise RateLimited(f"deezer: {message}", code="quota", provider_id="deezer")
        raise PermanentError(f"deezer: {message}", code="deezer_error", provider_id="deezer")

    data = payload.get("data") if isinstance(payload, dict) else None
    return [r for r in (data or []) if isinstance(r, dict)][:MAX_CANDIDATES]


def candidate_identity(row: dict) -> TrackIdentity:
    """One Deezer result as something the matcher can compare.

    Deezer reports `duration` in whole seconds; the column, and every
    comparison, is milliseconds, so the conversion happens here at the boundary
    and nowhere else.
    """
    artist = ((row.get("artist") or {}).get("name") or "")
    seconds = row.get("duration")
    duration_ms = int(round(float(seconds) * 1000)) if seconds else None
    return identity.build_identity(
        artist,
        row.get("title") or "",
        duration_ms=duration_ms,
        store="deezer",
        store_id=str(row.get("id") or ""),
        raw=row,
    )


def cover_url(row: dict) -> str:
    """The best cover link in a result, or an empty string.

    Deezer puts the art on the album rather than the track, and older responses
    carry only some of the size variants.
    """
    album = row.get("album") or {}
    for field in COVER_FIELDS:
        value = album.get(field)
        if value:
            return str(value)
    return ""


def choose(want: TrackIdentity, rows: Sequence[dict]):
    """Pick the result that really is this track, or refuse.

    Returns `(decision, index_or_None, accepted, detail)`. The decision is
    returned even when nothing is accepted, because the refusal is what gets
    written down and a lookup with no record of why it failed reads as one that
    never happened.

    Several results surviving is normal and is not by itself a refusal, which is
    where this parts company with `match.best_match`. A single is on the single,
    on the album and on two compilations, and claiming has to know which one
    because it is about to download a file. Enrichment is only writing a
    runtime, so releases that differ about which album they are on but agree
    about how long the recording is are not ambiguous for this purpose.

    Survivors that disagree about the runtime are refused outright, and that is
    the case this exists for: Deezer offers `She Talks To Angels` at 330, 362,
    370 and 375 seconds, all of them exact title matches on the right artist.
    Writing any one of those would make the duration gate veto a correct
    purchase of one of the others.

    That refusal is about the runtime only. The fifth return value says whether
    the artwork may still be taken, and for this case it may: the survivors
    already agree on artist and on an exact title, so they are the same song,
    and which sleeve a listener sees is cosmetic. A wrong runtime silently
    vetoes a correct purchase; a sleeve from a different pressing of the same
    song costs nothing. Refusing both would leave 61 of 164 rows with no cover
    for a risk only one of them carries.
    """
    scored = [(match.score(want, c), i, c)
              for i, c in enumerate(candidate_identity(r) for r in rows)]
    if not scored:
        decision, index = match.best_match(want, [])
        return decision, index, False, match.explain(want, None, decision), False

    eligible = [
        (d, i, c) for d, i, c in scored
        if d.outcome != "refused"
        and d.score >= MIN_SCORE
        and d.matched_via in EXACT_RUNGS
        and c.duration_ms
    ]
    if not eligible:
        best, index, candidate = max(scored, key=lambda s: s[0].score)
        if best.outcome != "refused":
            # The matcher was willing and enrichment is not, so `explain` would
            # describe a decision that reads "matched" on a row whose outcome
            # column says refused.
            detail = (f"{candidate.artist_raw} - {candidate.title_raw} matched only on the "
                      f"{best.matched_via} rung at {best.score}, and a runtime is written "
                      f"only on an exact title")
        else:
            detail = match.explain(want, candidate, best)
        # Nothing here cleared the artist and exact-title bar, so the artwork
        # is not known to belong to this song either.
        return best, index, False, detail, False

    eligible.sort(key=lambda e: (-e[0].score, e[1]))
    runtimes = [c.duration_ms for _, _, c in eligible]
    spread = max(runtimes) - min(runtimes)
    decision, index, chosen = eligible[0]
    if spread > MATCH_DURATION_GATE_S * 1000:
        detail = (f"{len(eligible)} releases of {chosen.artist_raw} - {chosen.title_raw} "
                  f"run from {min(runtimes) / 1000:.0f}s to {max(runtimes) / 1000:.0f}s, "
                  f"further apart than the {MATCH_DURATION_GATE_S}s the gate allows, so "
                  "none of them establishes the runtime")
        return decision, index, False, detail, True

    # The cover comes from the highest scoring of the agreeing releases. Which
    # sleeve a listener sees is a presentation choice among copies of the same
    # recording, not the identity question the rest of this is about.
    return decision, index, True, "", True


def enrich_track(svc: Any, track_id: int, *, http: HttpClient | None = None,
                 covers: CoverCache | None = None,
                 throttle: Throttle | None = None) -> dict:
    """Fill in one track's duration and cache its cover from one Deezer response.

    Returns what happened, which is what the job handler reports and what the
    sweep counts. `outcome` is one of `skipped` (nothing left to do),
    `enriched`, or `inconclusive` (no result confirmed, recorded so it is not
    asked again for a month).
    """
    track = svc.tracks.get(track_id)
    if track is None:
        raise LibwishError(f"track {track_id} no longer exists")

    cache = covers if covers is not None else _covers(svc)
    have_duration = track.get("duration_ms") is not None
    cached = cache.path_for(track_id)
    if have_duration and cached is not None:
        return {"track_id": track_id, "outcome": "skipped",
                "duration_ms": track["duration_ms"], "cover": str(cached),
                "detail": "already enriched"}

    rows = search(http or _client(svc), track["artist"] or "", track["title"] or "",
                  throttle=throttle)
    want = identity.build_identity(track["artist"] or "", track["title"] or "")
    decision, index, accepted, detail, cover_ok = choose(want, rows)

    if not accepted:
        _record(svc, track, want, None, decision, len(rows), "refused", detail=detail)
        log.info("no deezer result confirms this track",
                 context={"track": track_id, "results": len(rows),
                          "best": decision.score, "gate": decision.gate_failed})
        result = {"track_id": track_id, "outcome": "inconclusive",
                  "results": len(rows), "score": decision.score, "detail": detail}
        # The runtime is unusable but the song is identified, so the sleeve is
        # still this track's. Written before returning so the grid fills in even
        # for rows whose duration will stay unknown.
        if cover_ok and index is not None and cached is None:
            try:
                if cache.ensure(track_id, cover_url(rows[index])):
                    result["cover"] = True
            except (VerificationFailed, ProviderError) as exc:
                log.info("cover not cached", context={"track": track_id, "err": str(exc)})
        return result

    row = rows[index]
    chosen = candidate_identity(row)
    changed: dict[str, Any] = {}

    if not have_duration and chosen.duration_ms:
        _write_duration(svc, track_id, chosen.duration_ms)
        changed["duration_ms"] = chosen.duration_ms

    cover_path = cached
    if cover_path is None:
        url = cover_url(row)
        try:
            cover_path = cache.ensure(track_id, url)
            changed["cover"] = True
        except (VerificationFailed, ProviderError) as exc:
            # A duration confirmed by the matcher is worth keeping even when the
            # CDN hands back something that is not an image. The interface draws
            # its own placeholder for a missing cover; there is no placeholder
            # for a missing safety gate.
            log.warning("cover not cached: %s: %s", type(exc).__name__, exc,
                        context={"track": track_id, "url": url})

    _record(svc, track, want, row, decision, len(rows), decision.outcome,
            duration_ms=chosen.duration_ms)
    if changed:
        svc.bus.publish("track.updated", id=track_id, fields=changed)
    return {"track_id": track_id, "outcome": "enriched", "results": len(rows),
            "score": decision.score, "matched_via": decision.matched_via,
            "duration_ms": chosen.duration_ms,
            "cover": str(cover_path) if cover_path else None,
            "changed": sorted(changed)}


def _write_duration(svc: Any, track_id: int, duration_ms: int) -> None:
    """Set `duration_ms`, but only on a row that still has none.

    Guarded in the statement rather than by a prior read, because ingest also
    writes this column and a value already there came from a source that saw the
    track itself, which is better evidence than a search result.
    """
    conn = svc.db()
    try:
        conn.execute(
            "UPDATE tracks SET duration_ms=? WHERE id=? AND duration_ms IS NULL",
            (int(duration_ms), track_id),
        )
    finally:
        conn.close()


def _describe(ident: TrackIdentity) -> dict:
    """A JSON-safe summary of the query, for the audit row."""
    return {
        "artist": ident.artist_raw,
        "title": ident.title_raw,
        "artist_key": ident.artist_key,
        "title_key": ident.title.base,
        "version": sorted(ident.title.version),
        "duration_ms": ident.duration_ms,
    }


def _record(svc: Any, track: dict, want: TrackIdentity, row: dict | None,
            decision, considered: int, outcome: str,
            duration_ms: int | None = None, detail: str = "") -> None:
    """Write the audit row for one enrichment decision.

    Both outcomes are recorded. The refusal is the more useful of the two: it is
    what stops the sweep asking again, and without it a track with no duration
    is indistinguishable from one nobody has looked at yet.

    A failure to write is logged rather than raised. The duration is already on
    the row by this point, and losing the audit line is not a reason to fail a
    lookup that succeeded.
    """
    reasons = detail or ", ".join(decision.reasons) or decision.matched_via or outcome
    conn = svc.db()
    try:
        conn.execute(
            "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
            " matcher_version, lexicon_hash, outcome, score, gate_failed, matched_via,"
            " reasons, query_json, candidate_json, candidates_considered, duration_ms)"
            " VALUES(?,?,'enrich','deezer',?,?,?,?,?,?,?,?,?,?,?)",
            (track["id"], int(time.time()), MATCHER_VERSION, identity.lexicon_hash(),
             outcome, decision.score, decision.gate_failed, decision.matched_via,
             reasons[:2000], json.dumps(_describe(want), default=str),
             json.dumps(row, default=str) if row is not None else None,
             considered, duration_ms),
        )
    except Exception as exc:
        log.error("enrich decision not recorded: %s: %s", type(exc).__name__, exc,
                  context={"track": track["id"], "outcome": outcome})
    finally:
        conn.close()


def pending(svc: Any, limit: int = 25, *, covers: CoverCache | None = None,
            now: int | None = None) -> list[int]:
    """Track ids that still need a duration or a cover, oldest row first.

    The cover half of the question is a filesystem check, so the SQL narrows and
    the loop decides. Rows whose last enrichment was inconclusive are left out
    until `RETRY_AFTER_S` has passed, which is what keeps a track Deezer simply
    does not carry from consuming a slot on every sweep forever.
    """
    cache = covers if covers is not None else _covers(svc)
    cutoff = (now if now is not None else int(time.time())) - RETRY_AFTER_S
    conn = svc.db()
    try:
        rows = conn.execute(
            "SELECT id, duration_ms FROM tracks t"
            " WHERE t.merged_into IS NULL"
            "   AND NOT EXISTS (SELECT 1 FROM match_decision d"
            "                   WHERE d.track_id = t.id AND d.phase = 'enrich'"
            "                     AND d.outcome = 'refused' AND d.decided_at > ?)"
                        # Newest loves first, because that is the end of the list a
            # reader is looking at. Filling the oldest rows first left every
            # cover on the first screen a miss for half an hour.
            " ORDER BY t.added_at DESC, t.id DESC",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    due: list[int] = []
    for row in rows:
        if len(due) >= limit:
            break
        if row["duration_ms"] is None or not cache.exists(row["id"]):
            due.append(row["id"])
    return due


def sweep(svc: Any, limit: int = 25, *, covers: CoverCache | None = None) -> list[int]:
    """Queue enrichment for up to `limit` rows that still need it.

    Small batches on purpose. Every job is one request to an endpoint nobody is
    paying for, and a sweep that queued all 164 at once would hand the whole
    burst to the worker pool at the same moment.
    """
    ids = pending(svc, limit, covers=covers)
    for track_id in ids:
        svc.jobs.enqueue(JOB_KIND, track_id=track_id)
    if ids:
        log.info("queued enrichment", context={"tracks": len(ids)})
    return ids


def make_handler(svc: Any, *, http: HttpClient | None = None,
                 covers: CoverCache | None = None) -> Callable[[Any, Any], Any]:
    """The job handler for `JOB_KIND`, for the orchestrator to register.

    Built here and registered there, so this module never has to know whether a
    job queue exists, and a test can call `enrich_track` without one.
    """
    shared_covers = covers if covers is not None else _covers(svc)

    def handler(job, progress) -> dict:
        if job.track_id is None:
            raise LibwishError("an enrich job needs a track")
        progress("lookup", track_id=job.track_id)
        result = enrich_track(svc, job.track_id, http=http, covers=shared_covers)
        progress(result["outcome"], **{k: v for k, v in result.items()
                                       if k in ("duration_ms", "score", "results")})
        return result

    return handler


__all__ = [
    "DEEZER",
    "EXACT_RUNGS",
    "JOB_KIND",
    "MIN_SCORE",
    "RETRY_AFTER_S",
    "Throttle",
    "candidate_identity",
    "choose",
    "cover_url",
    "enrich_track",
    "make_handler",
    "pending",
    "query_for",
    "search",
    "sweep",
]
