#!/bin/bash
set -euo pipefail

# AIS Collector Install Script
# Installs the AIS-Catcher → InfluxDB collector as a systemd service

INSTALL_DIR="/opt/ais-collector"
ENV_FILE="/etc/ais-collector.env"
SERVICE_FILE="/etc/systemd/system/ais-collector.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== AIS Collector Installation ==="

# Create install directory
echo "Creating ${INSTALL_DIR}..."
sudo mkdir -p "${INSTALL_DIR}"

# Copy collector script
echo "Copying collector.py..."
sudo cp "${SCRIPT_DIR}/collector.py" "${INSTALL_DIR}/collector.py"

# Create venv
echo "Creating Python venv..."
sudo python3 -m venv "${INSTALL_DIR}/venv"

# No pip dependencies needed - collector uses only stdlib

# Install env file (don't overwrite existing)
if [ ! -f "${ENV_FILE}" ]; then
    echo "Installing ${ENV_FILE} (template)..."
    sudo cp "${SCRIPT_DIR}/ais-collector.env.example" "${ENV_FILE}"
    echo ""
    echo "!!! IMPORTANT: Edit ${ENV_FILE} and set your INFLUXDB_TOKEN !!!"
    echo ""
else
    echo "${ENV_FILE} already exists, not overwriting."
fi

# Install systemd service
echo "Installing systemd service..."
sudo cp "${SCRIPT_DIR}/ais-collector.service" "${SERVICE_FILE}"
sudo systemctl daemon-reload

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit ${ENV_FILE} and set your INFLUXDB_TOKEN"
echo "  2. sudo systemctl enable --now ais-collector"
echo "  3. sudo journalctl -u ais-collector -f"
