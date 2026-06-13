import csv
import io
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


BASE = Path(os.environ.get('TESSERA_SIM_BASE', '/var/lib/tessera-sim'))
LOG_DB = BASE / 'processor_logs.db'
RETENTION_SECONDS = 7 * 24 * 60 * 60
NAME_REFRESH_SECONDS = 10 * 60


def now_epoch() -> float:
    return time.time()


def now_text(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_epoch()).astimezone().isoformat(timespec='seconds')


def connect():
    BASE.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LOG_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_log_db():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processor_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_epoch REAL NOT NULL,
                received_at TEXT NOT NULL,
                processor_ip TEXT NOT NULL,
                transport TEXT NOT NULL,
                priority INTEGER,
                facility INTEGER,
                severity INTEGER,
                message TEXT NOT NULL,
                raw_message TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processors (
                ip TEXT PRIMARY KEY,
                name TEXT,
                name_checked_epoch REAL DEFAULT 0,
                last_seen_epoch REAL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processor_logs_ip_epoch ON processor_logs(processor_ip, received_epoch)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processor_logs_epoch ON processor_logs(received_epoch)")


def parse_syslog_message(raw: str):
    msg = raw.strip('\x00\r\n')
    match = re.match(r'^<(\d{1,3})>(.*)$', msg, re.S)
    if not match:
        return None, None, None, msg
    priority = int(match.group(1))
    return priority, priority // 8, priority % 8, match.group(2).strip()


def prune_old_logs():
    init_log_db()
    cutoff = now_epoch() - RETENTION_SECONDS
    with connect() as conn:
        conn.execute("DELETE FROM processor_logs WHERE received_epoch < ?", (cutoff,))


def processor_name_is_stale(row: sqlite3.Row | None) -> bool:
    if row is None:
        return True
    return now_epoch() - float(row['name_checked_epoch'] or 0) >= NAME_REFRESH_SECONDS


def fetch_processor_name(ip: str, timeout: float = 2.0) -> str | None:
    req = urllib.request.Request(
        f'http://{ip}/api/system/processor-name',
        headers={'Accept': 'application/json', 'User-Agent': 'tessera-control-and-monitoring'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        obj: Any = json.loads(response.read().decode('utf-8'))
    if isinstance(obj, dict):
        name = obj.get('processor-name') or obj.get('data')
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(obj, str) and obj.strip():
        return obj.strip()
    return None


def refresh_processor_name(ip: str, force: bool = False) -> str:
    init_log_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM processors WHERE ip = ?", (ip,)).fetchone()
        if not force and not processor_name_is_stale(row):
            return row['name'] or ip
    name = None
    try:
        name = fetch_processor_name(ip)
    except Exception:
        pass
    checked = now_epoch()
    with connect() as conn:
        existing = conn.execute("SELECT name FROM processors WHERE ip = ?", (ip,)).fetchone()
        final_name = name or (existing['name'] if existing and existing['name'] else ip)
        conn.execute("""
            INSERT INTO processors (ip, name, name_checked_epoch, last_seen_epoch)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                name = excluded.name,
                name_checked_epoch = excluded.name_checked_epoch
        """, (ip, final_name, checked, checked))
    return final_name


def record_log(processor_ip: str, transport: str, raw_message: str):
    init_log_db()
    priority, facility, severity, message = parse_syslog_message(raw_message)
    ts = now_epoch()
    with connect() as conn:
        conn.execute("""
            INSERT INTO processors (ip, last_seen_epoch)
            VALUES (?, ?)
            ON CONFLICT(ip) DO UPDATE SET last_seen_epoch = excluded.last_seen_epoch
        """, (processor_ip, ts))
        conn.execute("""
            INSERT INTO processor_logs (
                received_epoch, received_at, processor_ip, transport,
                priority, facility, severity, message, raw_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ts, now_text(ts), processor_ip, transport, priority, facility, severity, message, raw_message.strip('\x00\r\n')))
    if int(ts) % 60 == 0:
        prune_old_logs()
    refresh_processor_name(processor_ip)


def list_processors():
    init_log_db()
    with connect() as conn:
        rows = conn.execute("""
            SELECT p.ip, COALESCE(p.name, p.ip) AS name, p.last_seen_epoch, COUNT(l.id) AS log_count
            FROM processors p
            JOIN processor_logs l ON l.processor_ip = p.ip
            GROUP BY p.ip
            ORDER BY LOWER(name), p.ip
        """).fetchall()
    return [dict(row) for row in rows]


def list_logs(processor_ip: str = '', limit: int = 500):
    init_log_db()
    limit = max(1, min(int(limit or 500), 5000))
    params: list[Any] = []
    where = ''
    if processor_ip:
        where = 'WHERE l.processor_ip = ?'
        params.append(processor_ip)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(f"""
            SELECT l.*, COALESCE(p.name, l.processor_ip) AS processor_name
            FROM processor_logs l
            LEFT JOIN processors p ON p.ip = l.processor_ip
            {where}
            ORDER BY l.received_epoch DESC
            LIMIT ?
        """, params).fetchall()
    return [dict(row) for row in rows]


def clear_logs(processor_ip: str):
    init_log_db()
    with connect() as conn:
        conn.execute("DELETE FROM processor_logs WHERE processor_ip = ?", (processor_ip,))
        conn.execute("DELETE FROM processors WHERE ip = ? AND NOT EXISTS (SELECT 1 FROM processor_logs WHERE processor_ip = ?)", (processor_ip, processor_ip))


def export_logs_csv(minutes_back: int, processor_ip: str = '') -> str:
    init_log_db()
    minutes_back = max(1, min(int(minutes_back or 60), 60 * 24 * 7))
    cutoff = now_epoch() - (minutes_back * 60)
    params: list[Any] = [cutoff]
    where = 'WHERE l.received_epoch >= ?'
    if processor_ip:
        where += ' AND l.processor_ip = ?'
        params.append(processor_ip)
    with connect() as conn:
        rows = conn.execute(f"""
            SELECT l.received_at, COALESCE(p.name, l.processor_ip) AS processor_name,
                   l.processor_ip, l.transport, l.facility, l.severity,
                   l.message, l.raw_message
            FROM processor_logs l
            LEFT JOIN processors p ON p.ip = l.processor_ip
            {where}
            ORDER BY l.received_epoch ASC
        """, params).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['received_at', 'processor_name', 'processor_ip', 'transport', 'facility', 'severity', 'message', 'raw_message'])
    for row in rows:
        writer.writerow([row[k] for k in row.keys()])
    return out.getvalue()
