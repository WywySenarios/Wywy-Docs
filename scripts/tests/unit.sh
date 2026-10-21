#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(readlink -f "$(dirname "${BASH_SOURCE[0]}")/../..")"
cd "$REPO_ROOT"

# Prefer the project venv; fall back to uv if it is missing.
if [ ! -x ".venv/bin/python" ]; then
	echo "==> uv sync"
	uv sync
fi

echo "==> pytest"
.venv/bin/python -m pytest tests/ -v
