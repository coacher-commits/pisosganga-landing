#!/usr/bin/env bash
# AlphaTon Monitor — instalador automático
# Uso: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/alphaton-monitor"
REPO="https://github.com/coacher-commits/pisosganga-landing.git"
BRANCH="claude/sec-alphaton-alerts-nfXed"
SERVICE_NAME="alphaton-monitor"

echo "==> Creando directorio $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

echo "==> Escribiendo credenciales"
cat > "$INSTALL_DIR/.env" << 'ENVEOF'
TELEGRAM_BOT_TOKEN=8600945320:AAF0RpFpj82WRrJSEFu39-sjj9JC4kHyuJk
TELEGRAM_CHAT_ID=335742617
ETHERSCAN_API_KEY=72HP2BJF551NDHK7G8S8G4S72MCS9X4FHY
SEC_CHECK_INTERVAL_SECS=3600
ETH_CHECK_INTERVAL_SECS=300
MIN_TOKEN_AMOUNT=500000
STATE_FILE=/opt/alphaton-monitor/state.json
ENVEOF

echo "==> Descargando archivos del repositorio"
apt-get install -y --quiet git python3-pip 2>/dev/null || true
git clone --depth 1 --branch "$BRANCH" "$REPO" /tmp/alphaton-repo 2>/dev/null || \
  (cd /tmp/alphaton-repo && git pull)
cp /tmp/alphaton-repo/sec_alphaton_monitor.py  "$INSTALL_DIR/"
cp /tmp/alphaton-repo/sec-alphaton-monitor.service /etc/systemd/system/${SERVICE_NAME}.service
rm -rf /tmp/alphaton-repo

echo "==> Instalando dependencias Python"
apt-get install -y python3-requests

echo "==> Configurando permisos"
chown -R root:root "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/.env"

echo "==> Activando servicio"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo ""
echo "✅ Instalación completada."
echo "   Ver logs: journalctl -fu $SERVICE_NAME"
