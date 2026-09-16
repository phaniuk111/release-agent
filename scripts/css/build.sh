#!/usr/bin/env bash
# Regenerate src/release_agent/static/vendor/tailwind.css from the classes the
# portal uses. Run from anywhere; commit the result. Needs the Tailwind v3.4.17
# standalone CLI (a single binary, no Node or npm):
#   https://github.com/tailwindlabs/tailwindcss/releases/tag/v3.4.17
# put it on PATH as `tailwindcss`, or point TAILWIND_CLI at it.
set -euo pipefail
cd "$(dirname "$0")/../.."
cli="${TAILWIND_CLI:-tailwindcss}"
if ! command -v "$cli" >/dev/null 2>&1; then
    echo "Tailwind CLI not found — set TAILWIND_CLI or put 'tailwindcss' on PATH (v3.4.17)." >&2
    exit 1
fi
version="$("$cli" --help 2>&1 | grep -o "tailwindcss v[0-9.]*" | head -1 || true)"
case "$version" in
    *"v3.4.17"*) ;;
    *) echo "Expected Tailwind v3.4.17 (what the page was built against), got: $version" >&2; exit 1 ;;
esac
"$cli" -c scripts/css/tailwind.config.js -i scripts/css/input.css \
       -o src/release_agent/static/vendor/tailwind.css --minify
