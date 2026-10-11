#!/usr/bin/env bash
# Install "Shhh, I'm sleeping" as a systemd service.
# Run: sudo ./install.sh
set -euo pipefail

PREFIX=/opt/shhh-im-sleeping
CONFIG_DIR=/etc/shhh-im-sleeping
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root: sudo ./install.sh" >&2
    exit 1
fi

echo "Installing to $PREFIX ..."
install -d "$PREFIX"
install -m 0755 "$REPO_DIR/main.py" "$PREFIX/main.py"
install -d "$PREFIX/shhh_im_sleeping"
install -m 0644 "$REPO_DIR"/shhh_im_sleeping/*.py "$PREFIX/shhh_im_sleeping/"

# Keep an existing config; only create one the first time.
if [[ ! -f "$CONFIG_DIR/config.ini" ]]; then
    echo "Creating $CONFIG_DIR/config.ini from the example ..."
    install -d "$CONFIG_DIR"
    install -m 0644 "$REPO_DIR/config.ini.example" "$CONFIG_DIR/config.ini"
fi

echo "Installing the systemd unit ..."
install -m 0644 "$REPO_DIR/shhh-im-sleeping.service" /etc/systemd/system/shhh-im-sleeping.service

systemctl daemon-reload
systemctl enable shhh-im-sleeping.service
systemctl restart shhh-im-sleeping.service

echo
echo "Done. Check it with:"
echo "  systemctl status shhh-im-sleeping"
echo "  journalctl -u shhh-im-sleeping -f"
