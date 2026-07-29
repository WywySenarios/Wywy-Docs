# Wywy-Docs

## MCP tools

### delete_doc

Deletes a document by `path` (string, relative to the Wywy-Docs root directory).
If the path has no extension, `.mdx` is automatically appended.

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
