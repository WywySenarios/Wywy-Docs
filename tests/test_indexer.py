"""Tests for ``wywy_docs/indexer.py`` — FTS5 documentation indexer.

All tests create temporary directory trees and never depend on the real
symlinked ``docs/`` or ``internal/`` directories.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from wywy_docs.indexer import build_index, main, parse_file, scan_files


def _create_file(
    root: str, rel_path: str, content: str = "---\ntitle: X\n---\nbody",
) -> str:
    """Create a file at *root*/*rel_path* with *content*, creating parent
    directories as needed.  Returns the absolute path to the created file.
    """
    full_path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w") as f:
        f.write(content)
    return full_path


# ===========================================================================
# scan_files
# ===========================================================================


class TestScanFiles(unittest.TestCase):
    """``scan_files()`` discovers all ``.mdx`` files under given directories."""

    def test_scan_files_discovers_mdx_files(self) -> None:
        """Returns every ``.mdx`` file under both directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            expected = [
                _create_file(tmpdir, "docs/a.mdx"),
                _create_file(tmpdir, "docs/subdir/b.mdx"),
                _create_file(tmpdir, "internal/c.mdx"),
            ]

            # Non-.mdx file — should be ignored
            _create_file(tmpdir, "docs/notes.txt", "not mdx")

            result = scan_files(
                [
                    os.path.join(tmpdir, "docs"),
                    os.path.join(tmpdir, "internal"),
                ],
            )
            assert len(result) == 3
            for fp in expected:
                assert fp in result

    def test_scan_files_empty_directories(self) -> None:
        """Returns an empty list when no ``.mdx`` files exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            docs = os.path.join(tmpdir, "docs")
            internal = os.path.join(tmpdir, "internal")
            os.makedirs(docs)
            os.makedirs(internal)

            result = scan_files([docs, internal])
            assert result == []


# ===========================================================================
# parse_file
# ===========================================================================


class TestParseFile(unittest.TestCase):
    """``parse_file()`` parses YAML frontmatter and body content."""

    def test_parse_file_returns_expected_keys(self) -> None:
        """Returns a dict with ``title``, ``path``, ``content``, ``frontmatter``,
        and ``section``.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(tmpdir, "test.mdx", "---\ntitle: Hello\n---\nBody")

            result = parse_file(fp, root=tmpdir)
            assert "title" in result
            assert "path" in result
            assert "content" in result
            assert "frontmatter" in result
            assert "section" in result

    def test_parse_file_extracts_frontmatter(self) -> None:
        """Frontmatter YAML is correctly parsed and body is separated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir, "doc.mdx", "---\ntitle: Greeting\ncount: 3\n---\nHello world",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["title"] == "Greeting"
            assert result["frontmatter"]["count"] == 3
            assert result["content"] == "Hello world"

    def test_parse_file_frontmatter_in_body(self) -> None:
        """``---`` inside body (horizontal rules) does not affect parsing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir,
                "doc.mdx",
                "---\ntitle: Doc\n---\n"
                "Paragraph one\n\n"
                "---\n\n"
                "Paragraph two\n\n"
                "---\n\n"
                "Paragraph three",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["title"] == "Doc"
            # Subsequent --- should remain part of the body
            assert "---" in result["content"]
            assert "Paragraph two" in result["content"]

    def test_parse_file_no_frontmatter(self) -> None:
        """Files without frontmatter use filename stem as title."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir, "my-document.mdx", "Just body content, no frontmatter markers.",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["title"] == "my-document"
            assert result["frontmatter"] == {}

    def test_parse_file_empty_frontmatter(self) -> None:
        """Empty frontmatter (``---`` ``---``) yields ``{}``, not ``None``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir, "empty.mdx", "---\n---\nBody after empty frontmatter",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["frontmatter"] == {}

    def test_parse_file_path_relative_to_root(self) -> None:
        """The ``path`` key is the file path relative to *root*."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(tmpdir, "docs/guide.mdx", "---\ntitle: Guide\n---\nBody")

            result = parse_file(fp, root=tmpdir)
            assert result["path"] == "docs/guide.mdx"


# ===========================================================================
# build_index
# ===========================================================================


class TestBuildIndex(unittest.TestCase):
    """``build_index()`` creates the FTS5 database and indexes content."""

    def test_build_index_creates_fts5_tables(self) -> None:
        """Creates a SQLite DB with ``docs_fts`` and ``file_metadata`` tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(tmpdir, "docs/test.mdx", "---\ntitle: Test\n---\nBody content")

            db_path = os.path.join(tmpdir, "test.db")
            build_index(root_dirs=[os.path.join(tmpdir, "docs")], db_path=db_path)

            assert os.path.isfile(db_path)
            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            conn.close()
            assert "docs_fts" in tables
            assert "file_metadata" in tables

    def test_build_index_content_searchable(self) -> None:
        """Indexed content is searchable via FTS5 MATCH queries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(
                tmpdir,
                "docs/hello.mdx",
                "---\ntitle: Hello World\n---\nThis is the body content",
            )

            db_path = os.path.join(tmpdir, "test.db")
            build_index(root_dirs=[os.path.join(tmpdir, "docs")], db_path=db_path)

            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT title FROM docs_fts WHERE docs_fts MATCH 'body'")
            rows = cur.fetchall()
            conn.close()
            assert len(rows) == 1
            assert rows[0][0] == "Hello World"


# ===========================================================================
# Incremental indexing
# ===========================================================================


class TestIncrementalIndex(unittest.TestCase):
    """Re-building the index only processes changed files."""

    def test_rebuild_no_changes_no_new_rows(self) -> None:
        """Re-running ``build_index()`` without file changes produces no
        additional rows in ``docs_fts``.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(tmpdir, "docs/test.mdx", "---\ntitle: Test\n---\nBody content")

            db_path = os.path.join(tmpdir, "test.db")
            build_index(root_dirs=[os.path.join(tmpdir, "docs")], db_path=db_path)

            conn = sqlite3.connect(db_path)
            before = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            conn.close()

            # Rebuild without touching any files
            build_index(root_dirs=[os.path.join(tmpdir, "docs")], db_path=db_path)

            conn = sqlite3.connect(db_path)
            after = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            conn.close()
            assert before == after

    def test_touch_file_reindexes_only_that_file(self) -> None:
        """Touching a single ``.mdx`` file only re-indexes that file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files = {
                "a.mdx": "---\ntitle: A\n---\nContent A",
                "b.mdx": "---\ntitle: B\n---\nContent B",
                "c.mdx": "---\ntitle: C\n---\nContent C",
            }
            file_paths: dict[str, str] = {}
            for name, content in files.items():
                file_paths[name] = _create_file(tmpdir, f"docs/{name}", content)

            db_path = os.path.join(tmpdir, "test.db")
            build_index(root_dirs=[os.path.join(tmpdir, "docs")], db_path=db_path)

            # Touch only file "b.mdx"
            os.utime(file_paths["b.mdx"], None)
            b_mtime = os.stat(file_paths["b.mdx"]).st_mtime_ns

            # Rebuild
            build_index(root_dirs=[os.path.join(tmpdir, "docs")], db_path=db_path)

            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT path, mtime FROM file_metadata ORDER BY path",
            ).fetchall()
            conn.close()

            # All three should still be present
            assert len(rows) == 3

            # The entry for "b.mdx" should have the new mtime
            b_entries = [r for r in rows if r[0].endswith("b.mdx")]
            assert len(b_entries) == 1
            assert b_entries[0][1] == b_mtime


# ===========================================================================
# Section assignment
# ===========================================================================


class TestSectionAssignment(unittest.TestCase):
    """Section is inferred from the path prefix (``docs/`` vs ``internal/``)."""

    def test_section_docs(self) -> None:
        """Files under ``docs/`` have section ``'docs'``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(tmpdir, "docs/guide.mdx", "---\ntitle: Guide\n---\nBody")

            result = parse_file(fp, root=tmpdir)
            assert result["section"] == "docs"

    def test_section_internal(self) -> None:
        """Files under ``internal/`` have section ``'internal'``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir, "internal/guide.mdx", "---\ntitle: Guide\n---\nBody",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["section"] == "internal"

    def test_section_nested_path(self) -> None:
        """Section is determined by the first path component regardless of
        nesting depth.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir, "docs/sub/dir/nested.mdx", "---\ntitle: Nested\n---\nBody",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["section"] == "docs"


# ===========================================================================
# CLI entry point
# ===========================================================================


class TestCliEntryPoint(unittest.TestCase):
    """The ``main()`` function and ``__main__`` block."""

    def test_main_creates_expected_db(self) -> None:
        """``main()`` creates ``wywy_docs/docs_index.db`` with the correct tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create the conventional directory layout
            os.makedirs(os.path.join(tmpdir, "wywy_docs"))
            os.makedirs(os.path.join(tmpdir, "internal"), exist_ok=True)
            _create_file(tmpdir, "docs/test.mdx", "---\ntitle: Test\n---\nBody content")

            # Invoke the CLI entry point with an explicit root
            main(root_dir=tmpdir)

            db_path = os.path.join(tmpdir, "wywy_docs", "docs_index.db")
            assert os.path.isfile(db_path)

            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            conn.close()
            assert "docs_fts" in tables
            assert "file_metadata" in tables

    def test_main_respects_root_argument(self) -> None:
        """``main(root_dir=...)`` build the index relative to the given root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "wywy_docs"))
            _create_file(tmpdir, "docs/a.mdx", "---\ntitle: A\n---\nContent A")

            main(root_dir=tmpdir)

            db_path = os.path.join(tmpdir, "wywy_docs", "docs_index.db")
            assert os.path.isfile(db_path)

            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            conn.close()
            assert count == 1


if __name__ == "__main__":
    unittest.main()
