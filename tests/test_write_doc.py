"""End-to-end and argument-interpretation tests for the ``write_doc`` MCP tool.

These tests start a real server subprocess and exercise the tool over JSON-RPC.
Document-pipeline unit tests (frontmatter merging, re-index behaviour, path
validation) live in ``test_documents.py``.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from mcp.types import INVALID_PARAMS

from tests.test_server import (
    JsonRpcResponse,
    MCPClient,
    ServerProcess,
    build_test_index,
    cleanup_ephemeral_files,
    create_file,
    find_free_port,
    setup_temp_wywy_root,
)
from wywy_docs.indexer import parse_file

HOST = "127.0.0.1"


# ===========================================================================
# Subprocess server
# ===========================================================================


class TestWriteDocTool(unittest.TestCase):
    """The ``write_doc`` tool creates/updates .mdx documentation files.

    All tests share a single server process for speed.  Each test uses
    ``self._testMethodName`` to create unique file paths, preventing
    cross-test interference.
    """

    # Attributes set in setUpClass; annotated for mypy.
    root_dir: str
    port: int
    server: ServerProcess
    client: MCPClient

    @classmethod
    def setUpClass(cls) -> None:
        """Build the test index and start the server and client."""
        cls.root_dir = setup_temp_wywy_root()
        cls.port = find_free_port()
        build_test_index(
            cls.root_dir,
            {"docs/init.mdx": "---\ntitle: Init\n---\nInitial doc for setup."},
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

    def tearDown(self) -> None:
        """Clean up artifacts created by the just-completed test.

        Removes the file at ``{section}/{method_name}.mdx`` (if it exists)
        and its entries in ``docs_fts`` and ``file_metadata`` so state
        does not leak between test methods.
        """
        name = self._testMethodName
        rel_paths = [f"{section}/{name}.mdx" for section in ("docs", "internal")]
        cleanup_ephemeral_files(self.root_dir, rel_paths)

    # ── JSON-RPC id counter ────────────────────────────────────────────

    _next_id: int = 100

    def _next_msg_id(self) -> int:
        TestWriteDocTool._next_id += 1
        return TestWriteDocTool._next_id

    # ── MCP call helper ────────────────────────────────────────────────

    def _call(
        self,
        section: str,
        path: str,
        content: str,
        frontmatter: dict[str, object] | None = None,
    ) -> JsonRpcResponse:
        """Send a ``write_doc`` tool-call and return the JSON-RPC response."""
        return self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": self._next_msg_id(),
                "method": "tools/call",
                "params": {
                    "name": "write_doc",
                    "arguments": {
                        "section": section,
                        "path": path,
                        "content": content,
                        "frontmatter": frontmatter,
                    },
                },
            },
        )

    # ── E2E smoke tests ────────────────────────────────────────────────

    def test_write_new_doc_creates_file(self) -> None:
        """Writing a new doc creates the ``.mdx`` file with managed dates."""
        name = self._testMethodName
        resp = self._call(
            "docs",
            name,
            "# Hello\nBody.",
            frontmatter={"title": "New Doc", "key": "value"},
        )
        assert "result" in resp, f"Expected result, got error: {resp}"
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["path"] == f"docs/{name}.mdx"

        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        assert full_path.is_file()

        parsed = parse_file(str(full_path), root=self.root_dir)
        assert parsed["frontmatter"]["title"] == "New Doc"
        assert parsed["frontmatter"]["key"] == "value"
        assert "published" in parsed["frontmatter"]
        assert "last_updated" in parsed["frontmatter"]
        assert "# Hello" in parsed["content"]

    def test_write_doc_updates_existing_doc(self) -> None:
        """Writing over an existing doc replaces the body and given frontmatter."""
        name = self._testMethodName
        create_file(
            self.root_dir,
            f"docs/{name}.mdx",
            "---\ntitle: Original\n---\nOriginal body.",
        )

        resp = self._call(
            "docs",
            name,
            "# Updated body",
            frontmatter={"title": "Updated"},
        )
        assert "result" in resp
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["path"] == f"docs/{name}.mdx"

        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        parsed = parse_file(str(full_path), root=self.root_dir)
        assert parsed["content"] == "# Updated body"
        assert parsed["frontmatter"]["title"] == "Updated"

    def test_write_doc_without_frontmatter_arg_succeeds(self) -> None:
        """Calling ``write_doc`` with no ``frontmatter`` creates the file."""
        name = self._testMethodName
        resp = self._call("docs", name, "# Body", frontmatter=None)
        assert "result" in resp
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["path"] == f"docs/{name}.mdx"

        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        assert full_path.is_file()

    # ── Argument interpretation ────────────────────────────────────────

    def test_write_invalid_section_returns_error(self) -> None:
        """An invalid *section* returns JSON-RPC error -32602."""
        resp = self._call("invalid", "any-path", "body")
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    def test_write_path_traversal_returns_error(self) -> None:
        """A path containing ``..`` returns JSON-RPC error -32602."""
        resp = self._call("docs", "../outside/file", "body")
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    def test_write_non_existent_parent_dir_returns_error(self) -> None:
        """A path whose parent directory does not exist returns an error.

        The tool responds with JSON-RPC error -32602.
        """
        resp = self._call("docs", "nonexistent_parent_dir_12345/file", "body")
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    def test_write_symlink_escape_returns_error(self) -> None:
        """A path that escapes via a symlink returns an error.

        Escaping ``docs/`` or ``internal/`` yields JSON-RPC error -32602.
        """
        link_name = f"escape_{self._testMethodName}"
        link_path = Path(self.root_dir) / "docs" / link_name
        # Symlink to a sibling of ``docs/`` inside the temp root; resolving
        # through it escapes the section directory, which must be rejected.
        link_path.symlink_to(self.root_dir)
        try:
            resp = self._call("docs", f"{link_name}/evil_file", "body")
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS
        finally:
            link_path.unlink()

    def test_written_doc_is_immediately_searchable(self) -> None:
        """A written document is immediately searchable by its own path.

        Parses the ``search_docs`` result (no substring matching) and asserts
        the written document's exact path is present.
        """
        name = self._testMethodName
        unique_term = f"UNIQUE_SEARCH_TERM_{name}"
        resp = self._call(
            "docs",
            name,
            f"# {unique_term}\nImmediately searchable content.",
            frontmatter={"title": "Immediately Searchable"},
        )
        assert "result" in resp

        search_resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": self._next_msg_id(),
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": unique_term, "max_results": 10},
                },
            },
        )
        assert "result" in search_resp
        content = search_resp["result"]["content"]
        body_text = " ".join(str(item.get("text", "")) for item in content)
        results = json.loads(body_text)
        assert f"docs/{name}.mdx" in [r["path"] for r in results]

    def test_write_reserved_frontmatter_rejected(self) -> None:
        """Reserved frontmatter keys are rejected.

        Frontmatter containing ``published`` or ``last_updated`` yields
        JSON-RPC error -32602.
        """
        name = self._testMethodName
        # published
        resp = self._call(
            "docs",
            name,
            "# Body",
            frontmatter={"published": "2024-01-01"},
        )
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS
        assert "reserved" in resp["error"]["message"].lower()

        # last_updated
        name2 = f"{name}_lu"
        resp2 = self._call(
            "docs",
            name2,
            "# Body",
            frontmatter={"last_updated": "2024-01-01"},
        )
        assert "error" in resp2
        assert resp2["error"]["code"] == INVALID_PARAMS


if __name__ == "__main__":
    unittest.main()
