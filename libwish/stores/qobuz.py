"""Qobuz, as a store you already bought from.

Qobuz publishes no purchase API, so ownership is read from the server-rendered
download pages with the logged-in session cookie. Two pages matter: a listing of
everything bought, and a per-purchase options page carrying the download links.

Recovering a track title from those pages is the delicate part, and it is worth
explaining because getting it wrong has already cost a wrong download once.

The listing renders each purchase as

    Title <track> <album> <artist> Quality <q> Date <d> Order # <id>

with nothing separating the three name fields. The options page does not state
the track title at all; it states the album and the artist. So the title is
recovered by splitting the run of names on the album, which is known exactly.
A self-titled release puts the album string in twice, so every occurrence is
tried and the split whose trailing remainder is a recognisable artist wins.

What this provider must never do is decide whether a purchase is the track
someone asked for. It reports what it found; the pipeline matches. Scoring a
claim against the listing row as a whole is precisely how a request for
CHVRCHES "Lies" once collected their `Such Great Heights (From "Tell Me Lies
Season 3")`: the word occurs in the tie-in credit.
"""

from __future__ import annotations

import html as H
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterator, Sequence

from ..errors import StoreAuthError, StoreFormatUnavailable, TransientError
from ..models import (
    Identifiers, OwnedItem, Offer, ProviderContext, StoreCapabilities,
    StoreHealth, TrackQuery,
)
from . import register

BASE = "https://www.qobuz.com"
DOWNLOADS_PATH = "/profile/downloads/track"

# Qobuz format ids, best first within each family. 27 is 192kHz hi-res, 7 is
# 96kHz hi-res, 6 is CD-quality FLAC, 5 is MP3.
FORMAT_IDS = {"flac": (27, 7, 6), "mp3": (5,)}
FORMAT_NAMES = {27: "hi-res-192", 7: "hi-res", 6: "CD-FLAC", 5: "MP3"}

# Labels on the us-en storefront that delimit a listing row's name fields.
_ROW_START, _ROW_END = "Title", "Quality"


def _fold(text: str) -> str:
    """Casefold to bare alphanumerics, decomposing accents first.

    Returns "" for text that is entirely non-alphanumeric, which some artist
    names are. Callers must treat "" as unusable rather than as a value that
    matches anything.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _strip_html(fragment: str) -> str:
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _jstr(raw: str) -> str:
    """Decode a JSON string body, which arrives with \\uXXXX escapes intact."""
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return H.unescape(raw)


@register
class QobuzStore:
    id = "qobuz"
    name = "Qobuz"
    auth_kind = "cookie"
    capabilities = StoreCapabilities(
        search=False,            # no public search API, only a deep link
        deep_link=True,
        enumerate_owned=True,
        download=True,
        release_granular=False,  # track purchases are already individual
        async_prepare=False,
        formats=("flac", "mp3"),
    )

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self.log = ctx.log
        self._http = None

    @property
    def http(self):
        """The session-backed client.

        Requested lazily so that constructing the provider does not require a
        live credential, which is what lets the interface list a store the user
        has not connected yet.
        """
        if self._http is None:
            self._http = self.ctx.creds.http_client(base_url=BASE)
        return self._http

    # --- health ---------------------------------------------------------

    def check(self) -> StoreHealth:
        import time

        try:
            page = self.http.get(DOWNLOADS_PATH).text()
        except Exception as exc:
            return StoreHealth(ok=False, authed=False, detail=f"cannot reach Qobuz: {exc}",
                               checked_at=int(time.time()), owned_count=None)
        if self._is_signin(page):
            return StoreHealth(ok=True, authed=False, detail="signed out",
                               checked_at=int(time.time()), owned_count=None)
        count = len(self._entry_ids(page))
        return StoreHealth(ok=True, authed=True, detail="", checked_at=int(time.time()),
                           owned_count=count)

    @staticmethod
    def _is_signin(page: str) -> bool:
        # The signed-out response is a redirect to the sign-in form rather than
        # an error status, so the status code alone cannot tell us.
        return "/signin" in page[:3000]

    # --- discovery ------------------------------------------------------

    def buy_url(self, q: TrackQuery) -> str:
        from urllib.parse import quote

        return f"{BASE}/us-en/search/tracks/" + quote(f"{q.artist} {q.title}")

    def find_offers(self, q: TrackQuery, limit: int = 5) -> list[Offer]:
        """Empty by design.

        Qobuz exposes no search endpoint that works without an application id we
        do not have. Returning [] is honest; the deep link from `buy_url` is how
        a user reaches the store.
        """
        return []

    # --- ownership ------------------------------------------------------

    def list_owned(self, since: int | None = None) -> Iterator[OwnedItem]:
        page = self.http.get(DOWNLOADS_PATH).text()
        if self._is_signin(page):
            raise StoreAuthError("Qobuz session is not signed in", code="signed_out",
                                 provider_id=self.id)
        for entry_id, row_text in self._rows(page):
            item = self._owned_item(entry_id, row_text)
            if item is not None:
                yield item

    def expand(self, item: OwnedItem) -> Iterator[OwnedItem]:
        yield item

    @staticmethod
    def _entry_ids(page: str) -> list[str]:
        return re.findall(r"/account/download/(\d+)/1", page)

    @staticmethod
    def _rows(page: str) -> list[tuple[str, str]]:
        """Each purchase as (entry_id, rendered row text).

        The window is generous because the row's own labels are what get
        anchored on; leading page furniture is harmless.
        """
        rows, prev = [], 0
        for m in re.finditer(r"/account/download/(\d+)/1", page):
            rows.append((m.group(1), _strip_html(page[prev:m.end()])[-2000:]))
            prev = m.end()
        return rows

    def _options(self, entry_id: str) -> tuple[str | None, str, str]:
        """(order_id, album, artist) from a purchase's options page."""
        try:
            page = self.http.get(f"/account/download/{entry_id}/1").text()
        except TransientError:
            raise
        except Exception as exc:
            self.log.warning("options page unreadable", context={"entry": entry_id, "err": str(exc)})
            return None, "", ""
        order = re.search(rf"/account/download/track/{entry_id}/1/(\d+)/", page)
        artist_m = re.search(r'"(?:artistName|performer)":"([^"]*)"', page)
        album_m = re.search(r'"album":"([^"]*)"', page)
        artist = _jstr(artist_m.group(1)) if artist_m else ""
        album = _jstr(album_m.group(1)) if album_m else ""
        # The album field renders as "Artist - Album".
        if artist and album.lower().startswith(artist.lower() + " - "):
            album = album[len(artist) + 3:]
        return (order.group(1) if order else None), album, artist

    @staticmethod
    def split_row(row_text: str, album: str, *artist_hints: str) -> tuple[str, str]:
        """Split a listing row into (track_title, artist), or ("", "") if it cannot be.

        `artist_hints` are candidate artist spellings. The options page names the
        composer for classical releases while the row carries the performer, so
        more than one hint may be needed to find the boundary. A hint only
        locates the split; it grants nothing, because the recovered title is
        still compared by the matcher afterwards.
        """
        end = row_text.rfind(_ROW_END)
        if end < 0:
            return "", ""
        start = row_text.rfind(_ROW_START, 0, end)
        if start < 0:
            return "", ""
        span = re.sub(r"\s+", " ", row_text[start + len(_ROW_START):end]).strip()
        album = re.sub(r"\s+", " ", album or "").strip()
        if not album:
            return "", ""
        wanted = {_fold(h) for h in artist_hints if _fold(h)}
        low, target = span.lower(), album.lower()
        at = low.find(target)
        while at >= 0:
            title, who = span[:at].strip(), span[at + len(album):].strip()
            if title and (not wanted or _fold(who) in wanted):
                return title, who
            at = low.find(target, at + 1)
        return "", ""

    def _owned_item(self, entry_id: str, row_text: str) -> OwnedItem | None:
        order, album, artist = self._options(entry_id)
        if not order:
            return None
        title, row_artist = self.split_row(row_text, album, artist)
        if not title:
            # No hint at all, which accepts whatever name trails the album.
            # Enumeration is not matching: reporting an item the matcher will
            # judge is safe, whereas skipping it loses a purchase outright. A
            # classical release reaches here, because the options page names the
            # composer while the row carries the performer.
            title, row_artist = self.split_row(row_text, album)
        if not title:
            # Reporting an item whose title could not be established would hand
            # the matcher a blank to score against, and a blank matches nothing
            # or everything depending on the comparison. Omit it instead.
            self.log.warning("could not recover a title", context={"entry": entry_id})
            return None
        quality = re.search(r"\bQuality\s+(\S+)", row_text)
        return OwnedItem(
            store=self.id,
            item_key=entry_id,
            kind="track",
            artist=row_artist or artist,
            title=title,
            release_title=album or None,
            parent_key=None,
            purchased_at=None,
            duration_s=None,        # the download pages do not state a duration
            track_number=None,
            formats=("flac",) if (quality and "res" in quality.group(1).lower()) else ("flac", "mp3"),
            ids=Identifiers(store_track_id=entry_id),
            raw={"order": order, "album": album, "options_artist": artist},
        )

    # --- fetch ----------------------------------------------------------

    def download(self, item: OwnedItem, dest_dir: Path, prefer: Sequence[str],
                 progress) -> "DownloadResult":
        from ..models import DownloadResult

        order = (item.raw or {}).get("order")
        if not order:
            order, _, _ = self._options(item.item_key)
        if not order:
            raise StoreFormatUnavailable("no download order for this purchase",
                                         code="no_order", provider_id=self.id)

        codes: list[int] = []
        for family in prefer:
            codes.extend(FORMAT_IDS.get(family.lower(), ()))
        if not codes:
            raise StoreFormatUnavailable(f"Qobuz offers none of {list(prefer)}",
                                         code="no_format", provider_id=self.id)

        dest_dir.mkdir(parents=True, exist_ok=True)
        for code in codes:
            url = self._signed_url(item.item_key, order, code)
            if not url:
                continue
            result = self._fetch(url, dest_dir, code, progress)
            if result is not None:
                return DownloadResult(
                    path=result[0], requested_format=FORMAT_NAMES.get(code, str(code)),
                    bytes=result[1], source_host="qobuz", is_archive=False,
                    notes={"format_id": code},
                )
        raise StoreFormatUnavailable("no requested format could be downloaded",
                                     code="no_format", provider_id=self.id)

    def _signed_url(self, entry_id: str, order: str, code: int) -> str | None:
        try:
            body = self.http.get(f"/account/download/track/{entry_id}/1/{order}/{code}").json()
        except Exception:
            return None
        return body.get("url") if isinstance(body, dict) else None

    def _fetch(self, url: str, dest_dir: Path, code: int, progress) -> tuple[Path, int] | None:
        from urllib.parse import unquote

        resp = self.http.get(url)
        disposition = resp.headers.get("Content-Disposition", "")
        match = re.search(r'filename="([^"]+)"', disposition)
        name = unquote(match.group(1)) if match else f"qobuz_{code}.flac"
        dest = dest_dir / re.sub(r"[/:]", "-", name)
        dest.write_bytes(resp.body)
        size = dest.stat().st_size
        try:
            progress("download", bytes=size, total=size, format=FORMAT_NAMES.get(code))
        except Exception:
            pass        # progress reporting must never fail a download
        if size < 100_000:
            # Anything this small is an error page or a truncated transfer, not
            # audio. Verification proper is the pipeline's job, but publishing a
            # 2KB "file" would waste the whole claim.
            dest.unlink(missing_ok=True)
            return None
        return dest, size
