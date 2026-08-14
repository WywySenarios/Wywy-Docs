#!/usr/bin/env bash
set -euo pipefail

# $PWD change resistance
cd "$(dirname "$(realpath "$0")")"

# Bare-metal test runner: runs pytest and bats directly on the host, no
# Docker required.  Isolation is provided by tests/conftest.py (env
# sanitization, per-test temp roots) and per-test ephemeral ports, not by
# a container.
#
# CI still runs inside Docker for now:
#   ./test.sh --docker

if [ "${1:-}" = "--docker" ]; then
	echo "==> Building Docker image..."
	docker build -t wywy-docs:test .

	echo ""
	echo "==> Running tests..."
	docker run --rm --network none wywy-docs:test
	exit 0
fi

# Prefer the project venv; fall back to uv if it is missing.
if [ ! -x ".venv/bin/python" ]; then
	echo "==> Missing .venv — running uv sync..."
	uv sync
fi

if ! command -v bats >/dev/null 2>&1; then
	echo "Error: bats is required on the host (npm install -g bats)"
	exit 1
fi

echo "==> Python tests..."
.venv/bin/python -m pytest tests/ -v

echo ""
echo "==> BATS tests..."
bats tests/
