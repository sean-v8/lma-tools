#!/usr/bin/env bash
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
constraints="$(mktemp)"
trap 'rm -f -- "$constraints"' EXIT
uv export --locked --project "$repo" --no-dev --no-emit-project \
    --no-hashes --output-file "$constraints" >/dev/null
uv tool install --python 3.12 --reinstall --editable "$repo" \
    --constraints "$constraints" --with-executables-from internetarchive

# Remove a superseded standalone tool only after installation succeeds.
installed_tools="$(uv tool list)"
while read -r installed_tool installed_details; do
    if [[ "$installed_tool" == lma-views ]]; then
        uv tool uninstall lma-views
    fi
done <<< "$installed_tools"
