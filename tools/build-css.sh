#!/usr/bin/env bash
# Compile libwish/web/input.css into the stylesheet the app serves.
#
# The output is committed. The Docker build copies it and never runs Tailwind,
# which is why the runtime image needs no Node and no network. Run this whenever
# input.css, tailwind.config.js, or any template's class names change, and
# commit the result alongside them.
#
#   tools/build-css.sh            one shot
#   tools/build-css.sh --watch    rebuild on save while working
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAILWIND_VERSION="v4.3.3"
# Out of the source tree on purpose: it is a 110MB executable, not an asset.
BIN="$ROOT/tools/vendor/.cache/tailwindcss-$TAILWIND_VERSION"
IN="$ROOT/libwish/web/input.css"
OUT="$ROOT/libwish/web/static/css/app.css"

if [ ! -x "$BIN" ]; then
  echo "Tailwind $TAILWIND_VERSION is not here yet. Fetching it once:" >&2
  python3 "$ROOT/tools/fetch-vendor.py" tailwind >&2
fi

if [ ! -d "$ROOT/tools/vendor/basecoat" ]; then
  echo "Basecoat's source CSS is missing. Run: python3 tools/fetch-vendor.py basecoat" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
"$BIN" --input "$IN" --output "$OUT" --minify "$@"

echo "built $OUT ($(wc -c < "$OUT") bytes)"
