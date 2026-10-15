"""Tests for the ``write_doc`` MCP tool.

Tests 1-10 and 12 use the subprocess server pattern shared with
``TestSearchDocsTool`` (class-level server lifecycle).  Test 11 uses direct
import + mock (no subprocess).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
import yaml
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
from wywy_docs.indexer import build_index

HOST = "127.0.0.1"


# ── Frontmatter parser helper ──────────────────────────────────────────

# Splitting on "---" yields 3 parts: header, frontmatter, body.
_FRONTMATTER_PARTS = 3


def _parse_frontmatter(filepath: Path) -> tuple[dict[str, object], str]:
    """Read *filepath* and return ``(frontmatter_dict, body_text)``."""
    with filepath.open() as f:
        raw = f.read()
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= _FRONTMATTER_PARTS:
            fm = cast("dict[str, object]", yaml.safe_load(parts[1]) or {})
            body = parts[2].strip()
            return dict(fm), body
    return {}, raw.strip()


# ===========================================================================
# Tests 1-10, 12  —  subprocess server
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

    # ── Property 1 ─────────────────────────────────────────────────────

    def test_write_new_doc_creates_file(self) -> None:
        """Writing a new doc creates the ``.mdx`` file.

        Frontmatter contains *published* and *last_updated*.
        """
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

        fm, body = _parse_frontmatter(full_path)
        assert "published" in fm
        assert "last_updated" in fm
        assert fm.get("title") == "New Doc"
        assert fm.get("key") == "value"
        assert "# Hello" in body

    # ── Property 2 ─────────────────────────────────────────────────────

    def test_write_update_preserves_published(self) -> None:
        """Updating preserves the original *published* value.

        *last_updated* is refreshed and existing frontmatter keys are kept
        unless overridden.
        """
        name = self._testMethodName
        create_file(
            self.root_dir,
            f"docs/{name}.mdx",
            "---\n"
            "published: 2023-06-15T12:00:00\n"
            "title: Original\n"
            "desc: original\n"
            "tags: [a, b]\n"
            "---\n"
            "Original body.",
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
        fm, body = _parse_frontmatter(full_path)

        # published preserved
        assert fm.get("published") == "2023-06-15T12:00:00"
        # last_updated updated (rough check — time between call)
        assert "last_updated" in fm
        # title overridden
        assert fm.get("title") == "Updated"
        # desc preserved (not in user frontmatter)
        assert fm.get("desc") == "original"
        # tags preserved
        assert fm.get("tags") == ["a", "b"]
        assert "# Updated body" in body

    # ── Property 3 ─────────────────────────────────────────────────────

    def test_write_update_no_published_sets_current_time(self) -> None:
        """Updating a doc without *published* sets *published*.

        The value becomes the current time.
        """
        name = self._testMethodName
        create_file(
            self.root_dir,
            f"docs/{name}.mdx",
            "---\ntitle: No Published\ndesc: missing-pub\n---\nBody.",
        )

        resp = self._call(
            "docs",
            name,
            "# Updated",
            frontmatter={"title": "Now Has Published"},
        )
        assert "result" in resp

        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        fm, _body = _parse_frontmatter(full_path)
        assert "published" in fm
        assert "last_updated" in fm
        assert fm.get("title") == "Now Has Published"
        # desc should have been preserved from the original frontmatter
        assert fm.get("desc") == "missing-pub"

    # ── Property 4 ─────────────────────────────────────────────────────

    def test_write_update_no_frontmatter_sets_both_dates(self) -> None:
        """Updating a doc with no frontmatter at all succeeds.

        Both *published* and *last_updated* are set fresh.
        """
        name = self._testMethodName
        create_file(
            self.root_dir,
            f"docs/{name}.mdx",
            "Body with no frontmatter at all.",
        )

        resp = self._call("docs", name, "# New body")
        assert "result" in resp

        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        fm, body = _parse_frontmatter(full_path)
        assert "published" in fm
        assert "last_updated" in fm
        assert "# New body" in body

    # ── Property 5 ─────────────────────────────────────────────────────

    def test_write_no_frontmatter_arg_still_has_dates(self) -> None:
        """Calling ``write_doc`` with ``frontmatter=None`` still generates dates.

        Both *published* and *last_updated* are set.
        """
        name = self._testMethodName
        # New doc — no existing file
        resp = self._call("docs", name, "# Body", frontmatter=None)
        assert "result" in resp

        full_path = Path(self.root_dir) / "docs" / f"{name}.mdx"
        assert full_path.is_file()
        fm, body = _parse_frontmatter(full_path)
        assert "published" in fm
        assert "last_updated" in fm
        assert "# Body" in body

    # ── Property 6 ─────────────────────────────────────────────────────

    def test_write_invalid_section_returns_error(self) -> None:
        """An invalid *section* returns JSON-RPC error -32602."""
        resp = self._call("invalid", "any-path", "body")
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    # ── Property 7 ─────────────────────────────────────────────────────

    def test_write_path_traversal_returns_error(self) -> None:
        """A path containing ``..`` returns JSON-RPC error -32602."""
        resp = self._call("docs", "../outside/file", "body")
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    # ── Property 8 ─────────────────────────────────────────────────────

    def test_write_non_existent_parent_dir_returns_error(self) -> None:
        """A path whose parent directory does not exist returns an error.

        The tool responds with JSON-RPC error -32602.
        """
        resp = self._call("docs", "nonexistent_parent_dir_12345/file", "body")
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    # ── Property 9 ─────────────────────────────────────────────────────

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

    # ── Property 10 ────────────────────────────────────────────────────

    def test_write_triggers_reindex(self) -> None:
        """Writing triggers an incremental re-index.

        The new doc is immediately searchable via ``search_docs``.
        """
        name = self._testMethodName
        unique_term = f"UNIQUE_SEARCH_TERM_{name}"
        resp = self._call(
            "docs",
            name,
            f"# {unique_term}\nSearchable content.",
            frontmatter={"title": "Searchable Doc"},
        )
        assert "result" in resp

        # Search for the unique term via the shared client.
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
        assert unique_term in body_text

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

    # ── Property 12 ────────────────────────────────────────────────────

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


# ===========================================================================
# Per-document reindex —  subprocess server with explicit cleanup
# ===========================================================================


class TestWriteDocPerDocumentReindex(unittest.TestCase):
    """``write_doc`` reindexes only the written document.

    A whole-tree rebuild would also reimport an unrelated document whose file
    changed on disk without going through the tool.  This class owns its own
    server so the on-disk mutation cannot leak into other test classes.
    """

    # Attributes set in setUpClass; annotated for mypy.
    root_dir: str
    port: int
    server: ServerProcess
    client: MCPClient

    @classmethod
    def setUpClass(cls) -> None:
        """Build the index (two docs) and start the server and client."""
        cls.root_dir = setup_temp_wywy_root()
        cls.port = find_free_port()
        build_test_index(
            cls.root_dir,
            {
                "docs/first.mdx": "---\ntitle: First\n---\nFirst stable body.",
                "docs/unrelated.mdx": (
                    "---\ntitle: Unrelated\n---\nOriginal unrelated body."
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

    # ── JSON-RPC id counter ────────────────────────────────────────────

    _next_id: int = 500

    def _next_msg_id(self) -> int:
        TestWriteDocPerDocumentReindex._next_id += 1
        return TestWriteDocPerDocumentReindex._next_id

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

    def test_write_doc_leaves_unrelated_stale_row_untouched(self) -> None:
        """A file changed on disk outside the tool is not reindexed by write_doc.

        The unrelated document is rewritten on disk with a new body and mtime
        (exactly the state a whole-tree ``build_index`` would reindex).  After
        writing a third document through the tool, the unrelated document's
        indexed content and recorded mtime must be unchanged.
        """
        db_path = Path(self.root_dir) / "wywy_docs" / "docs_index.db"
        rel_path = "docs/unrelated.mdx"

        def read_index_state() -> tuple[str, int]:
            conn = sqlite3.connect(db_path)
            try:
                content = conn.execute(
                    "SELECT content FROM docs_fts WHERE path = ?",
                    (rel_path,),
                ).fetchone()[0]
                mtime = conn.execute(
                    "SELECT mtime FROM file_metadata WHERE path = ?",
                    (rel_path,),
                ).fetchone()[0]
            finally:
                conn.close()
            return content, mtime

        original_content, original_mtime = read_index_state()

        # Rewrite the unrelated file directly on disk with a new body, then
        # force a distinct mtime so a whole-tree rebuild would reindex it.
        create_file(
            self.root_dir,
            rel_path,
            "---\ntitle: Unrelated\n---\nRewritten on disk body.",
        )
        rewritten_mtime_ns = original_mtime + 10_000_000_000
        os.utime(
            Path(self.root_dir) / rel_path,
            ns=(rewritten_mtime_ns, rewritten_mtime_ns),
        )

        # Write an unrelated third document through the tool.
        name = self._testMethodName
        resp = self._call(
            "docs",
            name,
            "# Third document\nWritten through the tool.",
            frontmatter={"title": "Third"},
        )
        assert "result" in resp

        after_content, after_mtime = read_index_state()
        assert after_content == original_content
        assert after_mtime == original_mtime


# ===========================================================================
# Test 11  —  direct import + mock (no subprocess)
# ===========================================================================


class TestWriteDocToolIndexFailure(unittest.TestCase):
    """Behavior when the per-document reindex fails after a successful file write.

    The tool returns a JSON-RPC error -32603 and the ``file_metadata``
    entry for the path is deleted.  This class does **not** use
    ``ServerProcess``.  Instead it imports ``wywy_docs.server`` directly,
    sets ``_ROOT_DIR`` to a temporary directory, and patches
    ``reindex_doc`` to fail.
    """

    def setUp(self) -> None:
        """Create a temp root, an initial doc, and a built index."""
        self.root_dir = tempfile.mkdtemp()
        for d in ("docs", "internal", "wywy_docs"):
            (Path(self.root_dir) / d).mkdir(parents=True, exist_ok=True)
        create_file(
            self.root_dir,
            "docs/initial.mdx",
            "---\ntitle: Initial\n---\nInitial body.",
        )
        db_path = Path(self.root_dir) / "wywy_docs" / "docs_index.db"
        build_index(
            root_dirs=[
                str(Path(self.root_dir) / "docs"),
                str(Path(self.root_dir) / "internal"),
            ],
            db_path=str(db_path),
        )

        # Quick sanity: file_metadata has the initial entry
        verify_metadata(self.root_dir, "docs/initial.mdx", present=True)

    def tearDown(self) -> None:
        """Remove the temp root."""
        shutil.rmtree(self.root_dir, ignore_errors=True)

    # ── test ───────────────────────────────────────────────────────────

    def test_write_doc_index_failure_returns_error_and_cleans_metadata(
        self,
    ) -> None:
        """When ``reindex_doc`` raises, the tool returns -32603.

        The ``file_metadata`` entry for the just-written path is deleted.
        """
        import wywy_docs.server as server_mod

        server_mod._ROOT_DIR = self.root_dir

        # Pre-seed the row for the path about to be written so the cleanup
        # assertion below cannot pass vacuously.
        db_path = Path(self.root_dir) / "wywy_docs" / "docs_index.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO file_metadata(path, mtime) VALUES (?, ?)",
            ("docs/fail-test.mdx", 1_700_000_000_000_000_000),
        )
        conn.commit()
        conn.close()
        verify_metadata(self.root_dir, "docs/fail-test.mdx", present=True)

        with patch(
            "wywy_docs.server.reindex_doc",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError) as ctx:
                server_mod.write_doc(
                    section="docs",
                    path="fail-test",
                    content="# Failed indexing",
                    frontmatter={"title": "Fail Test"},
                )
            assert str(ctx.value) == "file written but index update failed: boom"

        # The file_metadata entry for the new path must have been removed.
        verify_metadata(self.root_dir, "docs/fail-test.mdx", present=False)


if __name__ == "__main__":
    unittest.main()
