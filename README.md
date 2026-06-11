# Tessera API Simulator

A local Brompton Tessera-style API simulator for developing and testing control clients without needing a physical processor on hand.

This project was built around the Tessera IP Control API 3.5.2 behavior and a real processor `/api` response sample. It is intended as a development/sandbox tool, not a Brompton-supported emulator.

## Features

- HTTP API rooted at `/api`
- Telnet-style TCP command service
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

- HTTP API and God Mode: `80`
- TCP command socket: `3000`

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

```bash
curl http://YOUR_LXC_IP/api
curl http://YOUR_LXC_IP/api/system/processor-type
curl http://YOUR_LXC_IP/api/system/current-date-time
curl http://YOUR_LXC_IP/api/system/temperature/cpu
```

Write a value through the normal API:

```bash
curl -X PUT http://YOUR_LXC_IP/api/output/global-colour/brightness \
  -H 'Content-Type: application/json' \
  -d '{"data":5000}'
```

Open God Mode:

```text
http://YOUR_LXC_IP/god
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
journalctl -u tessera-sim.service -f
```

## Runtime paths

```text
/opt/tessera-sim              installed application
/var/lib/tessera-sim/state.json
/var/lib/tessera-sim/files
/var/lib/tessera-sim/presets
```

## Notes

This repository does not include Brompton documentation. Use the official Tessera IP Control API documentation as the reference for production client behavior.
