#!/usr/bin/env bash
set -euo pipefail

# This script runs inside the Docker container.
cd "$(dirname "$0")"

echo "==> Python tests..."
uv run python -m pytest tests/ -v

echo ""
echo "==> BATS tests..."
bats tests/
