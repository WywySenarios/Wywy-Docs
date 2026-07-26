#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Resolve Python: prefer venv direct path, fall back to uv run
if [ -x ".venv/bin/python" ]; then
	PYTHON=".venv/bin/python"
else
	PYTHON="uv run python"
fi

# Dependency check: verify wywy_docs package is available via project venv
if ! $PYTHON -c "import wywy_docs.indexer" 2>/dev/null; then
	echo "Error: wywy_docs package is not installed. Run: uv sync"
	exit 1
fi

nohup $PYTHON -m wywy_docs.server >/dev/null 2>&1 &
echo "$!" >"wywy_docs/server.pid"
