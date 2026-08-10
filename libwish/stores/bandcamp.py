"""Bandcamp, the second store, and the one that proves the abstraction is real.

It differs from Qobuz in every dimension the interface models: it has a working
search API, it sells releases rather than tracks, its downloads are prepared
asynchronously into archives, and its ownership list needs a different cookie
entirely. Anything the store seam could not express would have shown up here.

Search returns covers and remixes uploaded by unrelated people, prolifically. A
live query for CHVRCHES "Lies" returns, in order: `Ikonika / Chvrches Lies (Dub
Rmx)`, `OMX / Lies (CHVRCHES) Remix`, `TomboFry / Lies (CHVRCHES)`. Not one is
the CHVRCHES recording.

This provider still returns all of them. Filtering here would be the store
deciding what counts as a match, which is the pipeline's job and the reason the
matcher exists. The matcher refuses every one of those on the artist gate. A
provider that pre-filtered would be applying a second, weaker, untested notion
of identity behind the first.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator, Sequence
from urllib.parse import quote

from ..errors import StoreAuthError, StoreFormatUnavailable, StorePreparing
from ..models import (
    Identifiers, Offer, OwnedItem, ProviderContext, StoreCapabilities,
    StoreHealth, TrackQuery,
)
from . import register

BASE = "https://bandcamp.com"
SEARCH_PATH = "/api/bcsearch_public_api/1/autocomplete_elastic"
COLLECTION_PATH = "/api/fancollection/1/collection_items"

# Bandcamp's search filter codes: t for tracks, a for albums, b for artists.
FILTER_TRACK, FILTER_ALBUM = "t", "a"


@register
class BandcampStore:
    id = "bandcamp"
    name = "Bandcamp"
    auth_kind = "cookie"
    capabilities = StoreCapabilities(
        search=True,
        deep_link=True,
        enumerate_owned=True,
        download=True,
        release_granular=True,   # purchases are releases; tracks live inside them
        async_prepare=True,      # downloads are encoded on demand
        formats=("flac", "mp3", "alac", "vorbis"),
    )

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self.log = ctx.log
        self._http = None
        self._anon = None

    @property
    def http(self):
        """The authenticated client, for anything touching the user's account."""
        if self._http is None:
            self._http = self.ctx.creds.http_client(base_url=BASE)
        return self._http

    @property
    def anon(self):
        """An unauthenticated client.

        Search works signed out, so it is done without the session. That keeps a
        browse of the catalogue from depending on a credential, and means the
        buy links still work when the cookie has expired.
        """
        if self._anon is None:
            from ..http import HttpClient

            self._anon = HttpClient(
                user_agent=self.ctx.settings.http_user_agent,
                timeout=self.ctx.settings.http_timeout_seconds,
                base_url=BASE, provider_id=self.id, log=self.log,
            )
        return self._anon

    # --- health ---------------------------------------------------------

    def check(self) -> StoreHealth:
        now = int(time.time())
        try:
            self._search("test", FILTER_TRACK)
        except Exception as exc:
            return StoreHealth(ok=False, authed=False, detail=f"cannot reach Bandcamp: {exc}",
                               checked_at=now, owned_count=None)
        try:
            fan_id = self._fan_id()
        except StoreAuthError as exc:
            return StoreHealth(ok=True, authed=False, detail=str(exc.message or "signed out"),
                               checked_at=now, owned_count=None)
        except Exception as exc:
            return StoreHealth(ok=True, authed=False, detail=f"cannot read your collection: {exc}",
                               checked_at=now, owned_count=None)
        return StoreHealth(ok=True, authed=bool(fan_id), detail="", checked_at=now,
                           owned_count=None)

    # --- discovery ------------------------------------------------------

    def buy_url(self, q: TrackQuery) -> str:
        return f"{BASE}/search?q=" + quote(f"{q.artist} {q.title}")

    def find_offers(self, q: TrackQuery, limit: int = 5) -> list[Offer]:
        offers: list[Offer] = []
        for kind, code in (("track", FILTER_TRACK), ("release", FILTER_ALBUM)):
            for hit in self._search(f"{q.artist} {q.title}", code)[:limit]:
                url = hit.get("item_url_path") or hit.get("url") or ""
                if not url:
                    continue
                offers.append(Offer(
                    store=self.id, kind=kind, url=url,
                    artist=hit.get("band_name"),
                    track_title=hit.get("name") if kind == "track" else None,
                    release_title=hit.get("album_name") or (hit.get("name") if kind == "release" else None),
                    price_cents=None,      # search results carry no price
                    currency=None,
                    formats=(),            # format is not committed until download
                    ids=Identifiers(store_track_id=str(hit.get("id") or "") or None),
                    raw=hit,
                ))
        return offers

    def _search(self, text: str, filter_code: str) -> list[dict]:
        body = self.anon.post(
            SEARCH_PATH,
            json={"search_text": text, "search_filter": filter_code,
                  "full_page": False, "fan_id": None},
        ).json()
        results = ((body or {}).get("auto") or {}).get("results") or []
        return [r for r in results if isinstance(r, dict)]

    # --- ownership ------------------------------------------------------

    def _fan_id(self) -> str:
        """The numeric account id, which every collection call needs.

        Read from the signed-in home page rather than stored, so that swapping
        the cookie for a different account cannot leave a stale id behind.
        """
        page = self.http.get("/").text()
        for needle in ('"fan_id":', '"fan_id" :'):
            at = page.find(needle)
            if at < 0:
                continue
            tail = page[at + len(needle):at + len(needle) + 24].strip()
            digits = "".join(c for c in tail if c.isdigit())
            if digits:
                return digits
        raise StoreAuthError(
            "Bandcamp is signed out. Open bandcamp.com in your browser to reconnect.",
            code="signed_out", provider_id=self.id,
        )

    def list_owned(self, since: int | None = None) -> Iterator[OwnedItem]:
        fan_id = self._fan_id()
        token = f"{int(time.time())}::a::"
        while True:
            body = self.http.post(COLLECTION_PATH,
                                  json={"fan_id": int(fan_id), "older_than_token": token,
                                        "count": 100}).json()
            items = (body or {}).get("items") or []
            if not items:
                return
            for raw in items:
                item = self._owned_item(raw)
                if item is not None:
                    yield item
            token = (body or {}).get("last_token") or ""
            if not token or not (body or {}).get("more_available"):
                return

    def _owned_item(self, raw: dict) -> OwnedItem | None:
        title = raw.get("item_title") or ""
        artist = raw.get("band_name") or raw.get("item_art_id") and "" or ""
        if not title:
            return None
        is_track = (raw.get("item_type") or "").lower() == "track"
        return OwnedItem(
            store=self.id,
            item_key=str(raw.get("sale_item_id") or raw.get("item_id") or ""),
            kind="track" if is_track else "release",
            artist=raw.get("band_name") or artist,
            title=title,
            release_title=raw.get("album_title") or (None if is_track else title),
            parent_key=str(raw.get("album_id")) if raw.get("album_id") else None,
            purchased_at=_epoch(raw.get("purchased")),
            duration_s=None,
            track_number=raw.get("track_number"),
            formats=self.capabilities.formats,
            ids=Identifiers(store_track_id=str(raw.get("item_id") or "") or None),
            raw={k: raw.get(k) for k in ("download_url", "item_type", "item_id", "sale_item_id")},
        )

    def expand(self, item: OwnedItem) -> Iterator[OwnedItem]:
        """Tracks inside a purchased release.

        A Bandcamp purchase is usually an album, and the loved track is one song
        on it, so ownership of the album has to become ownership of each track
        or the matcher never sees the thing the user actually asked for.
        """
        if item.kind == "track":
            yield item
            return
        url = (item.raw or {}).get("download_url")
        if not url:
            yield item
            return
        try:
            page = self.http.get(url).text()
        except Exception as exc:
            self.log.warning("release page unreadable", context={"item": item.item_key,
                                                                "err": str(exc)})
            yield item
            return
        for track in _tracks_from_download_page(page):
            yield OwnedItem(
                store=self.id, item_key=f"{item.item_key}:{track['num']}", kind="track",
                artist=track.get("artist") or item.artist, title=track["title"],
                release_title=item.title, parent_key=item.item_key,
                purchased_at=item.purchased_at,
                duration_s=track.get("duration"), track_number=track["num"],
                formats=item.formats, ids=Identifiers(), raw={"download_url": url},
            )

    # --- fetch ----------------------------------------------------------

    def download(self, item: OwnedItem, dest_dir: Path, prefer: Sequence[str],
                 progress) -> "DownloadResult":
        from ..models import DownloadResult

        url = (item.raw or {}).get("download_url")
        if not url:
            raise StoreFormatUnavailable("this purchase has no download link",
                                         code="no_download", provider_id=self.id)
        page = self.http.get(url).text()
        wanted = [f.lower() for f in prefer]
        links = _format_links(page)
        for fmt in wanted:
            target = links.get(fmt)
            if not target:
                continue
            resp = self.http.get(target)
            if b"still being prepared" in resp.body[:4000] or resp.status == 202:
                raise StorePreparing("Bandcamp is still encoding this download",
                                     code="preparing", provider_id=self.id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"bandcamp_{item.item_key}.{'zip' if item.kind == 'release' else fmt}"
            dest.write_bytes(resp.body)
            size = dest.stat().st_size
            try:
                progress("download", bytes=size, total=size, format=fmt)
            except Exception:
                pass
            return DownloadResult(path=dest, requested_format=fmt, bytes=size,
                                  source_host="bandcamp",
                                  is_archive=item.kind == "release", notes={})
        raise StoreFormatUnavailable(f"Bandcamp offers none of {list(prefer)} for this item",
                                     code="no_format", provider_id=self.id)


def _epoch(value) -> int | None:
    if not value:
        return None
    for fmt in ("%d %b %Y %H:%M:%S GMT", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(str(value), fmt)))
        except ValueError:
            continue
    return None


def _format_links(page: str) -> dict[str, str]:
    """Download URLs by format, read from the embedded JSON blob."""
    out: dict[str, str] = {}
    at = page.find("downloads")
    if at < 0:
        return out
    for chunk in page[at:at + 20000].split('"'):
        if chunk.startswith("http") and "/download/" in chunk:
            for name in ("flac", "mp3-320", "mp3-v0", "alac", "vorbis", "aiff", "wav"):
                if name in chunk:
                    out.setdefault("mp3" if name.startswith("mp3") else name, chunk)
    return out


def _tracks_from_download_page(page: str) -> list[dict]:
    """Track rows from a release's download page.

    Returns [] rather than guessing when the embedded data is not where it is
    expected. An invented track list would hand the matcher candidates that do
    not exist.
    """
    at = page.find('"tracks"')
    if at < 0:
        return []
    tail = page[at:]
    start = tail.find("[")
    if start < 0:
        return []
    depth, end = 0, -1
    for i, ch in enumerate(tail[start:start + 200000]):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = start + i + 1
                break
    if end < 0:
        return []
    try:
        rows = json.loads(tail[start:end])
    except ValueError:
        return []
    out = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("track_title")
        if not title:
            continue
        out.append({"num": row.get("track_num") or i, "title": title,
                    "artist": row.get("artist"), "duration": row.get("duration")})
    return out
