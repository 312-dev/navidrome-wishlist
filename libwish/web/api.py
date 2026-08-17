"""The JSON API and the event stream.

Every route here is thin. It validates its input, calls into a service, and
returns. Anything that could take longer than a request becomes a job, so the
browser never holds a connection open waiting for a purchase to be verified and
a download to finish. The browser learns the outcome over the event stream
instead.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import (
    Blueprint, Response, current_app, jsonify, redirect, request, send_file,
    stream_with_context,
)

from ..log import get

log = get("api")

bp = Blueprint("api", __name__, url_prefix="/api")

# One screenful at a time. The ceiling is what stops `?limit=100000` from
# asking the process to serialise the whole table into one response, which is
# the same cost as having no pagination at all.
DEFAULT_LIMIT = 60
MAX_LIMIT = 200

# Cover art keyed by track id, under the config volume. The layout is shared
# with the cache that writes the files.
COVER_DIR = "covers"

# A week. The point of caching a cover is that the browser stops asking Deezer
# for it, and a short lifetime would put most of those requests back. The ETag
# is what keeps a replaced file from being served stale forever once the week
# is up.
COVER_MAX_AGE = 604_800

COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif"}

# Confirming a refusal is a person taking responsibility for a purchase, so a
# batch is capped at a number someone could plausibly have read. Past the cap
# the response says how many were left, because a bulk action that silently
# drops the tail reads exactly like one that worked.
MAX_CONFIRM_BATCH = 50


def _svc():
    """The service bundle attached to the app at construction."""
    return current_app.extensions["libwish"]


@bp.get("/queue")
def queue():
    return _window(_svc().tracks.queued(request.args.get("q", "")))


@bp.get("/ignored")
def ignored():
    return _window(_svc().tracks.ignored(request.args.get("q", "")))


@bp.get("/owned")
def owned():
    return _window(_svc().tracks.owned(request.args.get("q", "")))


@bp.get("/counts")
def counts():
    """How many tracks each tab holds.

    Cheap enough to ask for on every membership change, which is what the tabs
    do rather than counting the change themselves. A number kept by adding and
    subtracting drifts the first time an event is missed, and this list has
    three places a track can move to and a live connection that is allowed to
    drop events under load.
    """
    return jsonify(_svc().tracks.view_counts())


def _window(rows: list[dict]):
    """One page of `rows`, with the unpaginated total in `X-Total-Count`.

    The body stays a bare array so a caller that ignores paging still gets the
    shape it always got. The count goes in a header for the same reason.

    The slice is taken here because the repo methods take no arguments, which
    means SQLite still returns every row and this only limits what crosses the
    wire. Pushing LIMIT and OFFSET into `TrackRepo` is the next step; the
    header does not change when that happens, only where the total is counted.
    """
    try:
        limit = min(_count_arg("limit", DEFAULT_LIMIT), MAX_LIMIT)
        offset = _count_arg("offset", 0)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    resp = jsonify(rows[offset:offset + limit])
    resp.headers["X-Total-Count"] = str(len(rows))
    return resp


def _count_arg(name: str, default: int) -> int:
    """A non-negative integer query parameter, or a refusal.

    Rejected rather than coerced. `limit=-1` and `limit=all` are both someone
    expecting behaviour this does not have, and quietly handing them the
    default would answer a question they did not ask.
    """
    raw = request.args.get(name)
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()):
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}")
    return int(raw)


@bp.get("/track/<int:track_id>")
def track(track_id: int):
    row = _svc().tracks.get(track_id)
    if row is None:
        return jsonify({"error": "no such track"}), 404
    # `subtitle` alongside the row's own fields, for the Cookie Broker banner.
    # That protocol describes an item as a title and a secondary line, and
    # which of this app's columns is the secondary line is this app's business
    # to say. Added rather than substituted: everything else reading this route
    # wants the columns it already knows.
    return jsonify({**dict(row), "subtitle": row["artist"]})


@bp.post("/ignore/<int:track_id>")
def ignore(track_id: int):
    svc = _svc()
    if svc.tracks.get(track_id) is None:
        return jsonify({"error": "no such track"}), 404
    svc.tracks.set_status(track_id, "ignored")
    svc.bus.publish("track.updated", id=track_id, status="ignored")
    return jsonify({"ok": True})


@bp.post("/restore/<int:track_id>")
def restore(track_id: int):
    svc = _svc()
    if svc.tracks.get(track_id) is None:
        return jsonify({"error": "no such track"}), 404
    svc.tracks.set_status(track_id, "queued")
    svc.bus.publish("track.updated", id=track_id, status="queued")
    return jsonify({"ok": True})


def _store_arg() -> str | None:
    """Which store a request named, however it was sent.

    The page posts these with htmx, which form-encodes by default, so reading
    only a JSON body silently loses the store the reader picked and hands the
    claim to a queue that then refuses it for naming none. Accepting both is
    one line here against a JSON-encoding extension on every posting element.
    """
    body = request.get_json(silent=True) or {}
    return body.get("store") or request.values.get("store") or None


@bp.post("/claim/<int:track_id>")
def claim(track_id: int):
    """Start a claim. Returns immediately with a job id.

    The work is deliberately not done inline. A claim contacts a store, matches,
    downloads and verifies, which is far longer than a request should hold open,
    and a browser that gives up partway would leave no record of what happened.
    """
    svc = _svc()
    if svc.tracks.get(track_id) is None:
        return jsonify({"error": "no such track"}), 404
    store_id = _store_arg()
    job_id = svc.jobs.enqueue("claim", track_id=track_id, provider_id=store_id)
    return jsonify({"ok": True, "job_id": job_id}), 202


@bp.post("/claim/<int:track_id>/confirm")
def claim_confirm(track_id: int):
    """Claim a track the matcher refused, on the user's explicit say-so.

    A separate endpoint rather than a flag on the ordinary claim, because it is
    a different act: someone looked at a refusal, disagreed with it, and took
    responsibility. Recording it under its own outcome is what keeps the audit
    honest about which purchases the software chose and which a person did.

    The audit row names the purchase that was on screen, so the claim downloads
    that one. Confirming is agreement with a particular candidate, and rescoring
    the account afterwards would answer a question nobody asked.
    """
    svc = _svc()
    if svc.tracks.get(track_id) is None:
        return jsonify({"error": "no such track"}), 404
    body = request.get_json(silent=True) or {}
    store_id = _store_arg()
    conn = svc.db()
    try:
        from .. import identity, match
        from .views import shown_candidate_key

        row = svc.tracks.get(track_id)
        # Which purchase the panel was showing when it was confirmed. Carried
        # so the claim downloads that item rather than rescoring the account
        # and refusing the same near miss all over again.
        key = shown_candidate_key(conn, track_id)
        conn.execute(
            "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
            " matcher_version, lexicon_hash, outcome, reasons, query_json,"
            " candidate_json, candidates_considered, chosen_store_id)"
            " VALUES(?,?,?,?,?,?,'user_confirmed',?,?,?,0,?)",
            (track_id, int(time.time()), "confirm", store_id or "user",
             getattr(match, "MATCHER_VERSION", "1"), identity.lexicon_hash(),
             (body.get("note") or "confirmed by the user after a refusal")[:2000],
             json.dumps({"artist": row["artist"], "title": row["title"]}),
             json.dumps({"item_key": key}) if key else None,
             store_id),
        )
    finally:
        conn.close()
    job_id = svc.jobs.enqueue("claim", track_id=track_id, provider_id=store_id)
    log.info("user confirmed a refused match", context={"track": track_id, "job": job_id})
    return jsonify({"ok": True, "job_id": job_id, "confirmed": True}), 202


@bp.post("/claim/confirm")
def claim_confirm_bulk():
    """Confirm a set of refused matches in one act.

    The same thing the single-track route does, once per track: an audit row
    under `user_confirmed` and a claim job. It exists because nothing in the
    queue carries an MBID or an ISRC, so the matcher stops at the confirm band
    on effectively all of it, and clearing that one track at a time is an
    afternoon of clicking.

    An unknown id fails the whole request rather than part of it. A response
    reporting success for some rows and leaving the caller to work out which is
    worse than being told to send a corrected list.
    """
    svc = _svc()
    body = request.get_json(silent=True) or {}
    raw = body.get("track_ids")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "track_ids must be a non-empty list of track ids"}), 400
    ids: list[int] = []
    for value in raw:
        # bool is an int in Python and `true` in a JSON body is not a track id.
        if isinstance(value, bool) or not isinstance(value, int):
            return jsonify({"error": f"track_ids must be integers, got {value!r}"}), 400
        if value not in ids:
            ids.append(value)

    requested = len(ids)
    accepted = ids[:MAX_CONFIRM_BATCH]
    rows = {}
    unknown = []
    for track_id in accepted:
        row = svc.tracks.get(track_id)
        if row is None:
            unknown.append(track_id)
        else:
            rows[track_id] = row
    if unknown:
        return jsonify({"error": "no such track", "unknown": unknown}), 404

    store_id = body.get("store")
    note = str(body.get("note") or "confirmed by the user after a refusal")[:2000]
    from .. import identity, match
    from .views import shown_candidate_key
    from ..db import transaction

    now = int(time.time())
    version = getattr(match, "MATCHER_VERSION", "1")
    lexicon = identity.lexicon_hash()
    conn = svc.db()
    try:
        # One transaction for the batch: a half-written audit trail would claim
        # a person confirmed tracks they were never asked about.
        with transaction(conn):
            for track_id, row in rows.items():
                # Per track, because each one was refused against its own
                # candidate. See the single-track route.
                key = shown_candidate_key(conn, track_id)
                conn.execute(
                    "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
                    " matcher_version, lexicon_hash, outcome, reasons, query_json,"
                    " candidate_json, candidates_considered, chosen_store_id)"
                    " VALUES(?,?,?,?,?,?,'user_confirmed',?,?,?,0,?)",
                    (track_id, now, "confirm", store_id or "user", version, lexicon, note,
                     json.dumps({"artist": row["artist"], "title": row["title"]}),
                     json.dumps({"item_key": key}) if key else None,
                     store_id),
                )
    finally:
        conn.close()

    job_ids = [svc.jobs.enqueue("claim", track_id=track_id, provider_id=store_id)
               for track_id in accepted]
    payload = {"ok": True, "confirmed": len(accepted), "job_ids": job_ids,
               "truncated": requested > len(accepted)}
    if payload["truncated"]:
        payload["requested"] = requested
        payload["cap"] = MAX_CONFIRM_BATCH
        payload["msg"] = (f"only the first {MAX_CONFIRM_BATCH} of {requested} tracks were"
                          " confirmed; send the rest in another request")
    log.info("user confirmed refused matches in bulk",
             context={"tracks": len(accepted), "requested": requested,
                      "truncated": payload["truncated"]})
    return jsonify(payload), 202


@bp.get("/purchases/<store_id>")
def purchases(store_id: str):
    """Recent purchases at one store, for the hand-picker on a refusal panel.

    Leaves out a purchase already recorded against a track this account owns
    at this store, so the same item cannot be filed twice from here. That
    check only works forward from the point `purchased_item_key` started
    being recorded: a purchase filed before this column existed has no key to
    compare and is offered regardless, deliberately, since matching it back by
    artist and title instead would risk hiding a purchase the reader still
    needs.

    The matcher never even reaches `_decide` against an empty inventory (it
    refuses with reason `empty_inventory` before building a candidate list at
    all), so a track showing a refusal panel already implies the store had
    something to enumerate. What has to be got right here instead is the two
    ways this route can otherwise look like "you own nothing" without meaning
    it: a store nobody configured, and a store whose session has died since
    the claim that produced the refusal, which `list_owned` reports as
    `StoreAuthError` rather than an empty iterator and this route must not
    flatten that back into one.
    """
    svc = _svc()
    store = (svc.stores or {}).get(store_id)
    if store is None:
        return jsonify({"error": f"store {store_id!r} is not configured"}), 404
    if not store.capabilities.enumerate_owned:
        return jsonify({"error": f"{store.name} cannot list what you own"}), 400
    try:
        limit = min(_count_arg("limit", DEFAULT_LIMIT), MAX_LIMIT)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    from ..errors import StoreAuthError

    already_owned = svc.tracks.owned_item_keys(store_id)
    found = []
    hidden = 0
    try:
        for item in store.list_owned():
            if item.item_key in already_owned:
                # Not dropped for good, only left off this response: the row
                # in `tracks` is the record of the filing, `hidden` here is
                # just this response saying so rather than quietly shrinking,
                # which would read as the store having lost the purchase.
                hidden += 1
                continue
            found.append({
                "item_key": item.item_key,
                "kind": item.kind,
                "title": item.title,
                "artist": item.artist,
                "release_title": item.release_title,
                "purchased_at": item.purchased_at,
                # The broker protocol's secondary line. The two music fields
                # above stay for this app's own screens, which have room to
                # show a release as well as a performer.
                "subtitle": " · ".join(x for x in (item.artist, item.release_title) if x),
            })
            if len(found) >= limit:
                break
    except StoreAuthError as exc:
        # Not an empty list. An empty list here reads as "you own nothing",
        # and a dead session is a different fact: the store is not answering
        # for this account at all right now.
        return jsonify({"error": str(exc), "code": "signed_out"}), 409

    payload = {"store": store_id, "purchases": found, "hidden": hidden}
    if hidden:
        payload["hidden_reason"] = "already filed against a track you own at this store"
    return jsonify(payload)


@bp.post("/claim/<int:track_id>/pick")
def claim_pick(track_id: int):
    """Claim one purchase the reader named directly, bypassing the matcher.

    Distinct from `claim_confirm`: that route is someone disagreeing with a
    close call the software already made, and still has to clear the confirm
    floor. This one is someone choosing by hand from their own purchase list,
    with no call of the matcher's to disagree with, so it is recorded under
    its own outcome, `user_picked`, and the pipeline (`ClaimPipeline._decide`)
    never scores it at all.

    What gets written here is the identity the reader was shown by
    `/api/purchases`, not a fresh lookup against the store: this route answers
    as fast as every other action on this page does, and a purchase that
    vanished between the two calls is exactly what the claim job's own
    `list_owned` re-check exists to catch.
    """
    svc = _svc()
    row = svc.tracks.get(track_id)
    if row is None:
        return jsonify({"error": "no such track"}), 404
    body = request.get_json(silent=True) or {}
    store_id = _store_arg()
    item_key = (body.get("item_key") or request.values.get("item_key") or "").strip()
    if not store_id:
        return jsonify({"error": "a store is required to pick a purchase"}), 400
    if not item_key:
        return jsonify({"error": "item_key is required"}), 400
    if store_id not in (svc.stores or {}):
        return jsonify({"error": f"store {store_id!r} is not configured"}), 404

    title = (body.get("title") or request.values.get("title") or "").strip()
    # `subtitle` is what the Cookie Broker protocol calls the secondary line;
    # `artist` is what this app's own screens post. Both name the same thing
    # here, and this is recorded only as the audit note of what the reader was
    # actually looking at when they chose.
    artist = (body.get("subtitle") or body.get("artist")
              or request.values.get("artist") or "").strip()

    conn = svc.db()
    try:
        from .. import identity, match

        conn.execute(
            "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
            " matcher_version, lexicon_hash, outcome, reasons, query_json,"
            " candidate_json, candidates_considered, chosen_store_id)"
            " VALUES(?,?,?,?,?,?,'user_picked',?,?,?,0,?)",
            (track_id, int(time.time()), "pick", store_id,
             getattr(match, "MATCHER_VERSION", "1"), identity.lexicon_hash(),
             "picked by hand from the purchase list, not matched",
             json.dumps({"artist": row["artist"], "title": row["title"]}),
             json.dumps({"item_key": item_key, "title": title, "artist": artist}),
             store_id),
        )
    finally:
        conn.close()
    job_id = svc.jobs.enqueue("claim", track_id=track_id, provider_id=store_id)
    log.info("user picked a purchase by hand",
             context={"track": track_id, "job": job_id, "item": item_key})
    return jsonify({"ok": True, "job_id": job_id, "picked": True}), 202


@bp.post("/refusal/<int:track_id>/dismiss")
def refusal_dismiss(track_id: int):
    """Mark the track's latest claim refusal acknowledged.

    Never a delete. `match_decision` is the audit trail of what the software
    decided about the reader's money, so clearing a refusal off the screen has
    to mean marking that one decision read rather than erasing it. A later
    claim writes its own row and is undismissed by construction, so refusing
    the track again shows the refusal panel again: this endpoint acknowledges
    one decision, not the track.

    Marking an already-dismissed refusal dismissed again is a no-op, not an
    error, because a reader clicking twice, or a stale tab replaying the same
    click after a reload, is not a mistake worth surfacing.
    """
    svc = _svc()
    if svc.tracks.get(track_id) is None:
        return jsonify({"error": "no such track"}), 404
    from .views import CLAIM_DECISION_PHASES

    marks = ", ".join("?" for _ in CLAIM_DECISION_PHASES)
    conn = svc.db()
    try:
        row = conn.execute(
            "SELECT id FROM match_decision"
            f" WHERE track_id=? AND outcome='refused' AND dismissed_at IS NULL"
            f" AND phase IN ({marks})"
            " ORDER BY decided_at DESC, id DESC LIMIT 1",
            (track_id, *CLAIM_DECISION_PHASES),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE match_decision SET dismissed_at=? WHERE id=?",
                (int(time.time()), row["id"]),
            )
    finally:
        conn.close()
    svc.bus.publish("track.updated", id=track_id, status="queued")
    return jsonify({"ok": True})


@bp.post("/failure/<int:track_id>/dismiss")
def failure_dismiss(track_id: int):
    """Mark the track's latest broken claim acknowledged.

    Never a delete. `jobs` is the record of what was actually attempted, so
    clearing a failure off the screen has to mean marking that one job read
    rather than erasing it. A later claim writes its own row and is
    undismissed by construction, so a claim that fails again shows the
    failure panel again: this endpoint acknowledges one job, not the track.

    Marking an already-dismissed failure dismissed again is a no-op, not an
    error, because a reader clicking twice, or a stale tab replaying the same
    click after a reload, is not a mistake worth surfacing.
    """
    svc = _svc()
    if svc.tracks.get(track_id) is None:
        return jsonify({"error": "no such track"}), 404
    conn = svc.db()
    try:
        row = conn.execute(
            "SELECT id FROM jobs"
            " WHERE track_id=? AND kind='claim' AND state IN ('failed', 'interrupted')"
            " AND dismissed_at IS NULL"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (track_id,),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE jobs SET dismissed_at=? WHERE id=?",
                (int(time.time()), row["id"]),
            )
    finally:
        conn.close()
    svc.bus.publish("track.updated", id=track_id, status="queued")
    return jsonify({"ok": True})


@bp.post("/claim/<int:track_id>/cancel")
def claim_cancel(track_id: int):
    """Stop a claim that has not started, and disown one that has.

    A running claim is not killed mid-flight. It may already be partway through
    a purchase, and there is no safe point to interrupt that from outside, so it
    is marked interrupted and left to finish or fail on its own.
    """
    svc = _svc()
    conn = svc.db()
    try:
        queued = conn.execute(
            "UPDATE jobs SET state='failed', error='cancelled', error_code='cancelled',"
            " finished_at=? WHERE track_id=? AND kind='claim' AND state='queued'",
            (int(time.time()), track_id),
        ).rowcount
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE track_id=? AND kind='claim' AND state='running'",
            (track_id,),
        ).fetchone()["n"]
    finally:
        conn.close()
    svc.bus.publish("track.updated", id=track_id, status="queued")
    return jsonify({"ok": True, "cancelled": queued, "still_running": running})


@bp.get("/cover/<track_id>")
def cover(track_id: str):
    """The cached cover, served off local disk.

    Never a redirect to the artwork's origin and never a live proxy of it.
    Caching covers exists so that opening a queue of 160 rows does not become
    160 requests to Deezer from the reader's browser, and either of those puts
    every one of them back. Nothing cached is a 404, which is the interface's
    cue to draw its own placeholder rather than to go and fetch the original.
    """
    # The id is matched as a string so that /api/cover/nonsense answers 404
    # like every other unknown track rather than 404ing at the router with an
    # HTML body the interface cannot read.
    if not (track_id.isascii() and track_id.isdigit()):
        return jsonify({"error": "no such track"}), 404
    svc = _svc()
    if svc.tracks.get(int(track_id)) is None:
        return jsonify({"error": "no such track"}), 404
    path = _cover_path(svc, int(track_id))
    if path is None:
        return jsonify({"error": "no cover cached"}), 404
    stat = path.stat()
    resp = send_file(
        path,
        mimetype=COVER_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        etag=f"cover-{track_id}-{stat.st_size:x}-{int(stat.st_mtime):x}",
        conditional=True,
        max_age=COVER_MAX_AGE,
    )
    resp.headers["Cache-Control"] = f"public, max-age={COVER_MAX_AGE}"
    return resp


def _cover_path(svc, track_id: int) -> Path | None:
    """The cached cover file for a track, or None when there is none.

    The cache is imported inside the call so that this module, and the app that
    registers it, still import where the media layer is absent. Without it the
    directory is read directly, which the two sides agree on anyway: the
    filename is the track id and the extension is whatever the bytes were.
    """
    try:
        from ..media import CoverCache
    except ImportError:
        directory = Path(svc.settings.config_dir) / COVER_DIR
        for path in sorted(directory.glob(f"{track_id}.*")):
            if path.is_file():
                return path
        return None
    return CoverCache(svc.settings.config_dir).path_for(track_id)


@bp.get("/preview/<int:track_id>")
def preview(track_id: int):
    """A fresh 30 second preview URL.

    Resolved on demand rather than stored, because the URLs are signed and
    expire within the hour, so a stored one is usually dead by the time anyone
    clicks it.
    """
    svc = _svc()
    row = svc.tracks.get(track_id)
    if row is None:
        return jsonify({"error": "no such track"}), 404
    return jsonify({"url": _deezer_preview(svc, row["artist"], row["title"])})


@bp.get("/buy/<int:track_id>")
def buy(track_id: int):
    """Where to go and buy this.

    Naming a store redirects there. Naming none answers with every store that
    sells it, because with more than one configured there is no single right
    answer and picking one here would be the interface making the choice
    silently. The row template always names one: it puts the choice in front of
    the reader as a menu and sends them to what they picked, which is why a
    link from a row lands in a shop rather than on this list.

    The redirect carries `lw=<track_id>` in the URL fragment, for a browser
    extension running on the store's page to read back with
    `URLSearchParams`. A fragment rather than a query parameter, because a
    fragment never leaves the browser: the shop's own server never sees it, so
    nothing about the wishlist is disclosed to the store and the store has no
    way to route on it.
    """
    svc = _svc()
    row = svc.tracks.get(track_id)
    if row is None:
        return jsonify({"error": "no such track"}), 404
    store_id = request.args.get("store")
    links = _buy_links(svc, row)
    if store_id:
        target = next((l for l in links if l["store"] == store_id), None)
        if target is None:
            return jsonify({"error": f"store {store_id!r} is not configured"}), 404
        return redirect(_with_track_fragment(target["url"], track_id))
    return jsonify({"track": track_id, "links": links})


@bp.get("/search/<int:track_id>")
def search(track_id: int):
    """Candidate products across the configured stores.

    Deliberately unfiltered. Stores return whatever their search gave, covers
    and remixes included, and the matcher is what decides. Filtering here would
    be a second, weaker notion of identity sitting in front of the real one.
    """
    svc = _svc()
    row = svc.tracks.get(track_id)
    if row is None:
        return jsonify({"error": "no such track"}), 404
    from ..models import Identifiers, TrackQuery

    query = TrackQuery(artist=row["artist"], title=row["title"], album=None,
                       duration_s=None, ids=Identifiers())
    offers = []
    for store in (svc.stores or {}).values():
        if not store.capabilities.search:
            continue
        try:
            for offer in store.find_offers(query, limit=5):
                offers.append({"store": offer.store, "kind": offer.kind, "url": offer.url,
                               "artist": offer.artist, "title": offer.track_title,
                               "release": offer.release_title})
        except Exception as exc:
            log.warning("search failed", context={"store": store.id, "err": str(exc)})
    return jsonify({"track": track_id, "offers": offers,
                    "links": _buy_links(svc, row)})


@bp.post("/import")
def import_purchases():
    """File audio files bought somewhere with no API to read.

    Done inline rather than queued. Everything expensive about a claim is the
    part that talks to a store, and there is no store here: the bytes are
    already on this machine by the time the route runs, and what is left is a
    signature check and a rename. The reader also needs to be told per file what
    happened to it, which a job id cannot say.

    One response per file, in the order they were sent, whether or not each one
    worked. A partial success is the normal case when a folder is dropped, and a
    request that failed as a whole because one file was a PDF would make the
    reader sort the folder by hand before trying again.
    """
    svc = _svc()
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify({"error": "send one or more files as `files`"}), 400

    from ..importer import ImportRefused, import_file

    results = []
    for upload in files:
        name = Path(upload.filename).name
        # Named for this request, not for the upload. A filename arrives from
        # the reader's disk and `staged()` is what keeps it from meaning
        # anything on ours; two files of the same name in one drop still need
        # to be two files while they are being checked.
        staged = svc.paths.staged(f"upload-{len(results)}-{name}")
        try:
            upload.save(staged)
            filed = import_file(svc, staged, original_name=name)
        except ImportRefused as exc:
            # Logged, because the reason otherwise exists only in the reply and
            # the reply is gone the moment the page is. "Why did that one not
            # take?" is the first question asked about an import, and the log
            # is where it gets asked.
            log.info("an upload was refused", context={"file": name, "why": str(exc)})
            results.append({"file": name, "ok": False, "msg": str(exc)})
            continue
        except Exception as exc:
            log.exception("an upload could not be filed", context={"file": name})
            svc.paths.discard(staged)
            results.append({"file": name, "ok": False,
                            "msg": f"{type(exc).__name__}: {exc}"})
            continue
        results.append({"file": name, "ok": True, "track_id": filed.track_id,
                        "artist": filed.artist, "title": filed.title,
                        "format": filed.fmt, "already_held": filed.already_held})

    filed_count = sum(1 for r in results if r["ok"])
    log.info("filed uploaded purchases",
             context={"sent": len(files), "filed": filed_count})
    return jsonify({"ok": True, "filed": filed_count, "results": results}), 200


@bp.post("/scan")
def scan():
    """Ask the music server to rescan, on demand."""
    svc = _svc()
    from ..claim import rescan

    if not svc.settings.rescan_cmd.strip():
        return jsonify({"ok": False, "msg": "no rescan command is configured"}), 400
    rescan(svc.settings.rescan_cmd)
    svc.bus.publish("scan.requested", path=str(svc.settings.music_dir))
    return jsonify({"ok": True})


def _with_track_fragment(url: str, track_id: int) -> str:
    """`url`, with `lw=<track_id>` folded into its fragment.

    Appended after `&` when the store URL already carries one, rather than
    replaced, so a store that means its own fragment (routing, an anchor)
    keeps meaning it; `URLSearchParams` on the reading end parses either
    shape the same way.
    """
    parts = urlsplit(url)
    marker = f"lw={track_id}"
    fragment = f"{parts.fragment}&{marker}" if parts.fragment else marker
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, fragment))


def _buy_links(svc, row) -> list[dict]:
    from ..models import Identifiers, TrackQuery

    query = TrackQuery(artist=row["artist"], title=row["title"], album=None,
                       duration_s=None, ids=Identifiers())
    out = []
    for store_id, store in (svc.stores or {}).items():
        try:
            resolved = _stored_link(store_id, row)
            out.append({"store": store_id, "name": store.name,
                        "url": resolved or store.buy_url(query),
                        # Which of the two this is, so the interface can offer
                        # the product itself rather than a search for it.
                        "direct": resolved is not None})
        except Exception as exc:
            log.warning("no buy link", context={"store": store_id, "err": str(exc)})
    return out


def _stored_link(store_id: str, row) -> str | None:
    """A product URL already resolved on the track row, if there is one.

    62 of the 164 rows carry a real Bandcamp product URL that an earlier
    resolver worked out. Building a search URL over the top of one sends the
    reader to do again what has already been done.

    Migration 0007 drops `bandcamp_url`, and this is the reader that has to
    move to wherever the resolved link lives before that lands.
    """
    if store_id != "bandcamp":
        return None
    try:
        value = row["bandcamp_url"]
    except (KeyError, IndexError, TypeError):
        return None
    value = (value or "").strip()
    return value or None


def _deezer_preview(svc, artist: str, title: str) -> str | None:
    """Deezer's public search, which needs no credential.

    Only the preview stream is taken from it. Its ranking is not evidence of
    anything: a live query for CHVRCHES "Lies" returns their `Such Great Heights
    (From "Tell Me Lies Season 3")` first, which is exactly the wrong-track
    confusion this application exists to prevent.
    """
    from ..http import HttpClient

    client = HttpClient(user_agent=svc.settings.http_user_agent, timeout=15,
                        provider_id="deezer", log=log)
    try:
        body = client.get("https://api.deezer.com/search/track",
                          params={"q": f'artist:"{artist}" track:"{title}"'},
                          retries=0).json()
    except Exception as exc:
        log.info("no preview available", context={"err": str(exc)})
        return None
    for hit in (body or {}).get("data", []):
        if hit.get("preview"):
            return hit["preview"]
    return None


@bp.post("/sync")
def sync():
    """Sweep every shop's purchases and claim what is recognised.

    One job, not one per shop: the sweep reads them in parallel itself, so a
    reader watching this has a single thing to watch. It queues an ordinary
    claim for every purchase that clears the matcher's automatic gate, which
    means nothing reaches the library by a route the single-track flow does not
    already use.

    Refuses to start a second while one is running. Two sweeps racing would
    both read the same unfiled purchases and queue the same claims twice, and
    the second would be work nobody asked for.
    """
    svc = _svc()
    running = [j for j in svc.jobs.recent()
               if j.get("kind") == "sync" and j.get("state") not in ("finished", "failed", "interrupted")]
    if running:
        return jsonify({"error": "A sync is already running.", "job_id": running[0]["id"]}), 409

    if not (svc.stores or {}):
        return jsonify({"error": "No shops are configured."}), 400

    job_id = svc.jobs.enqueue("sync")
    return jsonify({"job_id": job_id})


def _progress(raw: str | None) -> dict:
    """A job's stored progress, as an object. Unreadable is empty, not fatal.

    The column holds whatever a handler last reported, so a caller asking for
    it after a crash mid-write should get a page with a blank line under the
    button rather than a 500.
    """
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@bp.get("/sync")
def sync_state():
    """The newest sweep, however it ended, so a page can pick it back up.

    Every tab in this application is an ordinary link, so moving between them
    is a fresh document with no memory of the sweep that was running when the
    reader left. The button would come back enabled and the line under it
    blank, which reads as "nothing is happening" at the exact moment something
    is, and invites the second press `POST /sync` exists to refuse.

    Answered from the job row rather than from anything the browser kept,
    because the job row is what is true: a sweep that finished while the reader
    was on another tab has to come back finished, not still spinning. `phase`
    and `progress` are the same pair the live stream sends, so the interface
    builds its sentence one way for both.
    """
    svc = _svc()
    latest = next((j for j in svc.jobs.recent() if j.get("kind") == "sync"), None)
    if latest is None:
        return jsonify({"state": None})
    return jsonify({
        "id": latest["id"],
        "state": latest["state"],
        "phase": latest["phase"],
        "progress": _progress(latest.get("progress")),
        "error": latest.get("error"),
        "finished_at": latest.get("finished_at"),
    })


@bp.get("/jobs")
def jobs():
    return jsonify(list(_svc().jobs.recent()))


# What each claim phase means, in words for the person who just bought
# something rather than for the queue. Sent with the job because the phases are
# this app's own (CLAIM_PHASES in libwish/jobs.py) and a browser extension
# shared by every app cannot be expected to know them; it shows this sentence
# and falls back to "Working on it." when there is none.
PHASE_TEXT = {
    "session": "Checking your store session.",
    "enumerate": "Looking through your purchases.",
    "match": "Matching your purchase to this track.",
    "download": "Downloading the file.",
    "verify": "Checking the file is what it claims to be.",
}


@bp.get("/jobs/<int:job_id>")
def job(job_id: int):
    row = _svc().jobs.get(job_id)
    if row is None:
        return jsonify({"error": "no such job"}), 404
    out = dict(row)
    said = PHASE_TEXT.get(out.get("phase"))
    if said:
        out["phase_text"] = said
    return jsonify(out)


@bp.get("/status")
def status():
    """Provider health, for the banner and the settings screen.

    Reports `stale` separately from `ok` and from `error`. "Working", "broken"
    and "not checked recently" are three different claims and collapsing the
    third into either of the others tells the user something untrue.
    """
    svc = _svc()
    conn = svc.db()
    try:
        providers = [dict(r) for r in conn.execute(
            "SELECT kind, provider_id, state, detail, stale, updated_at FROM provider_state"
            " ORDER BY kind, provider_id"
        ).fetchall()]
    finally:
        conn.close()
    return jsonify({
        "providers": providers,
        "counts": svc.tracks.counts(),
        "clients": svc.bus.client_count,
        "version": svc.version,
    })


@bp.get("/events", endpoint="events_api")
def events_api():
    return _stream()


def _stream() -> Response:
    svc = _svc()
    sub = svc.bus.subscribe()

    @stream_with_context
    def gen():
        yield from sub.stream()

    resp = Response(gen(), mimetype="text/event-stream")
    # Without this an intermediary buffers the stream and the page appears
    # frozen until enough bytes accumulate to flush.
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    # Connection is deliberately not set here. It is a hop-by-hop header, which
    # PEP 3333 forbids a WSGI application from sending, and waitress refuses the
    # whole response rather than dropping the header. Keep-alive is the server's
    # business, not this route's.
    return resp
