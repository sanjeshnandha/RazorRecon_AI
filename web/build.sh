#!/usr/bin/env bash
# The SPA has no build step on purpose: one self-contained HTML file, zero npm
# dependencies, zero CDN calls, so the demo runs offline next to the API.
# "Building" is a copy. If you later swap in a bundler, replace this script and
# nothing else changes -- FastAPI just serves api/static/index.html.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$here/../api/static"
cp "$here/index.html" "$here/../api/static/index.html"
echo "built -> api/static/index.html ($(wc -c < "$here/index.html") bytes)"
