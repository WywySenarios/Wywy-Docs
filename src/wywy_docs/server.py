"""MCP HTTP SSE server for Wywy-Docs.

Exposes the FTS5 documentation index as MCP tools (``search_docs``,
``get_doc``) via the standard MCP SSE transport.
"""

from __future__ import annotations

import json
import os
import sqlite3

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

# ── Globals ───────────────────────────────────────────────────────────

_ROOT_DIR: str = ""

mcp = FastMCP("wywy-docs", message_path="/messages/")


# ── Index helpers ─────────────────────────────────────────────────────


def _db_path(root_dir: str | None = None) -> str:
    return os.path.join(root_dir or _ROOT_DIR, "wywy_docs", "docs_index.db")


def _ensure_index(root_dir: str) -> None:
    """Build the FTS5 index if ``docs_index.db`` does not exist."""
    db = _db_path(root_dir)
    if not os.path.isfile(db):
        os.makedirs(os.path.dirname(db), exist_ok=True)
        docs_dir = os.path.join(root_dir, "docs")
        internal_dir = os.path.join(root_dir, "internal")
        build_index(root_dirs=[docs_dir, internal_dir], db_path=db)


# ── Tool implementations ──────────────────────────────────────────────


@mcp.tool()
def search_docs(query: str, max_results: int = 10):
    """Full-text search across documentation.

    Args:
        query: Search query (FTS5 syntax).
        max_results: Maximum number of results (default 10).
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    db = _db_path()
    try:
        conn = sqlite3.connect(db)
        cur = conn.execute(
            """SELECT title, path,
                      snippet(docs_fts, 2, '<b>', '</b>', '...', 48) AS excerpt,
                      rank
               FROM docs_fts
               WHERE docs_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, max_results),
        )
        results = [
            {
                "title": row[0],
                "path": row[1],
                "excerpt": row[2],
                "score": float(row[3]) if row[3] is not None else 0.0,
                "section": None,
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
    abs_path = os.path.join(_ROOT_DIR, path)
    if not os.path.isfile(abs_path):
        raise ValueError("document not found")
    parsed = parse_file(abs_path, root=_ROOT_DIR)
    return json.dumps(
        {"content": parsed["content"], "frontmatter": parsed["frontmatter"]},
        default=str,
    )


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
    global _ROOT_DIR
    _ROOT_DIR = os.environ.get("WYWY_ROOT", os.getcwd())
    _ensure_index(_ROOT_DIR)
    port = int(os.environ.get("PORT", "2530"))
    mcp.settings.port = port
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
