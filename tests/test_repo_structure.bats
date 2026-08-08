bats_require_minimum_version 1.5.0

setup() {
    REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
}

@test "docs/ is a regular directory" {
    [ -d "$REPO_ROOT/docs" ]
    [ ! -L "$REPO_ROOT/docs" ]
}

@test "internal/ is a regular directory" {
    [ -d "$REPO_ROOT/internal" ]
    [ ! -L "$REPO_ROOT/internal" ]
}

@test "src/ directory exists" {
    [ -d "$REPO_ROOT/src" ]
}

@test "src/wywy_docs/ directory exists" {
    [ -d "$REPO_ROOT/src/wywy_docs" ]
}

@test "src/wywy_docs/__init__.py exists" {
    [ -f "$REPO_ROOT/src/wywy_docs/__init__.py" ]
}

@test "scripts/ directory exists" {
    [ -d "$REPO_ROOT/scripts" ]
}

@test "scripts/wywy-docs-mcp.service — systemd unit file exists" {
    [ -f "$REPO_ROOT/scripts/wywy-docs-mcp.service" ]
}

@test "scripts/install-service.sh — service install script exists" {
    [ -f "$REPO_ROOT/scripts/install-service.sh" ]
}

@test "scripts/wywy-docs-mcp.service — user-level systemd unit" {
    # User service: no User= directive, uses %h specifier, WantedBy=default.target
    grep -q "^WantedBy=default.target$" "$REPO_ROOT/scripts/wywy-docs-mcp.service"
    grep -q "^StartLimitIntervalSec=60$" "$REPO_ROOT/scripts/wywy-docs-mcp.service"
    grep -q "^StartLimitBurst=3$" "$REPO_ROOT/scripts/wywy-docs-mcp.service"
    grep -q "%h/" "$REPO_ROOT/scripts/wywy-docs-mcp.service"
    ! grep -q "^User=" "$REPO_ROOT/scripts/wywy-docs-mcp.service"
    grep -q "\-m wywy_docs.server" "$REPO_ROOT/scripts/wywy-docs-mcp.service"
    grep -q "EnvironmentFile=%h/.config/wywy-docs-mcp/environment" "$REPO_ROOT/scripts/wywy-docs-mcp.service"
}

@test "README.md exists" {
    [ -f "$REPO_ROOT/README.md" ]
}

@test ".gitignore excludes .venv/" {
    grep -q "^\.venv/$" "$REPO_ROOT/.gitignore"
}

@test "pyproject.toml declares mcp SDK dependency" {
    grep -q '^\s*"mcp",' "$REPO_ROOT/pyproject.toml"
}

@test "pyproject.toml has where = [\"src\"] in find config" {
    grep -q 'where = \["src"\]' "$REPO_ROOT/pyproject.toml"
}

@test "pyproject.toml includes wywy_docs in package find" {
    grep -q 'include = \["wywy_docs\*"\]' "$REPO_ROOT/pyproject.toml"
}

@test "pyproject.toml declares pyyaml dependency" {
    grep -q '^\s*"pyyaml",' "$REPO_ROOT/pyproject.toml"
}

@test "pyproject.toml declares uvicorn dependency" {
    grep -q '^\s*"uvicorn",' "$REPO_ROOT/pyproject.toml"
}

@test "src/wywy_docs/ has no stale mcp package references in imports" {
    # SDK imports (from mcp.server.fastmcp, from mcp.shared, from mcp.types) are valid.
    # Local package imports should all be "from wywy_docs".
    # Check that no import references the old local mcp package.
    ! grep -rn 'from mcp\.' "$REPO_ROOT/src/wywy_docs/" --include="*.py" | grep -vE '(fastmcp|shared|types)'
}

@test "scripts/install-service.sh references wywy-docs-mcp.service consistently" {
    grep -q 'SERVICE="wywy-docs-mcp"' "$REPO_ROOT/scripts/install-service.sh"
    grep -q '${SERVICE}.service' "$REPO_ROOT/scripts/install-service.sh"
}
