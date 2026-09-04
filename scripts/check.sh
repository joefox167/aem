#!/usr/bin/env bash
# The exact checks CI runs, against the exact pinned tool versions.
# Run before pushing (the pre-push hook does this automatically):
#   scripts/check.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

pinned=$(sed -n 's/^ *"ruff==\([0-9.]*\)",.*/\1/p' pyproject.toml)
installed=$("$PY" -m ruff --version 2>/dev/null | awk '{print $2}') || installed=""

if [[ -z "$installed" ]]; then
    echo "ruff is not installed in $PY -- run: $PY -m pip install -e '.[dev]'" >&2
    exit 1
fi

if [[ -n "$pinned" && "$installed" != "$pinned" ]]; then
    echo "ruff $installed installed, but pyproject pins $pinned." >&2
    echo "Local checks would not match CI. Run: $PY -m pip install -e '.[dev]'" >&2
    exit 1
fi

echo "ruff $installed  (matches the pin)"
"$PY" -m ruff check src tests
"$PY" -m pytest -q
