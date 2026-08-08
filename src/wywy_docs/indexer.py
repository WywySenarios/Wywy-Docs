"""FTS5 documentation indexer for Wywy-Docs.

Scans .mdx files, parses YAML frontmatter, and builds a SQLite FTS5 index.
"""

from __future__ import annotations

import os
import sqlite3
from typing import TypedDict, cast

import yaml


class ParsedFile(TypedDict):
    """Structured result of :func:`parse_file`."""

    title: str
    path: str
    content: str
    frontmatter: dict[str, object]
    section: str | None


def scan_files(root_dirs: list[str]) -> list[str]:
    """Scan *root_dirs* recursively for ``.mdx`` files.

    Returns a sorted list of absolute file paths.
    """
    files: list[str] = []
    for root_dir in root_dirs:
        for root, _dirs, filenames in os.walk(root_dir, followlinks=True):
            for fn in filenames:
                if fn.endswith(".mdx"):
                    files.append(os.path.join(root, fn))
    return sorted(files)


def parse_file(filepath: str, root: str) -> ParsedFile:
    """Parse a single ``.mdx`` file.

    Parameters
    ----------
    filepath:
        Absolute path to the ``.mdx`` file.
    root:
        Absolute path to the Wywy-Docs root directory (used to compute the
        relative *path* output key).

    Returns
    -------
    dict
        A dictionary with keys:

        - **title**       – from YAML frontmatter or filename stem fallback.
        - **path**        – *filepath* relative to *root*.
        - **content**     – body text after the frontmatter block.
        - **frontmatter** – parsed YAML dict (always a dict, never ``None``).
        - **section**     – ``"docs"`` if path starts with ``docs/``,
                            ``"internal"`` if path starts with ``internal/``,
                            ``None`` otherwise.
    """
    with open(filepath, "r") as f:
        raw = f.read()

    frontmatter: dict[str, object] = {}
    title: str | None = None
    content = raw

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            content = parts[2].lstrip("\n")
            if fm_text.strip():
                try:
                    parsed = yaml.safe_load(fm_text)
                    if isinstance(parsed, dict):
                        frontmatter = cast(dict[str, object], parsed)
                        title = cast(str | None, frontmatter.get("title"))
                except yaml.YAMLError:
                    frontmatter = {}
                    content = raw

    if title is None:
        title = os.path.splitext(os.path.basename(filepath))[0]

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
    """
    files = scan_files(root_dirs)

    # Determine the common root for relative path computation
    if len(root_dirs) == 1:
        root = os.path.dirname(root_dirs[0])
    else:
        root = os.path.commonpath(root_dirs)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            title,
            path UNINDEXED,
            content,
            section UNINDEXED,
            frontmatter_json UNINDEXED,
            tokenize='porter unicode61'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS file_metadata (
            path TEXT PRIMARY KEY,
            mtime INTEGER
        )"""
    )

    # Load known mtimes for incremental indexing
    known: dict[str, int] = {}
    try:
        cur = conn.execute("SELECT path, mtime FROM file_metadata")
        known = dict(cur.fetchall())
    except sqlite3.OperationalError:
        pass

    for fp in files:
        rel_path = os.path.relpath(fp, root)
        mtime = os.stat(fp).st_mtime_ns

        # Skip unchanged files
        if rel_path in known and known[rel_path] == mtime:
            continue

        # Re-index: remove old entry and insert fresh
        conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))

        parsed = parse_file(fp, root=root)
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

    conn.commit()
    conn.close()


def main(root_dir: str | None = None) -> None:
    """Convenience entry point that calls :func:`build_index` with the
    conventional Wywy-Docs directory layout.

    Parameters
    ----------
    root_dir:
        Wywy-Docs root.  If ``None``, auto-detected from this module's
        location.
    """
    if root_dir is None:
        root_dir = os.getcwd()
    docs_dir = os.path.join(root_dir, "docs")
    internal_dir = os.path.join(root_dir, "internal")
    db_path = os.path.join(root_dir, "wywy_docs", "docs_index.db")
    build_index(root_dirs=[docs_dir, internal_dir], db_path=db_path)


if __name__ == "__main__":
    main()
