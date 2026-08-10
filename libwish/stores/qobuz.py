"""Qobuz, as a store you already bought from.

Qobuz publishes no purchase API, so ownership is read from the server-rendered
download pages with the logged-in session cookie.

Every purchase is one row of the downloads table, and the row's own markup
names each field: the track title as an attribute on the title link, the album
and the performer as their own links beside it, and a download link carrying
the two numbers that identify the purchase.

    /account/download/68647832/6
                      ^ order  ^ line

The order is the transaction and the line is the item within it, so neither
number identifies a purchase on its own. Buying five tracks at once produces
five rows sharing one order, whose lines are not necessarily consecutive.
Reading only line 1, which is what this did until the markup was looked at
properly, collapses such an order into a single purchase and loses the rest
without saying so.

What this provider must never do is decide whether a purchase is the track
someone asked for. It reports what it found; the pipeline matches. Scoring a
claim against the row as a whole is precisely how a request for CHVRCHES
"Lies" once collected their `Such Great Heights (From "Tell Me Lies Season
3")`: the word occurs in the tie-in credit. Reading each labelled field on its
own is what keeps an album and a credit out of a title.
"""

from __future__ import annotations

import html as H
import re
from pathlib import Path
from typing import Any, Iterator, Sequence

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

# The class the storefront marks each purchase with. Splitting on it is what
# bounds a row, so every field below is searched within one purchase rather
# than across a window that may have run into its neighbour.
_ROW_CLASS = "account-purchases__table-row"

_LINK = re.compile(r"/account/download/(\d+)/(\d+)")
_TITLE = re.compile(r'"account-purchases__album-title"\s*>(.*?)</span>', re.S)
_ALBUM = re.compile(r'account-purchases__track--favorites"[^>]*>(.*?)</a>', re.S)
_ARTIST = re.compile(r'"account-purchases__album-artist"\s*>(.*?)</span>', re.S)
_QUALITY = re.compile(
    r'table-header--quality.*?"account-purchases__date"\s*>(.*?)</span>', re.S)
_DATE = re.compile(
    r'table-header--date.*?"account-purchases__date"\s*>(.*?)</span>', re.S)
# The exact string a link states, where the element's own text is the same
# name padded with the template's indentation.
_ATTR_TITLE = re.compile(r'title="([^"]*)"')


def _strip_html(fragment: str) -> str:
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


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
            res = self.http.get(DOWNLOADS_PATH)
        except Exception as exc:
            return StoreHealth(ok=False, authed=False, detail=f"cannot reach Qobuz: {exc}",
                               checked_at=int(time.time()), owned_count=None)
        if self._is_signin(res):
            return StoreHealth(ok=True, authed=False, detail="signed out",
                               checked_at=int(time.time()), owned_count=None)
        count = len(self._rows(res.text()))
        return StoreHealth(ok=True, authed=True, detail="", checked_at=int(time.time()),
                           owned_count=count)

    @staticmethod
    def _is_signin(res: Any) -> bool:
        """Whether this response is the sign-in form rather than the account.

        Signed out, Qobuz redirects to the sign-in page and answers 200, so the
        status cannot tell us and the address the response finally came from
        is the fact that can. The body is the wrong place to look: on a real
        signed-out page the first mention of the sign-in path is around 75,000
        characters in, past any window worth scanning, which is why a signed
        out session used to read as an account owning nothing.
        """
        from urllib.parse import urlsplit

        return urlsplit(getattr(res, "url", "") or "").path.rstrip("/").endswith("/signin")

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
        res = self.http.get(DOWNLOADS_PATH)
        if self._is_signin(res):
            raise StoreAuthError("Qobuz session is not signed in", code="signed_out",
                                 provider_id=self.id)
        for row in self._rows(res.text()):
            item = self._owned_item(row)
            if item is not None:
                yield item

    def expand(self, item: OwnedItem) -> Iterator[OwnedItem]:
        """Nothing to expand: the listing is already one row per track.

        An order containing several tracks is not a container in the sense this
        hook exists for. Its tracks are separate rows carrying separate download
        links, so they arrive from `list_owned` already separated.
        """
        yield item

    @classmethod
    def _rows(cls, page: str) -> list[dict[str, str]]:
        """Each purchase, as the fields its own markup labels.

        A block with no download link is not a purchase: the split also yields
        whatever precedes the first row, and the table's own furniture.
        """
        rows: list[dict[str, str]] = []
        for block in page.split(_ROW_CLASS)[1:]:
            link = _LINK.search(block)
            if link is None:
                continue
            order, line = link.groups()
            rows.append({
                "order": order,
                "line": line,
                "title": cls._field(block, _TITLE),
                "album": cls._field(block, _ALBUM),
                "artist": cls._field(block, _ARTIST),
                "quality": cls._field(block, _QUALITY),
                "date": cls._field(block, _DATE),
            })
        return rows

    @staticmethod
    def _field(block: str, pattern: re.Pattern[str]) -> str:
        """One labelled field of a row, preferring what a link states exactly.

        The element's text is the same name wrapped in template indentation,
        which collapses cleanly enough, but a `title` attribute is the name
        with nothing around it and is used wherever the markup carries one.
        """
        found = pattern.search(block)
        if found is None:
            return ""
        inner = found.group(1)
        stated = _ATTR_TITLE.search(inner)
        if stated is not None:
            return re.sub(r"\s+", " ", H.unescape(stated.group(1))).strip()
        return _strip_html(inner)

    def _owned_item(self, row: dict[str, str]) -> OwnedItem | None:
        if not row["title"]:
            # Reporting an item whose title could not be established would hand
            # the matcher a blank to score against, and a blank matches nothing
            # or everything depending on the comparison. Omit it instead.
            self.log.warning("could not recover a title",
                             context={"order": row["order"], "line": row["line"]})
            return None
        # Both numbers, because neither is unique on its own: one order covers
        # every track bought together, and line numbers restart with each order.
        key = f'{row["order"]}/{row["line"]}'
        return OwnedItem(
            store=self.id,
            item_key=key,
            kind="track",
            artist=row["artist"],
            title=row["title"],
            release_title=row["album"] or None,
            parent_key=None,
            purchased_at=None,      # the row states a date, but not which way round
            duration_s=None,        # the download pages do not state a duration
            track_number=None,
            formats=("flac",) if "res" in row["quality"].lower() else ("flac", "mp3"),
            ids=Identifiers(store_track_id=key),
            raw={"order": row["order"], "line": row["line"], "album": row["album"],
                 "quality": row["quality"], "purchased_on": row["date"]},
        )

    # --- fetch ----------------------------------------------------------

    def download(self, item: OwnedItem, dest_dir: Path, prefer: Sequence[str],
                 progress) -> "DownloadResult":
        from ..models import DownloadResult

        order, line = self._key_parts(item)
        token = (item.raw or {}).get("download_id") or self._download_id(order, line)
        if not token:
            raise StoreFormatUnavailable("no download link for this purchase",
                                         code="no_order", provider_id=self.id)

        codes: list[int] = []
        for family in prefer:
            codes.extend(FORMAT_IDS.get(family.lower(), ()))
        if not codes:
            raise StoreFormatUnavailable(f"Qobuz offers none of {list(prefer)}",
                                         code="no_format", provider_id=self.id)

        dest_dir.mkdir(parents=True, exist_ok=True)
        for code in codes:
            url = self._signed_url(order, line, token, code)
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

    @staticmethod
    def _key_parts(item: OwnedItem) -> tuple[str, str]:
        """The order and line this purchase was enumerated as.

        A key recorded before the line number was read is the order alone, and
        line 1 is the right reading of it: line 1 links are the only ones the
        reader of the time matched, so that is the purchase it filed.
        """
        raw = item.raw or {}
        if raw.get("order") and raw.get("line"):
            return str(raw["order"]), str(raw["line"])
        order, _, line = (item.item_key or "").partition("/")
        return order, line or "1"

    def _download_id(self, order: str, line: str) -> str | None:
        """The third number a signed download URL needs, from the options page.

        Only wanted at download time. Enumeration used to fetch this page for
        every row to recover an album name, which the listing states itself.
        """
        try:
            page = self.http.get(f"/account/download/{order}/{line}").text()
        except TransientError:
            raise
        except Exception as exc:
            self.log.warning("options page unreadable",
                             context={"order": order, "line": line, "err": str(exc)})
            return None
        found = re.search(rf"/account/download/track/{order}/{line}/(\d+)/", page)
        return found.group(1) if found else None

    def _signed_url(self, order: str, line: str, token: str, code: int) -> str | None:
        try:
            body = self.http.get(
                f"/account/download/track/{order}/{line}/{token}/{code}").json()
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
