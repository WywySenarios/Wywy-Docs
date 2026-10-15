"""Tests for ``wywy_docs/indexer.py`` — FTS5 documentation indexer.

All tests create temporary directory trees and never depend on the real
symlinked ``docs/`` or ``internal/`` directories.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wywy_docs.indexer import build_index, main, parse_file, scan_files


def _create_file(
    root: str,
    rel_path: str,
    content: str = "---\ntitle: X\n---\nbody",
) -> str:
    """Create a file at *root*/*rel_path* with *content*.

    Creates parent directories as needed.  Returns the absolute path to
    the created file.
    """
    full_path = Path(root) / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    with full_path.open("w") as f:
        f.write(content)
    return str(full_path)


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
                    str(Path(tmpdir) / "docs"),
                    str(Path(tmpdir) / "internal"),
                ],
            )
            assert len(result) == len(expected)
            for fp in expected:
                assert fp in result

    def test_scan_files_empty_directories(self) -> None:
        """Returns an empty list when no ``.mdx`` files exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            docs = Path(tmpdir) / "docs"
            internal = Path(tmpdir) / "internal"
            docs.mkdir(parents=True)
            internal.mkdir(parents=True)

            result = scan_files([str(docs), str(internal)])
            assert result == []


# ===========================================================================
# parse_file
# ===========================================================================


class TestParseFile(unittest.TestCase):
    """``parse_file()`` parses YAML frontmatter and body content."""

    def test_parse_file_returns_expected_keys(self) -> None:
        """Returns a dict with all expected keys.

        The dict includes ``title``, ``path``, ``content``,
        ``frontmatter``, and ``section``.
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
                tmpdir,
                "doc.mdx",
                "---\ntitle: Greeting\ncount: 3\n---\nHello world",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["title"] == "Greeting"
            assert result["frontmatter"]["count"] == 3  # noqa: PLR2004 - fixture value above
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
                tmpdir,
                "my-document.mdx",
                "Just body content, no frontmatter markers.",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["title"] == "my-document"
            assert result["frontmatter"] == {}

    def test_parse_file_empty_frontmatter(self) -> None:
        """Empty frontmatter (``---`` ``---``) yields ``{}``, not ``None``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir,
                "empty.mdx",
                "---\n---\nBody after empty frontmatter",
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

            db_path = Path(tmpdir) / "test.db"
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

            assert db_path.is_file()
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

            db_path = Path(tmpdir) / "test.db"
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

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
        """Re-running ``build_index()`` without file changes adds no rows.

        The ``docs_fts`` row count stays identical.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(tmpdir, "docs/test.mdx", "---\ntitle: Test\n---\nBody content")

            db_path = Path(tmpdir) / "test.db"
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

            conn = sqlite3.connect(db_path)
            before = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            conn.close()

            # Rebuild without touching any files
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

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

            db_path = Path(tmpdir) / "test.db"
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

            # Touch only file "b.mdx"
            os.utime(file_paths["b.mdx"], None)
            b_mtime = Path(file_paths["b.mdx"]).stat().st_mtime_ns

            # Rebuild
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT path, mtime FROM file_metadata ORDER BY path",
            ).fetchall()
            conn.close()

            # All three should still be present
            assert len(rows) == len(files)

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
                tmpdir,
                "internal/guide.mdx",
                "---\ntitle: Guide\n---\nBody",
            )

            result = parse_file(fp, root=tmpdir)
            assert result["section"] == "internal"

    def test_section_nested_path(self) -> None:
        """Section is determined by the first path component.

        This holds regardless of nesting depth.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir,
                "docs/sub/dir/nested.mdx",
                "---\ntitle: Nested\n---\nBody",
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
            (Path(tmpdir) / "wywy_docs").mkdir(parents=True)
            (Path(tmpdir) / "internal").mkdir(parents=True, exist_ok=True)
            _create_file(tmpdir, "docs/test.mdx", "---\ntitle: Test\n---\nBody content")

            # Invoke the CLI entry point with an explicit root
            main(root_dir=tmpdir)

            db_path = Path(tmpdir) / "wywy_docs" / "docs_index.db"
            assert db_path.is_file()

            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            conn.close()
            assert "docs_fts" in tables
            assert "file_metadata" in tables

    def test_main_respects_root_argument(self) -> None:
        """``main(root_dir=...)`` build the index relative to the given root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "wywy_docs").mkdir(parents=True)
            _create_file(tmpdir, "docs/a.mdx", "---\ntitle: A\n---\nContent A")

            main(root_dir=tmpdir)

            db_path = Path(tmpdir) / "wywy_docs" / "docs_index.db"
            assert db_path.is_file()

            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            conn.close()
            assert count == 1


# ===========================================================================
# Per-document index primitives (reindex_doc / remove_doc / doc_mtime)
# ===========================================================================


class TestReindexDoc(unittest.TestCase):
    """``reindex_doc()`` replaces exactly one path's index rows."""

    def test_reindex_doc_updates_only_target_path(self) -> None:
        """Re-indexing one path rewrites it and leaves the other path alone.

        Assertions:
        - the target path's ``docs_fts`` content equals the rewritten body
        - the target path's ``file_metadata.mtime`` equals ``st_mtime_ns``
        - the unrelated path's content and mtime are byte-for-byte unchanged
        - exactly two ``docs_fts`` rows remain.
        """
        from wywy_docs.indexer import reindex_doc

        with tempfile.TemporaryDirectory() as tmpdir:
            a_path = _create_file(
                tmpdir,
                "docs/a.mdx",
                "---\ntitle: A\n---\nOriginal A body",
            )
            _create_file(
                tmpdir,
                "docs/b.mdx",
                "---\ntitle: B\n---\nOriginal B body",
            )
            db_path = Path(tmpdir) / "test.db"
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

            conn = sqlite3.connect(db_path)
            a_mtime_before = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                ("docs/a.mdx",),
            ).fetchone()[0]
            b_content_before = conn.execute(
                "SELECT content FROM docs_fts WHERE path = ?",
                ("docs/b.mdx",),
            ).fetchone()[0]
            b_mtime_before = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                ("docs/b.mdx",),
            ).fetchone()[0]
            conn.close()

            # Rewrite a.mdx and force a distinct mtime so the change is real.
            with Path(a_path).open("w") as f:
                f.write("---\ntitle: A\n---\nRewritten A body")
            forced_mtime = a_mtime_before + 5_000_000_000
            os.utime(a_path, ns=(forced_mtime, forced_mtime))
            expected_a_mtime = Path(a_path).stat().st_mtime_ns
            assert expected_a_mtime != a_mtime_before

            reindex_doc(tmpdir, str(db_path), a_path, "docs/a.mdx")

            conn = sqlite3.connect(db_path)
            a_content = conn.execute(
                "SELECT content FROM docs_fts WHERE path = ?",
                ("docs/a.mdx",),
            ).fetchone()[0]
            a_mtime = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                ("docs/a.mdx",),
            ).fetchone()[0]
            b_content_after = conn.execute(
                "SELECT content FROM docs_fts WHERE path = ?",
                ("docs/b.mdx",),
            ).fetchone()[0]
            b_mtime_after = conn.execute(
                "SELECT mtime FROM file_metadata WHERE path = ?",
                ("docs/b.mdx",),
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
            conn.close()

            assert a_content == "Rewritten A body"
            assert a_mtime == expected_a_mtime
            assert b_content_after == b_content_before
            assert b_mtime_after == b_mtime_before
            assert total == 2  # noqa: PLR2004 - fixture count

    def test_reindex_doc_inserts_new_path(self) -> None:
        """Re-indexing a path with no prior entry inserts a fresh row.

        The new body becomes findable via ``docs_fts MATCH`` and
        ``file_metadata`` gains the path with its ``st_mtime_ns``.
        """
        from wywy_docs.indexer import reindex_doc

        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(
                tmpdir,
                "docs/existing.mdx",
                "---\ntitle: Existing\n---\nexisting body",
            )
            db_path = Path(tmpdir) / "test.db"
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

            new_path = _create_file(
                tmpdir,
                "docs/new.mdx",
                "---\ntitle: New\n---\nbrandnewtoken body",
            )
            expected_mtime = Path(new_path).stat().st_mtime_ns

            reindex_doc(tmpdir, str(db_path), new_path, "docs/new.mdx")

            conn = sqlite3.connect(db_path)
            matches = conn.execute(
                "SELECT path FROM docs_fts WHERE docs_fts MATCH 'brandnewtoken'",
            ).fetchall()
            meta = conn.execute(
                "SELECT path, mtime FROM file_metadata WHERE path = ?",
                ("docs/new.mdx",),
            ).fetchone()
            conn.close()

            assert [row[0] for row in matches] == ["docs/new.mdx"]
            assert meta == ("docs/new.mdx", expected_mtime)


class TestRemoveDoc(unittest.TestCase):
    """``remove_doc()`` deletes a path's rows idempotently."""

    def test_remove_doc_deletes_both_rows_and_is_idempotent(self) -> None:
        """Removing an indexed path clears both tables; second call is a no-op."""
        from wywy_docs.indexer import remove_doc

        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(
                tmpdir,
                "docs/gone.mdx",
                "---\ntitle: Gone\n---\ngone body",
            )
            db_path = Path(tmpdir) / "test.db"
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

            remove_doc(str(db_path), "docs/gone.mdx")

            conn = sqlite3.connect(db_path)
            fts_count = conn.execute(
                "SELECT COUNT(*) FROM docs_fts WHERE path = ?",
                ("docs/gone.mdx",),
            ).fetchone()[0]
            meta_count = conn.execute(
                "SELECT COUNT(*) FROM file_metadata WHERE path = ?",
                ("docs/gone.mdx",),
            ).fetchone()[0]
            conn.close()

            assert fts_count == 0
            assert meta_count == 0

            # Idempotent: removing again must not raise.
            remove_doc(str(db_path), "docs/gone.mdx")


class TestDocMtime(unittest.TestCase):
    """``doc_mtime()`` reports the recorded mtime for a path."""

    def test_doc_mtime_returns_recorded_or_none(self) -> None:
        """Known path yields its ``st_mtime_ns``; unknown path yields ``None``."""
        from wywy_docs.indexer import doc_mtime

        with tempfile.TemporaryDirectory() as tmpdir:
            fp = _create_file(
                tmpdir,
                "docs/a.mdx",
                "---\ntitle: A\n---\nbody",
            )
            db_path = Path(tmpdir) / "test.db"
            build_index(
                root_dirs=[str(Path(tmpdir) / "docs")],
                db_path=str(db_path),
            )

            expected = Path(fp).stat().st_mtime_ns
            assert doc_mtime(str(db_path), "docs/a.mdx") == expected
            assert doc_mtime(str(db_path), "docs/unknown.mdx") is None


class TestBuildIndexPrune(unittest.TestCase):
    """``build_index()`` prunes rows whose paths vanished from the scan."""

    def test_build_index_prunes_deleted_paths(self) -> None:
        """A file deleted from disk is removed from both tables on rebuild."""
        with tempfile.TemporaryDirectory() as tmpdir:
            a_path = _create_file(
                tmpdir,
                "docs/a.mdx",
                "---\ntitle: A\n---\ncontent A",
            )
            _create_file(
                tmpdir,
                "docs/b.mdx",
                "---\ntitle: B\n---\ncontent B",
            )
            root_dirs = [str(Path(tmpdir) / "docs")]
            db_path = Path(tmpdir) / "test.db"
            build_index(root_dirs=root_dirs, db_path=str(db_path))

            Path(a_path).unlink()
            build_index(root_dirs=root_dirs, db_path=str(db_path))

            conn = sqlite3.connect(db_path)
            fts_paths = {
                row[0] for row in conn.execute("SELECT path FROM docs_fts").fetchall()
            }
            meta_paths = {
                row[0]
                for row in conn.execute(
                    "SELECT path FROM file_metadata",
                ).fetchall()
            }
            conn.close()

            assert "docs/a.mdx" not in fts_paths
            assert "docs/a.mdx" not in meta_paths
            assert "docs/b.mdx" in fts_paths
            assert "docs/b.mdx" in meta_paths

    def test_build_index_skips_prune_when_root_missing(self) -> None:
        """A missing scanned root dir must not erase that section's rows.

        With ``internal/`` removed, a rebuild over ``docs/`` and ``internal/``
        leaves the previously indexed ``internal/`` rows intact.
        """
        import shutil

        with tempfile.TemporaryDirectory() as tmpdir:
            docs_dir = Path(tmpdir) / "docs"
            internal_dir = Path(tmpdir) / "internal"
            _create_file(
                tmpdir,
                "docs/only.mdx",
                "---\ntitle: Only\n---\nonly body",
            )
            _create_file(
                tmpdir,
                "internal/gone.mdx",
                "---\ntitle: Gone\n---\ngone body",
            )
            root_dirs = [str(docs_dir), str(internal_dir)]
            db_path = Path(tmpdir) / "test.db"
            build_index(root_dirs=root_dirs, db_path=str(db_path))

            shutil.rmtree(internal_dir)
            build_index(root_dirs=root_dirs, db_path=str(db_path))

            conn = sqlite3.connect(db_path)
            fts_paths = {
                row[0] for row in conn.execute("SELECT path FROM docs_fts").fetchall()
            }
            meta_paths = {
                row[0]
                for row in conn.execute(
                    "SELECT path FROM file_metadata",
                ).fetchall()
            }
            conn.close()

            assert "internal/gone.mdx" in fts_paths
            assert "internal/gone.mdx" in meta_paths
            assert "docs/only.mdx" in fts_paths
            assert "docs/only.mdx" in meta_paths


if __name__ == "__main__":
    unittest.main()
