"""Page rendering. Every route here answers with HTML and nothing else.

The JSON API, the SSE stream and application construction live next door in
api.py and app.py. The split is not decorative: this module may be imported and
its template rendering exercised without a database, which is what makes the
interface testable on fake rows.

Two things are worth knowing before editing:

**The plate is built here, not in a template.** `plate_for()` turns a track row
into the five-field dict the plate macro renders. One function decides which of
the five states a row is in, so the wanted view, the owned view and the mobile
strip cannot disagree about it.

**No value on the plate is ever estimated.** Neither shipping store tells us
what format it will deliver before purchase: Qobuz's offers endpoint needs an
application id a self-hoster cannot get, and Bandcamp commits to nothing until
the download starts. So the quality ladder appears on owned rows only, where the
number is a fact about a file on disk. A plate that guessed would be worse than
one that stays quiet, because the entire point of the object is that it can be
trusted.

**A page is a window, not the whole list.** `rack_window()` decides which rows a
response carries and decorates only those, so the first view costs one screenful
of markup and one refusal lookup per rendered row rather than per queued row.
More rows arrive on request, appended below what is already on screen, which is
the same insertion rule the live connection follows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from flask import Blueprint, abort, current_app, render_template, request

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

bp = Blueprint("web", __name__, template_folder="templates")


@bp.teardown_app_request
def _close_request_conn(exc=None):
    """Release the per-request connection however the request ended."""
    from flask import g

    conn = g.pop("_lw_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass

# Endpoints the templates post to. api.py owns them; they are listed in one
# place here so that renaming one is a single edit rather than a grep through
# every template.
API = {
    "ignore": "/api/ignore/{id}",
    "restore": "/api/restore/{id}",
    "claim": "/api/claim/{id}",
    "claim_confirm": "/api/claim/{id}/confirm",
    "claim_pick": "/api/claim/{id}/pick",
    "refusal_dismiss": "/api/refusal/{id}/dismiss",
    # No {id}: the picker appends the store itself, the same way a buy link
    # appends one to the shared `buy` route rather than the route templating
    # it in.
    "purchases": "/api/purchases",
    # One confirm for a whole selection. Separate from the per-track route
    # above because it takes a list and answers with a job per track.
    "claim_confirm_all": "/api/claim/confirm",
    "cancel": "/api/claim/{id}/cancel",
    "buy": "/api/buy/{id}",
    "search": "/api/search/{id}",
    "preview": "/api/preview/{id}",
    # Cover art comes from this server, off a local cache, and 404s until the
    # cache has it. Rendering the source URL the metadata carries would tell
    # whoever hosts it the reader's address and roughly what they love, once
    # per row per page load, and would leave the grid blank on a LAN with no
    # route out.
    "cover": "/api/cover/{id}",
    "scan": "/api/scan",
    # api.py mounts the stream inside its own /api prefix. The browser reads
    # this off the page rather than hard-coding it, so moving the stream to the
    # root is a one-line change here and nowhere else.
    "events": "/api/events",
}

# The five claim phases, mirroring CLAIM_PHASES in libwish/jobs.py. The plate
# prints "3 OF 5" and the denominator has to be true, so this table is keyed on
# the same names the job queue validates against and a phase added there without
# a label here renders as the bare phase name rather than silently as step 1.
CLAIM_STAGES: dict[str, dict[str, str]] = {
    "session": {"label": "SESSION", "line": "Checking your {store} session"},
    "enumerate": {"label": "FINDING", "line": "Looking through your {store} purchases"},
    "match": {"label": "MATCHING", "line": 'Matching your purchases to "{title}" by {artist}'},
    "download": {"label": "DOWNLOAD", "line": "Downloading"},
    "verify": {"label": "VERIFY", "line": "Checking the file is what it claims to be"},
}
PHASE_ORDER = tuple(CLAIM_STAGES)

# What a failed claim tells the user, by the phase it died in. Each one names
# what happened and what to do about it, and none of them apologises.
FAILURE_COPY: dict[str, dict[str, Any]] = {
    "session": {
        "text": "Your {store} session expired. Re-seed your cookies, then claim again.",
        "actions": [("Open auth", "/setup#stores")],
    },
    "enumerate": {
        "text": "No matching purchase in your {store} account. If you bought this as part"
                " of an album, claim the album instead.",
        "actions": [("Retry", None), ("Find album", None)],
    },
    "match": {
        "text": "Nothing scored high enough to download.",
        "actions": [("Retry", None)],
    },
    "download": {
        "text": "The download stopped before the file was complete.",
        "actions": [("Retry", None)],
    },
    "verify": {
        "text": "The downloaded file is not what it claims to be. Nothing was added to"
                " your library.",
        "actions": [("Retry", None), ("Report", None)],
    },
}

# Fields the row template reads that no v1 query produces. Defaulted in one
# place so a template never has to test for a key, and so that the claim
# pipeline can start recording what it wrote to disk without any change here.
#
# `cover_url` is the address the source metadata carried, and no template
# renders it. It is what the cover cache fetches from, server side; putting it
# in a page would make every reader's browser fetch from that host directly.
OPTIONAL_FIELDS = ("cover_url", "preview_url", "file_path", "quality",
                   "quality_note", "job", "refusal", "failure", "store",
                   "duration_ms", "duration_unknown")

# Which decisions the refusal panel is allowed to speak for.
#
# `match_decision` is shared by everything that matches anything. Enrichment
# writes a row per lookup with phase 'enrich' and provider 'deezer', and its
# refusals are ordinary: on the live queue 14 tracks of 40 carry one. The
# refusal panel says a purchase was refused and that nothing was downloaded, so
# a metadata lookup appearing there would tell someone their money was involved
# in a decision that never touched a store, and would print a Deezer search
# result as the candidate their claim was compared against.
#
# Listed positively rather than by excluding 'enrich', so that the next phase
# written to this table cannot leak into the panel the same way. The schema
# already names dedup, identity and backfill alongside them.
CLAIM_DECISION_PHASES = ("claim",)

# Rows in the first response, and rows added by one press of "load more". The
# figure matches the queue API's default page size so that a reader paging
# through the interface and a client paging through JSON see the same windows.
PAGE_SIZE = 60

# Ceiling on ?show=. A hand-edited URL asking for ten thousand rows would undo
# the windowing and hand the browser back the load it was built to avoid.
MAX_SHOW = 600


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def _component(name: str) -> Any:
    """Reach one collaborator that app.py registered.

    Tolerates the two shapes an application factory tends to use, an extensions
    object with attributes and a plain dict, so this module does not have to be
    edited when that decision is made next door.
    """
    ext = current_app.extensions.get("libwish")
    if ext is not None:
        if isinstance(ext, dict) and name in ext:
            return ext[name]
        if hasattr(ext, name):
            return getattr(ext, name)
    return current_app.config.get(f"LW_{name.upper()}")


def _tracks() -> Any:
    repo = _component("tracks")
    if repo is None:
        raise RuntimeError(
            "no track repository: app.py must set app.extensions['libwish'].tracks"
            " to a libwish.repo.TrackRepo"
        )
    return repo


def _jobs() -> Any:
    return _component("jobs")


# The files the page loads on every view, and the ones the service worker
# precaches. One list, because the cache key and the precache list have to
# cover exactly the same files: anything served under a key that does not
# fingerprint it is a file the worker can hold a stale copy of forever.
SHELL_ASSETS = (
    "css/app.css",
    "js/htmx.min.js",
    "js/alpine.min.js",
    "js/libwish.js",
)


def _asset_version() -> str:
    """Cache key derived from the content of everything the shell loads.

    Fingerprinting the files rather than stamping a build number means a
    redeploy that changed nothing does not force every client to re-download.

    All of them, not the stylesheet alone. Under an ordinary HTTP cache a key
    that missed a file cost a revalidation; under a worker that answers from
    cache first, it means a JavaScript-only change is invisible to every
    browser that has already installed, and stays invisible until something
    unrelated edits the CSS.
    """
    digest = hashlib.sha256()
    for path in SHELL_ASSETS:
        try:
            digest.update((STATIC_DIR / path).read_bytes())
        except OSError:
            return "dev"
    return digest.hexdigest()[:10]


_ASSET_V: str | None = None


def asset(path: str) -> str:
    global _ASSET_V
    if _ASSET_V is None:
        _ASSET_V = _asset_version()
    return f"/static/{path}?v={_ASSET_V}"


# ---------------------------------------------------------------------------
# presentation helpers
# ---------------------------------------------------------------------------


def _fmt_clock(epoch: int | None) -> str:
    """How long ago a track was loved, as an age rather than a time of day.

    A bare "21:18" beside a track title reads as a runtime, because that is what
    a number in that shape means everywhere else in a music interface. An age
    cannot be misread that way, and it is also the thing worth knowing here: no
    reader cares which minute a track was queued, only whether it is new.
    """
    if not epoch:
        return ""
    seconds = max(0, int(time.time()) - int(epoch))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    if days < 31:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 31}mo ago"
    return f"{days // 365}y ago"


def _fmt_day(epoch: int | None) -> str:
    if not epoch:
        return "UNDATED"
    return datetime.fromtimestamp(epoch).strftime("%a %-d %b").upper()


def _fmt_stamp_date(epoch: int | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch).strftime("%d.%m.%y")


def _fmt_duration(ms: int | None) -> str:
    """How long the recording runs, or "" when nothing has measured it.

    Enrichment fills `duration_ms` where Deezer matched the track confidently.
    A row it declined, or that no sweep has reached, holds NULL, and printing
    0:00 there would state a fact about a recording nobody has measured. The
    template pairs whatever comes back with the word "runs", because a bare
    4:12 beside a track name is read as a runtime by default, which is exactly
    how the loved-at timestamp became unreadable.
    """
    if not ms or ms < 1000:
        return ""
    seconds = int(ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def tier_for(quality: dict | None) -> dict | None:
    """Segments and label for a file we hold, or None when we hold no file.

    `quality` is what the claim pipeline recorded about the bytes on disk:
    codec, bit depth, sample rate. Absent means absent. There is deliberately no
    branch here that infers a tier from a store listing.
    """
    if not quality:
        return None
    codec = (quality.get("codec") or "").lower()
    bits = quality.get("bit_depth")
    rate = quality.get("sample_rate")
    if codec and codec not in ("flac", "alac", "wav", "aiff", "dsd"):
        return {"segments": 1, "label": f"{codec.upper()} {quality.get('bitrate') or ''}".strip()}
    if not bits or not rate:
        return None
    khz = round(rate / 1000, 1)
    khz_label = f"{int(khz)}" if float(khz).is_integer() else f"{khz}"
    label = f"{bits}/{khz_label} {codec.upper()}".strip()
    if bits >= 24 and rate > 96000:
        segments = 4
    elif bits >= 24:
        segments = 3
    elif rate <= 48000:
        segments = 2
    else:
        segments = 2
    return {"segments": segments, "label": label}


#: Every field the plate macro reads. Each branch of `plate_for` fills all of
#: them, so the template never has to ask whether a key is there: the macro
#: renders one rectangle and only its contents change.
PLATE_BLANK: dict[str, Any] = {
    "state": "no_store", "l1": "", "l1r": None, "l2": None, "l2r": None,
    "l2_icon": None, "step": None, "of": None, "tier": None, "mark": None,
}


def plate_for(track: dict) -> dict:
    """The plate's fields for one track row.

    States, in the order this decides them: a running claim wins over
    everything, then a refusal, then ownership, then whether the row has a store
    to buy at.
    """
    job = track.get("job") or {}
    refusal = track.get("refusal")
    status = track.get("status")

    if job.get("state") == "running" or job.get("phase"):
        phase = job.get("phase") or PHASE_ORDER[0]
        stage = CLAIM_STAGES.get(phase, {"label": phase.upper()})
        step = PHASE_ORDER.index(phase) + 1 if phase in PHASE_ORDER else 1
        return {**PLATE_BLANK,
                "state": "working",
                "l1": stage["label"],
                "l2": f"{step} OF {len(PHASE_ORDER)}",
                "step": step,
                "of": len(PHASE_ORDER)}

    if refusal:
        return {**PLATE_BLANK,
                "state": "refused",
                "l1": "MATCH REFUSED",
                "l2": f"{refusal['score']} / {refusal['threshold']}"}

    if status in ("purchased", "owned"):
        tier = tier_for(track.get("quality"))
        return {**PLATE_BLANK,
                "state": "owned",
                # No recorded file details means the plate says only what it
                # knows, which is that this is owned. It does not invent a
                # format to fill the line.
                "l1": tier["label"] if tier else "OWNED",
                "l2": (track.get("purchased_via") or "").upper(),
                "l2r": _fmt_stamp_date(track.get("purchased_at")),
                "tier": tier,
                # The mark is the stamp's word. With a tier recorded the first
                # line names the quality and the mark says OWNED beneath it.
                # With no tier the first line already says OWNED, and repeating
                # it makes a deliberate tilt read as a rendering fault rather
                # than as a stamp.
                "mark": "OWNED" if tier else ""}

    # NO STORE means nowhere to buy this, not "you have not picked one yet".
    # Only a row the user has explicitly assigned carries a store of its own, so
    # reading that alone reported every unassigned track as unbuyable while two
    # stores sat configured and able to sell it.
    if not _stores():
        return {**PLATE_BLANK,
                "state": "no_store",
                "l1": "NO STORE",
                "l2": "SET UP",
                "l2_icon": "arrow-up-right"}

    # The state, and nothing drawn for it. Wanted is what every row on the
    # front page is, so a badge repeating it beside a button that says "Buy at"
    # is the third place on one line to say the same thing. Where the stores
    # are named is the buy menu, which is where the choice between them is
    # actually made.
    return {**PLATE_BLANK, "state": "wanted", "l1": "WANTED"}


def _stores() -> list[dict]:
    """Configured stores, by id and name, in one order everywhere.

    Read per request rather than cached, because a store appearing or
    disappearing should change what the page says without a restart. Sorted by
    name so the plate, the buy menu and the bulk selector agree on the order:
    two lists of the same stores in different orders read as two different
    facts.
    """
    stores = _component("stores") or {}
    try:
        return sorted(
            ({"id": key, "name": getattr(s, "name", key)} for key, s in stores.items()),
            key=lambda s: s["name"].lower(),
        )
    except Exception:
        return []


def group_key_for(row: dict, by: str) -> str:
    """Which bucket a row belongs to, from the raw row.

    Readable before decoration so that a window over the list can be cut, and
    its group headers counted, without turning every queued row into a view
    model first.
    """
    if by == "artist":
        return row.get("artist") or "Unknown artist"
    return _fmt_day(row.get("added_at"))


def view_track(row: dict, group_by: str = "artist") -> dict:
    """One database row, plus everything the templates need that it lacks.

    `quality`, `quality_note` and `file_path` are passed through untouched.
    Nothing populates them in v1, and every template guards on their absence, so
    the claim pipeline can start recording what it wrote to disk without a
    change on this side.
    """
    t = dict(row)
    for field in OPTIONAL_FIELDS:
        t.setdefault(field, None)
    source = t.get("source_platform") or t.get("source") or ""
    clock = _fmt_clock(t.get("added_at"))
    t["origin"] = " · ".join(p for p in (source.replace("_", " "), clock) if p)
    t["day"] = _fmt_day(t.get("added_at"))
    t["duration"] = _fmt_duration(t.get("duration_ms"))
    # The store a retry has to go back to, not the platform the love came
    # from: `chosen_source` names Last.fm or ListenBrainz, and a track never
    # gets a `store` column of its own, because nothing is assigned to a shop
    # until a claim job actually names one. `last_claim_store` is that job's
    # provider_id, read off the query in repo.py.
    t["store_id"] = t.get("last_claim_store") or ""
    # Where this can be bought. A row assigned to one store offers that store
    # and nothing else; a row assigned to none offers every store configured,
    # because "nobody has picked yet" is not the same fact as "nowhere sells
    # it" and the interface used to render them identically.
    configured = _stores()
    if t["store_id"]:
        t["stores"] = [s for s in configured if s["id"] == t["store_id"]] or configured
    else:
        t["stores"] = configured
    t["store_label"] = t["stores"][0]["name"] if len(t["stores"]) == 1 else ""
    t["cover_src"] = API["cover"].format(id=t.get("id"))
    t["plate"] = plate_for(t)
    t["state"] = t["plate"]["state"]
    # Which rows a bulk confirm may include. A row with nowhere to buy from has
    # nothing to claim and a row mid-claim is already going, so neither takes a
    # checkbox: an unusable checkbox in a selection column is a promise the
    # confirm button cannot keep.
    t["selectable"] = bool(t["stores"]) and t["state"] in ("wanted", "refused")
    # The group this row belongs to, decided here rather than in the browser, so
    # a row arriving over the live connection lands under the right header
    # without the client having to reimplement the grouping rule.
    t["group_key"] = group_key_for(t, group_by)
    return t


def group_tracks(tracks: Iterable[dict], by: str) -> list[dict]:
    """Rows bucketed into the sticky-headed groups the rack renders.

    Grouping is a control rather than a fixed rule because loving forty tracks
    in one sitting makes a date group meaningless, and buying is release-shaped,
    so the artist grouping puts the things you would purchase together next to
    each other. 06 offers a tier grouping as the second option; that needs a
    promised tier, which no shipping store supplies, so artist takes its place.
    """
    buckets: dict[str, list[dict]] = {}
    for t in tracks:
        buckets.setdefault(t["group_key"], []).append(t)
    return [{"key": k, "tracks": v, "n": len(v)} for k, v in buckets.items()]


def order_rows(rows: list[dict], by: str) -> list[dict]:
    """Rows in the order their grouping needs.

    The repository answers in love order, which is what a date grouping wants
    and what an artist grouping cannot use: one artist's tracks would be
    scattered down the whole list, so a group would open, close and reopen, and
    every window over the list would reopen groups the previous one had already
    closed. Sorting by artist makes each bucket contiguous, which is what lets a
    page be a window rather than a second query.
    """
    if by != "artist":
        return list(rows)
    return sorted(
        rows,
        key=lambda r: ((r.get("artist") or "").casefold(),
                       -(r.get("added_at") or 0),
                       r.get("id") or 0),
    )


def rack_window(rows: list[dict], group_by: str,
                offset: int = 0, limit: int = PAGE_SIZE) -> tuple[list[dict], int]:
    """Group headers and decorated rows for one window over a list.

    Each header carries the size of the whole group rather than the part of it
    this window reached, so a header never contradicts itself when the next
    window fills in underneath. Only the rows in the window are decorated, which
    is what keeps one refusal lookup per rendered row from becoming one per
    queued row.
    """
    ordered = order_rows(rows, group_by)
    buckets: dict[str, list[dict]] = {}
    for row in ordered:
        buckets.setdefault(group_key_for(row, group_by), []).append(row)

    groups: list[dict] = []
    skip, left = max(0, offset), max(0, limit)
    for key, bucket in buckets.items():
        if left <= 0:
            break
        if skip >= len(bucket):
            skip -= len(bucket)
            continue
        take = bucket[skip:skip + left]
        skip = 0
        left -= len(take)
        groups.append({"key": key, "n": len(bucket),
                       "tracks": _decorate(take, group_by)})
    return groups, len(ordered)


def _counts() -> dict[str, int]:
    try:
        return _tracks().view_counts()
    except Exception:  # noqa: BLE001 - countless tabs beat a 500
        current_app.logger.exception("could not read track counts")
        return {}


def _claim_jobs() -> tuple[dict[int, dict], dict[int, dict]]:
    """Claims in flight and the most recent claim that broke, keyed by track.

    Both survive a page reload on purpose. A claim that vanished from the screen
    when the browser refreshed would look like it had finished, and a failure
    that vanished would look like nothing had ever been tried.
    """
    jobs = _jobs()
    if jobs is None:
        return {}, {}
    live: dict[int, dict] = {}
    broke: dict[int, dict] = {}
    for job in jobs.recent(limit=120):
        track_id = job.get("track_id")
        if job.get("kind") != "claim" or track_id is None:
            continue
        if job.get("state") in ("queued", "running"):
            live.setdefault(track_id, job)
        elif job.get("state") in ("failed", "interrupted"):
            broke.setdefault(track_id, job)
    return live, broke


def failure_for(job: dict) -> dict:
    """What a broken claim tells the user: the stage, what happened, what to do.

    An interrupted job is not a failure and is not worded as one. It may already
    have spent money, and telling someone their claim failed is how they end up
    buying the same track twice.
    """
    phase = job.get("phase") or ""
    if job.get("state") == "interrupted":
        return {
            "phase": phase,
            "text": "This claim was cut off when the server restarted. Check whether the"
                    " file arrived before claiming again.",
        }
    copy = FAILURE_COPY.get(phase)
    if copy is None:
        return {"phase": phase, "text": job.get("error") or "The claim stopped before it finished."}
    store = (job.get("provider_id") or "your store").title()
    entry: dict[str, Any] = {"phase": phase, "text": copy["text"].format(store=store)}
    for label, href in copy["actions"]:
        if href:
            entry["link"], entry["link_label"] = href, label
            break
    return entry


def _payload(raw: str | None) -> dict:
    """One stored provider payload, or an empty dict.

    A payload that will not parse is not worth a 500 on a screen whose job is to
    show its work: the rest of the record still names the score, the threshold
    and the store.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def refusal_for(track_id: int, conn: Any = None) -> dict | None:
    """The last undismissed claim refusal recorded against a track, or None.

    Only the phases in `CLAIM_DECISION_PHASES`, because this feeds a panel that
    states a purchase was refused and that nothing was downloaded. Enrichment
    records its own refusals in the same table against the same tracks, and one
    of those shown here would be a false claim about the reader's money.

    A dismissed row is skipped rather than the newest row unconditionally,
    which is what lets a reader clear a refusal off the screen and still have a
    later claim refuse again: dismissal marks one decision acknowledged, not
    the track immune to ever showing this panel again.

    The losing candidates live in `match_candidate`, keyed by decision, rather
    than in a column on the decision itself.
    """
    # A caller rendering a window passes its own connection. Opening one per row
    # meant 139 connections for a single page, each re-applying pragmas, and
    # each able to wait out the 15 second busy timeout behind the enrichment
    # writer. Switching tabs quickly was enough to stall the whole application.
    owned_conn = conn is None
    if owned_conn:
        db = _component("db")
        if db is None:
            return None
        conn = db()
    marks = ", ".join("?" for _ in CLAIM_DECISION_PHASES)
    try:
        row = conn.execute(
            "SELECT id, score, candidate_json, reasons, matched_via,"
            " gate_failed, decided_at, provider"
            " FROM match_decision"
            f" WHERE track_id=? AND outcome='refused' AND dismissed_at IS NULL"
            f" AND phase IN ({marks})"
            " ORDER BY decided_at DESC, id DESC LIMIT 1",
            (track_id, *CLAIM_DECISION_PHASES),
        ).fetchone()
        others = [] if row is None else [dict(r) for r in conn.execute(
            "SELECT score, gate_failed, candidate_json FROM match_candidate"
            " WHERE decision_id=? ORDER BY rank LIMIT 3",
            (row["id"],),
        ).fetchall()]
    except sqlite3.OperationalError as exc:
        # An absent table is a database from before migration 0003 and is the
        # one case worth tolerating quietly. Every other shape of this error is
        # this query disagreeing with the schema, which is a bug here, and it
        # has to announce itself: the failure it produces is an empty refusal
        # panel, and an empty panel reads as "the claim was never tried" rather
        # than "the record could not be read". A column named wrongly here went
        # unnoticed for exactly that reason.
        if "no such table" not in str(exc):
            current_app.logger.error(
                "the refusal query does not match the schema, track %s: %s", track_id, exc)
        return None
    finally:
        if owned_conn:
            conn.close()
    if not row:
        return None

    row = dict(row)
    candidate = _payload(row.get("candidate_json"))
    losers = []
    for other in others:
        payload = _payload(other.get("candidate_json"))
        losers.append({
            "score": other.get("score"),
            "gate_failed": other.get("gate_failed"),
            "title": payload.get("title") or payload.get("track_title") or "",
            "artist": payload.get("artist") or "",
        })
    from ..models import MATCH_AUTO_MIN

    return {
        "score": row.get("score") or 0,
        "threshold": MATCH_AUTO_MIN,
        "title": candidate.get("title") or candidate.get("track_title") or "",
        "artist": candidate.get("artist") or "",
        "store": (row.get("provider") or "").upper(),
        "reasons": row.get("reasons") or "",
        "decided_at": row.get("decided_at"),
        "candidates": losers,
    }


def _enrich_gaps(rows: list[dict]) -> set[int]:
    """Tracks whose metadata lookup came back inconclusive, of those asked for.

    An enrichment refusal is quiet news rather than an alarm, and it is worth
    saying: it means nothing established how long this recording runs, so the
    matcher's duration veto has no length to check a candidate against and one
    of the gates protecting the row is not armed.

    One query for the window, not one per row, and only for rows that have no
    runtime: a track with a duration was matched confidently and has nothing to
    explain.
    """
    ids = [r["id"] for r in rows if r.get("duration_ms") is None and r.get("id")]
    if not ids:
        return set()
    db = _component("db")
    if db is None:
        return set()
    marks = ", ".join("?" for _ in ids)
    conn = _request_conn()
    if conn is None:
        return set()
    try:
        found = conn.execute(
            "SELECT DISTINCT track_id FROM match_decision"
            f" WHERE phase='enrich' AND outcome='refused' AND track_id IN ({marks})",
            tuple(ids),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            current_app.logger.error(
                "the enrichment audit query does not match the schema: %s", exc)
        return set()
    return {r["track_id"] for r in found}


def _request_conn() -> Any:
    """One database connection per request, closed when the request ends.

    Grouping calls the decorator once per group, and an artist-grouped window of
    60 rows has around 54 of them, so a connection opened inside that work was
    opened 54 times. Each open re-applies pragmas and can wait out the busy
    timeout behind a writer, which is what made switching tabs quickly stall the
    whole application.
    """
    from flask import g

    conn = getattr(g, "_lw_conn", None)
    if conn is None:
        db = _component("db")
        if db is None:
            return None
        conn = db()
        g._lw_conn = conn
    return conn


def _request_cache(key: str, produce):
    """Compute something once per request.

    The same per-group repetition applies to the job and enrichment lookups:
    their answers cannot change within one render, so computing them per group
    is repeated work with no chance of a different result.
    """
    from flask import g

    cache = getattr(g, "_lw_cache", None)
    if cache is None:
        cache = {}
        g._lw_cache = cache
    if key not in cache:
        cache[key] = produce()
    return cache[key]


def _decorate(rows: list[dict], group_by: str = "artist") -> list[dict]:
    live, broke = _request_cache("claim_jobs", _claim_jobs)
    gaps = _enrich_gaps(rows)
    return _decorate_rows(rows, group_by, live, broke, gaps, _request_conn())


def _decorate_rows(rows, group_by, live, broke, gaps, conn) -> list[dict]:
    out = []
    for row in rows:
        row = dict(row)
        row["duration_unknown"] = row["id"] in gaps
        job = live.get(row["id"])
        if job:
            row["job"] = job
        elif row.get("status") == "queued":
            # A refusal outranks a plain failure: it is the state the user can
            # actually act on, and it carries the evidence the failure panel
            # does not have. `refusal_for` already excludes a dismissed
            # decision, so dismissing a refusal falls straight through to the
            # failure panel below when there is a failed job to show, exactly
            # as if the refusal had never been recorded.
            row["refusal"] = refusal_for(row["id"], conn)
            if not row["refusal"] and row["id"] in broke:
                row["failure"] = failure_for(broke[row["id"]])
        out.append(view_track(row, group_by))
    return out


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


@bp.record_once
def _install_globals(state) -> None:
    """Publish the shared names as Jinja globals rather than as a context
    processor.

    Macros are the reason. Jinja imports a macro file without the calling
    template's context unless every import says `with context`, so anything
    delivered by a context processor is invisible inside `plate()`, `row()` and
    `detail()`, which is where most of it is read. Globals are visible
    everywhere and cannot be forgotten at one import site.
    """
    state.app.jinja_env.globals.update(
        asset=asset,
        API=API,
        CLAIM_STAGES=CLAIM_STAGES,
        PHASE_ORDER=PHASE_ORDER,
        claim_stages_json=json.dumps({"stages": CLAIM_STAGES, "order": list(PHASE_ORDER)}),
        music_dir=state.app.config.get("LW_MUSIC_DIR", "/music"),
    )


@bp.app_context_processor
def _inject() -> dict:
    return {"now": int(time.time())}


@bp.route("/")
def wanted():
    counts = _counts()
    if not counts.get("wanted") and not counts.get("owned") and not counts.get("ignored"):
        return render_template("pages/first_run.html", counts=counts, view="wanted")
    group_by = _group_arg()
    show = _show_arg()
    query = _search_arg()
    groups, total = rack_window(_tracks().queued(query), group_by, 0, show)
    return render_template(
        "pages/wanted.html",
        view="wanted",
        q=query,
        counts=counts,
        group_by=group_by,
        groups=groups,
        total=total,
        shown=min(show, total),
        page_size=PAGE_SIZE,
    )


@bp.route("/owned")
def owned():
    tracks = _decorate(_tracks().owned(_search_arg()), "date")
    return render_template(
        "pages/owned.html",
        view="owned",
        counts=_counts(),
        groups=group_tracks(tracks, "date"),
        total=len(tracks),
    )


@bp.route("/ignored")
def ignored():
    tracks = [view_track(dict(r), "date") for r in _tracks().ignored(_search_arg())]
    return render_template(
        "pages/ignored.html", view="ignored", counts=_counts(), tracks=tracks
    )


@bp.route("/setup")
def setup():
    return render_template("pages/first_run.html", counts=_counts(), view="setup")


# ---------------------------------------------------------------------------
# installation
#
# Both of these are served from the root rather than out of /static, and for
# the same reason in each case: a service worker may only control the part of
# the site at or below the path it was served from, so one delivered from
# /static/js/ could see nothing but its own directory. The manifest follows it
# up here so the pair reads as one thing.
# ---------------------------------------------------------------------------


# Precached on install alongside SHELL_ASSETS, which are addressed by build
# digest. These are requested bare, because the stylesheet and the manifest ask
# for them that way, so a change to one of these does need a hard reload.
SHELL_BARE = (
    "/static/fonts/archivo-var-latin.woff2",
    "/static/icons/icon-192.png",
    "/static/icons/favicon.svg",
    "/static/offline.html",
)


@bp.route("/sw.js")
def service_worker():
    body = render_template(
        "sw.js",
        version=asset("css/app.css").rsplit("=", 1)[-1],
        precache=[asset(p) for p in SHELL_ASSETS] + list(SHELL_BARE),
        events_path=API["events"],
        offline_url="/static/offline.html",
    )
    response = current_app.response_class(body, mimetype="text/javascript")
    # Browsers revalidate a worker on every navigation anyway; saying so keeps
    # a proxy in the middle from holding an old one past a deploy.
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/manifest.webmanifest")
def manifest():
    response = current_app.send_static_file("manifest.webmanifest")
    # Named explicitly: the extension is not in Python's mimetypes table, so
    # the guess is text/plain and a browser will not read it as a manifest.
    response.mimetype = "application/manifest+json"
    return response


# ---------------------------------------------------------------------------
# fragments
#
# The live connection carries JSON, not markup. When an event says a row
# changed, the client fetches the affected fragment from here, so the server
# stays the only thing that decides what a row looks like. A plate resolution
# repaints 132 pixels rather than the row, which is what keeps focus and audio
# playback alive through an update.
# ---------------------------------------------------------------------------


def _search_arg() -> str:
    """The search box's current value.

    Searching happens in the database rather than over the rows already on
    screen. A window holds 60 of 160, so a filter applied to what is loaded
    answers a question nobody asked: "does this word appear in the part you
    happen to have". Capped because the value goes into a LIKE pattern and a
    pathologically long one is only ever an accident or an attack.
    """
    return (request.args.get("q") or "").strip()[:120]


def _group_arg() -> str:
    """Which grouping a request asked for, artist unless it said otherwise.

    Artist is the default because the date a row carries is the moment an
    import ran, not the moment anything was loved: in the queue this was built
    against, 156 of 160 rows share a single timestamp, so a date grouping puts
    almost the whole list under one header and the header stops being a
    landmark. Artist spreads the same rows across real buckets and puts the
    tracks you would buy together next to each other. Date stays on the control
    for the case a date grouping is meaningful, which is loves arriving live.
    """
    group = request.args.get("group", "artist")
    return group if group in ("date", "artist") else "artist"


def _show_arg() -> int:
    """How many rows this response carries, from ?show=.

    Present so that the load-more control works with scripting off: it is an
    ordinary link to a longer page. With scripting on, the client asks for the
    next window instead and appends it, which is cheaper and keeps the reader's
    position.
    """
    raw = request.args.get("show", "")
    try:
        want = int(raw)
    except ValueError:
        return PAGE_SIZE
    return max(PAGE_SIZE, min(want, MAX_SHOW))


def _one(track_id: int) -> dict:
    row = _tracks().get(track_id)
    if row is None:
        abort(404)
    return _decorate([row], _group_arg())[0]


@bp.route("/ui/row/<int:track_id>")
def ui_row(track_id: int):
    return render_template(
        "partials/row.html", t=_one(track_id), view=request.args.get("view", "wanted")
    )


@bp.route("/ui/rows")
def ui_rows():
    raw = request.args.get("ids", "")
    ids = [int(part) for part in raw.split(",") if part.strip().isdigit()][:200]
    repo = _tracks()
    rows = [r for r in (repo.get(i) for i in ids) if r]
    tracks = _decorate(rows, _group_arg())
    return render_template(
        "partials/rows.html", tracks=tracks, view=request.args.get("view", "wanted")
    )


@bp.route("/ui/page")
def ui_page():
    """The next window of the want list, headers and rows.

    Answers the load-more control. The client appends this below what is
    already on screen and never re-renders what is there, so asking for more
    cannot move a row someone is reading.
    """
    group_by = _group_arg()
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        limit = max(1, min(int(request.args.get("limit", PAGE_SIZE)), MAX_SHOW))
    except ValueError:
        limit = PAGE_SIZE
    groups, total = rack_window(_tracks().queued(_search_arg()), group_by, offset, limit)
    response = current_app.response_class(
        render_template("partials/page.html", groups=groups,
                        view=request.args.get("view", "wanted")),
        mimetype="text/html",
    )
    # The client tracks how much of the list it holds and hides the control
    # when it holds all of it. Sending the count with the rows means it never
    # has to guess from a short page.
    response.headers["X-Total-Count"] = str(total)
    return response


@bp.route("/ui/plate/<int:track_id>")
def ui_plate(track_id: int):
    return render_template("partials/plate.html", t=_one(track_id))


@bp.route("/ui/detail/<int:track_id>")
def ui_detail(track_id: int):
    return render_template("partials/detail.html", t=_one(track_id))
