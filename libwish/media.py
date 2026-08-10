"""Cover art, held on disk next to the database instead of hotlinked.

An `<img>` pointing at a store's CDN tells that store the reader's IP address
and, one request per row, roughly what is in their wishlist. A self-hosted
application whose whole premise is owning your music cannot hand that out as a
side effect of drawing a grid, so the bytes are fetched once by the server and
served from here afterwards. It also means the grid survives being offline and
survives the store renaming its image paths.

Two rules make the cache safe to serve from.

Nothing is written under its final name until it is complete. A cover is
downloaded, checked, written to a temporary name in the same directory and only
then renamed into place, so a reader either sees a whole image or a 404. The
rename is atomic within one filesystem, and the temporary file is deliberately a
sibling rather than a file in /tmp, which would make the publish step a copy.

Nothing is trusted to be an image because it claims to be one. The leading bytes
decide, and they also decide the extension. A CDN that has rate-limited you
answers with an HTML page, HTTP 200 and often an image content type; written out
under `.jpg` that is a plausible-looking file of the right name and the wrong
everything, which is the same failure the download verifier exists to catch.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import VerificationFailed
from .http import HttpClient
from .log import get

log = get("media")

# Signatures that sit at offset 0. Any of these identifies the whole format.
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)

# Signatures that sit behind a length or a box header, so offset 0 names the
# container and not the format. Checked as (offset, bytes) pairs.
BOXED: tuple[tuple[tuple[tuple[int, bytes], ...], str], ...] = (
    (((0, b"RIFF"), (8, b"WEBP")), "webp"),
    (((4, b"ftypavif"),), "avif"),
    (((4, b"ftypavis"),), "avif"),
    (((4, b"ftypheic"),), "heic"),
    (((4, b"ftypmif1"),), "heic"),
)

# Small enough that no real cover is refused, large enough that an HTML error
# page or a zero-length body never reaches the magic check with something that
# happens to start correctly.
MIN_BYTES = 256

# Covers are square art a few hundred pixels wide. Anything past this is either
# a mistake or someone using the cache as storage, and the interface would not
# render the extra bytes anyway.
MAX_BYTES = 4_000_000

# Enough of the body to recognise every signature above.
_PROBE = 16


def detect(data: bytes) -> str:
    """The file extension for these bytes, from the bytes themselves.

    Raises `VerificationFailed` rather than returning a default. There is no
    safe default: guessing `jpg` for unrecognised data is exactly how an error
    page gets stored under a name the interface will happily put in an `<img>`.
    """
    if len(data) < MIN_BYTES:
        head = data[:120].decode("utf-8", "replace")
        raise VerificationFailed(
            f"cover is only {len(data)} bytes, too small to be an image. "
            f"Starts with: {head!r}"
        )
    if len(data) > MAX_BYTES:
        raise VerificationFailed(
            f"cover is {len(data)} bytes, over the {MAX_BYTES} byte cap"
        )
    for magic, ext in MAGIC:
        if data.startswith(magic):
            return ext
    for probes, ext in BOXED:
        if all(data[at:at + len(sig)] == sig for at, sig in probes):
            return ext
    raise VerificationFailed(
        f"cover does not begin with any known image signature "
        f"(first bytes {data[:_PROBE]!r}); the source may have returned an error page"
    )


class CoverCache:
    """Local cover art, one file per track, named by the track's row id.

    `path_for` returns the cached file or None, because the extension is
    whatever the bytes turned out to be and a caller cannot know it in advance.
    A serving route reads that None as a 404 and lets the interface draw its own
    placeholder, which is the same thing it draws for a track whose cover has
    not been fetched yet.
    """

    def __init__(
        self,
        config_dir: Path | str,
        *,
        http: HttpClient | None = None,
        user_agent: str = "library-wishlist/1.0 (+self-hosted)",
        timeout: int = 20,
    ) -> None:
        self.dir = Path(config_dir) / "covers"
        self._http = http
        self._user_agent = user_agent
        self._timeout = timeout

    # ---- naming ----

    @staticmethod
    def key(track_id: int | str) -> str:
        """The filename stem for a track.

        Coercing to an integer is the whole of the sanitisation, and it is
        sufficient: a row id has no representation that escapes a directory, so
        a caller passing `../../etc/passwd` fails here with a ValueError instead
        of naming a path outside the cache.
        """
        return str(int(track_id))

    def path_for(self, track_id: int | str) -> Path | None:
        """The cached cover for this track, or None if there is not one."""
        stem = self.key(track_id)
        if not self.dir.is_dir():
            return None
        for path in sorted(self.dir.glob(f"{stem}.*")):
            if path.is_file():
                return path
        return None

    def exists(self, track_id: int | str) -> bool:
        return self.path_for(track_id) is not None

    # ---- writing ----

    def store(self, track_id: int | str, data: bytes) -> Path:
        """Check these bytes and put them in the cache under the right extension.

        The temporary file is a sibling of the destination so the publish step
        is a rename. A reader listing the directory during a write sees a
        `.part-` name it does not match, never a half-written `<id>.jpg`.
        """
        ext = detect(data)
        stem = self.key(track_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        dest = self.dir / f"{stem}.{ext}"

        fd, tmp_name = tempfile.mkstemp(prefix=f".part-{stem}-", dir=str(self.dir))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            # mkstemp creates the file private to this process; the cache is
            # read by whatever serves it, which may not be this uid.
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        # One cover per track. A previous fetch that produced a different format
        # would otherwise leave a second file that `path_for` could return
        # instead of the one just written.
        for stale in self.dir.glob(f"{stem}.*"):
            if stale != dest and stale.is_file():
                stale.unlink(missing_ok=True)
        return dest

    def ensure(self, track_id: int | str, url: str) -> Path:
        """The cached cover, downloading it first if it is not there yet.

        A cover that is already cached is never re-fetched. Artwork for a
        released recording does not change, and re-fetching it would put a
        request to the store back on every page load, which is the thing the
        cache exists to remove.
        """
        existing = self.path_for(track_id)
        if existing is not None:
            return existing
        if not url:
            raise VerificationFailed(f"no cover url for track {track_id}")
        resp = self._client().get(url, timeout=self._timeout)
        path = self.store(track_id, resp.body)
        log.info("cached cover", context={"track": self.key(track_id),
                                          "bytes": len(resp.body), "file": path.name})
        return path

    def forget(self, track_id: int | str) -> int:
        """Drop the cached cover. Returns how many files were removed."""
        stem = self.key(track_id)
        removed = 0
        if not self.dir.is_dir():
            return removed
        for path in self.dir.glob(f"{stem}.*"):
            if path.is_file():
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def sweep_partials(self) -> int:
        """Delete temporary files left by a process that died mid-write.

        A partial has by definition not been checked, so there is nothing to
        recover from it and keeping it only grows the volume.
        """
        removed = 0
        if not self.dir.is_dir():
            return removed
        for path in self.dir.glob(".part-*"):
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def _client(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(user_agent=self._user_agent,
                                    timeout=self._timeout, provider_id="covers")
        return self._http


__all__ = ["BOXED", "CoverCache", "MAGIC", "MAX_BYTES", "MIN_BYTES", "detect"]
