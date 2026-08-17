"""Finding a sleeve for a file that arrived without one.

A track fetched from Bandcamp or Qobuz comes with its artwork already inside
it, and the music server reads it straight off the file. An iTunes purchase
usually does not: Apple keeps the artwork in the Music app's own library and
sells a file with no `covr` atom in it, so a purchased song lands in a library
looking like a track nobody has ever heard of.

Two sources, in this order, and the order is the whole design.

Apple's own catalogue, looked up by the release id the file itself carries.
Nothing is matched, because nothing needs to be: the seller wrote down which
release this is, and the answer is that release's sleeve.

Deezer's album search, by artist and album name, for a file that names no id.
This one is a guess, so it has to be checked, and the check is not a formality.
Asked for "A Perfect Circle Eat the Elephant", an album Deezer does not carry,
it answers with eMOTIVe: a real album by the right artist and the wrong record
entirely. A sleeve that is confidently wrong is worse than an empty square,
because the empty square is honest about not knowing.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from . import identity, tags
from .errors import LibwishError
from .log import get

log = get("artwork")

APPLE_LOOKUP = "https://itunes.apple.com/lookup"
DEEZER_ALBUM_SEARCH = "https://api.deezer.com/search/album"

#: Apple serves whatever size is asked for by rewriting this segment of the
#: URL it gives out. The default is a hundred pixels square, which is a
#: thumbnail of a thumbnail on a modern display.
APPLE_THUMB = "100x100bb.jpg"
APPLE_FULL = "1000x1000bb.jpg"

#: Deezer's album cover fields, largest first.
DEEZER_COVERS = ("cover_xl", "cover_big", "cover_medium", "cover")

#: What a music server looks for beside the audio when the file itself carries
#: no artwork. Navidrome reads `cover.*` before `folder.*` and `front.*`, and
#: writing the one it prefers means the sleeve shows up without configuring
#: anything.
FOLDER_COVER = "cover"


def for_imported_file(http: Any, path: Path, found: tags.FileTags) -> str:
    """A cover URL for a file just filed, or an empty string.

    `path` is read for the seller's own release id before anything is guessed
    from names, which is what makes the common case exact.
    """
    apple_id = tags.apple_collection_id(path)
    if apple_id:
        url = apple_cover(http, apple_id)
        if url:
            log.info("cover from apple's catalogue",
                     context={"collection": apple_id, "title": found.title})
            return url
    if found.album:
        url = deezer_album_cover(http, found.artist, found.album)
        if url:
            log.info("cover from a deezer album match",
                     context={"album": found.album, "title": found.title})
            return url
    log.info("no cover found for an imported file",
             context={"artist": found.artist, "title": found.title,
                      "album": found.album, "apple_id": apple_id})
    return ""


def apple_cover(http: Any, collection_id: int) -> str:
    """The sleeve Apple sells this release under, at a usable size."""
    url = f"{APPLE_LOOKUP}?{urllib.parse.urlencode({'id': collection_id})}"
    payload = _json(http, url)
    for row in (payload.get("results") or []) if isinstance(payload, dict) else []:
        art = row.get("artworkUrl100") or row.get("artworkUrl60") or ""
        if art:
            return art.replace(APPLE_THUMB, APPLE_FULL)
    return ""


def deezer_album_cover(http: Any, artist: str, album: str) -> str:
    """A Deezer album's cover, only when it is the album that was asked for.

    Both the artist and the album title have to agree, compared the way the
    matcher compares anything: normalised, so "Mer De Noms" and "Mer de Noms"
    are the same record, and version qualifiers ignored, so a deluxe edition
    still counts as the album it is an edition of. A near miss is dropped
    rather than returned with a caveat, because nothing downstream reads
    caveats.
    """
    query = f'artist:"{artist}" album:"{album}"'
    rows = _albums(http, query)
    if not rows:
        rows = _albums(http, f"{artist} {album}".strip())

    want_artist = identity.build_identity(artist, album)
    want_album = want_artist.title.base
    for row in rows:
        their_artist = ((row.get("artist") or {}).get("name") or "")
        their_album = row.get("title") or ""
        theirs = identity.build_identity(their_artist, their_album)
        if theirs.artist_key != want_artist.artist_key:
            continue
        if theirs.title.base != want_album:
            continue
        for field in DEEZER_COVERS:
            if row.get(field):
                return str(row[field])
    return ""


def _albums(http: Any, query: str) -> list[dict]:
    url = f"{DEEZER_ALBUM_SEARCH}?{urllib.parse.urlencode({'q': query})}"
    payload = _json(http, url)
    data = payload.get("data") if isinstance(payload, dict) else None
    return [r for r in (data or []) if isinstance(r, dict)]


def _json(http: Any, url: str) -> dict:
    """One GET, decoded, with every failure turned into an empty answer.

    Artwork is the most optional thing this application fetches. A store that
    is down, rate limited or answering with something that is not JSON must not
    fail the import that asked, because the file is already in the library and
    the purchase is already recorded by the time anyone asks about a picture.
    """
    try:
        resp = http.get(url)
        payload = resp.json() if hasattr(resp, "json") else json.loads(resp.body)
    except (LibwishError, ValueError, OSError) as exc:
        log.info("artwork lookup failed", context={"err": f"{type(exc).__name__}: {exc}"})
        return {}
    return payload if isinstance(payload, dict) else {}


def write_folder_cover(cover: Path, album_dir: Path) -> Path | None:
    """Copy a cached cover in beside the audio, for the music server to find.

    Written rather than embedded. Embedding would mean rewriting a file this
    application has only ever moved, and a purchased file is the one thing here
    that should come out byte for byte the way the shop sold it.

    An existing cover is never replaced. It is either the same picture or one
    somebody chose deliberately, and neither is improved by overwriting.
    """
    if not cover.is_file() or not album_dir.is_dir():
        return None
    dest = album_dir / f"{FOLDER_COVER}{cover.suffix.lower()}"
    if any(album_dir.glob(f"{FOLDER_COVER}.*")):
        return None
    tmp = album_dir / f".{FOLDER_COVER}.part-{cover.name}"
    try:
        tmp.write_bytes(cover.read_bytes())
        tmp.replace(dest)
    except OSError as exc:
        log.info("folder cover not written",
                 context={"dir": str(album_dir), "err": str(exc)})
        tmp.unlink(missing_ok=True)
        return None
    log.info("wrote a folder cover", context={"file": str(dest)})
    return dest


__all__ = ["APPLE_FULL", "FOLDER_COVER", "apple_cover", "deezer_album_cover",
           "for_imported_file", "write_folder_cover"]
