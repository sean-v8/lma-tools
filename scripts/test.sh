#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"

uv run --locked --group dev ruff format --check .
uv run --locked --group dev ruff check .
uv run --locked --group dev python -B -m tests
