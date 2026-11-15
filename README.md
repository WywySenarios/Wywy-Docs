# Wywy-Docs

## MCP service configuration

Install the systemd user service from this checkout:

```sh
uv sync
uv run wywy-docs-install
```

`wywy-docs-install` derives the checkout root from the package location,
writes `wywy-docs-mcp.service` and the environment file
(`~/.config/wywy-docs-mcp/environment`, which stores `WYWY_DOCS_DIR`), then
prints the `systemctl --user` commands to enable and start it. It never
invokes `systemctl` itself — run the printed steps.

Manual dev run (no service):

```sh
uv run wywy-docs-serve --port 2530
```

Although the index is meant to be self-healing, you can force a full rebuild from the repo root:

```sh
rm "$PWD/wywy_docs/docs_index.db"
systemctl --user restart wywy-docs-mcp
```

## MCP tools

### get_doc

Reads a document by `path` (string, relative to the Wywy-Docs root directory) directly from disk, returning its content and frontmatter.
If the path does not end in `.mdx`, `.mdx` is appended.

### write_doc

Creates or updates a document.

- `section` (string): `docs` or `internal`.
- `path` (string): path relative to the section directory. If it does not end in `.mdx`, `.mdx` is appended.
- `content` (string): document body text (after the frontmatter).
- `frontmatter` (object, optional): YAML frontmatter fields. `published` and `last_updated` are managed by the server and cannot be set manually.

Existing frontmatter is preserved; `published` is kept and `last_updated` is refreshed.

Returns:

```json
{ "path": "docs/example.mdx" }
```

### edit_doc

Edits a document body by exact string replacement.

- `section` (string): `docs` or `internal`.
- `path` (string): path relative to the section directory. If it does not end in `.mdx`, `.mdx` is appended.
- `old_string` (string): non-empty literal text to find in the body.
- `new_string` (string): literal replacement text.
- `replace_all` (boolean, optional): replace every occurrence instead of requiring a unique match (default `false`).

Only the body is replaced.
`last_updated` is updated and the existing frontmatter is otherwise preserved.

The following will trigger errors:

- `old_string` that does not occur in the body
- a no-op edit
- an ambiguous match without `replace_all` is rejected.

Returns:

```json
{ "path": "docs/example.mdx", "replacements": 1 }
```

### delete_doc

Deletes a document by `path` (string, relative to the Wywy-Docs root directory).
If the path does not end in `.mdx`, `.mdx` is appended.

Returns:

```json
{ "path": "...", "deleted": true }
```

**Idempotent:** Deleting a non-existent file still returns `{"deleted": true}`.

## Security

### Symlink path escape protection

`delete_doc`, `write_doc`, and `edit_doc` resolve symlinks before path validation to prevent directory escape.
`get_doc` does NOT perform this check (read access is less restricted than write/delete access).
