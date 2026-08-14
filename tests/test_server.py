"""Tests for ``wywy_docs/server.py`` — HTTP SSE MCP server.

Starts the server as a subprocess and exercises the MCP tool interface
through the SSE transport protocol (raw HTTP + SSE stream reading).

All tests create temporary directory trees with sample .mdx files and an
FTS5 index, then start the server, connect via the SSE transport, and
assert on JSON-RPC responses.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import select
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, suppress
from http import HTTPStatus
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from mcp.types import INVALID_PARAMS

from wywy_docs.indexer import build_index

if TYPE_CHECKING:
    from collections.abc import Generator

HOST = "127.0.0.1"
# Bare-metal hosts can be heavily loaded; a cold first import of mcp can
# take tens of seconds (the 51s figure was measured on this host).  The
# poll loop sleeps 0.2s, so a generous timeout costs nothing on success.
SERVER_TIMEOUT = 60  # max seconds to wait for server startup
RESPONSE_TIMEOUT = 10  # max seconds to wait for a JSON-RPC response
# A warm server (existing index, page cache populated) must start quickly.
# 10s tolerates load spikes while still catching import hangs (30-60s).
STARTUP_BUDGET_SECONDS = 10.0  # max seconds a warm server may take to start

# Server-lifecycle error messages (raised via a constant so the raise
# statements stay short; kept at module level so the strings are greppable).
_ERR_SSE_CLOSED = "SSE stream closed before receiving endpoint event"
_ERR_NO_PROCESS = "Server process was not started"

logger = logging.getLogger(__name__)


# ===========================================================================
# JSON-RPC wire types (the subset the tests read/write)
# ===========================================================================


class JsonRpcError(TypedDict):
    """A JSON-RPC error object as consumed by the tests."""

    code: int
    message: str


class TextContentItem(TypedDict):
    """A ``TextContent`` item inside a JSON-RPC result."""

    type: str
    text: str


class ToolInfo(TypedDict):
    """A tool descriptor returned by ``tools/list``."""

    name: str
    description: str


class JsonRpcResult(TypedDict):
    """The ``result`` payload of a JSON-RPC response."""

    content: list[TextContentItem]
    tools: list[ToolInfo]


class JsonRpcResponse(TypedDict):
    """A JSON-RPC response as consumed by the tests.

    A real response carries exactly one of ``result`` or ``error``; both are
    declared so tests can read either without Optional-wrapping every access.
    Responses are only ever read (never constructed as literals), so this is
    safe.
    """

    result: JsonRpcResult
    error: JsonRpcError


class JsonRpcRequest(TypedDict, total=False):
    """A JSON-RPC request as sent by the tests."""

    jsonrpc: str
    id: int
    method: str
    params: dict[str, object]


# ===========================================================================
# Helpers
# ===========================================================================


def find_free_port() -> int:
    """Return a random ephemeral port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def create_file(
    root: str,
    rel_path: str,
    content: str = "---\ntitle: X\n---\nbody",
) -> str:
    """Create a file at *root*/*rel_path* with *content*.

    Creates parent directories as needed.  Returns the absolute path.
    """
    full_path = Path(root) / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    with full_path.open("w") as f:
        f.write(content)
    return str(full_path)


def setup_temp_wywy_root() -> str:
    """Create a temporary directory that mimics the Wywy-Docs repo layout.

    Returns the path to the temp root.
    """
    root = tempfile.mkdtemp()
    (Path(root) / "docs").mkdir(parents=True)
    (Path(root) / "internal").mkdir(parents=True)
    (Path(root) / "wywy_docs").mkdir(parents=True)
    return root


def _setup_temp_wywy_root_missing(*, docs: bool = False, internal: bool = False) -> str:
    """Create a temporary root with ``docs/``/``internal/`` absent as selected.

    Unlike ``_setup_temp_wywy_root``, the requested section directories are
    NOT created, so the server's ``_ensure_index`` must handle their absence
    (warn + still create an empty index).  Returns the path to the temp root.
    """
    root = tempfile.mkdtemp()
    if docs:
        (Path(root) / "docs").mkdir(parents=True)
    if internal:
        (Path(root) / "internal").mkdir(parents=True)
    return root


def build_test_index(root: str, files: dict[str, str]) -> str:
    """Create sample .mdx *files* under *root* and build the FTS5 index.

    Returns the database path.
    """
    for rel_path, content in files.items():
        create_file(root, rel_path, content)

    db_path = Path(root) / "wywy_docs" / "docs_index.db"
    build_index(
        root_dirs=[
            str(Path(root) / "docs"),
            str(Path(root) / "internal"),
        ],
        db_path=str(db_path),
    )
    return str(db_path)


@contextmanager
def _with_server(root_dir: str, port: int) -> Generator[ServerProcess, None, None]:
    """Start an MCP server subprocess for *root_dir* on *port*; stop on exit."""
    server = ServerProcess(root_dir, port)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def verify_metadata(root_dir: str, rel_path: str, *, present: bool) -> None:
    """Check whether *rel_path* exists in the ``file_metadata`` table.

    Raises an AssertionError if the state does not match *present*.
    """
    db_path = Path(root_dir) / "wywy_docs" / "docs_index.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT path FROM file_metadata WHERE path = ?",
            (rel_path,),
        ).fetchall()
        if present:
            assert len(rows) == 1, f"Expected {rel_path!r} in file_metadata, got {rows}"
        else:
            assert len(rows) == 0, f"Expected {rel_path!r} removed"
    finally:
        conn.close()


def cleanup_ephemeral_files(root_dir: str, rel_paths: list[str]) -> None:
    """Remove ephemeral test files and their index rows.

    Best-effort: any failure is logged and swallowed so a cleanup
    problem never cascades into the next test.
    """
    db_path = Path(root_dir) / "wywy_docs" / "docs_index.db"
    for rel_path in rel_paths:
        try:
            abs_path = Path(root_dir) / rel_path
            if abs_path.is_file():
                abs_path.unlink()
            if db_path.is_file():
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
                    conn.execute(
                        "DELETE FROM file_metadata WHERE path = ?",
                        (rel_path,),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except (OSError, sqlite3.Error):
            logger.warning("tearDown cleanup failed for %s", rel_path, exc_info=True)


# ===========================================================================
# MCP SSE client — raw HTTP implementation
# ===========================================================================


class MCPClient:
    """Minimal MCP client for testing the SSE transport.

    Opens a GET /sse stream, reads the ``endpoint`` event, then sends
    JSON-RPC messages via POST to the session endpoint.  Responses are
    received through the SSE stream and collected in a thread-safe queue.
    """

    def __init__(self, host: str, port: int) -> None:
        """Create a client for the SSE MCP server at *host*:*port*."""
        self.host = host
        self.port = port
        self._sse_conn: HTTPConnection | None = None
        self.messages_url: str = "/messages"
        self._response_queue: queue.Queue[JsonRpcResponse] = queue.Queue()
        self._reader_stop = threading.Event()
        self._reader_thread: threading.Thread | None = None

    def connect(self) -> None:
        """Open the SSE stream and discover the session endpoint."""
        self._sse_conn = HTTPConnection(self.host, self.port, timeout=30)
        self._sse_conn.request("GET", "/sse")
        response = self._sse_conn.getresponse()

        # Read SSE events until we get the "endpoint" event.
        event_type: str | None = None
        data_buffer: list[str] = []
        while True:
            raw = response.readline()
            if not raw:
                raise ConnectionError(_ERR_SSE_CLOSED)
            line = raw.decode("utf-8").strip()
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data_buffer.append(line[6:])
            elif line == "":
                # End of an SSE event
                if event_type == "endpoint" and data_buffer:
                    self.messages_url = "".join(data_buffer)
                    break
                event_type = None
                data_buffer = []

        # Start a daemon thread to read subsequent SSE events (responses).
        self._reader_thread = threading.Thread(
            target=self._read_events,
            args=(response,),
            daemon=True,
        )
        self._reader_thread.start()

        # Perform MCP initialize/initialized handshake.
        self.send_message(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "wywy-test", "version": "1.0"},
                },
            },
        )
        # Send notifications/initialized (fire-and-forget).
        self.send_notification(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )

    def _read_events(self, response: HTTPResponse) -> None:
        """Background task: read SSE events and enqueue message data."""
        event_type: str | None = None
        data_buffer: list[str] = []
        while not self._reader_stop.is_set():
            try:
                raw = response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8").strip()
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data_buffer.append(line[6:])
                elif line == "":
                    if event_type == "message" and data_buffer:
                        payload = "".join(data_buffer)
                        with suppress(json.JSONDecodeError):
                            self._response_queue.put(json.loads(payload))
                    event_type = None
                    data_buffer = []
            except Exception:  # noqa: BLE001 - daemon thread; absorb any stream error and end
                break

    def send_message(self, body: JsonRpcRequest) -> JsonRpcResponse:
        """Send a JSON-RPC message and return the parsed JSON-RPC response.

        The response is received through the SSE stream (not the POST
        response body).
        """
        # POST the message to the session endpoint.
        conn = HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request(
                "POST",
                self.messages_url,
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()  # consume — expected to be 202 Accepted
        finally:
            conn.close()

        # Wait for the JSON-RPC response on the SSE stream.
        return self._response_queue.get(timeout=RESPONSE_TIMEOUT)

    def send_notification(self, body: JsonRpcRequest) -> None:
        """Send a JSON-RPC notification (fire-and-forget, no response expected)."""
        conn = HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request(
                "POST",
                self.messages_url,
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            assert resp.status == HTTPStatus.ACCEPTED, (
                f"Expected 202, got {resp.status}"
            )
        finally:
            conn.close()

    def close(self) -> None:
        """Shut down the SSE reader and close the connection."""
        self._reader_stop.set()
        if self._sse_conn is not None:
            with suppress(OSError):
                self._sse_conn.close()


# ===========================================================================
# Server lifecycle manager
# ===========================================================================


# Absolute path to the Wywy-Docs project root (two levels up from this file).
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


class ServerProcess:
    """Manage the MCP server as a subprocess.

    The server is always launched from the real Wywy-Docs project root
    (where ``src/wywy_docs/``, ``docs/``, ``internal/`` live) so that the
    local ``wywy_docs`` package is found.  The ``WYWY_ROOT`` and ``PORT``
    environment variables point the server at a temporary test directory.

    Usage::

        server = ServerProcess(root_dir, port)
        server.start()
        try:
            # … tests …
        finally:
            server.stop()
    """

    def __init__(self, root_dir: str, port: int) -> None:
        """Record the *root_dir* and *port* the server will be launched with."""
        self.root_dir = root_dir
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        """Start the server and wait for it to become ready.

        On any startup failure the half-started process is terminated so a
        failed start never leaks an orphaned server on a bare-metal host.
        """
        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["WYWY_ROOT"] = self.root_dir
        # An ambient WYWY_DOCS_DIR would override WYWY_ROOT in the server
        # (server.py reads WYWY_DOCS_DIR first) and redirect the server at
        # production state.  Drop it so the temp root always wins.
        env.pop("WYWY_DOCS_DIR", None)
        # Prefer the project venv python (fast, no uv sync).  Fall back to
        # `uv run --offline` for environments without a local venv; the
        # offline flag matches run-tests.sh and avoids network sync hangs.
        venv_python = str(Path(_PROJECT_ROOT) / ".venv" / "bin" / "python")
        if Path(venv_python).is_file():
            command: list[str] = [venv_python, "-m", "wywy_docs.server"]
        else:
            command = [
                shutil.which("uv") or "uv",
                "run",
                "--offline",
                "python",
                "-m",
                "wywy_docs.server",
            ]
        self.process = subprocess.Popen(  # noqa: S603 - static command, no user input
            command,
            cwd=_PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self._wait_for_server()
        except Exception:
            # The server may still be running (e.g. it bound late, after the
            # timeout).  Terminate it so the failure does not leak a process.
            self.stop()
            raise

    def _wait_for_server(self) -> None:
        """Poll the /sse endpoint until it responds."""
        process = self.process
        if process is None:
            raise RuntimeError(_ERR_NO_PROCESS)
        deadline = time.time() + SERVER_TIMEOUT
        last_error: Exception | None = None
        while time.time() < deadline:
            # Quick check: if process exited early, abort.
            ret = process.poll()
            if ret is not None:
                msg = (
                    f"Server process exited early with code {ret}. "
                    f"stderr: {self.read_stderr()}"
                )
                raise RuntimeError(msg)
            try:
                resp = urlopen(f"http://{HOST}:{self.port}/sse", timeout=0.5)
                resp.readline()
            except (URLError, ConnectionRefusedError, OSError) as e:
                last_error = e
                time.sleep(0.2)
            else:
                return
        msg = (
            f"Server did not start within {SERVER_TIMEOUT}s. "
            f"Last error: {last_error}. "
            f"stderr: {self.read_stderr()}"
        )
        raise RuntimeError(msg)

    def read_stderr(self) -> str:
        """Read any captured stderr output from the process, without blocking.

        The server process may still be alive with the stderr pipe open;
        a plain ``read()`` would block forever.  ``select`` bounds the wait
        so an unresponsive server yields a message instead of a hang.
        """
        if self.process is None or self.process.stderr is None:
            return "<no stderr>"
        try:
            readable, _, _ = select.select([self.process.stderr], [], [], 1.0)
            if not readable:
                return "<stderr not yet available>"
            data = self.process.stderr.read()
        except OSError:
            return "<unreadable>"
        return data.decode("utf-8", errors="replace")[:2000]

    def stop(self) -> None:
        """Terminate the server process."""
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


# ===========================================================================
# Test cases
# ===========================================================================


class TestServerEndpoints(unittest.TestCase):
    """Server exposes the standard MCP SSE transport endpoints."""

    def setUp(self) -> None:
        """Create a temp Wywy root and pick a free port."""
        self.root_dir = setup_temp_wywy_root()
        self.port = find_free_port()

    def tearDown(self) -> None:
        """Remove the temp root."""
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_sse_endpoint_returns_200(self) -> None:
        """GET /sse returns HTTP 200 (SSE connection established)."""
        with _with_server(self.root_dir, self.port):
            resp = urlopen(f"http://{HOST}:{self.port}/sse", timeout=5)
            assert resp.status == HTTPStatus.OK
            # The connection stays open; read a bit to confirm SSE framing.
            chunk = resp.readline()
            assert b"event:" in chunk

    def test_messages_endpoint_returns_202(self) -> None:
        """POST /messages?session_id=... returns 202 (message accepted)."""
        with _with_server(self.root_dir, self.port):
            # Establish SSE session via MCPClient to discover session URL
            client = MCPClient(HOST, self.port)
            client.connect()
            # Raw POST a valid message through the session endpoint
            conn = HTTPConnection(HOST, self.port, timeout=5)
            conn.request(
                "POST",
                client.messages_url,  # e.g., /messages?session_id=xxx
                body=json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            assert resp.status == HTTPStatus.ACCEPTED
            conn.close()
            client.close()

    def test_server_fails_with_clear_error_when_port_occupied(self) -> None:
        """Starting the server on an occupied port fails with exit code 1.

        The error mentions 'address already in use' (not code
        3/NOTIMPLEMENTED).
        """
        # Occupy the port with a listening socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, self.port))
        s.listen()
        s.settimeout(5)
        try:
            server = ServerProcess(self.root_dir, self.port)
            with pytest.raises(RuntimeError) as ctx:
                server.start()
            msg = str(ctx.value)
            # Must mention the port conflict
            assert "address already in use" in msg.lower()
        finally:
            s.close()

    def test_venv_python_can_start_server_production_style(self) -> None:
        """Reproduce the production systemd invocation against a temp root.

        ``.venv/bin/python -m wywy_docs.server`` with WYWY_ROOT set to an
        isolated temporary root and an ephemeral port.  Captures ALL stderr
        to catch any LookupError, ModuleNotFoundError, or other runtime
        failure.  The root and port are isolated so the test never touches
        the real repository or the production default port (2530).
        """
        venv_python = str(Path(_PROJECT_ROOT) / ".venv" / "bin" / "python")
        assert Path(venv_python).is_file(), f"Venv Python not found at {venv_python}"

        # Binary-search which sub-import hangs (the whole module import times out)
        for label, code in [
            ("import mcp (SDK)", "import mcp; print('OK')"),
            ("import wywy_docs.indexer", "import wywy_docs.indexer; print('OK')"),
            ("import wywy_docs.server", "import wywy_docs.server; print('OK')"),
        ]:
            r = subprocess.run(  # noqa: S603 - test literals only
                [venv_python, "-c", code],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=_PROJECT_ROOT,
                check=False,  # returncode asserted below
            )
            assert r.returncode == 0, (
                f"{label} FAILED: exit={r.returncode} "
                f"stdout={r.stdout!r} stderr={r.stderr!r}"
            )
            assert "OK" in r.stdout, f"{label} did not print OK"

        # Now start the server as systemd would, but against the isolated
        # temp root from setUp on an ephemeral port.
        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["WYWY_ROOT"] = self.root_dir
        # An ambient WYWY_DOCS_DIR would override WYWY_ROOT (server.py reads
        # WYWY_DOCS_DIR first) and point the server at production state.
        env.pop("WYWY_DOCS_DIR", None)
        proc = subprocess.Popen(  # noqa: S603 - static production command
            [venv_python, "-m", "wywy_docs.server"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=_PROJECT_ROOT,
            env=env,
        )
        try:
            # Check for early exit (immediate crash)
            time.sleep(1.5)
            ret = proc.poll()
            if ret is not None:
                stderr = proc.stderr.read().decode() if proc.stderr else ""
                self.fail(
                    f"Server exited with code {ret} (systemd sees NOTIMPLEMENTED=3 "
                    f"or FAILURE=1). stderr follows:\n{stderr}",
                )
            # If still running, it started fine
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


class TestToolsList(unittest.TestCase):
    """The ``tools/list`` request returns the available tools."""

    def setUp(self) -> None:
        """Build a test index and start the server and client."""
        self.root_dir = setup_temp_wywy_root()
        self.port = find_free_port()
        build_test_index(
            self.root_dir,
            {"docs/dummy.mdx": "---\ntitle: Dummy\n---\nPlaceholder content."},
        )
        self.server = ServerProcess(self.root_dir, self.port)
        self.server.start()
        self.client = MCPClient(HOST, self.port)
        self.client.connect()

    def tearDown(self) -> None:
        """Close the client, stop the server, and remove the temp root."""
        self.client.close()
        self.server.stop()
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_tools_list_returns_search_docs_and_get_doc(self) -> None:
        """Calling ``tools/list`` returns both tool definitions."""
        resp = self.client.send_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert "result" in resp
        tools = resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert "search_docs" in tool_names
        assert "get_doc" in tool_names
        assert "delete_doc" in tool_names
        search_docs_tool = next(t for t in tools if t["name"] == "search_docs")
        assert "literal" in search_docs_tool["description"]


class TestSearchDocsTool(unittest.TestCase):
    """The ``search_docs`` tool performs FTS5 full-text search."""

    @classmethod
    def setUpClass(cls) -> None:
        """Build the test index and start the server and client."""
        cls.root_dir = setup_temp_wywy_root()
        cls.port = find_free_port()
        build_test_index(
            cls.root_dir,
            {
                "docs/hello.mdx": (
                    "---\n"
                    "title: Hello World\n"
                    "---\n"
                    "This is a shared_term greeting document."
                ),
                "docs/goodbye.mdx": "---\ntitle: Goodbye\n---\nFarewell message.",
                "docs/python.mdx": (
                    "---\ntitle: Python Guide\n---\nPython is a programming language."
                ),
                "docs/tree-map.mdx": (
                    "---\n"
                    "title: Tree Map\n"
                    "---\n"
                    "The tree-map visualization shows ancestry."
                ),
                "internal/guide.mdx": (
                    "---\n"
                    "title: Internal Guide\n"
                    "---\n"
                    "This is an internal guide with shared_term."
                ),
            },
        )
        cls.server = ServerProcess(cls.root_dir, cls.port)
        cls.server.start()
        cls.client = MCPClient(HOST, cls.port)
        cls.client.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        """Close the client, stop the server, and remove the temp root."""
        cls.client.close()
        cls.server.stop()
        shutil.rmtree(cls.root_dir, ignore_errors=True)

    def test_search_docs_returns_matching_results(self) -> None:
        """Searching returns results with title, path, excerpt, score."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "Python", "max_results": 10},
                },
            },
        )
        assert "result" in resp
        content = resp["result"]["content"]
        assert isinstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        assert "Python" in body_text
        # At least one result should be present
        assert len(content) > 0

    def test_search_docs_returns_section(self) -> None:
        """Results include a ``section`` field matching the path prefix.

        The section is ``"docs"`` or ``"internal"``.
        """
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "shared_term", "max_results": 10},
                },
            },
        )
        assert "result" in resp
        content = resp["result"]["content"]
        assert isinstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        results = json.loads(body_text)
        assert len(results) > 0
        for r in results:
            assert "section" in r
            assert r["section"] is not None
            path = r["path"]
            if path.startswith("docs/"):
                assert r["section"] == "docs"
            elif path.startswith("internal/"):
                assert r["section"] == "internal"

    def test_search_docs_empty_query_returns_error(self) -> None:
        """Empty query returns JSON-RPC error -32602."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": ""},
                },
            },
        )
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS
        assert "query must be a non-empty string" in resp["error"]["message"]

    def test_search_docs_operator_soup_is_literal(self) -> None:
        """Operator soup like ``a OR OR b`` is treated as literal text.

        The text is not interpreted as FTS5 operator syntax.
        """
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "a OR OR b", "max_results": 10},
                },
            },
        )
        assert "result" in resp
        content = resp["result"]["content"]
        assert isinstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        results = json.loads(body_text)
        assert results == []

    def test_search_docs_hyphenated_query_returns_results(self) -> None:
        """A hyphenated query is literal text, returning the matching doc.

        The server returns a result instead of a -32603 error.
        """
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "tree-map", "max_results": 10},
                },
            },
        )
        assert "result" in resp
        content = resp["result"]["content"]
        assert isinstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        results = json.loads(body_text)
        assert len(results) > 0
        assert "docs/tree-map.mdx" in [r["path"] for r in results]

    def test_search_docs_unbalanced_quotes_stripped(self) -> None:
        """An unbalanced double quote is stripped safely instead of raising -32603."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": 'say "hello', "max_results": 10},
                },
            },
        )
        assert "result" in resp

    def test_search_docs_quote_only_query_returns_empty_error(self) -> None:
        """A query that sanitizes to empty keeps the empty-query -32602 contract."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 23,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": '"'},
                },
            },
        )
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS
        assert "query must be a non-empty string" in resp["error"]["message"]

    def test_search_docs_nul_byte_query_does_not_error(self) -> None:
        """A NUL byte inside a query never produces -32603 (load-bearing guard)."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 24,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "tree\u0000map", "max_results": 10},
                },
            },
        )
        assert "result" in resp


class TestGetDocTool(unittest.TestCase):
    """The ``get_doc`` tool returns full document content and frontmatter."""

    @classmethod
    def setUpClass(cls) -> None:
        """Build the test index and start the server and client."""
        cls.root_dir = setup_temp_wywy_root()
        cls.port = find_free_port()
        build_test_index(
            cls.root_dir,
            {
                "docs/test.mdx": (
                    "---\ntitle: Test Doc\nkey: value\n---\nFull body content here."
                ),
            },
        )
        cls.server = ServerProcess(cls.root_dir, cls.port)
        cls.server.start()
        cls.client = MCPClient(HOST, cls.port)
        cls.client.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        """Close the client, stop the server, and remove the temp root."""
        cls.client.close()
        cls.server.stop()
        shutil.rmtree(cls.root_dir, ignore_errors=True)

    def test_get_doc_returns_content_and_frontmatter(self) -> None:
        """Valid path returns full document content and frontmatter."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "get_doc",
                    "arguments": {"path": "docs/test.mdx"},
                },
            },
        )
        assert "result" in resp
        content = resp["result"]["content"]
        assert isinstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        assert "Test Doc" in body_text
        assert "Full body content here" in body_text

    def test_get_doc_appends_mdx_auto(self) -> None:
        """Path without .mdx is normalized — .mdx appended automatically."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {
                    "name": "get_doc",
                    "arguments": {"path": "docs/test"},
                },
            },
        )
        assert "result" in resp
        content = resp["result"]["content"]
        assert isinstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        assert "Test Doc" in body_text
        assert "Full body content here" in body_text

    def test_get_doc_invalid_path_returns_error(self) -> None:
        """Invalid path returns JSON-RPC error -32602."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {
                    "name": "get_doc",
                    "arguments": {"path": "nonexistent.mdx"},
                },
            },
        )
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS
        assert "document not found" in resp["error"]["message"]


class TestAutoBuildIndex(unittest.TestCase):
    """Server auto-builds the FTS5 index on startup if missing."""

    def setUp(self) -> None:
        """Create a temp root with one doc but no index yet."""
        self.root_dir = setup_temp_wywy_root()
        self.port = find_free_port()
        # Create a doc file but do NOT build the index — server should do it.
        create_file(
            self.root_dir,
            "docs/auto.mdx",
            "---\ntitle: Auto\n---\nServer-built content.",
        )

    def tearDown(self) -> None:
        """Remove the temp root."""
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_start_without_index_creates_database(self) -> None:
        """Starting without ``docs_index.db`` creates it automatically."""
        db_path = Path(self.root_dir) / "wywy_docs" / "docs_index.db"
        assert not db_path.is_file()

        server = ServerProcess(self.root_dir, self.port)
        server.start()
        try:
            assert db_path.is_file()
            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            conn.close()
            assert "docs_fts" in tables
        finally:
            server.stop()

    def test_start_with_existing_index_does_not_reindex(self) -> None:
        """Starting with existing database does NOT re-import the indexer."""
        # Pre-build the index with exactly one document.
        build_test_index(
            self.root_dir,
            {"docs/initial.mdx": "---\ntitle: Initial\n---\nOriginal content"},
        )
        db_path = Path(self.root_dir) / "wywy_docs" / "docs_index.db"
        conn = sqlite3.connect(db_path)
        before_count = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
        conn.close()

        # Start the server — it should NOT add any new rows.
        server = ServerProcess(self.root_dir, self.port)
        server.start()
        try:
            conn = sqlite3.connect(db_path)
            after_count = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            conn.close()
            assert before_count == after_count
        finally:
            server.stop()


class TestEnsureIndexMissingDirectories(unittest.TestCase):
    """``_ensure_index`` warns when ``docs/`` or ``internal/`` are missing.

    But still creates an empty FTS5 index (server does not crash).
    Direct-import test (no subprocess), mirroring
    ``TestDeleteDocToolIndexFailure`` in ``tests/test_delete_doc.py``.
    """

    def setUp(self) -> None:
        """Create a temp root with no docs/ or internal/ directories."""
        # No docs/ or internal/ subdirectories.  ``_ensure_index`` creates
        # the wywy_docs dir itself; docs/ and internal/ stay missing.
        self.root_dir = _setup_temp_wywy_root_missing()

    def tearDown(self) -> None:
        """Remove the temp root."""
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_ensure_index_warns_when_docs_and_internal_missing(self) -> None:
        """Missing docs/ and internal/ produce warnings.

        The result is an empty FTS5 index instead of a crash.
        """
        import wywy_docs.server as server_mod  # type: ignore[attr-defined]

        with self.assertLogs(server_mod.logger, level="WARNING") as cm:
            server_mod._ensure_index(self.root_dir)  # type: ignore[reportPrivateUsage]

        log_text = "\n".join(cm.output)
        assert "docs directory does not exist" in log_text
        assert "internal directory does not exist" in log_text

        # Index database is still created with an empty FTS5 table.
        db_path = Path(self.root_dir) / "wywy_docs" / "docs_index.db"
        assert db_path.is_file()
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                ).fetchall()
            }
            count = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
        finally:
            conn.close()
        assert "docs_fts" in tables
        assert count == 0


class TestServerMissingDirectories(unittest.TestCase):
    """The real server (subprocess) starts and serves an empty index.

    Handles the case when ``docs/``/``internal/`` are missing, instead of
    crashing.  Subprocess-level counterpart of
    ``TestEnsureIndexMissingDirectories``: exercises the same
    ``_ensure_index`` behaviour through the actual ``wywy_docs.server``
    entry point via ``ServerProcess``.
    """

    def setUp(self) -> None:
        """Create a temp root with no docs/ or internal/ directories."""
        # Neither docs/ nor internal/ exists — the server must start anyway
        # and create an empty index.  The wywy_docs/ dir is created by
        # ``_ensure_index`` itself.
        self.root_dir = _setup_temp_wywy_root_missing()
        self.port = find_free_port()

    def tearDown(self) -> None:
        """Remove the temp root."""
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_server_starts_without_docs_and_internal(self) -> None:
        """Starting the server with neither section dir present does not crash."""
        # _with_server calls server.start(), which raises RuntimeError if
        # the process exits early.
        with _with_server(self.root_dir, self.port):
            pass

    def test_index_created_empty_without_dirs(self) -> None:
        """Missing sections still produce a ``docs_fts`` table with zero rows."""
        with _with_server(self.root_dir, self.port):
            db_path = Path(self.root_dir) / "wywy_docs" / "docs_index.db"
            assert db_path.is_file()
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            finally:
                conn.close()
            assert count == 0

    def test_search_docs_returns_empty_when_index_empty(self) -> None:
        """search_docs on an empty index returns ``[]`` for a valid query."""
        with _with_server(self.root_dir, self.port):
            client = MCPClient(HOST, self.port)
            client.connect()
            try:
                resp = client.send_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 30,
                        "method": "tools/call",
                        "params": {
                            "name": "search_docs",
                            "arguments": {"query": "nonexistent_term"},
                        },
                    },
                )
                assert "result" in resp
                content = resp["result"]["content"]
                body_text = " ".join(str(item.get("text", "")) for item in content)
                results = json.loads(body_text)
                assert results == []
            finally:
                client.close()

    def test_mixed_state_warns_for_missing_dir_only(self) -> None:
        """With only ``docs/`` present: warn about ``internal/`` only.

        Index files from the existing ``docs/`` directory.
        """
        create_file(
            self.root_dir,
            "docs/only.mdx",
            "---\ntitle: Only\n---\nZephyrflorabranch content for searching.",
        )
        with _with_server(self.root_dir, self.port) as server:
            client = MCPClient(HOST, self.port)
            client.connect()
            try:
                resp = client.send_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 40,
                        "method": "tools/call",
                        "params": {
                            "name": "search_docs",
                            "arguments": {"query": "zephyrflorabranch"},
                        },
                    },
                )
                assert "result" in resp
                content = resp["result"]["content"]
                body_text = " ".join(str(item.get("text", "")) for item in content)
                results = json.loads(body_text)
                assert len(results) > 0
                assert results[0]["path"] == "docs/only.mdx"
            finally:
                client.close()

        stderr = server.read_stderr()
        assert "internal directory does not exist" in stderr
        assert "docs directory does not exist" not in stderr


class TestServerStartupTime(unittest.TestCase):
    """Server starts quickly with an existing database."""

    def setUp(self) -> None:
        """Build a test index and pick a free port."""
        self.root_dir = setup_temp_wywy_root()
        self.port = find_free_port()
        build_test_index(
            self.root_dir,
            {"docs/bench.mdx": "---\ntitle: Bench\n---\nBenchmark document."},
        )

    def tearDown(self) -> None:
        """Remove the temp root."""
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_server_startup_within_budget(self) -> None:
        """With existing index, server starts within the budget."""
        start = time.time()
        server = ServerProcess(self.root_dir, self.port)
        server.start()
        elapsed = time.time() - start
        server.stop()
        assert elapsed < STARTUP_BUDGET_SECONDS


if __name__ == "__main__":
    unittest.main()
