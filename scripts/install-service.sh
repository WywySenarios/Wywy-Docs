#!/usr/bin/env bash
set -euo pipefail

SERVICE="wywy-docs-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/${SERVICE}.service"
TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${SERVICE}.service"

if [ ! -f "$SOURCE" ]; then
	echo "Error: $SOURCE not found"
	exit 1
fi

echo "==> Installing $SERVICE service..."
mkdir -p "$(dirname "$TARGET")"
cp "$SOURCE" "$TARGET"
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
