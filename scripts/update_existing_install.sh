#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
APP=/opt/tessera-sim
DATA=/var/lib/tessera-sim
if [ ! -d "$APP" ]; then
  echo "ERROR: $APP not found. Run ./install.sh first."
  exit 1
fi
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p "$APP/backups/$TS"
cp -a "$APP"/*.py "$APP"/*.json "$APP/backups/$TS/" 2>/dev/null || true
cp -r "$SCRIPT_DIR"/app/* "$APP/"
"$APP/venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
mkdir -p "$DATA/files" "$DATA/presets"
chown -R tessera-sim:tessera-sim "$DATA" 2>/dev/null || true
systemctl daemon-reload
systemctl restart tessera-sim.service tessera-sim-tcp.service
systemctl --no-pager --full status tessera-sim.service || true
echo "Updated existing install. Backed up previous app files to $APP/backups/$TS"
