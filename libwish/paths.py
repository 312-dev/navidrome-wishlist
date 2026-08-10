"""Where files land, and how they get there.

A download is written into a staging directory inside the music volume and only
moved into the library once it has been checked. The move is a rename, which is
atomic within one filesystem, so a scanner watching the library either sees the
finished file or does not see it at all. It never sees a partial one.

That is also why staging lives inside the music volume rather than beside the
database. A staging directory on a different device turns the publish step into
a copy, and a copy can be observed halfway through.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError, LibwishError

# Characters no common filesystem tolerates, plus the ones that make a path
# ambiguous to shell tooling later.
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_DOTS = re.compile(r"^\.+")

MAX_COMPONENT = 120


class FileExists(LibwishError):
    """The destination is already occupied.

    Publishing refuses rather than replacing. A file already sitting at that path
    is evidence that the track is owned, and silently overwriting it would
    discard whatever the user already had, possibly a better master than the one
    just downloaded.
    """


def safe_component(text: str, fallback: str = "unknown") -> str:
    """Turn arbitrary metadata into one safe path component.

    Accents are preserved rather than folded away. This produces a filename a
    person recognises, and unlike matching, where folding prevents a false
    positive, a filename has nothing to compare against.
    """
    text = unicodedata.normalize("NFC", (text or "").strip())
    text = _UNSAFE.sub("-", text)
    text = _DOTS.sub("", text).strip(" .")
    text = re.sub(r"\s+", " ", text)
    if len(text) > MAX_COMPONENT:
        text = text[:MAX_COMPONENT].rstrip()
    return text or fallback


@dataclass(frozen=True)
class PathService:
    """The filesystem surface a provider is allowed to touch.

    Providers receive this rather than raw paths so that a provider cannot write
    outside the volumes the application owns.
    """

    music_dir: Path
    staging_dir: Path

    def staged(self, name: str) -> Path:
        """A path inside staging for an in-progress download."""
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        return self.staging_dir / safe_component(name, "download")

    def library_path(self, artist: str, title: str, *, album: str = "",
                     suffix: str = ".flac") -> Path:
        """Where a finished track belongs in the library."""
        parts = [safe_component(artist, "Unknown Artist")]
        if album:
            parts.append(safe_component(album))
        leaf = safe_component(title, "Untitled") + suffix
        return self.music_dir.joinpath(*parts, leaf)

    def publish(self, staged: Path, dest: Path, *, overwrite: bool = False) -> Path:
        """Move a verified download into the library atomically.

        Raises `FileExists` when the destination is occupied unless the caller
        explicitly asks to overwrite, which only a deliberate re-download does.
        """
        staged = Path(staged)
        dest = Path(dest)
        if not staged.is_file():
            raise ConfigError(f"nothing to publish at {staged}")
        if dest.exists() and not overwrite:
            raise FileExists(f"{dest} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if staged.stat().st_dev != dest.parent.stat().st_dev:
            raise ConfigError(
                f"{staged} and {dest.parent} are on different filesystems, "
                "so this move could not be atomic"
            )
        os.replace(staged, dest)
        return dest

    def discard(self, staged: Path) -> None:
        """Remove a failed download. Never raises; the caller is already failing."""
        try:
            Path(staged).unlink(missing_ok=True)
        except OSError:
            pass

    def sweep_staging(self) -> int:
        """Delete anything left in staging by a process that died mid-download.

        Called at startup. A staged file has by definition not been verified, so
        there is nothing to recover and keeping it would only grow the volume.
        """
        removed = 0
        if not self.staging_dir.is_dir():
            return removed
        for path in self.staging_dir.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed
