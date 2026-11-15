"""MCP HTTP SSE server for Wywy-Docs.

Exposes the FTS5 documentation index as MCP tools (``search_docs``,
``get_doc``, ``write_doc``, ``edit_doc``, ``delete_doc``) via the standard
MCP SSE transport.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path

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

from wywy_docs.documents import INDEX_LOCK as _INDEX_LOCK
from wywy_docs.documents import edit_document, write_document
from wywy_docs.documents import normalize_doc_path as _normalize_doc_path
from wywy_docs.documents import resolve_section_path as _resolve_section_path
from wywy_docs.errors import (
    DeleteFileError,
    EmptyQueryError,
    IndexDeleteError,
    IndexReconciliationError,
    InvalidDeletePathError,
    SearchError,
)
from wywy_docs.errors import (
    DocNotFoundError as DocNotFoundError,  # noqa: PLC0414  # explicit re-export
)
from wywy_docs.indexer import (
    build_index,
    doc_mtime,
    parse_file,
    reindex_doc,
    remove_doc,
)
from wywy_docs.models import Section  # noqa: TC001  # FastMCP needs it at runtime

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


def _reconcile_doc(rel_path: str, abs_path: str) -> None:
    """Re-index *rel_path* when its on-disk mtime differs from the index.

    A manual edit bypasses the server, so the index keeps stale content until
    the document is read back.  A path with no metadata row, or one whose
    recorded mtime differs from the file's, is re-indexed; a match performs no
    write.  The comparison and any rewrite run under ``_INDEX_LOCK``.

    Args:
        rel_path: Index key for the document, relative to the root.
        abs_path: Absolute path to the on-disk file.

    Raises:
        IndexReconciliationError: If the re-index fails (fail-closed).

    """
    mtime = Path(abs_path).stat().st_mtime_ns
    db_path = _db_path()
    try:
        with _INDEX_LOCK:
            # Keep the comparison and the rewrite in one acquisition so two
            # concurrent readers cannot both rewrite a stale document.
            if doc_mtime(db_path, rel_path) == mtime:
                return
            reindex_doc(
                root=_ROOT_DIR,
                db_path=db_path,
                abs_path=abs_path,
                rel_path=rel_path,
            )
    except (OSError, RuntimeError, UnicodeError, sqlite3.Error) as e:
        logger.exception("Index reconciliation failed for %s", rel_path)
        raise IndexReconciliationError(error=e) from e


def _sanitize_query(query: str) -> str:
    r"""Convert a user query into a literal FTS5 phrase-AND query.

    Strips embedded double quotes and C0 control characters (``\x00-\x1f``,
    NUL included), splits on whitespace, wraps each token in double quotes,
    and joins with spaces so FTS5 treats every token as a literal phrase
    rather than operator syntax (``-``, ``OR``, ``*``, etc.).

    Raises:
        EmptyQueryError: If no tokens remain after sanitization.

    """
    _strip = {ord('"'): None, **dict.fromkeys(range(32))}
    tokens = query.translate(_strip).split()
    if not tokens:
        raise EmptyQueryError
    return " ".join(f'"{t}"' for t in tokens)


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
        raise EmptyQueryError
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
        raise SearchError(error=e) from e
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
        # The vanished file leaves stale rows behind; drop them so search stops
        # surfacing the path.  Best-effort: a cleanup failure is logged, never
        # surfaced, so it cannot mask the not-found result.
        with _INDEX_LOCK:
            try:
                remove_doc(_db_path(), path)
            except sqlite3.Error:
                logger.debug(
                    "Failed to remove stale index entry for %s",
                    path,
                    exc_info=True,
                )
        raise DocNotFoundError(rel_path=path, index_removed=True)
    # Only paths the indexer records (`docs/`, `internal/`) are reconciled;
    # a section-less path is read straight from disk with no index side effect.
    if path.startswith(("docs/", "internal/")):
        _reconcile_doc(path, abs_path)
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
    rel_path = write_document(
        _ROOT_DIR,
        _db_path(),
        section,
        path,
        content,
        frontmatter,
    )
    return json.dumps({"path": rel_path})


@mcp.tool()
def edit_doc(  # type: ignore[no-untyped-def]  # noqa: ANN201
    section: Section,
    path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
):
    """Edit a document body by exact string replacement.

    Only the body is edited.
    ``last_updated`` is refreshed and existing frontmatter is otherwise preserved.

    Args:
        section: Section to edit ("docs" or "internal").
        path: Path relative to the section directory.
        old_string: Literal text to find in the body.
        new_string: Literal replacement text.
        replace_all: Replace every occurrence instead of requiring the
            match to be unique (default false).

    """
    rel_path, count = edit_document(
        _ROOT_DIR,
        _db_path(),
        section,
        path,
        old_string,
        new_string,
        replace_all=replace_all,
    )
    return json.dumps({"path": rel_path, "replacements": count})


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
        raise InvalidDeletePathError

    # Strip section prefix and normalize
    path = path[len(section) + 1 :]
    path = _normalize_doc_path(path)
    abs_path = _resolve_section_path(_ROOT_DIR, section, path)

    # Delete file (idempotent: already gone → success)
    try:
        Path(abs_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        raise DeleteFileError(error=e) from e

    # Clean index entries
    rel_path = f"{section}/{path}"
    db = _db_path()
    conn: sqlite3.Connection | None = None
    with _INDEX_LOCK:
        try:
            conn = sqlite3.connect(db)
            conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
            conn.execute("DELETE FROM file_metadata WHERE path = ?", (rel_path,))
            conn.commit()
        except sqlite3.OperationalError as e:
            raise IndexDeleteError(error=e) from e
        finally:
            if conn is not None:
                conn.close()

    return json.dumps({"path": rel_path, "deleted": True})


# ── Errors ────────────────────────────────────────────────────────────


# JSON-rpc error code
DOCUMENT_NOT_FOUND = -32010


# ── Custom call-tool handler ──────────────────────────────────────────
# FastMCP returns tool errors as CallToolResult(isError=True), but the tests
# expect JSON-RPC error responses (with an "error" key).  We override the
# handler to raise McpError, matching these types in order:
#   DocNotFoundError → DOCUMENT_NOT_FOUND
#   ValueError       → -32602 (INVALID_PARAMS)
#   RuntimeError     → -32603 (INTERNAL_ERROR)


async def _call_tool_handler(req: CallToolRequest) -> ServerResult:
    """Handle CallToolRequest, converting tool errors to McpError."""
    tool_name = req.params.name
    arguments = req.params.arguments or {}

    try:
        result = await mcp.call_tool(tool_name, arguments)
    except ToolError as e:
        cause = e.__cause__
        if isinstance(cause, DocNotFoundError):
            raise McpError(
                ErrorData(code=DOCUMENT_NOT_FOUND, message=str(cause)),
            ) from cause
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
