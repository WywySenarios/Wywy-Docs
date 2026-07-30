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

if [ -f "wywy_docs/server.pid" ]; then
	pid=$(cat "wywy_docs/server.pid")
	kill "$pid" 2>/dev/null || true
	# Wait up to 10s for graceful shutdown, then force-kill.
	for i in $(seq 1 10); do
		kill -0 "$pid" 2>/dev/null || break
		sleep 1
	done
	if kill -0 "$pid" 2>/dev/null; then
		kill -9 "$pid" 2>/dev/null || true
	fi
	rm -f "wywy_docs/server.pid"
fi
