#!/bin/bash
set -euo pipefail

# Tracking API Install Script

INSTALL_DIR="/opt/tracking-api"
ENV_FILE="/etc/tracking-api.env"
SERVICE_FILE="/etc/systemd/system/tracking-api.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Tracking API Installation ==="

echo "Creating ${INSTALL_DIR}..."
sudo mkdir -p "${INSTALL_DIR}"

echo "Copying api.py..."
sudo cp "${SCRIPT_DIR}/api.py" "${INSTALL_DIR}/api.py"

echo "Creating Python venv..."
sudo python3 -m venv "${INSTALL_DIR}/venv"

if [ ! -f "${ENV_FILE}" ]; then
    echo "Installing ${ENV_FILE} (template)..."
    sudo cp "${SCRIPT_DIR}/tracking-api.env.example" "${ENV_FILE}"
    echo ""
    echo "!!! IMPORTANT: Edit ${ENV_FILE} and set your INFLUXDB_TOKEN !!!"
    echo ""
else
    echo "${ENV_FILE} already exists, not overwriting."
fi

echo "Installing systemd service..."
sudo cp "${SCRIPT_DIR}/tracking-api.service" "${SERVICE_FILE}"
sudo systemctl daemon-reload

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit ${ENV_FILE} and set INFLUXDB_TOKEN (+ restrict CORS_ORIGIN)"
echo "  2. sudo systemctl enable --now tracking-api"
echo "  3. sudo journalctl -u tracking-api -f"
echo ""
echo "Nginx snippet for reverse proxy:"
echo "  location /api/ {"
echo "      proxy_pass http://127.0.0.1:8787;"
echo "  }"
