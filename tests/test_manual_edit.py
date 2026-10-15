"""Tests for lazy index reconciliation of manually edited documents.

A manual edit — the opencode ``edit`` tool, an editor, or a shell redirect —
bypasses the server, so the FTS5 index keeps the pre-edit content until the
document is read back.  ``get_doc`` reconciles one document at a time: when a
file's mtime disagrees with the mtime recorded in ``file_metadata``, it
reindexes that path.  A manual edit therefore becomes searchable after the
next ``get_doc`` of that path, or after a server restart.

``search_docs`` stays index-only; it never stats files.
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

from tests.test_server import (
    JsonRpcResponse,
    MCPClient,
    ServerProcess,
    build_test_index,
    create_file,
    find_free_port,
    setup_temp_wywy_root,
)
from wywy_docs.indexer import build_index

HOST = "127.0.0.1"

_MANUAL_DOC = "docs/manual.mdx"


def _index_db(root_dir: str) -> Path:
    """Return the index database path for *root_dir*."""
    return Path(root_dir) / "wywy_docs" / "docs_index.db"


class TestManualEditReconciliation(unittest.TestCase):
    """``get_doc`` reconciles a manually edited document with the index."""

    root_dir: str
    port: int
    server: ServerProcess
    client: MCPClient

    def setUp(self) -> None:
        """Index one document, then start the server and client."""
        self.root_dir = setup_temp_wywy_root()
        self.port = find_free_port()
        self.doc_path = Path(self.root_dir) / _MANUAL_DOC
        build_test_index(
            self.root_dir,
            {_MANUAL_DOC: "---\ntitle: Manual\n---\nretiredterm body content."},
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

    def _call(self, name: str, arguments: dict[str, object]) -> JsonRpcResponse:
        """Send a tool call over JSON-RPC and return the raw response."""
        return self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": 50,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )

    @staticmethod
    def _text(response: JsonRpcResponse) -> str:
        """Return the concatenated text of a successful response."""
        assert "result" in response, response
        content = response["result"]["content"]
        return " ".join(str(item.get("text", "")) for item in content)

    def _search_paths(self, query: str) -> list[str]:
        """Return the ``path`` of every ``search_docs`` hit for *query*."""
        body = self._text(
            self._call("search_docs", {"query": query, "max_results": 10}),
        )
        return [entry["path"] for entry in json.loads(body)]

    def _read_doc(self, path: str) -> dict[str, object]:
        """Call ``get_doc`` and return the parsed document payload."""
        return cast(
            "dict[str, object]",
            json.loads(self._text(self._call("get_doc", {"path": path}))),
        )

    def _manual_edit(self, old: str, new: str) -> None:
        """Rewrite the document as the ``edit`` tool does — no server call.

        The mtime is bumped explicitly so it is guaranteed to disagree with
        the recorded mtime regardless of filesystem timestamp granularity.
        """
        text = self.doc_path.read_text()
        self.doc_path.write_text(text.replace(old, new))
        stat = self.doc_path.stat()
        bumped = stat.st_mtime_ns + 1_000_000_000
        os.utime(self.doc_path, ns=(bumped, bumped))

    def test_manual_edit_is_reflected_after_get_doc(self) -> None:
        """After ``get_doc``, search sees edited-in text and not edited-out text."""
        self._manual_edit("retiredterm", "introducedterm")

        assert (
            self._read_doc("docs/manual")["content"] == "introducedterm body content."
        )

        assert self._search_paths("introducedterm") == [_MANUAL_DOC]
        assert self._search_paths("retiredterm") == []

    def test_get_doc_repairs_document_missing_from_index(self) -> None:
        """A file with no index row gains one when it is read."""
        rel_path = "docs/handadded.mdx"
        create_file(
            self.root_dir,
            rel_path,
            "---\ntitle: Handadded\n---\nhandaddedtoken body.",
        )

        conn = sqlite3.connect(_index_db(self.root_dir))
        try:
            before = conn.execute(
                "SELECT path FROM docs_fts WHERE path = ?",
                (rel_path,),
            ).fetchall()
        finally:
            conn.close()
        assert before == []

        assert self._read_doc("docs/handadded")["content"] == "handaddedtoken body."

        expected_mtime = (Path(self.root_dir) / rel_path).stat().st_mtime_ns
        conn = sqlite3.connect(_index_db(self.root_dir))
        try:
            indexed = conn.execute(
                "SELECT path FROM docs_fts WHERE path = ?",
                (rel_path,),
            ).fetchall()
            recorded = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                (rel_path,),
            ).fetchone()
        finally:
            conn.close()
        assert indexed == [(rel_path,)]
        assert recorded == (expected_mtime,)

    def test_get_doc_leaves_unchanged_document_untouched(self) -> None:
        """Reading an unchanged document does not rewrite its index rows."""
        conn = sqlite3.connect(_index_db(self.root_dir))
        try:
            before = conn.execute(
                "SELECT content FROM docs_fts WHERE path = ?",
                (_MANUAL_DOC,),
            ).fetchone()
            before_mtime = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                (_MANUAL_DOC,),
            ).fetchone()
        finally:
            conn.close()

        self._read_doc("docs/manual")

        conn = sqlite3.connect(_index_db(self.root_dir))
        try:
            after = conn.execute(
                "SELECT content FROM docs_fts WHERE path = ?",
                (_MANUAL_DOC,),
            ).fetchone()
            after_mtime = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                (_MANUAL_DOC,),
            ).fetchone()
        finally:
            conn.close()
        assert after == before
        assert after_mtime == before_mtime


class TestGetDocReconciliationEdges(unittest.TestCase):
    """Direct-import tests for ``get_doc`` reconciliation edge cases."""

    def setUp(self) -> None:
        """Create a temp root with one indexed document."""
        self.root_dir = tempfile.mkdtemp()
        for name in ("docs", "internal", "wywy_docs"):
            (Path(self.root_dir) / name).mkdir(parents=True, exist_ok=True)
        create_file(
            self.root_dir,
            "docs/stale.mdx",
            "---\ntitle: Stale\n---\nstale body.",
        )
        self.doc_path = Path(self.root_dir) / "docs" / "stale.mdx"
        build_index(
            root_dirs=[
                str(Path(self.root_dir) / "docs"),
                str(Path(self.root_dir) / "internal"),
            ],
            db_path=str(_index_db(self.root_dir)),
        )

    def tearDown(self) -> None:
        """Remove the temp root."""
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def _force_stale_mtime(self) -> None:
        """Make the file's mtime disagree with the recorded mtime."""
        conn = sqlite3.connect(_index_db(self.root_dir))
        try:
            recorded = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                ("docs/stale.mdx",),
            ).fetchone()[0]
        finally:
            conn.close()
        bumped = recorded + 1_000_000_000
        os.utime(self.doc_path, ns=(bumped, bumped))

    def test_get_doc_reindex_failure_fails_the_read(self) -> None:
        """A reindex raised while reading surfaces as ``RuntimeError``."""
        import wywy_docs.server as server_mod

        server_mod._ROOT_DIR = self.root_dir
        self._force_stale_mtime()

        with (
            patch(
                "wywy_docs.server.reindex_doc",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError),
        ):
            server_mod.get_doc(path="docs/stale")

    def test_get_doc_outside_sections_does_not_index(self) -> None:
        """Reading a path outside ``docs/``/``internal/`` adds no index row."""
        import wywy_docs.server as server_mod

        server_mod._ROOT_DIR = self.root_dir
        create_file(
            self.root_dir,
            "loose.mdx",
            "---\ntitle: Loose\n---\nloose body.",
        )

        server_mod.get_doc(path="loose")

        conn = sqlite3.connect(_index_db(self.root_dir))
        try:
            rows = conn.execute(
                "SELECT path FROM docs_fts WHERE path = ?",
                ("loose.mdx",),
            ).fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_get_doc_missing_file_clears_index_rows(self) -> None:
        """A missing file clears its index rows and raises ``DocNotFoundError``."""
        import wywy_docs.server as server_mod
        from wywy_docs.server import DocNotFoundError

        server_mod._ROOT_DIR = self.root_dir
        self.doc_path.unlink()

        with pytest.raises(DocNotFoundError):
            server_mod.get_doc(path="docs/stale")

        conn = sqlite3.connect(_index_db(self.root_dir))
        try:
            fts = conn.execute(
                "SELECT path FROM docs_fts WHERE path = ?",
                ("docs/stale.mdx",),
            ).fetchall()
            meta = conn.execute(
                "SELECT path FROM file_metadata WHERE path = ?",
                ("docs/stale.mdx",),
            ).fetchall()
        finally:
            conn.close()
        assert fts == []
        assert meta == []


if __name__ == "__main__":
    unittest.main()
