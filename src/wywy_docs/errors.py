"""Application error classes for Wywy-Docs."""

from __future__ import annotations

from typing import ClassVar


class _MessageMixin:
    """Fill ``args`` from ``MESSAGE``/``MESSAGE_TEMPLATE`` when none is passed.

    This is not an ``Exception`` subclass on purpose: it creates no catchable
    group above the builtins. ``args`` is assigned directly instead of calling
    ``super().__init__`` because the concrete bases are builtins.
    """

    args: tuple[object, ...]
    MESSAGE: ClassVar[str] = ""
    MESSAGE_TEMPLATE: ClassVar[str] = ""

    def __init__(self, message: str | None = None, **fields: object) -> None:
        """Resolve the message from the class constants and set ``args``."""
        if message is None:
            message = (
                self.MESSAGE_TEMPLATE.format(**fields)
                if self.MESSAGE_TEMPLATE
                else self.MESSAGE
            )
        self.args = (message,)


# ── Invalid client input → INVALID_PARAMS (-32602) ─────────────────────


class PathEscapeError(_MessageMixin, ValueError):
    """A path resolves outside its allowed section directory.

    ``..`` traversal and symlink escape are the same failure: the resolved
    path left the section. The mechanism is carried by the message, not by a
    separate class.
    """

    MESSAGE = "path escapes the allowed directory"
    TRAVERSAL_MESSAGE = "path traversal ('..') is not allowed"
    SYMLINK_MESSAGE = "path escapes the allowed directory via symlink"


class MissingParentDirectoryError(_MessageMixin, ValueError):
    """The target document's parent directory does not exist."""

    MESSAGE = "parent directory does not exist"


class EmptyQueryError(_MessageMixin, ValueError):
    """The search query is empty after sanitization."""

    MESSAGE = "query must be a non-empty string"


class InvalidDeletePathError(_MessageMixin, ValueError):
    """The delete path is not under ``docs/`` or ``internal/``."""

    MESSAGE = "path must be under docs/ or internal/"


class InvalidFrontmatterError(_MessageMixin, ValueError):
    """The frontmatter failed schema validation."""

    MESSAGE_TEMPLATE = "invalid frontmatter: {error}"


class ReservedFrontmatterError(_MessageMixin, ValueError):
    """A managed frontmatter key was supplied by the caller."""

    MESSAGE_TEMPLATE = (
        "'{key}' is a reserved frontmatter key and cannot be set manually"
    )


class MalformedFrontmatterError(_MessageMixin, ValueError):
    """The document's frontmatter header cannot be safely rewritten."""

    MESSAGE = "document has malformed frontmatter; use write_doc"


class EmptyOldStringError(_MessageMixin, ValueError):
    """The edit target string is empty."""

    MESSAGE = "old_string must not be empty"


class UnchangedEditError(_MessageMixin, ValueError):
    """The replacement is a no-op."""

    MESSAGE = "old_string and new_string must differ"


class EditTargetNotFoundError(_MessageMixin, ValueError):
    """The edit target string does not occur in the document body."""

    MESSAGE_TEMPLATE = "old_string not found in {rel_path}"


class AmbiguousEditError(_MessageMixin, ValueError):
    """The edit target occurs more than once without ``replace_all``."""

    MESSAGE_TEMPLATE = (
        "old_string matches {count} times in {rel_path}; "
        "set replace_all=true to replace all occurrences"
    )


# ── Document lookup → DOCUMENT_NOT_FOUND (-32010) ──────────────────────


class DocNotFoundError(_MessageMixin, Exception):
    """A requested document has no file on disk."""

    MESSAGE_TEMPLATE = "document not found: {rel_path}"
    INDEX_REMOVED_SUFFIX = "; its index entry was removed"

    def __init__(self, rel_path: str, *, index_removed: bool = False) -> None:
        """Build the message from *rel_path* and the cleanup flag."""
        message = self.MESSAGE_TEMPLATE.format(rel_path=rel_path)
        if index_removed:
            message += self.INDEX_REMOVED_SUFFIX
        super().__init__(message)


# ── Server / index failures → INTERNAL_ERROR (-32603) ──────────────────


class IndexReconciliationError(_MessageMixin, RuntimeError):
    """A stale document could not be re-indexed before reading it."""

    MESSAGE_TEMPLATE = "index reconciliation failed: {error}"


class IndexUpdateError(_MessageMixin, RuntimeError):
    """A document was written but its index row could not be updated."""

    MESSAGE_TEMPLATE = "{action} but index update failed: {error}"


class SearchError(_MessageMixin, RuntimeError):
    """The full-text search query failed in SQLite."""

    MESSAGE_TEMPLATE = "search failed: {error}"


class DeleteFileError(_MessageMixin, RuntimeError):
    """The document file could not be removed."""

    MESSAGE_TEMPLATE = "failed to delete file: {error}"


class IndexDeleteError(_MessageMixin, RuntimeError):
    """The document's index rows could not be removed."""

    MESSAGE_TEMPLATE = "failed to delete index entry: {error}"


class DocumentWriteError(_MessageMixin, RuntimeError):
    """The document file could not be written."""

    MESSAGE_TEMPLATE = "failed to write document: {error}"


class DocumentReadError(_MessageMixin, RuntimeError):
    """The document file could not be read or decoded."""

    MESSAGE_TEMPLATE = "failed to read document: {error}"


# ── Installer (CLI, no JSON-RPC mapping) ───────────────────────────────


class NotEditableInstallError(_MessageMixin, RuntimeError):
    """The installer was not run from an editable checkout."""

    MESSAGE_TEMPLATE = (
        "wywy-docs-install must run from an editable install of the "
        "Wywy-Docs checkout (run `uv sync` in the repo); derived root "
        "{root} has no pyproject.toml"
    )


class MissingConsoleScriptError(_MessageMixin, RuntimeError):
    """The checkout venv lacks the ``wywy-docs-serve`` console script."""

    MESSAGE_TEMPLATE = (
        "missing console script {serve}; run `uv sync` in {root} "
        "to create it before installing the service"
    )
