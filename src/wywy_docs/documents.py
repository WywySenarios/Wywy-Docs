"""Shared document-mutation pipeline for Wywy-Docs.

This module owns:

* Document mutation (:func:`write_document`, :func:`edit_document`).
* Path containment (:func:`normalize_doc_path`, :func:`resolve_section_path`).
* Frontmatter merge and serialization, including the reserved ``published``
  and ``last_updated`` fields.
* Atomic file writes (:func:`atomic_write`).
* Metadata cleanup when a post-write re-index fails.
* Per-document re-indexing (:func:`reindex_after_write`).
* The process-wide :data:`INDEX_LOCK` that serializes document writes with
  index mutations.

``write_document`` acquires :data:`INDEX_LOCK` internally; the indexer
functions never acquire it, so there is one lock owner and no nested
acquisition.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path

import yaml

from wywy_docs.errors import (
    AmbiguousEditError,
    DocumentReadError,
    DocumentWriteError,
    EditTargetNotFoundError,
    EmptyOldStringError,
    IndexUpdateError,
    InvalidFrontmatterError,
    MalformedFrontmatterError,
    MissingParentDirectoryError,
    PathEscapeError,
    ReservedFrontmatterError,
    UnchangedEditError,
)
from wywy_docs.errors import (
    DocNotFoundError as DocNotFoundError,  # noqa: PLC0414  # explicit re-export
)
from wywy_docs.indexer import parse_file, reindex_doc
from wywy_docs.models import RESERVED_FRONTMATTER_KEYS, DocFrontmatter, Section

logger = logging.getLogger(__name__)


# Serialises document writes and index mutations (write reindex, read
# reconciliation, delete cleanup) within this process. Callers acquire it
# exactly once; the indexer functions never do, so there is exactly one lock
# owner and no nested acquisition.
INDEX_LOCK = threading.Lock()


# A frontmatter block splits into at most three parts: "", YAML, body.
_FRONTMATTER_PARTS = 3


# ── Path helpers ────────────────────────────────────────────────────────


def normalize_doc_path(path: str) -> str:
    """Strip leading/trailing slashes and append ``.mdx`` if needed.

    Rejects path traversal.

    Args:
        path: Relative document path (e.g. ``foo/bar`` or ``/foo/bar/``).

    Returns:
        Normalized path (e.g. ``foo/bar.mdx``).

    Raises:
        PathEscapeError: If path contains ``..`` traversal.

    """
    path = path.strip("/")

    if ".." in path.split("/"):
        raise PathEscapeError(PathEscapeError.TRAVERSAL_MESSAGE)

    if not path.endswith(".mdx"):
        path = path + ".mdx"

    return path


def resolve_section_path(root: str, section: str, path: str) -> str:
    """Resolve an absolute filesystem path within a section.

    Guards against symlink-based directory escape.

    Args:
        root: Absolute path to the Wywy-Docs root directory.
        section: ``"docs"`` or ``"internal"``.
        path: Normalized path relative to the section directory.

    Returns:
        Absolute, symlink-resolved path.

    Raises:
        PathEscapeError: If the resolved path escapes the section directory.

    """
    section_dir = str(Path(root) / section)
    abs_path = str(Path(section_dir) / path)
    real_abs = os.path.realpath(abs_path)
    real_prefix = os.path.realpath(section_dir)
    if not real_abs.startswith(real_prefix + "/") and real_abs != real_prefix:
        raise PathEscapeError(PathEscapeError.SYMLINK_MESSAGE)
    return abs_path


# ── Frontmatter helpers ────────────────────────────────────────────────


def read_existing_frontmatter(root: str, abs_path: str) -> dict[str, object]:
    """Return the frontmatter of *abs_path*, or ``{}`` if missing/unreadable."""
    if not Path(abs_path).is_file():
        return {}
    try:
        parsed = parse_file(abs_path, root=root)
    except OSError:
        logger.debug(
            "Failed to read existing frontmatter for %s",
            abs_path,
            exc_info=True,
        )
        return {}
    return parsed["frontmatter"]


def resolve_published(existing_fm: dict[str, object]) -> str:
    """Return the ``published`` value, keeping *existing_fm* or defaulting to now."""
    pub_val = existing_fm.get("published")
    if pub_val is None:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    if isinstance(pub_val, str):
        return pub_val
    if isinstance(pub_val, (date, datetime)):
        return pub_val.isoformat()
    return str(pub_val)


def _merge_frontmatter(
    existing_fm: dict[str, object],
    user_fm: dict[str, object],
) -> dict[str, object]:
    """Merge *user_fm* over *existing_fm* and refresh the managed date fields.

    Reserved keys (``published``, ``last_updated``) are always taken from
    *existing_fm* or the current time, never from *user_fm*.
    """
    existing_filtered = {
        k: v for k, v in existing_fm.items() if k not in ("published", "last_updated")
    }
    return {
        **existing_filtered,
        **user_fm,
        "published": resolve_published(existing_fm),
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }


def _serialize_document(frontmatter: dict[str, object], body: str) -> str:
    """Render *frontmatter* plus *body* as a complete ``.mdx`` file."""
    fm_yaml = yaml.safe_dump(
        frontmatter,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return f"---\n{fm_yaml}---\n\n{body}"


# ── Write helpers ──────────────────────────────────────────────────────


def atomic_write(abs_path: str, full_content: str) -> None:
    """Write *full_content* to *abs_path* via a temp file in the same dir."""
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=Path(abs_path).parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(full_content)
        Path(tmp_path).rename(abs_path)
    except OSError as e:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)
        raise DocumentWriteError(error=e) from e


def delete_metadata_after_failed_index(db_path: str, rel_path: str) -> None:
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


def reindex_after_write(
    root: str,
    db_path: str,
    rel_path: str,
    abs_path: str,
    *,
    failure_prefix: str = "file written",
) -> None:
    """Re-index the written document; on failure clean up metadata and re-raise.

    Only the written document is re-indexed, so an unrelated file changed on
    disk outside the tool keeps its existing index rows.  The caller owns
    :data:`INDEX_LOCK`; this function does not acquire it.

    Args:
        root: Absolute path to the Wywy-Docs root directory.
        db_path: Path to the SQLite index database.
        rel_path: Index key for the document, relative to the root.
        abs_path: Absolute path to the just-written file.
        failure_prefix: Prefix for the error raised on re-index failure.

    Raises:
        IndexUpdateError: If the re-index fails (the file is already written).

    """
    try:
        reindex_doc(
            root=root,
            db_path=db_path,
            abs_path=abs_path,
            rel_path=rel_path,
        )
    except (OSError, RuntimeError, UnicodeError, sqlite3.Error) as e:
        logger.exception("Index update failed after document mutation")
        delete_metadata_after_failed_index(db_path, rel_path)
        raise IndexUpdateError(action=failure_prefix, error=e) from e


# ── Shared write pipeline ──────────────────────────────────────────────


def write_document(  # noqa: PLR0913
    root: str,
    db_path: str,
    section: Section,
    path: str,
    content: str,
    frontmatter: dict[str, object] | None,
) -> str:
    """Create or update a document through the shared write pipeline.

    Args:
        root: Absolute path to the Wywy-Docs root directory.
        db_path: Path to the SQLite index database.
        section: Section to write to ("docs" or "internal").
        path: Path relative to the section directory.
        content: Document content (body text, after frontmatter).
        frontmatter: Optional YAML frontmatter fields.

    Returns:
        The document's path relative to the root (``"{section}/{path}"``).

    Raises:
        PathEscapeError: If the path escapes the section directory.
        MissingParentDirectoryError: If the parent directory does not exist.
        ReservedFrontmatterError: If a managed key is supplied.
        InvalidFrontmatterError: If the frontmatter fails validation.
        DocumentWriteError: If the file cannot be written.
        IndexUpdateError: If the re-index fails.

    """
    path = normalize_doc_path(path)
    abs_path = resolve_section_path(root, section, path)

    parent = Path(abs_path).parent
    if not parent.is_dir():
        raise MissingParentDirectoryError

    if frontmatter is None:
        frontmatter = {}

    for key in RESERVED_FRONTMATTER_KEYS:
        if key in frontmatter:
            raise ReservedFrontmatterError(key=key)

    # Validate user frontmatter before any I/O.
    try:
        DocFrontmatter(frontmatter)
    except ValueError as e:
        raise InvalidFrontmatterError(error=e) from e

    rel_path = f"{section}/{path}"

    with INDEX_LOCK:
        existing_fm = read_existing_frontmatter(root, abs_path)
        merged_fm = _merge_frontmatter(existing_fm, frontmatter)

        full_content = _serialize_document(merged_fm, content)
        atomic_write(abs_path, full_content)
        reindex_after_write(root, db_path, rel_path, abs_path)

    return rel_path


# ── Edit helpers ───────────────────────────────────────────────────────


def _assert_editable_frontmatter(raw: str) -> None:
    """Reject a frontmatter header that an edit cannot safely rewrite.

    An absent header and an empty header are both allowed, because the edit
    replaces them with managed frontmatter.  An unterminated header, or a
    terminated one whose YAML is invalid or not a mapping, is rejected.  This
    runs before any write, so a rejected document stays untouched.

    Args:
        raw: Full raw file text.

    Raises:
        MalformedFrontmatterError: If the header is unterminated, malformed,
            or not a mapping.

    """
    if not raw.startswith("---"):
        return

    parts = raw.split("---", 2)
    if len(parts) < _FRONTMATTER_PARTS:
        raise MalformedFrontmatterError

    header = parts[1]
    if not header.strip():
        return

    try:
        parsed = yaml.safe_load(header)
    except yaml.YAMLError as e:
        raise MalformedFrontmatterError from e

    if not isinstance(parsed, dict):
        # The public contract reports malformed input as a ValueError.
        raise MalformedFrontmatterError


def _replace_body(
    body: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool,
    rel_path: str,
) -> tuple[str, int]:
    """Replace literal occurrences of *old_string* in *body*.

    Args:
        body: Document body text (frontmatter already stripped).
        old_string: Literal text to find.
        new_string: Literal replacement text.
        replace_all: Replace every occurrence instead of exactly one.
        rel_path: Document path relative to the root, used in error messages.

    Returns:
        The updated body and the number of occurrences that matched.

    Raises:
        EmptyOldStringError: If *old_string* is empty.
        UnchangedEditError: If *old_string* equals *new_string*.
        EditTargetNotFoundError: If *old_string* does not occur in *body*.
        AmbiguousEditError: If *old_string* matches ambiguously without
            *replace_all*.

    """
    if not old_string:
        raise EmptyOldStringError
    if old_string == new_string:
        raise UnchangedEditError

    count = body.count(old_string)
    if count == 0:
        raise EditTargetNotFoundError(rel_path=rel_path)
    if count > 1 and not replace_all:
        raise AmbiguousEditError(count=count, rel_path=rel_path)

    if replace_all:
        new_body = body.replace(old_string, new_string)
    else:
        new_body = body.replace(old_string, new_string, 1)

    return new_body, count


# ── Shared edit pipeline ───────────────────────────────────────────────


def edit_document(  # noqa: PLR0913
    root: str,
    db_path: str,
    section: Section,
    path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool,
) -> tuple[str, int]:
    """Edit a document body through the shared mutation pipeline.

    The frontmatter is preserved (with ``last_updated`` refreshed) and the
    body is edited by literal string replacement.  The whole operation runs
    under :data:`INDEX_LOCK`.

    Args:
        root: Absolute path to the Wywy-Docs root directory.
        db_path: Path to the SQLite index database.
        section: Section to edit ("docs" or "internal").
        path: Path relative to the section directory.
        old_string: Literal text to find in the body.
        new_string: Literal replacement text.
        replace_all: Replace every occurrence instead of exactly one.

    Returns:
        The document's path relative to the root (``"{section}/{path}"``)
        and the number of replacements made.

    Raises:
        DocNotFoundError: If the document has no file on disk.
        PathEscapeError: If the path escapes the section directory.
        MalformedFrontmatterError: If the frontmatter cannot be edited.
        EmptyOldStringError: If *old_string* is empty.
        UnchangedEditError: If the edit is a no-op.
        EditTargetNotFoundError: If *old_string* is absent from the body.
        AmbiguousEditError: If the match is ambiguous without *replace_all*.
        DocumentReadError: If the document cannot be read.
        DocumentWriteError: If the document cannot be written.
        IndexUpdateError: If the re-index fails.

    """
    path = normalize_doc_path(path)
    abs_path = resolve_section_path(root, section, path)
    rel_path = f"{section}/{path}"

    with INDEX_LOCK:
        if not Path(abs_path).is_file():
            raise DocNotFoundError(rel_path=rel_path)

        try:
            raw = Path(abs_path).read_text(encoding="utf-8")
            _assert_editable_frontmatter(raw)
            parsed = parse_file(abs_path, root=root, raw=raw)
        except (OSError, UnicodeError) as e:
            raise DocumentReadError(error=e) from e

        new_body, count = _replace_body(
            parsed["content"],
            old_string,
            new_string,
            replace_all=replace_all,
            rel_path=rel_path,
        )

        merged_fm = _merge_frontmatter(parsed["frontmatter"], {})
        full_content = _serialize_document(merged_fm, new_body)
        atomic_write(abs_path, full_content)
        reindex_after_write(
            root,
            db_path,
            rel_path,
            abs_path,
            failure_prefix="file edited",
        )

    return rel_path, count
