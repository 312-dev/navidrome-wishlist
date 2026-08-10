#!/usr/bin/env python3
"""Render every template against fake rows and fail on the first Jinja error.

This exists because a template error is invisible until someone loads the exact
page and the exact row state that triggers it, and several of the states here
(a refusal, an interrupted claim, an owned row whose file details were never
recorded) are ones a developer will not casually reach. The fixtures below cover
all five plate states on purpose.

It runs against Jinja directly rather than through Flask, so it needs no
database and no application factory. Output goes to a directory you can open in
a browser.

    python3 tools/render_check.py [outdir]
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, StrictUndefined  # noqa: E402

from libwish.web import views  # noqa: E402

DAY = 24 * 3600
NOW = int(time.time())

ROWS = [
    {
        "id": 1, "artist": "CHVRCHES", "title": "Lies", "status": "queued",
        "added_at": NOW - 3600, "cover_url": "/static/nothing.jpg",
        "chosen_source": "qobuz", "source_platform": "lastfm",
    },
    {
        "id": 2, "artist": "Billy Bragg", "title": "Hesitating Beauty",
        "status": "queued", "added_at": NOW - 7200, "cover_url": None,
        "chosen_source": "bandcamp", "source_platform": "listenbrainz",
    },
    {
        # No store to buy at. The plate says so rather than showing an empty box.
        "id": 3, "artist": "Slowdive", "title": "Souvlaki Space Station",
        "status": "queued", "added_at": NOW - 9000, "cover_url": None,
        "chosen_source": None, "source_platform": "navidrome",
    },
    {
        # A claim running right now, stopped at the middle phase.
        "id": 4, "artist": "Nils Frahm", "title": "Says", "status": "queued",
        "added_at": NOW - 400, "chosen_source": "qobuz", "source_platform": "lastfm",
        "job": {"id": 91, "kind": "claim", "state": "running", "phase": "match",
                "track_id": 4, "provider_id": "qobuz"},
    },
    {
        # The refusal. Everything the decision rested on is printed.
        "id": 5, "artist": "CHVRCHES", "title": "Lies", "status": "queued",
        "added_at": NOW - 1200, "chosen_source": "qobuz", "source_platform": "lastfm",
        "refusal": {
            "score": 42, "threshold": 90, "store": "qobuz",
            "title": 'Such Great Heights (From "Tell Me Lies Season 3")',
            "artist": "The Postal Service",
            "reasons": "TITLE_MISMATCH, TIE_IN_CREDIT",
            "decided_at": NOW - 1100,
            "candidates": [
                {"score": 42, "title": 'Such Great Heights (From "Tell Me Lies Season 3")',
                 "artist": "The Postal Service", "gate_failed": "title"},
                {"score": 31, "title": "Lies (Live at Brixton)", "artist": "CHVRCHES",
                 "gate_failed": "duration"},
                {"score": 12, "title": "Lie", "artist": "Chvrches", "gate_failed": "title"},
            ],
        },
    },
    {
        # A claim that broke, with the stage named and the fix stated.
        "id": 6, "artist": "Aphex Twin", "title": "Xtal", "status": "queued",
        "added_at": NOW - 2000, "chosen_source": "qobuz", "source_platform": "lastfm",
        "failure": {"phase": "session",
                    "text": "Your Qobuz session expired. Re-seed your cookies, then claim again.",
                    "link": "/setup#stores", "link_label": "Open auth"},
    },
    {
        # Owned, with what actually landed on disk.
        "id": 7, "artist": "Glen Campbell", "title": "Wichita Lineman",
        "status": "purchased", "added_at": NOW - 6 * DAY,
        "purchased_at": NOW - 5 * DAY, "purchased_via": "qobuz",
        "quality": {"codec": "flac", "bit_depth": 24, "sample_rate": 96000},
        "file_path": "Music/Glen_Campbell/Wichita_Lineman.flac",
        "quality_note": "listed 24/96, file is 16/44",
    },
    {
        # Owned, but nothing was recorded about the file. The plate stays quiet
        # rather than inventing a format.
        "id": 8, "artist": "Grouper", "title": "Heavy Water", "status": "owned",
        "added_at": NOW - 9 * DAY, "purchased_at": NOW - 8 * DAY,
        "purchased_via": "bandcamp",
    },
]

COUNTS = {"wanted": 6, "owned": 2, "ignored": 3}

IGNORED = [
    {"id": 20, "artist": "Justice", "title": "D.A.N.C.E.", "status": "ignored",
     "added_at": NOW - 3 * DAY, "cover_url": None},
    {"id": 21, "artist": "Boards of Canada", "title": "Roygbiv", "status": "ignored",
     "added_at": NOW - 4 * DAY, "cover_url": None},
]


def env() -> Environment:
    e = Environment(
        loader=FileSystemLoader(str(ROOT / "libwish" / "web" / "templates")),
        autoescape=True,
        undefined=StrictUndefined,
    )
    e.globals.update(
        asset=lambda p: f"/static/{p}?v=check",
        API=views.API,
        CLAIM_STAGES=views.CLAIM_STAGES,
        PHASE_ORDER=views.PHASE_ORDER,
        claim_stages_json=json.dumps(
            {"stages": views.CLAIM_STAGES, "order": list(views.PHASE_ORDER)}
        ),
        music_dir="/music",
        now=NOW,
    )
    return e


def main() -> int:
    # Outside the repository by default. These are throwaway artefacts and a
    # directory of them inside the tree is one more thing to keep out of git.
    out = Path(sys.argv[1] if len(sys.argv) > 1
               else Path(tempfile.gettempdir()) / "libwish-render-check")
    out.mkdir(parents=True, exist_ok=True)
    e = env()

    wanted = [views.view_track(r, "date") for r in ROWS if r["status"] == "queued"]
    owned = [views.view_track(r, "date") for r in ROWS if r["status"] in ("purchased", "owned")]
    ignored = [views.view_track(r, "date") for r in IGNORED]

    states = sorted({t["state"] for t in wanted + owned})
    print("plate states exercised:", ", ".join(states))
    expected = {"no_store", "owned", "refused", "wanted", "working"}
    missing = expected - set(states)
    if missing:
        print("FAIL: fixtures never reach", ", ".join(sorted(missing)))
        return 1

    pages = {
        "wanted.html": ("pages/wanted.html", {
            "view": "wanted", "counts": COUNTS, "group_by": "date",
            "groups": views.group_tracks(wanted, "date"), "total": len(wanted),
        }),
        "wanted-by-artist.html": ("pages/wanted.html", {
            "view": "wanted", "counts": COUNTS, "group_by": "artist",
            "groups": views.group_tracks(
                [views.view_track(r, "artist") for r in ROWS if r["status"] == "queued"],
                "artist"),
            "total": len(wanted),
        }),
        "wanted-empty.html": ("pages/wanted.html", {
            "view": "wanted", "counts": {"wanted": 0, "owned": 2, "ignored": 3},
            "group_by": "date", "groups": [], "total": 0,
        }),
        "owned.html": ("pages/owned.html", {
            "view": "owned", "counts": COUNTS,
            "groups": views.group_tracks(owned, "date"), "total": len(owned),
        }),
        "owned-empty.html": ("pages/owned.html", {
            "view": "owned", "counts": COUNTS, "groups": [], "total": 0,
        }),
        "ignored.html": ("pages/ignored.html", {
            "view": "ignored", "counts": COUNTS, "tracks": ignored,
        }),
        "ignored-empty.html": ("pages/ignored.html", {
            "view": "ignored", "counts": COUNTS, "tracks": [],
        }),
        "first-run.html": ("pages/first_run.html", {
            "view": "setup", "counts": {"wanted": 0, "owned": 0, "ignored": 0},
        }),
        "partial-row.html": ("partials/row.html", {"t": wanted[0], "view": "wanted"}),
        "partial-rows.html": ("partials/rows.html", {"tracks": wanted, "view": "wanted"}),
        "partial-plate-refused.html": ("partials/plate.html", {"t": wanted[4]}),
        "partial-plate-owned.html": ("partials/plate.html", {"t": owned[0]}),
        "partial-detail-refusal.html": ("partials/detail.html", {"t": wanted[4]}),
        "partial-detail-claim.html": ("partials/detail.html", {"t": wanted[3]}),
        "partial-detail-failure.html": ("partials/detail.html", {"t": wanted[5]}),
        "partial-skeleton.html": ("partials/skeleton.html", {}),
    }

    failed = 0
    for name, (template, ctx) in pages.items():
        try:
            html = e.get_template(template).render(**ctx)
        except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
            print(f"FAIL {name} <- {template}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        (out / name).write_text(html, encoding="utf-8")
        print(f"ok   {name:32s} {len(html):>7,} bytes")

    # The refusal is the screen the whole identity layer exists for, so its two
    # contractual strings are asserted rather than eyeballed.
    refusal_html = (out / "partial-detail-refusal.html").read_text(encoding="utf-8")
    for needle, why in (
        ("Nothing was downloaded.", "the outcome must be stated before the explanation"),
        ("42 / 90", "score and threshold must both be shown, in that form"),
        ("Tell Me Lies Season 3", "the rejected candidate must be printed verbatim"),
    ):
        if needle not in refusal_html:
            print(f"FAIL refusal: {why} ({needle!r} missing)")
            failed += 1

    print(f"\n{len(pages) - failed} of {len(pages)} templates rendered into {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
