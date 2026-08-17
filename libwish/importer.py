"""Filing a track bought somewhere this application cannot read.

A claim works backwards from the want list: the track is already a row, and the
job is to find the purchase that matches it. This works the other way. The file
arrives first, from a shop with no API worth reading, and the row is whatever
that file turns out to be.

Everything after identification is the same path a download takes, deliberately:
the same signature check, the same library layout, the same atomic publish, the
same enrichment queue. A file dropped here ends up indistinguishable from one
this application fetched itself, which is the whole point of accepting it.

The file's own tags are read to say which track it is, and to ask after its
sleeve. Neither is kept as the row's description: what the plate shows and what
the row says come from the same enrichment every other row gets, so an iTunes
purchase and a Qobuz one describe themselves the same way rather than in
Apple's words. Artwork is the exception, and only because Apple sells a file
with none in it; see `artwork.py` for why the seller's own release id is a
better answer there than any search.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import artwork, enrich, identity, tags
from .claim import rescan, verify_audio  # the same check a download passes
from .db import transaction
from .errors import LibwishError, VerificationFailed
from .log import get
from .paths import FileExists

log = get("import")

#: What an imported purchase is filed under. It appears on the owned plate, so
#: it names the shop rather than the mechanism: "UPLOAD" would tell the reader
#: how the bytes arrived, which is the one thing about a purchase they already
#: know and the least useful thing to be reminded of a year later.
DEFAULT_STORE = "itunes"

#: Formats worth accepting from a drop. Narrower than what `verify_audio`
#: recognises: an Ogg stream is audio, but nothing sells one, and a file this
#: application cannot explain the provenance of does not belong in a library
#: whose whole claim is that every file in it was bought.
ACCEPT = ("m4a", "flac", "mp3")


class ImportRefused(LibwishError):
    """The file cannot be filed, and says why in terms of the file."""


@dataclass(frozen=True)
class Imported:
    """What happened to one file."""

    track_id: int
    artist: str
    title: str
    path: Path
    fmt: str
    #: True when the library already held this track and the bytes were
    #: discarded. Not a failure: the reader's library has the track either way,
    #: and reporting it as one would have them hunting for a problem they do
    #: not have.
    already_held: bool = False


def import_file(svc: Any, staged: Path, *, original_name: str = "",
                store_id: str = DEFAULT_STORE) -> Imported:
    """File one verified-on-arrival audio file into the library.

    `staged` must already be inside the staging directory, because publishing is
    a rename and a rename only works within one filesystem. The caller writing
    the upload there is what makes that true.

    On any refusal the staged file is discarded. A rejected upload that leaves
    bytes behind fills the volume with files nobody can see and nothing will
    ever collect.
    """
    staged = Path(staged)
    shown = original_name or staged.name
    try:
        fmt = _verified(staged, shown)
        found = _identify(staged, original_name)
        # Read before publishing, because publishing is a rename and this file
        # is about to stop being at this path.
        apple_id = tags.apple_collection_id(staged)
        track_id, fresh = _row_for(svc, found)
        dest, already_held = _publish(svc, staged, found, fmt)
    except Exception:
        svc.paths.discard(staged)
        raise

    svc.tracks.mark_purchased(track_id, store_id, _item_key(original_name or staged.name))
    log.info("filed an uploaded purchase",
             context={"track": track_id, "store": store_id, "format": fmt,
                      "held": already_held})
    _artwork(svc, track_id, dest, found, apple_id)

    # The row is new to every other part of the application, so it is announced
    # as one. An existing row that was on the want list has changed status, and
    # the list the reader is looking at moves it across on that.
    if fresh:
        svc.bus.publish("track.added", id=track_id, artist=found.artist,
                        title=found.title, source=store_id)
    svc.bus.publish("track.updated", id=track_id, status="purchased",
                    store=store_id, path=str(dest))

    # The same queue a source row joins. Cover, duration and MBIDs come from
    # here, not from the file, which is what keeps an imported row describing
    # itself the way every other row does.
    svc.jobs.enqueue(enrich.JOB_KIND, track_id=track_id)

    rescan(svc.settings.rescan_cmd)
    svc.bus.publish("scan.requested", path=str(dest.parent))
    return Imported(track_id=track_id, artist=found.artist, title=found.title,
                    path=dest, fmt=fmt, already_held=already_held)


def _verified(staged: Path, shown: str) -> str:
    """The format, or a refusal phrased for whoever dropped the file.

    `shown` is the name the reader knows the file by. Staging renames it for
    the transfer, and a refusal about `upload-0-track.m4a` is a refusal about a
    file they have never seen.
    """
    try:
        fmt = verify_audio(staged, name=shown, hint="it is not an audio file")
    except VerificationFailed as exc:
        raise ImportRefused(str(exc)) from None
    if fmt not in ACCEPT:
        raise ImportRefused(
            f"{shown} is {fmt}, which is not a format this files. "
            f"Accepted: {', '.join(ACCEPT)}."
        )
    return fmt


def _identify(staged: Path, original_name: str) -> tags.FileTags:
    """Who this is, from the file's own metadata.

    The filename is a fallback and not a good one, so it only answers when it
    is unambiguous. Refusing beats guessing: a wrong guess files the purchase
    under a name the reader has to notice is wrong before they can fix it, and
    an owned row is exactly where nobody looks.
    """
    found = tags.read(staged)
    if not found.identifies():
        from_name = tags.from_filename(original_name or staged.name)
        found = tags.FileTags(
            artist=found.artist or from_name.artist,
            title=found.title or from_name.title,
            album=found.album,
        )
    if not found.identifies():
        raise ImportRefused(
            f"{original_name or staged.name} carries no artist and title, and the "
            f"filename does not read as \"Artist - Title\", so there is nothing to "
            f"file it as."
        )
    return found


def _row_for(svc: Any, found: tags.FileTags) -> tuple[int, bool]:
    """The track this file is, creating the row when it is new.

    An import of something already on the want list files that row rather than
    making a second one. It is the same identity ladder a love goes through, so
    a track loved on Last.fm and bought on iTunes is one track, and the row the
    reader has been looking at is the row that turns owned.
    """
    ident = identity.build_identity(found.artist, found.title)
    now = int(time.time())
    conn = svc.db()
    try:
        with transaction(conn):
            track_id = identity.find_existing(conn, ident)
            if track_id is not None:
                return track_id, False
            cur = conn.execute(
                "INSERT INTO tracks(artist, title, added_at, status,"
                " artist_key, title_key, qualifier_key, fp_key,"
                " identity_degraded, duration_ms)"
                " VALUES(?,?,?,'queued',?,?,?,?,?,NULL)",
                (found.artist, found.title, now, ident.artist_key, ident.title.base,
                 identity.qualifier_key(ident), identity.fingerprint(ident),
                 int(bool(ident.identity_degraded))),
            )
            return cur.lastrowid, True
    finally:
        conn.close()


def _publish(svc: Any, staged: Path, found: tags.FileTags, fmt: str) -> tuple[Path, bool]:
    """Move the file into the library. Returns where it went and whether it
    was already there.

    A destination that exists is left alone. The file on disk is evidence the
    reader already owns this, possibly in a better master than an AAC from
    iTunes, and replacing it would be this application deciding that a purchase
    it just learned about outranks one it has held for a year.
    """
    dest = svc.paths.library_path(found.artist, found.title,
                                  album=found.album or "", suffix=f".{fmt}")
    try:
        svc.paths.publish(staged, dest)
    except FileExists:
        svc.paths.discard(staged)
        log.info("already in the library", context={"dest": str(dest)})
        return dest, True
    return dest, False


def _artwork(svc: Any, track_id: int, dest: Path, found: tags.FileTags,
             apple_id: int | None) -> None:
    """Find this track a sleeve, and leave one where the music server looks.

    Two places, because they are two different readers. The cache is what the
    want list draws; the file beside the audio is what a music server reads,
    and it reads the file rather than asking this application anything.

    Never fatal. The track is filed and the purchase is recorded before this
    runs, and a missing picture is not a reason to report that a purchase
    failed.
    """
    covers = enrich.cover_cache(svc)
    cached = covers.path_for(track_id)
    if cached is None:
        url = artwork.for_imported_file(enrich.http_client(svc), dest, found) if (
            apple_id or found.album) else ""
        if not url:
            return
        try:
            cached = covers.ensure(track_id, url)
        except (LibwishError, OSError) as exc:
            log.info("cover not cached",
                     context={"track": track_id, "err": f"{type(exc).__name__}: {exc}"})
            return
        svc.bus.publish("track.updated", id=track_id, fields={"cover": True})
    artwork.write_folder_cover(cached, dest.parent)


def _item_key(name: str) -> str:
    """What gets recorded as the purchase's key at the store.

    A filename rather than an identifier, because iTunes gives a download no id
    that survives leaving the store. It is what the reader dropped, which is the
    only thing that can be checked against by hand later.
    """
    return Path(name).name[:200]


__all__ = ["ACCEPT", "DEFAULT_STORE", "ImportRefused", "Imported", "import_file"]
