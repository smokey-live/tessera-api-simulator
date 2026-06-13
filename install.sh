#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP=/opt/tessera-sim
DATA=/var/lib/tessera-sim
PORT=${PORT:-80}
TCPPORT=${TCPPORT:-23}
SYSLOGPORT=${SYSLOGPORT:-514}
USER_NAME=tessera-sim

apt update
apt install -y python3 python3-venv python3-pip curl

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home "$DATA" --shell /usr/sbin/nologin "$USER_NAME"
fi

mkdir -p "$APP" "$DATA/files" "$DATA/presets"
cp -r "$SCRIPT_DIR"/app/* "$APP/"
chown -R "$USER_NAME:$USER_NAME" "$DATA"

python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --upgrade pip
"$APP/venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

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

cat >/etc/systemd/system/tessera-sim-tcp.service <<EOT
[Unit]
Description=Tessera Control and Monitoring Telnet-style TCP Service
After=network-online.target tessera-sim.service
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
Environment=TESSERA_SIM_BASE=$DATA
Environment=TESSERA_TCP_PORT=$TCPPORT
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/tcp_server.py
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
systemctl enable --now tessera-sim.service tessera-sim-tcp.service tessera-sim-syslog.service

echo "Installed. Home: http://YOUR_LXC_IP:${PORT}/  API: http://YOUR_LXC_IP:${PORT}/api  God Mode: http://YOUR_LXC_IP:${PORT}/god  TCP: port ${TCPPORT}  Syslog: UDP/TCP ${SYSLOGPORT}"
