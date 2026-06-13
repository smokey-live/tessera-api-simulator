# Architecture

The simulator is a FastAPI application that serves a Tessera-like API tree under `/api` and an admin interface under `/god`.

Runtime data lives in `/var/lib/tessera-sim`:

- `state.json` - active simulator API tree
- `files/` - uploaded bytearray/file endpoint content
- `presets/` - saved preset JSON snapshots with name and notes
- `live_read.json` - Live Read status/configuration
- `processor_logs.db` - SQLite store for processor syslog messages and cached processor names

Application files live in `/opt/tessera-sim`:

- `tessera_sim.py` - HTTP API, God Mode UI, presets, file upload, Live Read
- `tcp_server.py` - telnet-style TCP command server
- `syslog_server.py` - UDP/TCP syslog collector for processor logs
- `log_store.py` - SQLite storage, retention and processor-name caching for logs
- `endpoints.json` - endpoint metadata, access specifiers, ranges and datatypes
- `default_state.json` - seeded processor-style default state

## Locked endpoints

God Mode cannot manually edit these values because they are generated from the host system during normal SIM operation:

- `system/current-date-time`
- `system/uptime`
- anything under `system/temperature`

Live Read mode intentionally replaces the whole active state with the full `/api` response from a real processor every polling cycle, including date/time and temperature values from the live processor.

## Presets

Saving a preset snapshots the current state except locked generated values. Recalling a preset replaces the active SIM state rather than merging with it. Loading a preset from a file is non-destructive and only stores the uploaded JSON as a recallable preset.

## Live Read Real Processor

Live Read is a safety buffer for API-client development. The simulator polls a real Tessera processor, replaces its own active API state with that data, and your client talks only to the simulator. Writes from the client never reach the real processor and are overwritten again on the next successful Live Read poll.

## Processor Logs

The syslog collector listens on UDP and TCP port 514. Incoming messages are stored with the server receive time, sender IP address and transport because processor clocks may be wrong and different processors may send logs to the same collector.

Processor names are cached from `/api/system/processor-name` on each sender IP and refreshed every 10 minutes. Logs older than 7 days are deleted by the collector.
