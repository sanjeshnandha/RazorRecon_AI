#!/usr/bin/env bash
# The SPA has no build step on purpose: one self-contained HTML file, zero npm
# dependencies, zero CDN calls, so the demo runs offline next to the API.
# "Building" is a copy: web/index.html  ->  api/static/index.html
#
# api/static/index.html is GENERATED. Edit web/index.html and rebuild; an edit
# made directly to the generated file is silently destroyed by the next
# `make serve`, because serve builds first. That has bitten this project once,
# so the copy is now guarded: if the generated file has changed independently of
# its source, this script stops instead of overwriting the change.
#
#   ./web/build.sh          copy, refusing to clobber an unexplained edit
#   FORCE=1 ./web/build.sh  copy regardless (the edit is already carried across)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/index.html"
dst="$here/../api/static/index.html"
mkdir -p "$(dirname "$dst")"

if [ -f "$dst" ] && [ -z "${FORCE:-}" ] && ! cmp -s "$src" "$dst"; then
  if [ "$dst" -nt "$src" ]; then
    echo "refusing to build: api/static/index.html is NEWER than web/index.html" >&2
    echo "" >&2
    echo "  Something edited the generated file directly. Copying now would" >&2
    echo "  destroy that change. Its diff against the source:" >&2
    echo "" >&2
    diff "$src" "$dst" | sed 's/^/    /' >&2 || true
    echo "" >&2
    echo "  Move the change into web/index.html, then rebuild." >&2
    echo "  To discard it instead:  FORCE=1 ./web/build.sh" >&2
    exit 1
  fi
fi

cp "$src" "$dst"
echo "built -> api/static/index.html ($(wc -c < "$src" | tr -d ' ') bytes)"
