"""Reading a dropped file's own metadata, far enough to say which track it is.

This is identification, not enrichment. What comes out of here is an artist and
a title good enough to find or create the right row; the cover, the duration and
the MBIDs are then filled in by `enrich.py` exactly as they are for a track that
arrived from a source. A file's own tags are the only thing available at the
moment it lands, and they are the wrong thing to keep afterwards: iTunes writes
"Love Me Tender (2000 Remaster)" as the title, and the matcher already knows
what to do with a version qualifier.

MP4 is the only container parsed here, because an iTunes purchase is the case
this exists for. Two of its details drive the code:

A box is size-then-type, and a size of 1 means the real size is a 64-bit value
sitting after the type, while 0 means the box runs to the end of the file. A
reader that trusts the 32-bit size alone walks off the end of any file with an
`mdat` over 4 GB, which is most albums.

`meta` is a full box, so its children start four bytes late. Not every muxer
writes those four bytes, and a reader that always skips them finds nothing in
the files that omit them. The child list is probed instead of assumed.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

#: How far into the file the tag walk will read. Tags live in `moov`, which
#: iTunes writes before the audio, and a purchased track is a few megabytes of
#: `mdat` that there is no reason to page through. A file whose `moov` is at the
#: end simply reports no tags, which the caller already has to handle.
MAX_SCAN = 4 * 1024 * 1024

#: Names as MP4 spells them. The leading byte is 0xA9, the copyright sign that
#: marks Apple's own text atoms.
_TITLE = b"\xa9nam"
_ARTIST = b"\xa9ART"
_ALBUM = b"\xa9alb"
_ALBUM_ARTIST = b"aART"

#: `data` payload types. Only text is read; the artwork atom is a JPEG and the
#: track number is a packed pair, and neither identifies anything the name does
#: not say better.
_TEXT_TYPES = (1, 18)

#: A box type is four printable ASCII bytes, give or take the 0xA9 prefix. Used
#: to tell a real child list from four bytes of version and flags.
_TYPE_RE = re.compile(rb"[\x20-\x7e\xa9]{4}")


@dataclass(frozen=True)
class FileTags:
    """What a file says about itself. Every field may be empty."""

    artist: str = ""
    title: str = ""
    album: str = ""

    def identifies(self) -> bool:
        """Whether this is enough to file the track against.

        Both halves or neither. An artist with no title cannot be told apart
        from the artist's other purchases, and a title with no artist matches
        every cover version ever recorded.
        """
        return bool(self.artist and self.title)


def read(path: Path | str) -> FileTags:
    """The artist, title and album an MP4 file carries.

    Returns empty fields rather than raising. A file with no usable tags is an
    ordinary outcome that the caller reports to the reader, not an error in the
    parsing.
    """
    try:
        with open(path, "rb") as fh:
            blob = fh.read(MAX_SCAN)
    except OSError:
        return FileTags()
    ilst = _find(blob, (b"moov", b"udta", b"meta", b"ilst"))
    if ilst is None:
        return FileTags()
    items = _items(blob, *ilst)
    artist = items.get(_ARTIST) or items.get(_ALBUM_ARTIST, "")
    return FileTags(artist=artist, title=items.get(_TITLE, ""),
                    album=items.get(_ALBUM, ""))


def has_audio_track(path: Path | str) -> bool:
    """Whether an MP4 file contains a sound track.

    The brand after `ftyp` does not answer this. iTunes sells music videos in
    the same container as songs, and a self-hosted music library is the wrong
    place for one, so the handler each track declares is what decides.

    A file this cannot parse answers False. The caller is deciding whether to
    put it in a music library, and "the structure was unreadable" is not a
    reason to go ahead.
    """
    try:
        with open(path, "rb") as fh:
            blob = fh.read(MAX_SCAN)
    except OSError:
        return False
    moov = _find(blob, (b"moov",))
    if moov is None:
        return False
    for kind, trak_start, trak_end in _boxes(blob, *moov):
        if kind != b"trak":
            continue
        mdia = _descend(blob, trak_start, trak_end, (b"mdia",))
        if mdia is None:
            continue
        for inner, hdlr_start, hdlr_end in _boxes(blob, *mdia):
            # A handler box is four bytes of version and flags, four reserved,
            # then the type. `soun` is the only one worth a music library.
            if inner == b"hdlr" and blob[hdlr_start + 8:hdlr_start + 12] == b"soun":
                return True
    return False


def from_filename(name: str) -> FileTags:
    """A last resort reading of "Artist - Title" out of a filename.

    Deliberately narrow. It takes the one convention that means what it looks
    like and leaves everything else empty, because a wrong guess here files a
    purchase under a name the reader then has to find and undo. A leading track
    number is dropped; anything else stays in the title, qualifiers included,
    since the matcher reads those.
    """
    stem = Path(name).stem.strip()
    stem = re.sub(r"^\d{1,3}[\s._-]+", "", stem)
    parts = re.split(r"\s+-\s+", stem, maxsplit=1)
    if len(parts) != 2:
        return FileTags()
    artist, title = (p.strip() for p in parts)
    return FileTags(artist=artist, title=title) if artist and title else FileTags()


# ---------------------------------------------------------------------------
# MP4 boxes
# ---------------------------------------------------------------------------


def _boxes(blob: bytes, start: int, end: int):
    """Yield `(type, payload_start, payload_end)` for each box in a range.

    Stops on anything malformed rather than guessing. A box that claims to be
    smaller than its own header, or to extend past its parent, means the offsets
    are no longer trustworthy, and continuing from a bad offset finds tag-shaped
    noise in the middle of the audio.
    """
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(blob[pos:pos + 4], "big")
        kind = blob[pos + 4:pos + 8]
        head = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(blob[pos + 8:pos + 16], "big")
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            return
        yield kind, pos + head, pos + size
        pos += size


def _find(blob: bytes, path: tuple[bytes, ...]) -> tuple[int, int] | None:
    """Walk a box path from the top of the file. Returns the payload range."""
    return _descend(blob, 0, len(blob), path)


def _descend(blob: bytes, start: int, end: int,
             path: tuple[bytes, ...]) -> tuple[int, int] | None:
    """Walk a box path inside one box's payload."""
    for want in path:
        for kind, child_start, child_end in _boxes(blob, start, end):
            if kind == want:
                start, end = child_start, child_end
                if want == b"meta":
                    start = _past_full_box_header(blob, start, end)
                break
        else:
            return None
    return start, end


def _past_full_box_header(blob: bytes, start: int, end: int) -> int:
    """Where `meta`'s children actually begin.

    A full box carries a version and flags before its children, but files exist
    that write the child list straight after the header. Whether the four bytes
    are there is decided by looking: a child list starts with a size and a
    four-character type, and four bytes of version and flags do not.
    """
    if start + 8 <= end and _TYPE_RE.fullmatch(blob[start + 4:start + 8]):
        return start
    return min(start + 4, end)


def _items(blob: bytes, start: int, end: int) -> dict[bytes, str]:
    """Every text tag in an `ilst`, keyed by its four-byte name."""
    out: dict[bytes, str] = {}
    for kind, item_start, item_end in _boxes(blob, start, end):
        for inner, data_start, data_end in _boxes(blob, item_start, item_end):
            if inner != b"data" or data_start + 8 > data_end:
                continue
            # Version and type share a word, then four bytes of locale, then the
            # value. The type says how to read the value, and reading a JPEG as
            # text is how a cover ends up in the artist field.
            (type_code,) = struct.unpack(">I", blob[data_start:data_start + 4])
            if (type_code & 0xFFFFFF) not in _TEXT_TYPES:
                continue
            text = blob[data_start + 8:data_end].decode("utf-8", "replace").strip()
            if text and kind not in out:
                out[kind] = text
    return out


__all__ = ["FileTags", "MAX_SCAN", "from_filename", "read"]
