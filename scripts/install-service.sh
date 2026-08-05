#!/usr/bin/env bash
set -euo pipefail

SERVICE="wywy-docs-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/${SERVICE}.service"
TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${SERVICE}.service"
ENVIRONMENT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/wywy-docs-mcp"
ENVIRONMENT="$ENVIRONMENT_DIR/environment"

if [ -z "${WYWY_DOCS_DIR:-}" ]; then
	echo "Error: WYWY_DOCS_DIR must name the Wywy-Docs checkout"
	exit 1
fi

case "$WYWY_DOCS_DIR" in
/*) ;;
*)
	echo "Error: WYWY_DOCS_DIR must be an absolute directory"
	exit 1
	;;
esac

if [ ! -d "$WYWY_DOCS_DIR" ] || [ ! -x "$WYWY_DOCS_DIR/.venv/bin/python" ]; then
	echo "Error: WYWY_DOCS_DIR must contain executable .venv/bin/python"
	exit 1
fi

if [ ! -f "$SOURCE" ]; then
	echo "Error: $SOURCE not found"
	exit 1
fi

echo "==> Installing $SERVICE service..."
mkdir -p "$(dirname "$TARGET")"
cp "$SOURCE" "$TARGET"
mkdir -p "$ENVIRONMENT_DIR"
printf 'WYWY_DOCS_DIR=%s\n' "$WYWY_DOCS_DIR" >"$ENVIRONMENT"
systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE"
echo "==> Service $SERVICE started and enabled."
echo ""
echo "Note: User services only run while logged in."
echo "To keep the service running after logout, enable linger:"
echo "  loginctl enable-linger"

echo ""
echo "Commands:"
echo "  systemctl --user status $SERVICE"
echo "  systemctl --user start $SERVICE"
echo "  systemctl --user stop $SERVICE"
echo "  journalctl --user -u $SERVICE -f"
