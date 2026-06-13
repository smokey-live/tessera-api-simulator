#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
APP=/opt/tessera-sim
DATA=/var/lib/tessera-sim
USER_NAME=tessera-sim
PORT=${PORT:-80}
SYSLOGPORT=${SYSLOGPORT:-514}
if [ ! -d "$APP" ]; then
  echo "ERROR: $APP not found. Run ./install.sh first."
  exit 1
fi
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home "$DATA" --shell /usr/sbin/nologin "$USER_NAME"
fi
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p "$APP/backups/$TS"
cp -a "$APP"/*.py "$APP"/*.json "$APP/backups/$TS/" 2>/dev/null || true
cp -r "$SCRIPT_DIR"/app/* "$APP/"
"$APP/venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
mkdir -p "$DATA/files" "$DATA/presets"
chown -R tessera-sim:tessera-sim "$DATA" 2>/dev/null || true

cat >/etc/systemd/system/tessera-sim.service <<EOT
[Unit]
Description=Tessera Control and Monitoring HTTP Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
Environment=TESSERA_SIM_BASE=$DATA
Environment=PORT=$PORT
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/tessera_sim.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOT

cat >/etc/systemd/system/tessera-sim-syslog.service <<EOT
[Unit]
Description=Tessera Control and Monitoring Syslog Collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
Environment=TESSERA_SIM_BASE=$DATA
Environment=TESSERA_SYSLOG_PORT=$SYSLOGPORT
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/syslog_server.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOT

systemctl daemon-reload
systemctl enable --now tessera-sim-syslog.service
systemctl restart tessera-sim.service tessera-sim-tcp.service tessera-sim-syslog.service
systemctl --no-pager --full status tessera-sim.service || true
echo "Updated existing install. Backed up previous app files to $APP/backups/$TS"
