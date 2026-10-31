#!/usr/bin/env bash
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
constraints="$(mktemp)"
trap 'rm -f -- "$constraints"' EXIT
uv export --locked --project "$repo" --no-dev --no-emit-project \
    --no-hashes --output-file "$constraints" >/dev/null
uv tool install --python 3.12 --reinstall --editable "$repo" \
    --constraints "$constraints" --with-executables-from internetarchive
