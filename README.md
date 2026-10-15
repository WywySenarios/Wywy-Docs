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

`delete_doc` resolves symlinks before path validation to prevent directory escape.
The same protection applies to `write_doc`.
`get_doc` does NOT perform this check (read access is less restricted than write/delete access).
