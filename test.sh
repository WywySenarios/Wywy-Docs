#!/usr/bin/env bash
set -euo pipefail

# $PWD change resistance
cd "$(dirname "$(realpath "$0")")"

# Test runner: runs pytest directly on the host, no Docker required.
# Isolation is provided by tests/conftest.py (env sanitization, per-test
# temp roots) and per-test ephemeral ports, not by a container.
#
# CI runs the same command inside a uv container job
# (see .github/workflows/ci.yml).

# Prefer the project venv; fall back to uv if it is missing.
if [ ! -x ".venv/bin/python" ]; then
	echo "==> Missing .venv — running uv sync..."
	uv sync
fi

echo "==> Python tests..."
.venv/bin/python -m pytest tests/ -v
