"""FTS5 documentation indexer for Wywy-Docs.

Scans .mdx files, parses YAML frontmatter, and builds a SQLite FTS5 index.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator


class ParsedFile(TypedDict):
    """Structured result of :func:`parse_file`."""

    title: str
    path: str
    content: str
    frontmatter: dict[str, object]
    section: str | None


# A frontmatter block splits into at most three parts: "", YAML, body.
_FRONTMATTER_PARTS = 3

# 30s busy timeout: overlapping starts (restart-looping unit + manual smoke
# run) must not fail with `database is locked`.
_BUSY_TIMEOUT_SECONDS = 30

_CREATE_DOCS_FTS = """CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title,
    path UNINDEXED,
    content,
    section UNINDEXED,
    frontmatter_json UNINDEXED,
    tokenize='porter unicode61'
)"""

_CREATE_FILE_METADATA = """CREATE TABLE IF NOT EXISTS file_metadata (
    path TEXT PRIMARY KEY,
    mtime INTEGER
)"""


def _connect(db_path: str) -> sqlite3.Connection:
    """Open *db_path* and ensure both index tables exist."""
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_DOCS_FTS)
    conn.execute(_CREATE_FILE_METADATA)
    return conn


@contextmanager
def _connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """Yield a schema-initialized connection to *db_path*, closed on exit."""
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _delete_doc(conn: sqlite3.Connection, rel_path: str) -> None:
    """Delete *rel_path*'s rows from both index tables."""
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM file_metadata WHERE path = ?", (rel_path,))


def _upsert_doc(
    conn: sqlite3.Connection,
    parsed: ParsedFile,
    rel_path: str,
    mtime: int,
) -> None:
    """Replace *rel_path*'s index rows with *parsed* and *mtime*."""
    _delete_doc(conn, rel_path)
    conn.execute(
        "INSERT INTO docs_fts(title, path, content, section, frontmatter_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            parsed["title"],
            rel_path,
            parsed["content"],
            parsed["section"],
            str(parsed["frontmatter"]),
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO file_metadata(path, mtime) VALUES (?, ?)",
        (rel_path, mtime),
    )


def _prune(conn: sqlite3.Connection, scanned: set[str]) -> None:
    """Delete index rows whose ``path`` is not in *scanned*.

    Both tables are written together by every indexer, so a path is stale
    only when it is absent from the union of the two tables.
    """
    indexed_fts = {row[0] for row in conn.execute("SELECT path FROM docs_fts")}
    indexed_meta = {row[0] for row in conn.execute("SELECT path FROM file_metadata")}
    for rel_path in (indexed_fts | indexed_meta) - scanned:
        _delete_doc(conn, rel_path)


def _mtime_ns(path: str) -> int:
    """Return *path*'s modification time in nanoseconds."""
    return Path(path).stat().st_mtime_ns


def scan_files(root_dirs: list[str]) -> list[str]:
    """Scan *root_dirs* recursively for ``.mdx`` files.

    Returns a sorted list of absolute file paths.
    """
    files: list[str] = []
    for root_dir in root_dirs:
        for root, _dirs, filenames in os.walk(root_dir, followlinks=True):
            files.extend(
                str(Path(root) / fn) for fn in filenames if fn.endswith(".mdx")
            )
    return sorted(files)


def parse_file(filepath: str, root: str) -> ParsedFile:
    """Parse a single ``.mdx`` file.

    Args:
        filepath: Absolute path to the ``.mdx`` file.
        root: Absolute path to the Wywy-Docs root directory (used to compute
            the relative ``path`` output key).

    Returns:
        A dict with keys:

        - ``title``: from YAML frontmatter or the filename stem fallback.
        - ``path``: *filepath* relative to *root*.
        - ``content``: body text after the frontmatter block.
        - ``frontmatter``: parsed YAML dict (always a dict, never ``None``).
        - ``section``: ``"docs"`` if the path starts with ``docs/``,
          ``"internal"`` if it starts with ``internal/``, ``None`` otherwise.

    """
    with Path(filepath).open() as f:
        raw = f.read()

    frontmatter: dict[str, object] = {}
    title: str | None = None
    content = raw

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= _FRONTMATTER_PARTS:
            fm_text = parts[1]
            content = parts[2].lstrip("\n")
            if fm_text.strip():
                try:
                    parsed = yaml.safe_load(fm_text)
                    if isinstance(parsed, dict):
                        frontmatter = cast("dict[str, object]", parsed)
                        title = cast("str | None", frontmatter.get("title"))
                except yaml.YAMLError:
                    frontmatter = {}
                    content = raw

    if title is None:
        title = Path(filepath).stem

    rel_path = os.path.relpath(filepath, root)

    section: str | None = None
    if rel_path.startswith("docs/"):
        section = "docs"
    elif rel_path.startswith("internal/"):
        section = "internal"

    return {
        "title": title,
        "path": rel_path,
        "content": content,
        "frontmatter": frontmatter,
        "section": section,
    }


def build_index(root_dirs: list[str], db_path: str) -> None:
    """Build (or update) the FTS5 index at *db_path*.

    Scans ``.mdx`` files under *root_dirs*, parses frontmatter, and populates
    the ``docs_fts`` virtual table and ``file_metadata`` table.  Uses mtime
    comparisons to perform incremental indexing.

    Args:
        root_dirs: Directories to scan for ``.mdx`` files.
        db_path: Path to the SQLite index database.

    """
    files = scan_files(root_dirs)

    # Determine the common root for relative path computation
    if len(root_dirs) == 1:
        root = str(Path(root_dirs[0]).parent)
    else:
        root = os.path.commonpath(root_dirs)

    with _connection(db_path) as conn:
        # Load known mtimes for incremental indexing
        known_mtimes: dict[str, int] = dict(
            conn.execute("SELECT path, mtime FROM file_metadata").fetchall(),
        )

        for fp in files:
            rel_path = os.path.relpath(fp, root)
            mtime = _mtime_ns(fp)

            # Skip unchanged files
            if rel_path in known_mtimes and known_mtimes[rel_path] == mtime:
                continue

            _upsert_doc(conn, parse_file(fp, root=root), rel_path, mtime)

        # Never prune against a partial scan: a missing `docs/` or `internal/`
        # would otherwise erase the section's rows.
        if all(Path(root_dir).exists() for root_dir in root_dirs):
            _prune(conn, {os.path.relpath(fp, root) for fp in files})

        conn.commit()


def reindex_doc(root: str, db_path: str, abs_path: str, rel_path: str) -> None:
    """Re-index a single ``.mdx`` file into *db_path*.

    Parses *abs_path* relative to *root*, replaces any existing ``docs_fts``
    row for *rel_path*, and records the file's current mtime.

    Args:
        root: Absolute path to the Wywy-Docs root (used to parse *abs_path*).
        db_path: Path to the SQLite index database.
        abs_path: Absolute path to the ``.mdx`` file to index.
        rel_path: Index key for the file, relative to *root*.

    """
    parsed = parse_file(abs_path, root=root)
    mtime = _mtime_ns(abs_path)
    with _connection(db_path) as conn:
        _upsert_doc(conn, parsed, rel_path, mtime)
        conn.commit()


def remove_doc(db_path: str, rel_path: str) -> None:
    """Delete *rel_path*'s rows from the index database.

    Removes the path from both ``docs_fts`` and ``file_metadata``.  Safe to
    call for a path that was never indexed.
    """
    with _connection(db_path) as conn:
        _delete_doc(conn, rel_path)
        conn.commit()


def doc_mtime(db_path: str, rel_path: str) -> int | None:
    """Return the recorded mtime for *rel_path*, or ``None`` if absent."""
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT mtime FROM file_metadata WHERE path = ?",
            (rel_path,),
        ).fetchone()

    if row is None:
        return None
    return cast("int", row[0])


def main(root_dir: str | None = None) -> None:
    """Convenience entry point that calls :func:`build_index`.

    Uses the conventional Wywy-Docs directory layout.

    Args:
        root_dir: Wywy-Docs root.  If ``None``, defaults to the current
            working directory.

    """
    if root_dir is None:
        root_dir = str(Path.cwd())
    docs_dir = str(Path(root_dir) / "docs")
    internal_dir = str(Path(root_dir) / "internal")
    db_path = str(Path(root_dir) / "wywy_docs" / "docs_index.db")
    build_index(root_dirs=[docs_dir, internal_dir], db_path=db_path)


if __name__ == "__main__":
    main()
