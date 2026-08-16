"""MCP HTTP SSE server for Wywy-Docs.

Exposes the FTS5 documentation index as MCP tools (``search_docs``,
``get_doc``) via the standard MCP SSE transport.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolRequest,
    CallToolResult,
    ErrorData,
    ServerResult,
    TextContent,
)

from wywy_docs.indexer import build_index, parse_file
from wywy_docs.models import DocFrontmatter, Section

logger = logging.getLogger(__name__)

# ── Globals ───────────────────────────────────────────────────────────

_ROOT_DIR: str = ""

mcp = FastMCP("wywy-docs", message_path="/messages/")


# ── Index helpers ─────────────────────────────────────────────────────


def _db_path(root_dir: str | None = None) -> str:
    return str(Path(root_dir or _ROOT_DIR) / "wywy_docs" / "docs_index.db")


def _section_dirs(root_dir: str) -> tuple[str, str]:
    """Return the conventional ``docs/`` and ``internal/`` paths under *root_dir*."""
    return (
        str(Path(root_dir) / "docs"),
        str(Path(root_dir) / "internal"),
    )


def _ensure_index(root_dir: str) -> None:
    """Build (or incrementally refresh) the FTS5 index.

    Runs on every start; ``build_index`` skips files whose mtime is
    unchanged, so this is cheap after the first run.  Warns (without
    crashing) if ``docs/`` or ``internal/`` is missing; the index is
    still created, empty.
    """
    db = _db_path(root_dir)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    docs_dir, internal_dir = _section_dirs(root_dir)
    if not Path(docs_dir).is_dir():
        logger.warning("docs directory does not exist: %s", docs_dir)
    if not Path(internal_dir).is_dir():
        logger.warning("internal directory does not exist: %s", internal_dir)
    build_index(root_dirs=[docs_dir, internal_dir], db_path=db)


# ── Path helpers ────────────────────────────────────────────────────────


def _normalize_doc_path(path: str) -> str:
    """Strip leading/trailing slashes and append ``.mdx`` if needed.

    Rejects path traversal.

    Args:
        path: Relative document path (e.g. ``foo/bar`` or ``/foo/bar/``).

    Returns:
        Normalized path (e.g. ``foo/bar.mdx``).

    Raises:
        ValueError: If path contains ``..`` traversal.

    """
    path = path.strip("/")

    if ".." in path.split("/"):
        msg = "path traversal ('..') is not allowed"
        raise ValueError(msg)

    if not path.endswith(".mdx"):
        path = path + ".mdx"

    return path


def _sanitize_query(query: str) -> str:
    r"""Convert a user query into a literal FTS5 phrase-AND query.

    Strips embedded double quotes and C0 control characters (``\x00-\x1f``,
    NUL included), splits on whitespace, wraps each token in double quotes,
    and joins with spaces so FTS5 treats every token as a literal phrase
    rather than operator syntax (``-``, ``OR``, ``*``, etc.).

    Raises:
        ValueError: If no tokens remain after sanitization.

    """
    _strip = {ord('"'): None, **dict.fromkeys(range(32))}
    tokens = query.translate(_strip).split()
    if not tokens:
        msg = "query must be a non-empty string"
        raise ValueError(msg)
    return " ".join(f'"{t}"' for t in tokens)


def _resolve_section_path(section: str, path: str) -> str:
    """Resolve an absolute filesystem path within a section.

    Guards against symlink-based directory escape.

    Args:
        section: ``"docs"`` or ``"internal"``.
        path: Normalized path relative to the section directory.

    Returns:
        Absolute, symlink-resolved path.

    Raises:
        ValueError: If the resolved path escapes the section directory.

    """
    section_dir = str(Path(_ROOT_DIR) / section)
    abs_path = str(Path(section_dir) / path)
    real_abs = os.path.realpath(abs_path)
    real_prefix = os.path.realpath(section_dir)
    if not real_abs.startswith(real_prefix + "/") and real_abs != real_prefix:
        msg = "path escapes the allowed directory via symlink"
        raise ValueError(msg)
    return abs_path


# ── write_doc helpers ─────────────────────────────────────────────────


def _read_existing_frontmatter(abs_path: str) -> dict[str, object]:
    """Return the frontmatter of *abs_path*, or ``{}`` if missing/unreadable."""
    if not Path(abs_path).is_file():
        return {}
    try:
        parsed = parse_file(abs_path, root=_ROOT_DIR)
    except OSError:
        logger.debug(
            "Failed to read existing frontmatter for %s",
            abs_path,
            exc_info=True,
        )
        return {}
    return parsed["frontmatter"]


def _resolve_published(existing_fm: dict[str, object]) -> str:
    """Return the ``published`` value, keeping *existing_fm* or defaulting to now."""
    pub_val = existing_fm.get("published")
    if pub_val is None:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    if isinstance(pub_val, str):
        return pub_val
    if isinstance(pub_val, (date, datetime)):
        return pub_val.isoformat()
    return str(pub_val)


def _atomic_write(abs_path: str, full_content: str) -> None:
    """Write *full_content* to *abs_path* via a temp file in the same dir."""
    try:
        fd, tmp_path = tempfile.mkstemp(dir=Path(abs_path).parent)
        with os.fdopen(fd, "w") as f:
            f.write(full_content)
        Path(tmp_path).rename(abs_path)
    except OSError as e:
        raise RuntimeError(str(e)) from e


def _delete_metadata_after_failed_index(db_path: str, rel_path: str) -> None:
    """Best-effort removal of the *rel_path* ``file_metadata`` entry."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "DELETE FROM file_metadata WHERE path = ?",
            (rel_path,),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        logger.debug(
            "Failed to clean up file_metadata after failed re-index",
            exc_info=True,
        )


def _reindex_after_write(section: str, path: str) -> None:
    """Rebuild the FTS5 index; on failure clean up metadata and re-raise.

    Raises:
        RuntimeError: If the re-index fails (the file is already written).

    """
    db_path = _db_path()
    docs_dir, internal_dir = _section_dirs(_ROOT_DIR)
    try:
        build_index(root_dirs=[docs_dir, internal_dir], db_path=db_path)
    except Exception as e:
        # Any re-index failure must still clean up and report; `from e`
        # keeps the original error visible.
        logger.exception("Index update failed after write")
        _delete_metadata_after_failed_index(db_path, f"{section}/{path}")
        msg = f"file written but index update failed: {e}"
        raise RuntimeError(msg) from e


# ── Tool implementations ──────────────────────────────────────────────


# These tool functions deliberately have no return annotation. FastMCP
# uses a return annotation to advertise an outputSchema in tools/list, which
# makes standards-compliant MCP clients fail with -32600 on successful calls
# (the server returns TextContent-only). The per-line type-ignore/noqa below
# keep mypy strict and ruff(ALL) satisfied.
@mcp.tool()
def search_docs(query: str, max_results: int = 10):  # type: ignore[no-untyped-def]  # noqa: ANN201
    """Full-text search across documentation.

    Args:
        query: Literal text to search for. Hyphens and punctuation are
            treated literally.
        max_results: Maximum number of results (default 10).

    """
    if not query or not query.strip():
        msg = "query must be a non-empty string"
        raise ValueError(msg)
    sanitized = _sanitize_query(query)
    db = _db_path()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db)
        cur = conn.execute(
            """SELECT title, path,
                      snippet(docs_fts, 2, '<b>', '</b>', '...', 48) AS excerpt,
                      rank,
                      section
               FROM docs_fts
               WHERE docs_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (sanitized, max_results),
        )
        results = [
            {
                "title": row[0],
                "path": row[1],
                "excerpt": row[2],
                "score": float(row[3]) if row[3] is not None else 0.0,
                "section": row[4],
            }
            for row in cur.fetchall()
        ]
        return json.dumps(results)
    except sqlite3.OperationalError as e:
        raise RuntimeError(str(e)) from e
    finally:
        if conn is not None:
            conn.close()


@mcp.tool()
def get_doc(path: str):  # type: ignore[no-untyped-def]  # noqa: ANN201
    """Retrieve document content and frontmatter by path.

    Args:
        path: Document path relative to Wywy-Docs root.

    """
    path = _normalize_doc_path(path)
    abs_path = str(Path(_ROOT_DIR) / path)
    if not Path(abs_path).is_file():
        msg = "document not found"
        raise ValueError(msg)
    parsed = parse_file(abs_path, root=_ROOT_DIR)
    return json.dumps(
        {"content": parsed["content"], "frontmatter": parsed["frontmatter"]},
        default=str,
    )


@mcp.tool()
def write_doc(  # type: ignore[no-untyped-def]  # noqa: ANN201
    section: Section,
    path: str,
    content: str,
    frontmatter: dict[str, object] | None = None,
):
    """Create or update a documentation file.

    Args:
        section: Section to write to ("docs" or "internal").
        path: Path relative to the section directory.
        content: Document content (body text, after frontmatter).
        frontmatter: Optional YAML frontmatter fields.

    """
    # Normalize path and resolve symlink-safe absolute path
    path = _normalize_doc_path(path)
    abs_path = _resolve_section_path(section, path)

    # Check parent directory exists
    parent = Path(abs_path).parent
    if not parent.is_dir():
        msg = "parent directory does not exist"
        raise ValueError(msg)

    # Normalise frontmatter
    if frontmatter is None:
        frontmatter = {}

    # Validate user frontmatter before any I/O
    try:
        DocFrontmatter(frontmatter)
    except ValueError as e:
        raise ValueError(str(e)) from e

    # Merge: existing (minus reserved) → user fields → published/last_updated
    existing_fm = _read_existing_frontmatter(abs_path)
    existing_filtered = {
        k: v for k, v in existing_fm.items() if k not in ("published", "last_updated")
    }
    merged_fm = {
        **existing_filtered,
        **frontmatter,
        "published": _resolve_published(existing_fm),
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }

    # Serialize frontmatter and write atomically, then re-index
    fm_yaml = yaml.safe_dump(
        merged_fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    full_content = f"---\n{fm_yaml}---\n\n{content}"
    _atomic_write(abs_path, full_content)
    _reindex_after_write(section, path)

    return json.dumps({"path": f"{section}/{path}"})


@mcp.tool()
def delete_doc(path: str):  # type: ignore[no-untyped-def]  # noqa: ANN201
    """Delete a documentation file.

    Args:
        path: Document path relative to Wywy-Docs root.

    """
    path = path.strip("/")

    # Determine section from path prefix
    if path.startswith("docs/"):
        section = "docs"
    elif path.startswith("internal/"):
        section = "internal"
    else:
        msg = "path must be under docs/ or internal/"
        raise ValueError(msg)

    # Strip section prefix and normalize
    path = path[len(section) + 1 :]
    path = _normalize_doc_path(path)
    abs_path = _resolve_section_path(section, path)

    # Delete file (idempotent: already gone → success)
    try:
        Path(abs_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        raise RuntimeError(str(e)) from e

    # Clean index entries
    rel_path = f"{section}/{path}"
    db = _db_path()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
        conn.execute("DELETE FROM file_metadata WHERE path = ?", (rel_path,))
        conn.commit()
    except sqlite3.OperationalError as e:
        raise RuntimeError(str(e)) from e
    finally:
        if conn is not None:
            conn.close()

    return json.dumps({"path": rel_path, "deleted": True})


# ── Custom call-tool handler ──────────────────────────────────────────
# FastMCP returns tool errors as CallToolResult(isError=True), but the tests
# expect JSON-RPC error responses (with "error" key) using specific codes:
#   ValueError  → -32602 (INVALID_PARAMS)
#   RuntimeError → -32603 (INTERNAL_ERROR)
# We override the handler to raise McpError, which produces JSON-RPC errors.


async def _call_tool_handler(req: CallToolRequest) -> ServerResult:
    """Handle CallToolRequest, converting ValueError/RuntimeError to McpError."""
    tool_name = req.params.name
    arguments = req.params.arguments or {}

    try:
        result = await mcp.call_tool(tool_name, arguments)
    except ToolError as e:
        cause = e.__cause__
        if isinstance(cause, ValueError):
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message=str(cause)),
            ) from cause
        if isinstance(cause, RuntimeError):
            raise McpError(
                ErrorData(code=INTERNAL_ERROR, message=str(cause)),
            ) from cause
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=str(e))) from e
    except Exception as e:
        # Boundary safety net: any unexpected error becomes INTERNAL_ERROR.
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=str(e))) from e

    if isinstance(result, list):
        return ServerResult(CallToolResult(content=result, isError=False))
    return ServerResult(
        CallToolResult(
            content=[TextContent(type="text", text=str(result))],
            isError=False,
        ),
    )


# Replace the default CallToolRequest handler with our custom one. FastMCP
# exposes no public API for this; `_mcp_server` access is the documented
# workaround, so the private-access lint is suppressed.
mcp._mcp_server.request_handlers[CallToolRequest] = _call_tool_handler  # noqa: SLF001  # type: ignore[reportPrivateUsage]


# ── Entry point ───────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP SSE server, resolving root dir and port from env/args."""
    logging.basicConfig(
        level=logging.INFO,
        format="wywy-docs: %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    args, _ = parser.parse_known_args()
    # `_ROOT_DIR` is a module-level mutable shared with tests; the `global`
    # statement is the deliberate mechanism to set it from `main()`.
    global _ROOT_DIR  # noqa: PLW0603
    _ROOT_DIR = os.environ.get(
        "WYWY_DOCS_DIR",
        os.environ.get("WYWY_ROOT", str(Path.cwd())),
    )
    _ensure_index(_ROOT_DIR)
    # `or "2530"` guards against an empty PORT env var (e.g. a systemd
    # Environment=PORT=), which would make int("") raise at startup.
    port = args.port if args.port is not None else int(os.environ.get("PORT") or "2530")
    mcp.settings.port = port
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
