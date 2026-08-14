#!/usr/bin/env bats

# ============================================================================
# Test suite for lifecycle shell scripts under scripts/
#
# Each test creates an isolated temporary directory that mimics the Wywy-Docs
# repo layout (docs/, internal/, wywy_docs/, scripts/) with sample .mdx
# content, builds an FTS5 index, then invokes the script under test.
# Servers are launched on a per-test ephemeral port so bare-metal runs never
# collide with production on the default port (2530) or with each other.
# ============================================================================

get_free_port() {
    python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
'
}

setup() {
    REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    TEST_DIR=$(mktemp -d)
    TEST_PORT=$(get_free_port)

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
    cp "$REPO_ROOT/src/wywy_docs/models.py"   "$TEST_DIR/wywy_docs/models.py"
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
    # NOTE: no `run` wrapper — bats `run` treats `VAR=x cmd` as a literal
    # command name and fails with 127.  Assert on the pid file instead.
    PORT=$TEST_PORT ./scripts/start-server.sh
    [ -f wywy_docs/server.pid ]
    local pid
    pid=$(cat wywy_docs/server.pid)
    [[ "$pid" =~ ^[0-9]+$ ]]
    # Verify the process is actually running
    kill -0 "$pid" 2>/dev/null
}

@test "start-server.sh: server is reachable via SSE endpoint" {
    cd "$TEST_DIR"
    PORT=$TEST_PORT ./scripts/start-server.sh

    # Bare-metal hosts can be heavily loaded; a cold first import of mcp can
    # take tens of seconds, so match the pytest-side SERVER_TIMEOUT=60.
    local deadline=$((SECONDS + 60))
    local ok=false
    while [ $SECONDS -lt $deadline ]; do
        if python3 -c "
from urllib.request import urlopen
ok = False
try:
    r = urlopen('http://127.0.0.1:$TEST_PORT/sse', timeout=2)
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
    PORT=$TEST_PORT ./scripts/start-server.sh
    local pid
    pid=$(cat wywy_docs/server.pid)
    kill -0 "$pid" 2>/dev/null  # confirm process is alive

    run ./scripts/stop-server.sh
    [ "$status" -eq 0 ]
    ! kill -0 "$pid" 2>/dev/null
}

@test "stop-server.sh: removes server.pid" {
    cd "$TEST_DIR"
    PORT=$TEST_PORT ./scripts/start-server.sh
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

# ===========================================================================
# PORT forwarding tests (start-server.sh)
# ===========================================================================

@test "start-server.sh: forwards PORT as --port to Python" {
    cd "$TEST_DIR"
    PORT=$TEST_PORT ./scripts/start-server.sh

    # PID file must exist
    [ -f wywy_docs/server.pid ]
    local pid
    pid=$(cat wywy_docs/server.pid)
    [[ "$pid" =~ ^[0-9]+$ ]]

    # Give the process a moment to start
    sleep 1

    # The process must be alive
    kill -0 "$pid" 2>/dev/null

    # Read the full command line from /proc
    # The cmdline file uses NUL bytes as separators; translate to spaces.
    local cmdline
    cmdline=$(cat "/proc/$pid/cmdline" 2>/dev/null | tr '\0' ' ') || true
    echo "cmdline: $cmdline"

    # The command line MUST contain "--port $TEST_PORT"
    echo "$cmdline" | grep -q -- "--port $TEST_PORT"
}

@test "start-server.sh: without PORT no --port argument" {
    # The default port 2530 must be free for this test to be meaningful; if
    # something already listens there (e.g. production), the server cannot
    # bind, so skip instead of producing a misleading failure.
    if ! python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 2530))' 2>/dev/null; then
        skip "port 2530 is in use; cannot test default-port startup"
    fi
    cd "$TEST_DIR"
    ./scripts/start-server.sh

    [ -f wywy_docs/server.pid ]
    local pid
    pid=$(cat wywy_docs/server.pid)
    [[ "$pid" =~ ^[0-9]+$ ]]

    sleep 1
    kill -0 "$pid" 2>/dev/null

    local cmdline
    cmdline=$(cat "/proc/$pid/cmdline" 2>/dev/null | tr '\0' ' ') || true
    echo "cmdline: $cmdline"

    # When PORT is unset, no --port should appear
    ! echo "$cmdline" | grep -q "\-\-port"
}
