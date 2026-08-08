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

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.tool_manager import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ErrorData,
    INVALID_PARAMS,
    INTERNAL_ERROR,
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
    return os.path.join(root_dir or _ROOT_DIR, "wywy_docs", "docs_index.db")


def _section_dirs(root_dir: str) -> tuple[str, str]:
    """Return the conventional ``docs/`` and ``internal/`` paths under *root_dir*."""
    return (
        os.path.join(root_dir, "docs"),
        os.path.join(root_dir, "internal"),
    )


def _ensure_index(root_dir: str) -> None:
    """Build the FTS5 index if ``docs_index.db`` does not exist.

    Warns (without crashing) if ``docs/`` or ``internal/`` is missing;
    the index is still created, empty.
    """
    db = _db_path(root_dir)
    if not os.path.isfile(db):
        os.makedirs(os.path.dirname(db), exist_ok=True)
        docs_dir, internal_dir = _section_dirs(root_dir)
        if not os.path.isdir(docs_dir):
            logger.warning("docs directory does not exist: %s", docs_dir)
        if not os.path.isdir(internal_dir):
            logger.warning("internal directory does not exist: %s", internal_dir)
        build_index(root_dirs=[docs_dir, internal_dir], db_path=db)


# ── Path helpers ────────────────────────────────────────────────────────


def _normalize_doc_path(path: str) -> str:
    """Strip leading/trailing slashes, reject path traversal, and
    append ``.mdx`` if no extension is present.

    Args:
        path: Relative document path (e.g. ``foo/bar`` or ``/foo/bar/``).

    Returns:
        Normalized path (e.g. ``foo/bar.mdx``).

    Raises:
        ValueError: If path contains ``..`` traversal.
    """
    path = path.strip("/")

    if ".." in path.split("/"):
        raise ValueError("path traversal ('..') is not allowed")

    if not path.endswith(".mdx"):
        path = path + ".mdx"

    return path


def _sanitize_query(query: str) -> str:
    """Convert a user query into a literal FTS5 phrase-AND query.

    Strips embedded double quotes and C0 control characters (``\\x00-\\x1f``,
    NUL included), splits on whitespace, wraps each token in double quotes,
    and joins with spaces so FTS5 treats every token as a literal phrase
    rather than operator syntax (``-``, ``OR``, ``*``, etc.).

    Raises:
        ValueError: If no tokens remain after sanitization.
    """
    _strip = {ord('"'): None, **{i: None for i in range(0x20)}}
    tokens = query.translate(_strip).split()
    if not tokens:
        raise ValueError("query must be a non-empty string")
    return " ".join(f'"{t}"' for t in tokens)


def _resolve_section_path(section: str, path: str) -> str:
    """Resolve an absolute filesystem path within a section, guarding
    against symlink-based directory escape.

    Args:
        section: ``"docs"`` or ``"internal"``.
        path: Normalized path relative to the section directory.

    Returns:
        Absolute, symlink-resolved path.

    Raises:
        ValueError: If the resolved path escapes the section directory.
    """
    section_dir = os.path.join(_ROOT_DIR, section)
    abs_path = os.path.join(section_dir, path)
    real_abs = os.path.realpath(abs_path)
    real_prefix = os.path.realpath(section_dir)
    if not real_abs.startswith(real_prefix + "/") and real_abs != real_prefix:
        raise ValueError("path escapes the allowed directory via symlink")
    return abs_path


# ── Tool implementations ──────────────────────────────────────────────


@mcp.tool()
def search_docs(query: str, max_results: int = 10):
    """Full-text search across documentation.

    Args:
        query: Literal text to search for. Hyphens and punctuation are treated literally.
        max_results: Maximum number of results (default 10).
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    sanitized = _sanitize_query(query)
    db = _db_path()
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
        raise RuntimeError(str(e))
    finally:
        conn.close()


@mcp.tool()
def get_doc(path: str):
    """Retrieve document content and frontmatter by path.

    Args:
        path: Document path relative to Wywy-Docs root.
    """
    path = _normalize_doc_path(path)
    abs_path = os.path.join(_ROOT_DIR, path)
    if not os.path.isfile(abs_path):
        raise ValueError("document not found")
    parsed = parse_file(abs_path, root=_ROOT_DIR)
    return json.dumps(
        {"content": parsed["content"], "frontmatter": parsed["frontmatter"]},
        default=str,
    )


@mcp.tool()
def write_doc(
    section: Section, path: str, content: str, frontmatter: dict | None = None
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
    parent = os.path.dirname(abs_path)
    if not os.path.isdir(parent):
        raise ValueError("parent directory does not exist")

    # Normalise frontmatter
    if frontmatter is None:
        frontmatter = {}

    # Validate user frontmatter before any I/O
    try:
        DocFrontmatter(frontmatter)
    except ValueError as e:
        raise ValueError(str(e))

    # ── Read existing file for merge ────────────────────────────────
    existing_fm: dict = {}
    if os.path.isfile(abs_path):
        try:
            parsed = parse_file(abs_path, root=_ROOT_DIR)
            existing_fm = parsed["frontmatter"]
        except Exception:
            pass

    # Filter out reserved fields from existing frontmatter
    existing_filtered = {
        k: v for k, v in existing_fm.items() if k not in ("published", "last_updated")
    }

    # Determine published value
    if "published" in existing_fm and existing_fm["published"] is not None:
        pub_val = existing_fm["published"]
        if isinstance(pub_val, str):
            published = pub_val
        elif hasattr(pub_val, "isoformat"):
            published = pub_val.isoformat()
        else:
            published = str(pub_val)
    else:
        published = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    # last_updated is always current
    last_updated = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    # Merge: existing (minus reserved) → user fields → published/last_updated
    merged_fm = {
        **existing_filtered,
        **frontmatter,
        "published": published,
        "last_updated": last_updated,
    }

    # Serialize frontmatter
    fm_yaml = yaml.safe_dump(
        merged_fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    full_content = f"---\n{fm_yaml}---\n\n{content}"

    # Atomic write: tempfile in same directory + os.rename
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(abs_path))
        with os.fdopen(fd, "w") as f:
            f.write(full_content)
        os.rename(tmp_path, abs_path)
    except OSError as e:
        raise RuntimeError(str(e))

    # ── Re-index ────────────────────────────────────────────────────
    db_path = _db_path()
    docs_dir, internal_dir = _section_dirs(_ROOT_DIR)
    try:
        build_index(root_dirs=[docs_dir, internal_dir], db_path=db_path)
    except Exception as e:
        logger.error("Index update failed after write: %s", e)
        # Clean up file_metadata entry for the just-written path
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "DELETE FROM file_metadata WHERE path = ?",
                (f"{section}/{path}",),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        raise RuntimeError(f"file written but index update failed: {e}")

    return json.dumps({"path": f"{section}/{path}"})


@mcp.tool()
def delete_doc(path: str):
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
        raise ValueError("path must be under docs/ or internal/")

    # Strip section prefix and normalize
    path = path[len(section) + 1 :]
    path = _normalize_doc_path(path)
    abs_path = _resolve_section_path(section, path)

    # Delete file (idempotent: already gone → success)
    try:
        os.remove(abs_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise RuntimeError(str(e))

    # Clean index entries
    rel_path = f"{section}/{path}"
    db = _db_path()
    try:
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
        conn.execute("DELETE FROM file_metadata WHERE path = ?", (rel_path,))
        conn.commit()
    except sqlite3.OperationalError as e:
        raise RuntimeError(str(e))
    finally:
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
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(cause)))
        elif isinstance(cause, RuntimeError):
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=str(cause)))
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=str(e)))
    except Exception as e:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=str(e)))

    if isinstance(result, list):
        return ServerResult(CallToolResult(content=result, isError=False))
    return ServerResult(
        CallToolResult(
            content=[TextContent(type="text", text=str(result))],
            isError=False,
        )
    )


# Replace the default CallToolRequest handler with our custom one.
mcp._mcp_server.request_handlers[CallToolRequest] = _call_tool_handler


# ── Entry point ───────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="wywy-docs: %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    args, _ = parser.parse_known_args()
    global _ROOT_DIR
    _ROOT_DIR = os.environ.get(
        "WYWY_DOCS_DIR", os.environ.get("WYWY_ROOT", os.getcwd())
    )
    _ensure_index(_ROOT_DIR)
    port = args.port if args.port is not None else int(os.environ.get("PORT", "2530"))
    mcp.settings.port = port
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
