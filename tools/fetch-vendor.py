#!/usr/bin/env python3
"""Pull every third-party asset the interface needs into the repo.

Run this on a machine with outbound internet, then commit what it writes. The
runtime image never runs it: a LAN app that may have no route to the internet
cannot fetch a font or a script at page load, and a Docker build that reaches
npm or Google is a build that breaks the day either one is unreachable.

Three destinations, three lifetimes:

  tools/vendor/basecoat/       build-time only, read by the Tailwind CLI
  tools/vendor/.cache/         the standalone binary, ~110MB, must stay out of git
  libwish/web/static/          served to the browser, committed

The font URLs come from the Google Fonts CSS API rather than being hard-coded,
because gstatic filenames carry a content hash that changes whenever the family
is revised. Asking the API for the current ones and downloading them here is the
only part of this that needs the network.
"""

from __future__ import annotations

import re
import stat
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "tools" / "vendor"
STATIC = ROOT / "libwish" / "web" / "static"

BASECOAT_VERSION = "1.0.2"
TAILWIND_VERSION = "v4.3.3"
HTMX_VERSION = "2.0.4"
ALPINE_VERSION = "3.14.9"

# Only the components the interface actually uses. Basecoat ships forty; pulling
# all of them would compile dead CSS for a sidebar and a combobox this app has no
# screen for.
BASECOAT_FILES = [
    "base/base.css",
    "components/button.css",
    "components/input.css",
    "components/label.css",
    "components/native-select.css",
    "styles/vega.css",
]

# A browser UA is required: the Google Fonts CSS API serves woff2 only to clients
# it believes can read it, and returns truetype to anything it does not recognise.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Latin and latin-ext both. Artist names in a real music library are full of
# accented characters, and a missing glyph falls back to a different face
# mid-word, which is more visible than the few KB latin-ext costs.
FONT_SUBSETS = ("latin", "latin-ext")

FONTS = {
    # One file, two apparent typefaces: the width axis is what makes the
    # masthead read as expanded record-label type without a second download.
    "archivo": ("Archivo:wdth,wght@62.5..125,100..900", "archivo-var"),
    "plexmono-400": ("IBM+Plex+Mono:wght@400", "plexmono-400"),
    "plexmono-600": ("IBM+Plex+Mono:wght@600", "plexmono-600"),
}


def fetch(url: str, *, ua: str = "libwish-vendor") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"  {path.relative_to(ROOT)}  {len(data):,} bytes")


def basecoat() -> None:
    print("basecoat-css", BASECOAT_VERSION)
    for name in BASECOAT_FILES:
        url = f"https://cdn.jsdelivr.net/npm/basecoat-css@{BASECOAT_VERSION}/dist/{name}"
        write(VENDOR / "basecoat" / name, fetch(url))


def scripts() -> None:
    print("browser scripts")
    write(STATIC / "js" / "htmx.min.js",
          fetch(f"https://cdn.jsdelivr.net/npm/htmx.org@{HTMX_VERSION}/dist/htmx.min.js"))
    write(STATIC / "js" / "alpine.min.js",
          fetch(f"https://cdn.jsdelivr.net/npm/alpinejs@{ALPINE_VERSION}/dist/cdn.min.js"))


def fonts() -> None:
    print("fonts")
    for key, (family, stem) in FONTS.items():
        css = fetch(
            f"https://fonts.googleapis.com/css2?family={family}&display=swap",
            ua=BROWSER_UA,
        ).decode()
        blocks = css.split("/* ")
        wanted = []
        for block in blocks:
            subset = block.split(" */", 1)[0].strip()
            if subset not in FONT_SUBSETS:
                continue
            match = re.search(r"src: url\((https://[^)]+\.woff2)\)", block)
            if match:
                wanted.append((subset, match.group(1)))
        if not wanted:
            sys.exit(f"no woff2 found for {family}; the CSS API shape changed")
        for subset, url in wanted:
            write(STATIC / "fonts" / f"{stem}-{subset}.woff2", fetch(url, ua=BROWSER_UA))


def tailwind() -> None:
    target = VENDOR / ".cache" / f"tailwindcss-{TAILWIND_VERSION}"
    if target.exists():
        print(f"tailwind {TAILWIND_VERSION} already present")
        return
    print(f"tailwind standalone {TAILWIND_VERSION} (large, one time)")
    url = (f"https://github.com/tailwindlabs/tailwindcss/releases/download/"
           f"{TAILWIND_VERSION}/tailwindcss-linux-x64")
    write(target, fetch(url))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def main() -> int:
    only = sys.argv[1:] or ["basecoat", "scripts", "fonts", "tailwind"]
    for step in only:
        globals()[step]()
    print("\ndone. Commit everything under libwish/web/static/ and tools/vendor/basecoat/.")
    print("tools/vendor/.cache/ holds the 110MB Tailwind binary and must be gitignored;")
    print("tools/build-css.sh refetches it on any machine that lacks it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
