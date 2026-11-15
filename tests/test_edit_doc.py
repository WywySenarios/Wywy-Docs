"""End-to-end, argument-interpretation, and server error-mapping tests for ``edit_doc``.

Document-pipeline unit tests (replacement/frontmatter/date rules, degenerate
edits, malformed headers, and index-failure cleanup) live in
``test_documents.py``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolRequest,
    CallToolRequestParams,
)

import wywy_docs.server
from tests.test_server import (
    JsonRpcResponse,
    MCPClient,
    ServerProcess,
    build_test_index,
    cleanup_ephemeral_files,
    find_free_port,
    setup_temp_wywy_root,
)
from wywy_docs import indexer

HOST = "127.0.0.1"


# ===========================================================================
# Subprocess server — E2E / argument / error mapping
# ===========================================================================


class TestEditDocTool(unittest.TestCase):
    """The ``edit_doc`` tool edits a document body in place.

    All tests share one server process.  Each test seeds its own document(s)
    through :func:`build_test_index` and records their paths so ``tearDown``
    can remove both the file and its index rows.
    """

    # Attributes set in setUpClass; annotated for mypy.
    root_dir: str
    port: int
    server: ServerProcess
    client: MCPClient

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
        """Start each test with an empty ephemeral-file ledger."""
        self._created_files: list[str] = []

    def tearDown(self) -> None:
        """Remove this test's files and their index rows."""
        cleanup_ephemeral_files(self.root_dir, self._created_files)

    @classmethod
    def tearDownClass(cls) -> None:
        """Close the client, stop the server, and remove the temp root."""
        cls.client.close()
        cls.server.stop()
        shutil.rmtree(cls.root_dir, ignore_errors=True)

    # ── JSON-RPC id counter ────────────────────────────────────────────

    _next_id: int = 300

    def _next_msg_id(self) -> int:
        TestEditDocTool._next_id += 1
        return TestEditDocTool._next_id

    # ── Helpers ────────────────────────────────────────────────────────

    def _seed(self, files: dict[str, str]) -> None:
        """Create and index *files*, recording them for ``tearDown``."""
        build_test_index(self.root_dir, files)
        self._created_files.extend(files)

    def _call(self, **arguments: object) -> JsonRpcResponse:
        """Send an ``edit_doc`` tool-call and return the JSON-RPC response."""
        return self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": self._next_msg_id(),
                "method": "tools/call",
                "params": {"name": "edit_doc", "arguments": arguments},
            },
        )

    @staticmethod
    def _parse_result(resp: JsonRpcResponse) -> object:
        """Return the parsed JSON payload of a successful tool result."""
        assert "result" in resp, f"Expected result, got error: {resp.get('error')}"
        return json.loads(resp["result"]["content"][0]["text"])

    @staticmethod
    def _assert_error(resp: JsonRpcResponse, *, code: int, message: str) -> None:
        """Assert *resp* carries the expected JSON-RPC error code and message."""
        assert "error" in resp, f"Expected error, got result: {resp.get('result')}"
        assert resp["error"]["code"] == code, resp["error"]["message"]
        assert resp["error"]["message"] == message

    def _search(self, query: str) -> list[dict[str, object]]:
        """Run ``search_docs`` for *query* and return the parsed results."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": self._next_msg_id(),
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": query, "max_results": 10},
                },
            },
        )
        return cast("list[dict[str, object]]", self._parse_result(resp))

    # ── E2E smoke ──────────────────────────────────────────────────────

    def test_edit_single_replacement(self) -> None:
        """One replacement updates the body (E2E smoke test)."""
        self._seed(
            {
                "docs/single.mdx": (
                    "---\n"
                    "published: 2023-06-15T12:00:00\n"
                    "last_updated: 2023-06-15T12:00:00\n"
                    "title: Original\n"
                    "desc: keep\n"
                    "---\n"
                    "alpha beta gamma."
                ),
            },
        )

        resp = self._call(
            section="docs",
            path="single",
            old_string="beta",
            new_string="BETA",
        )
        result = cast("dict[str, object]", self._parse_result(resp))
        assert result["path"] == "docs/single.mdx"
        assert result["replacements"] == 1

        parsed = indexer.parse_file(
            str(Path(self.root_dir, "docs/single.mdx")),
            root=self.root_dir,
        )
        assert parsed["content"] == "alpha BETA gamma."

    # ── Argument interpretation ────────────────────────────────────────

    def test_edit_replace_all_replaces_every_occurrence(self) -> None:
        """``replace_all`` replaces every occurrence and reports the count."""
        self._seed({"docs/all.mdx": "red red red."})

        resp = self._call(
            section="docs",
            path="all",
            old_string="red",
            new_string="blue",
            replace_all=True,
        )
        result = cast("dict[str, object]", self._parse_result(resp))
        assert result["replacements"] == 3  # noqa: PLR2004

        parsed = indexer.parse_file(
            str(Path(self.root_dir, "docs/all.mdx")),
            root=self.root_dir,
        )
        assert parsed["content"] == "blue blue blue."

    def test_edit_ambiguous_match_rejected(self) -> None:
        """An ambiguous match without ``replace_all`` is rejected."""
        self._seed({"docs/multi.mdx": "red red."})

        resp = self._call(
            section="docs",
            path="multi",
            old_string="red",
            new_string="blue",
        )
        self._assert_error(
            resp,
            code=INVALID_PARAMS,
            message=(
                "old_string matches 2 times in docs/multi.mdx; "
                "set replace_all=true to replace all occurrences"
            ),
        )

    # ── Error mapping ──────────────────────────────────────────────────

    def test_edit_missing_document_returns_not_found(self) -> None:
        """Editing a missing document returns ``DOCUMENT_NOT_FOUND``."""
        from wywy_docs.server import DOCUMENT_NOT_FOUND

        resp = self._call(
            section="docs",
            path="missing",
            old_string="x",
            new_string="y",
        )
        self._assert_error(
            resp,
            code=DOCUMENT_NOT_FOUND,
            message="document not found: docs/missing.mdx",
        )
        assert not Path(self.root_dir, "docs/missing.mdx").is_file()

    # ── Error mapping ──────────────────────────────────────────────────

    def test_edit_path_traversal_rejected(self) -> None:
        """A path containing ``..`` is rejected before any read or write."""
        self._assert_error(
            self._call(
                section="docs",
                path="../outside/file",
                old_string="x",
                new_string="y",
            ),
            code=INVALID_PARAMS,
            message="path traversal ('..') is not allowed",
        )

    def test_edit_symlink_escape_rejected(self) -> None:
        """A path escaping via a symlink is rejected before any write."""
        link_name = f"escape_{self._testMethodName}"
        link_path = Path(self.root_dir, "docs", link_name)
        link_path.symlink_to(self.root_dir)
        try:
            resp = self._call(
                section="docs",
                path=f"{link_name}/evil_file",
                old_string="x",
                new_string="y",
            )
            self._assert_error(
                resp,
                code=INVALID_PARAMS,
                message="path escapes the allowed directory via symlink",
            )
            assert not Path(self.root_dir, "evil_file.mdx").exists()
        finally:
            link_path.unlink()

    # ── Search integration ─────────────────────────────────────────────

    def test_edit_is_immediately_searchable(self) -> None:
        """An edit replaces the old term with the new term in the search index."""
        self._seed(
            {
                "docs/indexed.mdx": ("---\ntitle: Indexed\n---\nretiredterm content."),
            },
        )

        initial_paths = [r["path"] for r in self._search("retiredterm")]
        assert "docs/indexed.mdx" in initial_paths

        resp = self._call(
            section="docs",
            path="indexed",
            old_string="retiredterm",
            new_string="introducedterm",
        )
        result = cast("dict[str, object]", self._parse_result(resp))
        assert result["replacements"] == 1

        introduced_paths = [r["path"] for r in self._search("introducedterm")]
        assert "docs/indexed.mdx" in introduced_paths
        retired_paths = [r["path"] for r in self._search("retiredterm")]
        assert "docs/indexed.mdx" not in retired_paths

    # ── Error mapping ──────────────────────────────────────────────────

    def test_edit_malformed_frontmatter_returns_invalid_params(self) -> None:
        """Malformed frontmatter maps to ``INVALID_PARAMS``."""
        self._seed({"docs/malformed.mdx": "---\ntitle: [unclosed\n---\nbody text.\n"})

        resp = self._call(
            section="docs",
            path="malformed",
            old_string="body",
            new_string="BODY",
        )
        self._assert_error(
            resp,
            code=INVALID_PARAMS,
            message="document has malformed frontmatter; use write_doc",
        )

    # ── Tool registry ──────────────────────────────────────────────────

    def test_tools_list_advertises_edit_doc(self) -> None:
        """``tools/list`` advertises both ``write_doc`` and ``edit_doc``."""
        resp = self.client.send_message(
            {
                "jsonrpc": "2.0",
                "id": self._next_msg_id(),
                "method": "tools/list",
                "params": {},
            },
        )
        assert "result" in resp, f"Expected result, got error: {resp.get('error')}"
        names = {tool["name"] for tool in resp["result"]["tools"]}
        assert "write_doc" in names
        assert "edit_doc" in names

    # ── Argument interpretation ────────────────────────────────────────

    def test_edit_internal_section(self) -> None:
        """Edits work in ``internal/`` and the result is immediately searchable."""
        self._seed({"internal/ops.mdx": "---\ntitle: Ops\n---\nops token"})

        resp = self._call(
            section="internal",
            path="ops",
            old_string="token",
            new_string="TOKEN",
        )
        result = cast("dict[str, object]", self._parse_result(resp))
        assert result["path"] == "internal/ops.mdx"

        parsed = indexer.parse_file(
            str(Path(self.root_dir, "internal/ops.mdx")),
            root=self.root_dir,
        )
        assert parsed["content"] == "ops TOKEN"

        paths = [r["path"] for r in self._search("TOKEN")]
        assert "internal/ops.mdx" in paths


# ===========================================================================
# Direct call-tool handler (no subprocess)
# ===========================================================================


class TestEditDocHandlerIndexFailure(unittest.TestCase):
    """The call-tool handler maps a post-edit reindex failure to ``INTERNAL_ERROR``.

    This class does **not** use ``ServerProcess``.  It invokes
    ``_call_tool_handler`` directly with a ``CallToolRequest`` while
    ``wywy_docs.documents.reindex_doc`` is patched to fail.
    """

    def setUp(self) -> None:
        """Create a temp root with one indexed document."""
        self._saved_root = wywy_docs.server._ROOT_DIR
        self.root_dir = tempfile.mkdtemp()
        for name in ("docs", "internal", "wywy_docs"):
            (Path(self.root_dir) / name).mkdir(parents=True, exist_ok=True)
        build_test_index(self.root_dir, {"docs/fail-handler.mdx": "body text"})
        wywy_docs.server._ROOT_DIR = self.root_dir

    def tearDown(self) -> None:
        """Restore the previous root and remove the temp root."""
        wywy_docs.server._ROOT_DIR = self._saved_root
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def test_edit_handler_maps_index_failure_to_internal_error(self) -> None:
        """A reindex failure becomes ``INTERNAL_ERROR`` with the exact message."""
        request = CallToolRequest(
            params=CallToolRequestParams(
                name="edit_doc",
                arguments={
                    "section": "docs",
                    "path": "fail-handler",
                    "old_string": "body",
                    "new_string": "BODY",
                },
            ),
        )

        with (
            patch(
                "wywy_docs.documents.reindex_doc",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(McpError) as ctx,
        ):
            asyncio.run(wywy_docs.server._call_tool_handler(request))

        assert ctx.value.error.code == INTERNAL_ERROR
        assert ctx.value.error.message == "file edited but index update failed: boom"


if __name__ == "__main__":
    unittest.main()
