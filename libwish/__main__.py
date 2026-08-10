"""Command line entry point: `python -m libwish <command>`."""

from __future__ import annotations

import sys

from .settings import Settings

USAGE = """usage: python -m libwish <command>

  serve              run the web application
  migrate            bring the database up to date and exit
  version            print the schema version and exit
  import-watermarks  adopt the previous application's poll positions
"""


def _import_watermarks(stack_dir: str) -> int:
    """Carry the old flat-file poll positions into source_state.

    The previous application kept its high-water marks in `queue/lastfm_hw.txt`
    and `queue/lb_hw.txt`. SQL cannot read files, so this cannot live in a
    migration. Without it the first poll after cutover starts from nothing and
    re-queues the entire loved-tracks history, most of which the user has
    already decided about.

    The mark is written as an inclusive cursor, matching how the sources compare
    it, so the boundary second is re-delivered once and deduplicated on ingest.
    """
    import json
    import time
    from pathlib import Path

    from . import db
    from .settings import Settings

    settings = Settings.from_env()
    settings.ensure_dirs()
    db.migrate(settings.db_path)

    files = {"lastfm": Path(stack_dir) / "queue" / "lastfm_hw.txt",
             "listenbrainz": Path(stack_dir) / "queue" / "lb_hw.txt"}
    conn = db.connect(settings.db_path)
    imported = 0
    try:
        for source_id, path in files.items():
            if not path.is_file():
                print(f"  {source_id}: no watermark at {path}, will start from seed")
                continue
            try:
                mark = int(path.read_text().strip())
            except ValueError:
                print(f"  {source_id}: {path} does not hold a timestamp, skipping")
                continue
            conn.execute(
                "INSERT INTO source_state(source_id, enabled, mode, cursor, health,"
                " last_attempt_at) VALUES(?,1,'incremental',?, 'ok', ?)"
                " ON CONFLICT(source_id) DO UPDATE SET cursor=excluded.cursor",
                (source_id, json.dumps({"after": mark}), int(time.time())),
            )
            print(f"  {source_id}: adopted {mark} ({time.strftime('%Y-%m-%d %H:%M', time.gmtime(mark))} UTC)")
            imported += 1
    finally:
        conn.close()
    return imported


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "serve"

    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if command == "serve":
        from .web.app import serve
        serve()
        return 0

    if command == "migrate":
        from . import db
        settings = Settings.from_env()
        settings.ensure_dirs()
        print(db.migrate(settings.db_path))
        return 0

    if command == "import-watermarks":
        if len(argv) < 2:
            print("usage: python -m libwish import-watermarks <old-stack-dir>", file=sys.stderr)
            return 2
        print(f"imported {_import_watermarks(argv[1])} watermark(s)")
        return 0

    if command == "version":
        from . import db
        print(db.version(Settings.from_env().db_path))
        return 0

    print(f"unknown command {command!r}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
