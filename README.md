# Tessera Control and Monitoring

A local Brompton Tessera-style API simulator for developing and testing control clients without needing a physical processor on hand.

This project was built around the Tessera IP Control API 3.5.2 behavior and a real processor `/api` response sample. It is intended as a development/sandbox tool, not a Brompton-supported emulator.

## Features

- HTTP API rooted at `/api`
- Telnet-style TCP command service
- Home page at `/` for choosing the current tool
- API contents browser at `/api-contents`
- Processor syslog collection on UDP/TCP port `514`
- Processor log viewer at `/logs` with CSV export and per-processor clearing
- SX40 topology monitoring at `/topology`
- Persistent writable API state
- Endpoint validation for known datatypes, ranges and access rules
- Read-only enforcement for normal API clients
- God Mode web UI at `/god`
- God Mode editing of read-only values, except generated date/time, uptime and temperature values
- Preset save, recall, delete and import
- Non-destructive preset import from JSON
- Load preset from a live processor by IP address
- Live Read mode that polls a real processor and replaces the simulator's active API state on each successful poll
- File/bytearray endpoint handling, including `.cube` upload support for 3D LUT data
- Host-derived system time, uptime and CPU-temperature-backed temperature endpoints during normal SIM operation

## Install on Ubuntu 24.04 LXC

Run as root:

```bash
apt update
apt install -y git
cd /opt
git clone https://github.com/YOUR_USERNAME/tessera-api-simulator.git
cd tessera-api-simulator
./install.sh
```

Default ports:

- HTTP API, Home and God Mode: `80`
- TCP command socket: `23`
- Syslog collector: UDP/TCP `514`

Override ports during install:

```bash
PORT=8080 TCPPORT=3001 ./install.sh
```

## Updating an existing install

From a freshly pulled repo copy:

```bash
git pull
./scripts/update_existing_install.sh
```

This backs up the current app files under `/opt/tessera-sim/backups/` and restarts the services.

## Test

Run locally with an isolated runtime directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
TESSERA_SIM_BASE=.runtime PORT=8080 python app/tessera_sim.py
```

Then in another shell:

```bash
curl http://127.0.0.1:8080/api
curl http://127.0.0.1:8080/api/system/processor-type
curl http://127.0.0.1:8080/api/system/current-date-time
curl http://127.0.0.1:8080/api/system/temperature/cpu
```

Open the home page:

```text
http://127.0.0.1:8080/
```

Write a value through the normal API:

```bash
curl -X PUT http://127.0.0.1:8080/api/output/global-colour/brightness \
  -H 'Content-Type: application/json' \
  -d '{"data":5000}'
```

Open God Mode:

```text
http://127.0.0.1:8080/god
```

Run the smoke tests:

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests
```

## Preset behavior

- Save Preset snapshots the current SIM state except generated locked values.
- Recall Preset replaces the active SIM state. It does not merge branches or append `devices/items/*`.
- Load Preset From File stores the uploaded JSON as a preset only. It does not load that file into the active SIM until the preset is recalled.
- Delete Preset removes the preset after confirmation.

## Live Read behavior

Live Read Real Processor asks for a real processor IP and poll interval. On each successful poll, the simulator queries the real processor's `/api` endpoint and completely replaces the simulator's active API state with the returned data.

This is designed so your development client can read live processor data from the SIM without being able to write to the real processor. Any writes your client makes to the SIM will be overwritten on the next Live Read poll.

## Services

```bash
systemctl status tessera-sim.service
systemctl status tessera-sim-tcp.service
systemctl status tessera-sim-syslog.service
journalctl -u tessera-sim.service -f
journalctl -u tessera-sim-syslog.service -f
```

## Runtime paths

```text
/opt/tessera-sim              installed application
/var/lib/tessera-sim/state.json
/var/lib/tessera-sim/processor_logs.db
/var/lib/tessera-sim/topology_monitors.json
/var/lib/tessera-sim/files
/var/lib/tessera-sim/presets
```

## Processor Logs

Configure Tessera processors to send syslog to the server IP. The collector listens on both UDP and TCP port `514` and records which transport received each message.

Log entries are timestamped with the local server receive time because processor clocks are often wrong. The collector stores the sender IP and refreshes the processor display name from `http://PROCESSOR_IP/api/system/processor-name` every 10 minutes.

The `/logs` page shows received logs by processor, can export CSV for a chosen number of minutes back from the present, and can clear logs for an individual processor. Logs older than 7 days are pruned automatically.

## Topology Monitoring

The `/topology` page can monitor up to 20 processors. Each monitor stores the processor IP address and polling interval, defaulting to 10 seconds.

Topology polling only requests the endpoints required for this feature:

- `/api/system/processor-name`
- `/api/system/processor-type`
- `/api/output/network/cable-redundancy/loops/1/state`
- `/api/output/network/cable-redundancy/loops/2/state`

Only SX40 loop rendering is currently supported. Other processor types display "Loop monitoring not currently supported."

## Notes

This repository does not include Brompton documentation. Use the official Tessera IP Control API documentation as the reference for production client behavior.
