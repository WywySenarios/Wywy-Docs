"""Tests for the ``delete_doc`` MCP tool.

Tests 1-7 use the subprocess server pattern (class-level server lifecycle).
Test 8 uses direct import + mock (no subprocess).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
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
    verify_metadata,
)

HOST = "127.0.0.1"


# ===========================================================================
# Tests 1-7  —  subprocess server
# ===========================================================================


class TestDeleteDocTool(unittest.TestCase):
    """The ``delete_doc`` tool removes a doc from disk and cleans up.

    It removes the documentation file and its index entries.  All tests
    share a single server process for speed.  Each test uses
    ``self._testMethodName`` to reference its own pre-indexed doc.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Create a temp root, start the server, and connect the client."""
        cls.root_dir = setup_temp_wywy_root()
        cls.port = find_free_port()
        cls.server = ServerProcess(cls.root_dir, cls.port)
        cls.server.start()
        cls.client = MCPClient(HOST, cls.port)
        cls.client.connect()

    def setUp(self) -> None:
        """Create this test's ephemeral file(s) and index them."""
        name = self._testMethodName
        contents = {
            "test_delete_existing_doc": (
                "docs/test_delete_existing_doc.mdx",
                "---\ntitle: Delete Existing\n---\n"
                "UNIQUE_TERM_test_delete_existing_doc content.",
            ),
            "test_delete_auto_appends_mdx": (
                "docs/test_delete_auto_appends_mdx.mdx",
                "---\ntitle: Auto Append\n---\n"
                "UNIQUE_TERM_test_delete_auto_appends_mdx content.",
            ),
            "test_delete_internal_doc": (
                "internal/test_delete_internal_doc.mdx",
                "---\ntitle: Internal Delete\n---\n"
                "INTERNAL_UNIQUE_test_delete_internal_doc content.",
            ),
        }
        self._created_files: list[str] = []
        if name in contents:
            rel_path, content = contents[name]
            self._created_files = [rel_path]
            build_test_index(self.root_dir, {rel_path: content})

    def tearDown(self) -> None:
        """Ensure this test's ephemeral files no longer exist.

        Removes any file left behind (e.g. by a failed test) and its
        entries in ``docs_fts`` and ``file_metadata`` so state does not
        leak between test methods.
        """
        cleanup_ephemeral_files(self.root_dir, self._created_files)

    @classmethod
    def tearDownClass(cls) -> None:
        """Close the client, stop the server, and remove the temp root."""
        cls.client.close()
        cls.server.stop()
        shutil.rmtree(cls.root_dir, ignore_errors=True)

    # ── JSON-RPC id counter ────────────────────────────────────────────

    _next_id: int = 200

    def _next_msg_id(self) -> int:
        TestDeleteDocTool._next_id += 1
        return TestDeleteDocTool._next_id

    # ── MCP call helper ────────────────────────────────────────────────

    def _call(self, path: str) -> JsonRpcResponse:
        """Send a ``delete_doc`` tool-call and return the JSON-RPC response."""
        return self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": self._next_msg_id(),
                "method": "tools/call",
                "params": {
                    "name": "delete_doc",
                    "arguments": {"path": path},
                },
            },
        )

    # ── Internal helpers ───────────────────────────────────────────────

    def _verify_not_searchable(self, term: str) -> None:
        """Call ``search_docs`` and assert *term* is absent from results."""
        search_resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": self._next_msg_id(),
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": term, "max_results": 10},
                },
            },
        )
        assert "result" in search_resp
        content = search_resp["result"]["content"]
        body_text = " ".join(str(item.get("text", "")) for item in content)
        assert term not in body_text

    # ── Property 1: Delete existing doc ────────────────────────────────

    def test_delete_existing_doc(self) -> None:
        """Deleting an existing doc removes the file and metadata.

        The index entry is removed and the success response returned.
        """
        name = self._testMethodName
        path_arg = f"docs/{name}.mdx"

        resp = self._call(path_arg)
        assert "result" in resp, f"Expected result, got error: {resp.get('error')}"
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result == {"path": path_arg, "deleted": True}

        # File gone from disk
        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        assert not full_path.is_file()

        # Metadata entry removed
        verify_metadata(self.root_dir, path_arg, present=False)

        # Not searchable
        self._verify_not_searchable(f"UNIQUE_TERM_{name}")

    # ── Property 2: Delete non-existent file ───────────────────────────

    def test_delete_non_existent_file(self) -> None:
        """Deleting a path that never existed returns success with no error."""
        name = self._testMethodName
        path_arg = f"docs/{name}.mdx"

        # Sanity: file does NOT exist before the call
        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        assert not full_path.is_file()

        resp = self._call(path_arg)
        assert "result" in resp, f"Expected result, got error: {resp.get('error')}"
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result == {"path": path_arg, "deleted": True}

        # File still doesn't exist
        assert not full_path.is_file()

    # ── Property 3: Path traversal ─────────────────────────────────────

    def test_delete_path_traversal(self) -> None:
        """A path containing ``..`` returns JSON-RPC error -32602."""
        resp = self._call("docs/../outside/file.mdx")
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    # ── Property 4: Symlink escape ─────────────────────────────────────

    def test_delete_symlink_escape(self) -> None:
        """A path that escapes via a symlink returns JSON-RPC error -32602."""
        link_name = f"escape_{self._testMethodName}"
        link_path = Path(self.root_dir) / "docs" / link_name
        link_path.symlink_to("/tmp")
        try:
            resp = self._call(f"docs/{link_name}/evil_file.mdx")
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS
        finally:
            link_path.unlink()

    # ── Property 5: Auto-append .mdx ───────────────────────────────────

    def test_delete_auto_appends_mdx(self) -> None:
        """Calling ``delete_doc`` without ``.mdx`` still deletes the file.

        The ``.mdx`` extension is appended automatically.
        """
        name = self._testMethodName

        # Call without the .mdx extension
        resp = self._call(f"docs/{name}")
        assert "result" in resp, f"Expected result, got error: {resp.get('error')}"
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result == {"path": f"docs/{name}.mdx", "deleted": True}

        # The .mdx file is gone from disk
        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        assert not full_path.is_file()

    # ── Property 6: Internal section ───────────────────────────────────

    def test_delete_internal_doc(self) -> None:
        """Deleting from ``internal/`` works identically to ``docs/``."""
        name = self._testMethodName
        path_arg = f"internal/{name}.mdx"

        resp = self._call(path_arg)
        assert "result" in resp, f"Expected result, got error: {resp.get('error')}"
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result == {"path": path_arg, "deleted": True}

        # File gone from disk
        full_path = Path(self.root_dir) / "internal" / f"{name}.mdx"
        assert not full_path.is_file()

        # Metadata entry removed
        verify_metadata(self.root_dir, path_arg, present=False)

        # Not searchable
        self._verify_not_searchable(f"INTERNAL_UNIQUE_{name}")

    # ── Property 7: Unknown section ────────────────────────────────────

    def test_delete_unknown_section(self) -> None:
        """A path outside ``docs/`` and ``internal/`` returns an error.

        The tool responds with JSON-RPC error -32602.
        """
        resp = self._call("other/file.mdx")
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS


# ===========================================================================
# Test 8  —  direct import + mock (no subprocess)
# ===========================================================================


class TestDeleteDocToolIndexFailure(unittest.TestCase):
    """Behavior when ``os.remove`` fails after finding the document.

    The tool raises ``RuntimeError`` which maps to JSON-RPC error -32603.
    This class does **not** use ``ServerProcess``.  Instead it imports
    ``wywy_docs.server`` directly, sets ``_ROOT_DIR`` to a temporary
    directory, and patches ``os.remove`` to fail with ``PermissionError``.
    """

    def setUp(self) -> None:
        """Create a temp root, an initial doc, and a built index."""
        self.root_dir = tempfile.mkdtemp()
        for d in ("docs", "internal", "wywy_docs"):
            (Path(self.root_dir) / d).mkdir(parents=True, exist_ok=True)
        create_file(
            self.root_dir,
            "docs/test_delete_os_remove_failure.mdx",
            "---\ntitle: OS Remove Failure\n---\nContent.",
        )
        db_path = Path(self.root_dir) / "wywy_docs" / "docs_index.db"
        from wywy_docs.indexer import build_index as _bi

        _bi(
            root_dirs=[
                str(Path(self.root_dir) / "docs"),
                str(Path(self.root_dir) / "internal"),
            ],
            db_path=str(db_path),
        )

        # Sanity: file exists and is indexed
        assert (
            Path(self.root_dir) / "docs" / "test_delete_os_remove_failure.mdx"
        ).is_file()

    def tearDown(self) -> None:
        """Remove the temp root."""
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_delete_os_remove_failure_returns_error(self) -> None:
        """When the file unlink raises ``PermissionError``, the tool errors.

        The ``PermissionError`` is a subclass of ``OSError``; the tool
        raises ``RuntimeError`` → -32603.
        """
        import wywy_docs.server as server_mod  # type: ignore[attr-defined]

        server_mod._ROOT_DIR = self.root_dir  # type: ignore[reportPrivateUsage]

        with patch(
            "pathlib.Path.unlink",
            side_effect=PermissionError("Permission denied"),
        ):
            with pytest.raises(RuntimeError) as ctx:
                server_mod.delete_doc(path="docs/test_delete_os_remove_failure.mdx")
            assert "Permission denied" in str(ctx.value)

        # The file should still be on disk since deletion failed
        assert (
            Path(self.root_dir) / "docs" / "test_delete_os_remove_failure.mdx"
        ).is_file()


if __name__ == "__main__":
    unittest.main()
