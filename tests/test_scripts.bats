#!/usr/bin/env bats

# ============================================================================
# Test suite for lifecycle shell scripts under scripts/
#
# Each test creates an isolated temporary directory that mimics the Wywy-Docs
# repo layout (docs/, internal/, wywy_docs/, scripts/) with sample .mdx
# content, builds an FTS5 index, then invokes the script under test.
# ============================================================================

setup() {
    REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    TEST_DIR=$(mktemp -d)

    # Create repo-like directory layout
    mkdir -p "$TEST_DIR/docs" "$TEST_DIR/internal" "$TEST_DIR/wywy_docs" "$TEST_DIR/scripts"

    # Sample .mdx file for indexing
    cat > "$TEST_DIR/docs/test.mdx" << 'EOF'
---
title: Test Doc
---
Hello world
EOF

    # Copy the wywy_docs module so Python can import it
    cp "$REPO_ROOT/src/wywy_docs/__init__.py" "$TEST_DIR/wywy_docs/__init__.py"
    cp "$REPO_ROOT/src/wywy_docs/indexer.py"  "$TEST_DIR/wywy_docs/indexer.py"
    cp "$REPO_ROOT/src/wywy_docs/server.py"   "$TEST_DIR/wywy_docs/server.py"

    # Symlink .venv so start-server.sh uses the real project's venv Python
    # (with all dependencies installed) instead of uv run ephemeral env.
    ln -s "$REPO_ROOT/.venv" "$TEST_DIR/.venv"

    # Copy the scripts under test
    cp "$REPO_ROOT/scripts/build-index.sh"  "$TEST_DIR/scripts/build-index.sh"
    cp "$REPO_ROOT/scripts/start-server.sh" "$TEST_DIR/scripts/start-server.sh"
    cp "$REPO_ROOT/scripts/stop-server.sh"  "$TEST_DIR/scripts/stop-server.sh"

    # Build the initial FTS5 index using project venv
    uv run --directory "$REPO_ROOT" python -c "
import os, sys
td = '$TEST_DIR'
sys.path.insert(0, td)
from wywy_docs.indexer import build_index
build_index(
    root_dirs=[os.path.join(td, 'docs'), os.path.join(td, 'internal')],
    db_path=os.path.join(td, 'wywy_docs', 'docs_index.db')
)
"
}

teardown() {
    # Kill any lingering server process
    if [ -f "$TEST_DIR/wywy_docs/server.pid" ]; then
        pid=$(cat "$TEST_DIR/wywy_docs/server.pid")
        kill "$pid" 2>/dev/null || true
        rm -f "$TEST_DIR/wywy_docs/server.pid"
    fi

    # Wipe the entire test tree
    rm -rf "$TEST_DIR"
}

# ===========================================================================
# build-index.sh
# ===========================================================================

@test "build-index.sh: exits 0 and produces wywy_docs/docs_index.db" {
    cd "$TEST_DIR"
    rm -f wywy_docs/docs_index.db
    run ./scripts/build-index.sh
    [ "$status" -eq 0 ]
    [ -f wywy_docs/docs_index.db ]
}

@test "build-index.sh: --force re-indexes all files regardless of mtime" {
    cd "$TEST_DIR"
    local before
    before=$(stat -c %Y wywy_docs/docs_index.db)
    sleep 1
    run ./scripts/build-index.sh --force
    [ "$status" -eq 0 ]
    local after
    after=$(stat -c %Y wywy_docs/docs_index.db)
    [ "$after" -gt "$before" ]
}

# ===========================================================================
# start-server.sh
# ===========================================================================

@test "start-server.sh: starts server and writes numeric PID to server.pid" {
    cd "$TEST_DIR"
    run ./scripts/start-server.sh
    [ "$status" -eq 0 ]
    [ -f wywy_docs/server.pid ]
    local pid
    pid=$(cat wywy_docs/server.pid)
    [[ "$pid" =~ ^[0-9]+$ ]]
    # Verify the process is actually running
    kill -0 "$pid" 2>/dev/null
}

@test "start-server.sh: server is reachable via SSE endpoint within 5 seconds" {
    cd "$TEST_DIR"
    ./scripts/start-server.sh

    local deadline=$((SECONDS + 5))
    local ok=false
    while [ $SECONDS -lt $deadline ]; do
        if python3 -c "
from urllib.request import urlopen
ok = False
try:
    r = urlopen('http://127.0.0.1:2530/sse', timeout=2)
    ok = r.status == 200
except Exception:
    pass
exit(0 if ok else 1)
" 2>/dev/null; then
            ok=true
            break
        fi
        sleep 0.5
    done

    [ "$ok" = true ]
}

# ===========================================================================
# stop-server.sh
# ===========================================================================

@test "stop-server.sh: terminates server process" {
    cd "$TEST_DIR"
    ./scripts/start-server.sh
    local pid
    pid=$(cat wywy_docs/server.pid)
    kill -0 "$pid" 2>/dev/null  # confirm process is alive

    run ./scripts/stop-server.sh
    [ "$status" -eq 0 ]
    ! kill -0 "$pid" 2>/dev/null
}

@test "stop-server.sh: removes server.pid" {
    cd "$TEST_DIR"
    ./scripts/start-server.sh
    [ -f wywy_docs/server.pid ]

    run ./scripts/stop-server.sh
    [ "$status" -eq 0 ]
    [ ! -f wywy_docs/server.pid ]
}

@test "stop-server.sh: handles missing PID file gracefully" {
    cd "$TEST_DIR"
    rm -f wywy_docs/server.pid
    run ./scripts/stop-server.sh
    [ "$status" -eq 0 ]
}
