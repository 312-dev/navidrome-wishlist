"""SQLite access and the forward-only migration runner.

Two things here are load-bearing.

The first is adopt-at-1. An existing installation already has the original
`tracks` table, created by an application that had no migration system. Running
`0001_baseline.sql` against it would fail on a table that already exists, so when
an unversioned database is found to already carry that schema its version is
stamped to 1 without executing anything. `0001` therefore only ever runs on a
genuinely empty database, and both paths converge on the same shape.

The second is that every migration is preceded by an online backup taken through
SQLite's own backup API rather than a file copy. A copy of a live database can
catch a write in progress; the backup API takes a consistent snapshot even while
the application is running.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import ConfigError
from .log import get

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

log = get("db")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection configured the way every caller needs it.

    WAL lets the web request handling read while a background job writes, which
    the previous single-file journal mode would have serialised.
    """
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction, rolling back if it raises.

    Do not call `executescript` inside this. That method commits whatever is open
    before it runs anything, so the transaction this opens would be closed out
    from under it and the commit at the end would find nothing active. Migrations
    take the other route below.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _apply_script(conn: sqlite3.Connection, sql: str, version: int) -> None:
    """Apply one migration and its version stamp as a single atomic unit.

    The transaction is written into the script rather than opened around it,
    because `executescript` commits any open transaction before executing. The
    version stamp travels inside the same transaction so that a database can
    never be left carrying a migration's tables while still reporting the
    previous version.
    """
    script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
    try:
        conn.executescript(script)
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass        # already rolled back by the failure itself
        raise


def discover() -> list[tuple[int, str, Path]]:
    """Every migration on disk, ordered, as (version, name, path)."""
    found: list[tuple[int, str, Path]] = []
    if not MIGRATIONS_DIR.is_dir():
        return found
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _NAME_RE.match(path.name)
        if not m:
            raise ConfigError(f"migration {path.name} does not match NNNN_name.sql")
        found.append((int(m.group(1)), m.group(2), path))
    versions = [v for v, _, _ in found]
    if versions != sorted(set(versions)):
        raise ConfigError(f"migration versions must be unique and ordered, got {versions}")
    if versions and versions != list(range(1, len(versions) + 1)):
        raise ConfigError(f"migration versions must start at 1 with no gaps, got {versions}")
    return found


def _carries_legacy_schema(conn: sqlite3.Connection) -> bool:
    """True when this is the pre-migration database of the original application."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracks'"
    ).fetchone()
    if not row:
        return False
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}
    return "dedup_key" in cols


def backup_to(conn: sqlite3.Connection, dest: Path) -> Path:
    """Take a consistent snapshot of a possibly-live database."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(dest)) as target:
        conn.backup(target)
    return dest


def migrate(db_path: Path | str, *, backup: bool = True) -> int:
    """Bring the database up to the newest migration. Returns the version reached."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    migrations = discover()
    conn = connect(db_path)
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]

        if current == 0 and _carries_legacy_schema(conn):
            conn.execute("PRAGMA user_version=1")
            current = 1
            log.info("adopted an existing database at version 1 without executing 0001")

        target = migrations[-1][0] if migrations else 0
        if current > target:
            raise ConfigError(
                f"database is at version {current} but this build only knows {target}; "
                "migrations are forward-only, so downgrading is not possible"
            )
        if current == target:
            log.info("schema up to date", context={"version": current})
            return current

        for version, name, path in migrations:
            if version <= current:
                continue
            if backup:
                dest = db_path.with_name(f"{db_path.stem}.pre-{version:04d}{db_path.suffix}")
                backup_to(conn, dest)
                log.info("backed up before migrating", context={"to": str(dest)})
            log.info("applying migration", context={"version": version, "name": name})
            _apply_script(conn, path.read_text(), version)
            current = version
        return current
    finally:
        conn.close()


def version(db_path: Path | str) -> int:
    conn = connect(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
