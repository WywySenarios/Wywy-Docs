"""Install the Wywy-Docs MCP server as a systemd user service.

Writes the systemd unit file and the environment file (documentation root),
then prints the ``systemctl`` commands to enable and start the service.  It
never invokes ``systemctl`` itself.
"""

from __future__ import annotations

import os
from pathlib import Path

SYSTEMD_UNIT = """\
[Unit]
Description=Wywy-Docs MCP Server
Documentation=https://github.com/WywySenarios/Wywy-Docs
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
# %E is the systemd user configuration root: $XDG_CONFIG_HOME or
# ~/.config.  Matches where wywy-docs-install writes the environment file.
EnvironmentFile=%E/wywy-docs-mcp/environment
WorkingDirectory=%h
ExecStart=/bin/sh -c 'exec "$WYWY_DOCS_DIR/.venv/bin/wywy-docs-serve"'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _config_dir() -> Path:
    """Return the user config dir (``$XDG_CONFIG_HOME`` or ``~/.config``)."""
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


def _repo_root() -> Path:
    """Derive the Wywy-Docs checkout root from this module's location.

    The src-layout package lives at ``<root>/src/wywy_docs/install.py``,
    so ``parents[2]`` is the checkout root.  For non-editable installs
    (site-packages) the derived path lacks ``pyproject.toml``, which is
    rejected.
    """
    candidate = Path(__file__).resolve().parents[2]
    if not (candidate / "pyproject.toml").is_file():
        msg = (
            "wywy-docs-install must run from an editable install of the "
            "Wywy-Docs checkout (run `uv sync` in the repo); derived root "
            f"{candidate} has no pyproject.toml"
        )
        raise RuntimeError(msg)
    return candidate


def _require_venv_script(root: Path) -> None:
    """Raise unless ``wywy-docs-serve`` exists in the checkout venv."""
    serve = root / ".venv" / "bin" / "wywy-docs-serve"
    if not serve.is_file():
        msg = (
            f"missing console script {serve}; run `uv sync` in {root} "
            "to create it before installing the service"
        )
        raise RuntimeError(msg)


def main() -> None:
    """Write the unit + environment files and print systemctl next steps."""
    root = _repo_root()
    _require_venv_script(root)

    config = _config_dir()
    unit_dir = config / "systemd" / "user"
    env_dir = config / "wywy-docs-mcp"

    unit_dir.mkdir(parents=True, exist_ok=True)
    env_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "wywy-docs-mcp.service").write_text(SYSTEMD_UNIT)
    (env_dir / "environment").write_text(f"WYWY_DOCS_DIR={root}\n")

    print(f"==> Wrote {unit_dir / 'wywy-docs-mcp.service'}")
    print(f"==> Wrote {env_dir / 'environment'}")
    print()
    print("Enable and start the service:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user reset-failed wywy-docs-mcp")
    print("  systemctl --user enable --now wywy-docs-mcp")
    print()
    print("Commands:")
    print("  systemctl --user status wywy-docs-mcp")
    print("  systemctl --user start wywy-docs-mcp")
    print("  systemctl --user stop wywy-docs-mcp")
    print("  journalctl --user -u wywy-docs-mcp -f")
    print()
    print("Note: user services only run while logged in.")
    print("To keep the service running after logout, enable linger:")
    print("  loginctl enable-linger")


if __name__ == "__main__":
    main()
