"""Direct unit tests for ``wywy_docs/documents.py``.

These tests exercise the document-mutation functions against a temporary root
and an explicit database path.  No MCP server is started and
``wywy_docs.server._ROOT_DIR`` is never consulted.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from tests.test_server import create_file, verify_metadata
from wywy_docs.documents import (
    DocNotFoundError,
    atomic_write,
    edit_document,
    normalize_doc_path,
    resolve_published,
    resolve_section_path,
    write_document,
)
from wywy_docs.indexer import ParsedFile, build_index, parse_file


class BaseDocumentTest(unittest.TestCase):
    """Temporary root and index helpers shared by the direct tests."""

    root: str
    db_path: str

    def setUp(self) -> None:
        """Create a temporary root with the conventional section layout."""
        self.root = tempfile.mkdtemp()
        for name in ("docs", "internal", "wywy_docs"):
            Path(self.root, name).mkdir(parents=True, exist_ok=True)
        self.db_path = str(Path(self.root, "wywy_docs", "docs_index.db"))

    def tearDown(self) -> None:
        """Remove the temporary root."""
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self, files: dict[str, str]) -> None:
        """Write *files* under the root and build the index."""
        for rel_path, content in files.items():
            create_file(self.root, rel_path, content)
        build_index(
            root_dirs=[
                str(Path(self.root, "docs")),
                str(Path(self.root, "internal")),
            ],
            db_path=self.db_path,
        )

    def _parse(self, rel_path: str) -> ParsedFile:
        """Parse *rel_path* relative to the test root."""
        return parse_file(str(Path(self.root, rel_path)), root=self.root)

    def _row(self, rel_path: str) -> tuple[str, str]:
        """Return the ``(content, frontmatter_json)`` row for *rel_path*."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT content, frontmatter_json FROM docs_fts WHERE path = ?",
                (rel_path,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, f"No docs_fts row for {rel_path}"
        return cast("tuple[str, str]", row)

    def _file_metadata(self, rel_path: str) -> int | None:
        """Return the recorded ``mtime`` for *rel_path*, or ``None``."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                (rel_path,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return cast("int", row[0])


class TestWriteDocument(BaseDocumentTest):
    """Direct tests for :func:`wywy_docs.documents.write_document`."""

    def test_write_document_creates_document(self) -> None:
        """A new document is written with managed dates and indexed."""
        rel_path = write_document(
            self.root,
            self.db_path,
            "docs",
            "new",
            "# Hello",
            {"title": "New", "key": "value"},
        )

        assert rel_path == "docs/new.mdx"
        assert Path(self.root, "docs", "new.mdx").is_file()

        parsed = self._parse("docs/new.mdx")
        frontmatter = parsed["frontmatter"]
        assert frontmatter["title"] == "New"
        assert frontmatter["key"] == "value"
        assert "published" in frontmatter
        assert "last_updated" in frontmatter
        assert parsed["content"] == "# Hello"
        assert self._row("docs/new.mdx")[0] == "# Hello"

    def test_write_document_update_preserves_existing_frontmatter(self) -> None:
        """Updating keeps existing keys and the original ``published`` value."""
        self._seed(
            {
                "docs/update.mdx": (
                    "---\n"
                    "published: 2023-06-15T12:00:00\n"
                    "title: Original\n"
                    "desc: original\n"
                    "tags: [a, b]\n"
                    "---\n"
                    "Original body."
                ),
            },
        )

        write_document(
            self.root,
            self.db_path,
            "docs",
            "update",
            "# Updated body",
            {"title": "Updated"},
        )

        parsed = self._parse("docs/update.mdx")
        frontmatter = parsed["frontmatter"]
        assert frontmatter["published"] == "2023-06-15T12:00:00"
        assert frontmatter["last_updated"] != frontmatter["published"]
        assert frontmatter["title"] == "Updated"
        assert frontmatter["desc"] == "original"
        assert frontmatter["tags"] == ["a", "b"]
        assert parsed["content"] == "# Updated body"

    def test_write_document_sets_published_when_missing(self) -> None:
        """An existing document without ``published`` gains managed dates."""
        self._seed(
            {
                "docs/nopub.mdx": (
                    "---\ntitle: No Published\ndesc: missing-pub\n---\nBody."
                ),
            },
        )

        write_document(
            self.root,
            self.db_path,
            "docs",
            "nopub",
            "# Updated",
            {"title": "Now Has Published"},
        )

        parsed = self._parse("docs/nopub.mdx")
        frontmatter = parsed["frontmatter"]
        assert "published" in frontmatter
        assert "last_updated" in frontmatter
        assert frontmatter["title"] == "Now Has Published"
        assert frontmatter["desc"] == "missing-pub"

    def test_write_document_no_frontmatter_adds_dates(self) -> None:
        """Writing with ``frontmatter=None`` still adds both managed dates."""
        self._seed({"docs/bare.mdx": "Body with no frontmatter."})

        write_document(
            self.root,
            self.db_path,
            "docs",
            "bare",
            "# New body",
            None,
        )

        parsed = self._parse("docs/bare.mdx")
        assert "published" in parsed["frontmatter"]
        assert "last_updated" in parsed["frontmatter"]
        assert parsed["content"] == "# New body"

    def test_write_document_rejects_reserved_frontmatter(self) -> None:
        """Reserved keys are rejected before any file is written."""
        for key in ("published", "last_updated"):
            with pytest.raises(ValueError, match="reserved"):
                write_document(
                    self.root,
                    self.db_path,
                    "docs",
                    "reserved",
                    "# Body",
                    {key: "2024-01-01"},
                )
            assert not Path(self.root, "docs", "reserved.mdx").exists()

    def test_write_document_rejects_path_traversal(self) -> None:
        """A path containing ``..`` is rejected."""
        with pytest.raises(ValueError, match="path traversal"):
            write_document(
                self.root,
                self.db_path,
                "docs",
                "../outside/file",
                "body",
                None,
            )

    def test_write_document_rejects_symlink_escape(self) -> None:
        """A path escaping the section through a symlink is rejected."""
        link = "escape_write"
        link_path = Path(self.root, "docs", link)
        link_path.symlink_to(self.root)
        try:
            with pytest.raises(
                ValueError,
                match="path escapes the allowed directory via symlink",
            ):
                write_document(
                    self.root,
                    self.db_path,
                    "docs",
                    f"{link}/evil_file",
                    "body",
                    None,
                )
            assert not Path(self.root, "evil_file.mdx").exists()
        finally:
            link_path.unlink()

    def test_write_document_rejects_missing_parent(self) -> None:
        """A path whose parent directory is absent is rejected."""
        with pytest.raises(ValueError, match="parent directory does not exist"):
            write_document(
                self.root,
                self.db_path,
                "docs",
                "missing_dir/file",
                "body",
                None,
            )

    def test_write_document_reindexes_only_target(self) -> None:
        """A write does not reindex an unrelated file changed on disk."""
        self._seed(
            {
                "docs/first.mdx": ("---\ntitle: First\n---\nFirst stable body."),
                "docs/unrelated.mdx": (
                    "---\ntitle: Unrelated\n---\nOriginal unrelated body."
                ),
            },
        )

        original_content = self._row("docs/unrelated.mdx")[0]
        original_mtime = self._file_metadata("docs/unrelated.mdx")
        assert original_mtime is not None

        # Rewrite the unrelated file outside the tool and force a new mtime,
        # which is exactly the state a whole-tree rebuild would reindex.
        create_file(
            self.root,
            "docs/unrelated.mdx",
            "---\ntitle: Unrelated\n---\nRewritten on disk body.",
        )
        rewritten_mtime = original_mtime + 10_000_000_000
        os.utime(
            Path(self.root, "docs", "unrelated.mdx"),
            ns=(rewritten_mtime, rewritten_mtime),
        )

        write_document(
            self.root,
            self.db_path,
            "docs",
            "third",
            "# Third\nWritten through the tool.",
            {"title": "Third"},
        )

        assert self._row("docs/unrelated.mdx")[0] == original_content
        assert self._file_metadata("docs/unrelated.mdx") == original_mtime

    def test_write_document_reindex_failure_cleans_metadata(self) -> None:
        """A failed reindex raises and removes the written path's metadata."""
        self._seed({"docs/fail.mdx": "---\ntitle: Fail\n---\nbody"})
        verify_metadata(self.root, "docs/fail.mdx", present=True)

        with (
            patch(
                "wywy_docs.documents.reindex_doc",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError) as ctx,
        ):
            write_document(
                self.root,
                self.db_path,
                "docs",
                "fail",
                "# Failed indexing",
                {"title": "Fail"},
            )

        assert str(ctx.value) == "file written but index update failed: boom"
        verify_metadata(self.root, "docs/fail.mdx", present=False)
        assert Path(self.root, "docs", "fail.mdx").is_file()


class TestEditDocument(BaseDocumentTest):
    """Direct tests for :func:`wywy_docs.documents.edit_document`."""

    def test_edit_document_single_replacement_preserves_frontmatter(self) -> None:
        """One replacement updates the body and refreshes ``last_updated``."""
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

        rel_path, count = edit_document(
            self.root,
            self.db_path,
            "docs",
            "single",
            "beta",
            "BETA",
            replace_all=False,
        )

        assert rel_path == "docs/single.mdx"
        assert count == 1

        parsed = self._parse("docs/single.mdx")
        frontmatter = parsed["frontmatter"]
        assert frontmatter["title"] == "Original"
        assert frontmatter["desc"] == "keep"
        assert frontmatter["published"] == "2023-06-15T12:00:00"
        assert frontmatter["last_updated"] != frontmatter["published"]
        assert parsed["content"] == "alpha BETA gamma."

    def test_edit_document_replace_all(self) -> None:
        """``replace_all`` replaces every occurrence and reports the count."""
        self._seed({"docs/all.mdx": "red red red."})

        rel_path, count = edit_document(
            self.root,
            self.db_path,
            "docs",
            "all",
            "red",
            "blue",
            replace_all=True,
        )

        assert rel_path == "docs/all.mdx"
        assert count == 3  # noqa: PLR2004
        assert self._parse("docs/all.mdx")["content"] == "blue blue blue."

    def test_edit_document_ambiguous_without_replace_all(self) -> None:
        """An ambiguous match is rejected and the file stays unchanged."""
        self._seed({"docs/multi.mdx": "red red."})

        with pytest.raises(
            ValueError,
            match=r"old_string matches 2 times in docs/multi.mdx",
        ):
            edit_document(
                self.root,
                self.db_path,
                "docs",
                "multi",
                "red",
                "blue",
                replace_all=False,
            )

        assert self._parse("docs/multi.mdx")["content"] == "red red."

    def test_edit_document_degenerate_edits(self) -> None:
        """Absent, empty, and no-op edits are rejected without a rewrite."""
        self._seed({"docs/degenerate.mdx": "zzz body."})

        cases = [
            (("absent", "x"), "old_string not found in docs/degenerate.mdx"),
            (("", "x"), "old_string must not be empty"),
            (("zzz", "zzz"), "old_string and new_string must differ"),
        ]
        for (old_string, new_string), message in cases:
            with pytest.raises(ValueError, match="old_string") as ctx:
                edit_document(
                    self.root,
                    self.db_path,
                    "docs",
                    "degenerate",
                    old_string,
                    new_string,
                    replace_all=False,
                )
            assert str(ctx.value) == message
            assert self._parse("docs/degenerate.mdx")["content"] == "zzz body."

    def test_edit_document_missing_file(self) -> None:
        """Editing a document with no file raises ``DocNotFoundError``."""
        with pytest.raises(
            DocNotFoundError,
            match=r"document not found: docs/missing.mdx",
        ):
            edit_document(
                self.root,
                self.db_path,
                "docs",
                "missing",
                "x",
                "y",
                replace_all=False,
            )

        assert not Path(self.root, "docs", "missing.mdx").exists()

    def test_edit_document_path_traversal(self) -> None:
        """A path containing ``..`` is rejected."""
        with pytest.raises(ValueError, match="path traversal"):
            edit_document(
                self.root,
                self.db_path,
                "docs",
                "../outside/file",
                "x",
                "y",
                replace_all=False,
            )

    def test_edit_document_symlink_escape(self) -> None:
        """A path escaping the section through a symlink is rejected."""
        link = "escape_edit"
        link_path = Path(self.root, "docs", link)
        link_path.symlink_to(self.root)
        try:
            with pytest.raises(
                ValueError,
                match="path escapes the allowed directory via symlink",
            ):
                edit_document(
                    self.root,
                    self.db_path,
                    "docs",
                    f"{link}/evil_file",
                    "x",
                    "y",
                    replace_all=False,
                )
            assert not Path(self.root, "evil_file.mdx").exists()
        finally:
            link_path.unlink()

    def test_edit_document_no_frontmatter_adds_dates(self) -> None:
        """Editing a bare document adds both managed dates."""
        self._seed({"docs/bare.mdx": "bare body."})

        edit_document(
            self.root,
            self.db_path,
            "docs",
            "bare",
            "bare",
            "BARE",
            replace_all=False,
        )

        parsed = self._parse("docs/bare.mdx")
        assert parsed["content"] == "BARE body."
        assert "published" in parsed["frontmatter"]
        assert "last_updated" in parsed["frontmatter"]

    def test_edit_document_malformed_frontmatter(self) -> None:
        """Malformed headers are rejected and leave the file and index intact."""
        files = {
            "docs/malformed.mdx": "---\ntitle: [unclosed\n---\nbody text.\n",
            "docs/scalar-header.mdx": "---\njust-a-string\n---\nbody text.\n",
            "docs/list-header.mdx": "---\n- a\n- b\n---\nbody text.\n",
            "docs/unterminated.mdx": "---\ntitle: x",
        }
        self._seed(files)

        for rel_path in files:
            stem = Path(rel_path).stem
            before = self._parse(rel_path)
            before_row = self._row(rel_path)

            with pytest.raises(
                ValueError,
                match="document has malformed frontmatter; use write_doc",
            ):
                edit_document(
                    self.root,
                    self.db_path,
                    "docs",
                    stem,
                    "body",
                    "BODY",
                    replace_all=False,
                )

            assert self._parse(rel_path) == before
            assert self._row(rel_path) == before_row

    def test_edit_document_empty_header_allowed(self) -> None:
        """An empty frontmatter header is treated as no frontmatter."""
        self._seed({"docs/empty-header.mdx": "---\n---\nbody text.\n"})

        edit_document(
            self.root,
            self.db_path,
            "docs",
            "empty-header",
            "body",
            "BODY",
            replace_all=False,
        )

        parsed = self._parse("docs/empty-header.mdx")
        assert parsed["content"] == "BODY text.\n"
        assert "published" in parsed["frontmatter"]
        assert "last_updated" in parsed["frontmatter"]

    def test_edit_document_reindex_failure_cleans_metadata(self) -> None:
        """A failed reindex raises and removes the edited path's metadata."""
        self._seed({"docs/fail-edit.mdx": "body text"})
        verify_metadata(self.root, "docs/fail-edit.mdx", present=True)

        with (
            patch(
                "wywy_docs.documents.reindex_doc",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError) as ctx,
        ):
            edit_document(
                self.root,
                self.db_path,
                "docs",
                "fail-edit",
                "body",
                "BODY",
                replace_all=False,
            )

        assert str(ctx.value) == "file edited but index update failed: boom"
        verify_metadata(self.root, "docs/fail-edit.mdx", present=False)
        assert self._parse("docs/fail-edit.mdx")["content"] == "BODY text"

    def test_edit_document_internal_section(self) -> None:
        """Edits work in ``internal/`` and update the index row."""
        self._seed({"internal/ops.mdx": "ops token"})

        rel_path, count = edit_document(
            self.root,
            self.db_path,
            "internal",
            "ops",
            "token",
            "TOKEN",
            replace_all=False,
        )

        assert rel_path == "internal/ops.mdx"
        assert count == 1
        assert self._parse("internal/ops.mdx")["content"] == "ops TOKEN"
        assert self._row("internal/ops.mdx")[0] == "ops TOKEN"

    def test_edit_document_is_body_only(self) -> None:
        """Frontmatter text is not a valid edit target."""
        self._seed(
            {
                "docs/body-only.mdx": ("---\ntitle: Original\n---\nbody text."),
            },
        )

        with pytest.raises(
            ValueError,
            match=r"old_string not found in docs/body-only.mdx",
        ):
            edit_document(
                self.root,
                self.db_path,
                "docs",
                "body-only",
                "Original",
                "Changed",
                replace_all=False,
            )

        parsed = self._parse("docs/body-only.mdx")
        assert parsed["frontmatter"]["title"] == "Original"
        assert parsed["content"] == "body text."


class TestDocumentHelpers(BaseDocumentTest):
    """Direct tests for the path, date, and write helpers."""

    def test_normalize_doc_path(self) -> None:
        """Normalization appends ``.mdx`` and strips surrounding slashes."""
        assert normalize_doc_path("foo/bar") == "foo/bar.mdx"
        assert normalize_doc_path("/foo/bar/") == "foo/bar.mdx"
        assert normalize_doc_path("foo/bar.mdx") == "foo/bar.mdx"
        with pytest.raises(ValueError, match="path traversal"):
            normalize_doc_path("../x")

    def test_resolve_section_path_returns_contained_path(self) -> None:
        """A contained path resolves under the section directory."""
        result = resolve_section_path(self.root, "docs", "nested/page.mdx")
        assert result == str(Path(self.root, "docs", "nested", "page.mdx"))

    def test_resolve_published(self) -> None:
        """The published value keeps strings and normalizes other types."""
        assert (
            resolve_published({"published": "2023-06-15T12:00:00"})
            == "2023-06-15T12:00:00"
        )

        day = date(2024, 1, 2)
        assert resolve_published({"published": day}) == day.isoformat()

        moment = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
        assert resolve_published({"published": moment}) == moment.isoformat()

        generated = resolve_published({})
        assert isinstance(generated, str)
        assert datetime.strptime(generated, "%Y-%m-%dT%H:%M:%S")  # noqa: DTZ007

        assert resolve_published({"published": 42}) == "42"

    def test_atomic_write_unlinks_temp_on_failure(self) -> None:
        """A rename failure removes the temporary file and raises."""
        target_dir = Path(self.root, "docs")
        target = str(target_dir / "atomic.mdx")

        with (
            patch("pathlib.Path.rename", side_effect=OSError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            atomic_write(target, "content")

        assert list(target_dir.iterdir()) == []
