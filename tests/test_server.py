"""Tests for ``wywy_docs/server.py`` — HTTP SSE MCP server.

Starts the server as a subprocess and exercises the MCP tool interface
through the SSE transport protocol (raw HTTP + SSE stream reading).

All tests create temporary directory trees with sample .mdx files and an
FTS5 index, then start the server, connect via the SSE transport, and
assert on JSON-RPC responses.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
import queue
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection, HTTPResponse
from urllib.error import URLError
from urllib.request import urlopen

HOST = "127.0.0.1"
DEFAULT_PORT = 2530
SERVER_TIMEOUT = 10  # max seconds to wait for server startup
RESPONSE_TIMEOUT = 10  # max seconds to wait for a JSON-RPC response

logger = logging.getLogger(__name__)


# ===========================================================================
# Helpers
# ===========================================================================


def _find_free_port() -> int:
    """Return a random ephemeral port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _create_file(
    root: str, rel_path: str, content: str = "---\ntitle: X\n---\nbody"
) -> str:
    """Create a file at *root*/*rel_path* with *content*.

    Creates parent directories as needed.  Returns the absolute path.
    """
    full_path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w") as f:
        f.write(content)
    return full_path


def _setup_temp_wywy_root() -> str:
    """Create a temporary directory that mimics the Wywy-Docs repo layout.

    Returns the path to the temp root.
    """
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "docs"))
    os.makedirs(os.path.join(root, "internal"))
    os.makedirs(os.path.join(root, "wywy_docs"))
    return root


def _setup_temp_wywy_root_missing(*, docs: bool = False, internal: bool = False) -> str:
    """Create a temporary root with ``docs/``/``internal/`` absent as selected.

    Unlike ``_setup_temp_wywy_root``, the requested section directories are
    NOT created, so the server's ``_ensure_index`` must handle their absence
    (warn + still create an empty index).  Returns the path to the temp root.
    """
    root = tempfile.mkdtemp()
    if docs:
        os.makedirs(os.path.join(root, "docs"))
    if internal:
        os.makedirs(os.path.join(root, "internal"))
    return root


def _build_test_index(root: str, files: dict[str, str]) -> str:
    """Create sample .mdx *files* (rel_path -> content) under *root* and
    build the FTS5 index.  Returns the database path.
    """
    for rel_path, content in files.items():
        _create_file(root, rel_path, content)

    from wywy_docs.indexer import build_index

    db_path = os.path.join(root, "wywy_docs", "docs_index.db")
    build_index(
        root_dirs=[
            os.path.join(root, "docs"),
            os.path.join(root, "internal"),
        ],
        db_path=db_path,
    )
    return db_path


@contextmanager
def _with_server(root_dir: str, port: int):
    """Start an MCP server subprocess for *root_dir* on *port*; stop on exit."""
    server = ServerProcess(root_dir, port)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _verify_metadata(root_dir: str, rel_path: str, *, present: bool) -> None:
    """Check whether *rel_path* exists in the ``file_metadata`` table.

    Raises an AssertionError if the state does not match *present*.
    """
    db_path = os.path.join(root_dir, "wywy_docs", "docs_index.db")
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


def _cleanup_ephemeral_files(root_dir: str, rel_paths: list[str]) -> None:
    """Remove ephemeral test files and their index rows.

    Best-effort: any failure is logged and swallowed so a cleanup
    problem never cascades into the next test.
    """
    db_path = os.path.join(root_dir, "wywy_docs", "docs_index.db")
    for rel_path in rel_paths:
        try:
            abs_path = os.path.join(root_dir, rel_path)
            if os.path.isfile(abs_path):
                os.remove(abs_path)
            if os.path.isfile(db_path):
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
                    conn.execute(
                        "DELETE FROM file_metadata WHERE path = ?", (rel_path,)
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
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
        self.host = host
        self.port = port
        self._sse_conn: HTTPConnection | None = None
        self._messages_url: str = "/messages"
        self._response_queue: queue.Queue = queue.Queue()
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
                raise ConnectionError(
                    "SSE stream closed before receiving endpoint event"
                )
            line = raw.decode("utf-8").strip()
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data_buffer.append(line[6:])
            elif line == "":
                # End of an SSE event
                if event_type == "endpoint" and data_buffer:
                    self._messages_url = "".join(data_buffer)
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
        init_result = self.send_message(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "wywy-test", "version": "1.0"},
                },
            }
        )
        # Send notifications/initialized (fire-and-forget).
        self.send_notification(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
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
                        try:
                            self._response_queue.put(json.loads(payload))
                        except json.JSONDecodeError:
                            pass
                    event_type = None
                    data_buffer = []
            except Exception:
                break

    def send_message(self, body: dict) -> dict:
        """Send a JSON-RPC message and return the parsed JSON-RPC response.

        The response is received through the SSE stream (not the POST
        response body).
        """
        # POST the message to the session endpoint.
        conn = HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request(
                "POST",
                self._messages_url,
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()  # consume — expected to be 202 Accepted
        finally:
            conn.close()

        # Wait for the JSON-RPC response on the SSE stream.
        return self._response_queue.get(timeout=RESPONSE_TIMEOUT)

    def send_notification(self, body: dict) -> None:
        """Send a JSON-RPC notification (fire-and-forget, no response expected)."""
        conn = HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request(
                "POST",
                self._messages_url,
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 202, f"Expected 202, got {resp.status}"
        finally:
            conn.close()

    def close(self) -> None:
        """Shut down the SSE reader and close the connection."""
        self._reader_stop.set()
        if self._sse_conn is not None:
            try:
                self._sse_conn.close()
            except Exception:
                pass


# ===========================================================================
# Server lifecycle manager
# ===========================================================================


# Absolute path to the Wywy-Docs project root (two levels up from this file).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        self.root_dir = root_dir
        self.port = port
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        """Start the server and wait for it to become ready."""
        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["WYWY_ROOT"] = self.root_dir
        self.process = subprocess.Popen(
            [
                shutil.which("uv") or "uv",
                "run",
                "python",
                "-m",
                "wywy_docs.server",
            ],
            cwd=_PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._wait_for_server()

    def _wait_for_server(self) -> None:
        """Poll the /sse endpoint until it responds."""
        deadline = time.time() + SERVER_TIMEOUT
        last_error: Exception | None = None
        while time.time() < deadline:
            # Quick check: if process exited early, abort.
            ret = self.process.poll()
            if ret is not None:
                raise RuntimeError(
                    f"Server process exited early with code {ret}. "
                    f"stderr: {self._read_stderr()}"
                )
            try:
                resp = urlopen(f"http://{HOST}:{self.port}/sse", timeout=0.5)
                resp.readline()
                return
            except (URLError, ConnectionRefusedError, OSError) as e:
                last_error = e
                time.sleep(0.2)
        raise RuntimeError(
            f"Server did not start within {SERVER_TIMEOUT}s. "
            f"Last error: {last_error}. "
            f"stderr: {self._read_stderr()}"
        )

    def _read_stderr(self) -> str:
        """Read any captured stderr output from the process."""
        if self.process and self.process.stderr:
            try:
                return self.process.stderr.read().decode("utf-8", errors="replace")[
                    :2000
                ]
            except Exception:
                return "<unreadable>"
        return "<no stderr>"

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
        self.root_dir = _setup_temp_wywy_root()
        self.port = _find_free_port()

    def tearDown(self) -> None:
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_sse_endpoint_returns_200(self) -> None:
        """GET /sse returns HTTP 200 (SSE connection established)."""
        with _with_server(self.root_dir, self.port):
            resp = urlopen(f"http://{HOST}:{self.port}/sse", timeout=5)
            self.assertEqual(resp.status, 200)
            # The connection stays open; read a bit to confirm SSE framing.
            chunk = resp.readline()
            self.assertIn(b"event:", chunk)

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
                client._messages_url,  # e.g., /messages?session_id=xxx
                body=json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 202)
            conn.close()
            client.close()

    def test_server_fails_with_clear_error_when_port_occupied(self) -> None:
        """Starting the server on an occupied port fails with exit code 1
        and 'address already in use' (not code 3/NOTIMPLEMENTED)."""
        # Occupy the port with a listening socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, self.port))
        s.listen()
        s.settimeout(5)
        try:
            server = ServerProcess(self.root_dir, self.port)
            with self.assertRaises(RuntimeError) as ctx:
                server.start()
            msg = str(ctx.exception)
            # Must mention the port conflict
            self.assertIn("address already in use", msg.lower())
        finally:
            s.close()

    def test_venv_python_can_start_server_on_default_port(self) -> None:
        """Reproduce the production systemd invocation::

            .venv/bin/python -m wywy_docs.server

        with PORT=2530 and WYWY_ROOT set to the project root.
        Captures ALL stderr to catch any LookupError, ModuleNotFoundError,
        or other runtime failure.
        """
        venv_python = os.path.join(_PROJECT_ROOT, ".venv", "bin", "python")
        self.assertTrue(
            os.path.isfile(venv_python),
            f"Venv Python not found at {venv_python}",
        )

        # Binary-search which sub-import hangs (the whole module import times out)
        for label, code in [
            ("import mcp (SDK)", "import mcp; print('OK')"),
            ("import wywy_docs.indexer", "import wywy_docs.indexer; print('OK')"),
            ("import wywy_docs.server", "import wywy_docs.server; print('OK')"),
        ]:
            r = subprocess.run(
                [venv_python, "-c", code],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=_PROJECT_ROOT,
            )
            self.assertEqual(
                r.returncode,
                0,
                f"{label} FAILED: exit={r.returncode} "
                f"stdout={r.stdout!r} stderr={r.stderr!r}",
            )
            self.assertIn("OK", r.stdout, f"{label} did not print OK")

        # Now start the server as systemd would
        env = os.environ.copy()
        env["PORT"] = "2530"  # production default, may be in use
        env["WYWY_ROOT"] = _PROJECT_ROOT
        proc = subprocess.Popen(
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
                    f"or FAILURE=1). stderr follows:\n{stderr}"
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
        self.root_dir = _setup_temp_wywy_root()
        self.port = _find_free_port()
        _build_test_index(
            self.root_dir,
            {"docs/dummy.mdx": "---\ntitle: Dummy\n---\nPlaceholder content."},
        )
        self.server = ServerProcess(self.root_dir, self.port)
        self.server.start()
        self.client = MCPClient(HOST, self.port)
        self.client.connect()

    def tearDown(self) -> None:
        self.client.close()
        self.server.stop()
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_tools_list_returns_search_docs_and_get_doc(self) -> None:
        """Calling ``tools/list`` returns both tool definitions."""
        resp = self.client.send_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        self.assertIn("result", resp)
        tools = resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        self.assertIn("search_docs", tool_names)
        self.assertIn("get_doc", tool_names)
        self.assertIn("delete_doc", tool_names)


class TestSearchDocsTool(unittest.TestCase):
    """The ``search_docs`` tool performs FTS5 full-text search."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root_dir = _setup_temp_wywy_root()
        cls.port = _find_free_port()
        _build_test_index(
            cls.root_dir,
            {
                "docs/hello.mdx": "---\ntitle: Hello World\n---\nThis is a shared_term greeting document.",
                "docs/goodbye.mdx": "---\ntitle: Goodbye\n---\nFarewell message.",
                "docs/python.mdx": "---\ntitle: Python Guide\n---\nPython is a programming language.",
                "internal/guide.mdx": "---\ntitle: Internal Guide\n---\nThis is an internal guide with shared_term.",
            },
        )
        cls.server = ServerProcess(cls.root_dir, cls.port)
        cls.server.start()
        cls.client = MCPClient(HOST, cls.port)
        cls.client.connect()

    @classmethod
    def tearDownClass(cls) -> None:
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
            }
        )
        self.assertIn("result", resp)
        content = resp["result"]["content"]
        self.assertIsInstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        self.assertIn("Python", body_text)
        # At least one result should be present
        self.assertGreater(len(content), 0)

    def test_search_docs_returns_section(self) -> None:
        """Results include a ``section`` field ("docs" or "internal") matching the path prefix."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "shared_term", "max_results": 10},
                },
            }
        )
        self.assertIn("result", resp)
        content = resp["result"]["content"]
        self.assertIsInstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        results = json.loads(body_text)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertIn("section", r)
            self.assertIsNotNone(r["section"])
            path = r["path"]
            if path.startswith("docs/"):
                self.assertEqual(r["section"], "docs")
            elif path.startswith("internal/"):
                self.assertEqual(r["section"], "internal")

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
            }
        )
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("query must be a non-empty string", resp["error"]["message"])

    def test_search_docs_malformed_query_returns_error(self) -> None:
        """Malformed FTS5 query returns JSON-RPC error -32603."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "a OR OR b", "max_results": 10},
                },
            }
        )
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32603)


class TestGetDocTool(unittest.TestCase):
    """The ``get_doc`` tool returns full document content and frontmatter."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root_dir = _setup_temp_wywy_root()
        cls.port = _find_free_port()
        _build_test_index(
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
            }
        )
        self.assertIn("result", resp)
        content = resp["result"]["content"]
        self.assertIsInstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        self.assertIn("Test Doc", body_text)
        self.assertIn("Full body content here", body_text)

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
            }
        )
        self.assertIn("result", resp)
        content = resp["result"]["content"]
        self.assertIsInstance(content, list)
        body_text = " ".join(str(item.get("text", "")) for item in content)
        self.assertIn("Test Doc", body_text)
        self.assertIn("Full body content here", body_text)

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
            }
        )
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("document not found", resp["error"]["message"])


class TestAutoBuildIndex(unittest.TestCase):
    """Server auto-builds the FTS5 index on startup if missing."""

    def setUp(self) -> None:
        self.root_dir = _setup_temp_wywy_root()
        self.port = _find_free_port()
        # Create a doc file but do NOT build the index — server should do it.
        _create_file(
            self.root_dir,
            "docs/auto.mdx",
            "---\ntitle: Auto\n---\nServer-built content.",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_start_without_index_creates_database(self) -> None:
        """Starting without ``docs_index.db`` creates it automatically."""
        db_path = os.path.join(self.root_dir, "wywy_docs", "docs_index.db")
        self.assertFalse(os.path.isfile(db_path))

        server = ServerProcess(self.root_dir, self.port)
        server.start()
        try:
            self.assertTrue(os.path.isfile(db_path))
            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            conn.close()
            self.assertIn("docs_fts", tables)
        finally:
            server.stop()

    def test_start_with_existing_index_does_not_reindex(self) -> None:
        """Starting with existing database does NOT re-import the indexer."""
        # Pre-build the index with exactly one document.
        _build_test_index(
            self.root_dir,
            {"docs/initial.mdx": "---\ntitle: Initial\n---\nOriginal content"},
        )
        db_path = os.path.join(self.root_dir, "wywy_docs", "docs_index.db")
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
            self.assertEqual(before_count, after_count)
        finally:
            server.stop()


class TestEnsureIndexMissingDirectories(unittest.TestCase):
    """``_ensure_index`` warns when ``docs/`` or ``internal/`` are missing
    but still creates an empty FTS5 index (server does not crash).

    Direct-import test (no subprocess), mirroring
    ``TestDeleteDocToolIndexFailure`` in ``tests/test_delete_doc.py``.
    """

    def setUp(self) -> None:
        # No docs/ or internal/ subdirectories.  ``_ensure_index`` creates
        # the wywy_docs dir itself; docs/ and internal/ stay missing.
        self.root_dir = _setup_temp_wywy_root_missing()

    def tearDown(self) -> None:
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_ensure_index_warns_when_docs_and_internal_missing(self) -> None:
        """Missing docs/ and internal/ produce warnings plus an empty FTS5
        index instead of a crash."""
        import wywy_docs.server as server_mod  # type: ignore[attr-defined]

        with self.assertLogs(server_mod.logger, level="WARNING") as cm:
            server_mod._ensure_index(self.root_dir)

        log_text = "\n".join(cm.output)
        self.assertIn("docs directory does not exist", log_text)
        self.assertIn("internal directory does not exist", log_text)

        # Index database is still created with an empty FTS5 table.
        db_path = os.path.join(self.root_dir, "wywy_docs", "docs_index.db")
        self.assertTrue(os.path.isfile(db_path))
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            count = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
        finally:
            conn.close()
        self.assertIn("docs_fts", tables)
        self.assertEqual(count, 0)


class TestServerMissingDirectories(unittest.TestCase):
    """The real server (subprocess) starts and serves an empty index when
    ``docs/``/``internal/`` are missing, instead of crashing.

    Subprocess-level counterpart of ``TestEnsureIndexMissingDirectories``:
    exercises the same ``_ensure_index`` behaviour through the actual
    ``wywy_docs.server`` entry point via ``ServerProcess``.
    """

    def setUp(self) -> None:
        # Neither docs/ nor internal/ exists — the server must start anyway
        # and create an empty index.  The wywy_docs/ dir is created by
        # ``_ensure_index`` itself.
        self.root_dir = _setup_temp_wywy_root_missing()
        self.port = _find_free_port()

    def tearDown(self) -> None:
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_server_starts_without_docs_and_internal(self) -> None:
        """Starting the server with neither section dir present does not crash."""
        # _with_server calls server.start(), which raises RuntimeError if
        # the process exits early.
        with _with_server(self.root_dir, self.port) as server:
            pass

    def test_index_created_empty_without_dirs(self) -> None:
        """Missing sections still produce a ``docs_fts`` table with zero rows."""
        with _with_server(self.root_dir, self.port) as server:
            db_path = os.path.join(self.root_dir, "wywy_docs", "docs_index.db")
            self.assertTrue(os.path.isfile(db_path))
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_search_docs_returns_empty_when_index_empty(self) -> None:
        """search_docs on an empty index returns ``[]`` for a valid query."""
        with _with_server(self.root_dir, self.port) as server:
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
                    }
                )
                self.assertIn("result", resp)
                content = resp["result"]["content"]
                body_text = " ".join(str(item.get("text", "")) for item in content)
                results = json.loads(body_text)
                self.assertEqual(results, [])
            finally:
                client.close()

    def test_mixed_state_warns_for_missing_dir_only(self) -> None:
        """With only ``docs/`` present: warn about ``internal/`` only and
        index files from the existing ``docs/`` directory."""
        _create_file(
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
                    }
                )
                self.assertIn("result", resp)
                content = resp["result"]["content"]
                body_text = " ".join(str(item.get("text", "")) for item in content)
                results = json.loads(body_text)
                self.assertGreater(len(results), 0)
                self.assertEqual(results[0]["path"], "docs/only.mdx")
            finally:
                client.close()

        stderr = server._read_stderr()
        self.assertIn("internal directory does not exist", stderr)
        self.assertNotIn("docs directory does not exist", stderr)


class TestServerStartupTime(unittest.TestCase):
    """Server starts quickly with an existing database."""

    def setUp(self) -> None:
        self.root_dir = _setup_temp_wywy_root()
        self.port = _find_free_port()
        _build_test_index(
            self.root_dir,
            {"docs/bench.mdx": "---\ntitle: Bench\n---\nBenchmark document."},
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_server_startup_under_two_seconds(self) -> None:
        """With existing index, server starts in under 2 seconds."""
        start = time.time()
        server = ServerProcess(self.root_dir, self.port)
        server.start()
        elapsed = time.time() - start
        server.stop()
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
