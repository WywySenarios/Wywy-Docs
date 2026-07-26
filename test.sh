#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Building Docker image..."
docker build -t wywy-docs:test .

echo ""
echo "==> Running tests..."
docker run --rm wywy-docs:test
