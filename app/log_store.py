import csv
import io
import json
import os
import re
import shutil
import sqlite3
import time
import urllib.request
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


BASE = Path(os.environ.get('TESSERA_SIM_BASE', '/var/lib/tessera-sim'))
LOG_DB = BASE / 'processor_logs.db'
RETENTION_SECONDS = 7 * 24 * 60 * 60
NAME_REFRESH_SECONDS = 10 * 60
PAUSED_BUFFER_SECONDS = 2 * 60 * 60


def now_epoch() -> float:
    return time.time()


def now_text(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_epoch(), timezone.utc).isoformat(timespec='seconds')


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
                processor_time TEXT,
                message_type TEXT,
                message TEXT NOT NULL,
                raw_message TEXT NOT NULL
            )
        """)
        columns = {row['name'] for row in conn.execute("PRAGMA table_info(processor_logs)").fetchall()}
        if 'processor_time' not in columns:
            conn.execute("ALTER TABLE processor_logs ADD COLUMN processor_time TEXT")
        if 'message_type' not in columns:
            conn.execute("ALTER TABLE processor_logs ADD COLUMN message_type TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processors (
                ip TEXT PRIMARY KEY,
                name TEXT,
                name_checked_epoch REAL DEFAULT 0,
                last_seen_epoch REAL DEFAULT 0,
                paused INTEGER DEFAULT 0,
                ignored INTEGER DEFAULT 0
            )
        """)
        processor_columns = {row['name'] for row in conn.execute("PRAGMA table_info(processors)").fetchall()}
        if 'paused' not in processor_columns:
            conn.execute("ALTER TABLE processors ADD COLUMN paused INTEGER DEFAULT 0")
        if 'ignored' not in processor_columns:
            conn.execute("ALTER TABLE processors ADD COLUMN ignored INTEGER DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paused_log_buffer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_epoch REAL NOT NULL,
                received_at TEXT NOT NULL,
                processor_ip TEXT NOT NULL,
                transport TEXT NOT NULL,
                priority INTEGER,
                facility INTEGER,
                severity INTEGER,
                processor_time TEXT,
                message_type TEXT,
                message TEXT NOT NULL,
                raw_message TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processor_logs_ip_epoch ON processor_logs(processor_ip, received_epoch)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processor_logs_epoch ON processor_logs(received_epoch)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paused_log_buffer_ip_epoch ON paused_log_buffer(processor_ip, received_epoch)")
        rows = conn.execute("""
            SELECT id, raw_message
            FROM processor_logs
            WHERE processor_time IS NULL OR message_type IS NULL
        """).fetchall()
        for row in rows:
            _, _, _, processor_time, message_type, message = parse_syslog_message(row['raw_message'] or '')
            conn.execute("""
                UPDATE processor_logs
                SET processor_time = ?, message_type = ?, message = ?
                WHERE id = ?
            """, (processor_time, message_type, message, row['id']))
        conn.execute("DELETE FROM paused_log_buffer WHERE received_epoch < ?", (now_epoch() - PAUSED_BUFFER_SECONDS,))


def parse_syslog_message(raw: str):
    msg = raw.strip('\x00\r\n')
    match = re.match(r'^<(\d{1,3})>(.*)$', msg, re.S)
    if not match:
        priority = facility = severity = None
        payload = msg
    else:
        priority = int(match.group(1))
        facility = priority // 8
        severity = priority % 8
        payload = match.group(2).strip()
    processor_time = ''
    time_match = re.match(r'^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(.*)$', payload, re.S)
    if time_match:
        processor_time = time_match.group(1)
        payload = time_match.group(2).strip()
    message_type = ''
    type_match = re.match(r'^(tessera|kernel):\s*(.*)$', payload, re.I | re.S)
    if not type_match:
        type_match = re.match(r'^\S+\s+(tessera|kernel):\s*(.*)$', payload, re.I | re.S)
    if type_match:
        message_type = type_match.group(1).lower()
        payload = type_match.group(2).strip()
    return priority, facility, severity, processor_time, message_type, payload


def prune_old_logs():
    init_log_db()
    cutoff = now_epoch() - RETENTION_SECONDS
    with connect() as conn:
        conn.execute("DELETE FROM processor_logs WHERE received_epoch < ?", (cutoff,))
        conn.execute("DELETE FROM paused_log_buffer WHERE received_epoch < ?", (now_epoch() - PAUSED_BUFFER_SECONDS,))


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
    priority, facility, severity, processor_time, message_type, message = parse_syslog_message(raw_message)
    ts = now_epoch()
    with connect() as conn:
        conn.execute("""
            INSERT INTO processors (ip, last_seen_epoch)
            VALUES (?, ?)
            ON CONFLICT(ip) DO UPDATE SET last_seen_epoch = excluded.last_seen_epoch
        """, (processor_ip, ts))
        processor = conn.execute("SELECT paused, ignored FROM processors WHERE ip = ?", (processor_ip,)).fetchone()
        if processor and int(processor['ignored'] or 0):
            return
        target = 'paused_log_buffer' if processor and int(processor['paused'] or 0) else 'processor_logs'
        conn.execute("""
            INSERT INTO {target} (
                received_epoch, received_at, processor_ip, transport,
                priority, facility, severity, processor_time, message_type,
                message, raw_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """.format(target=target), (ts, now_text(ts), processor_ip, transport, priority, facility, severity, processor_time, message_type, message, raw_message.strip('\x00\r\n')))
        conn.execute("DELETE FROM paused_log_buffer WHERE received_epoch < ?", (ts - PAUSED_BUFFER_SECONDS,))
    if int(ts) % 60 == 0:
        prune_old_logs()
    refresh_processor_name(processor_ip)
    try:
        from processor_discovery import remember_passive_processor
        remember_passive_processor(processor_ip, source='syslog')
    except Exception:
        pass


def list_processors():
    init_log_db()
    with connect() as conn:
        rows = conn.execute("""
            SELECT p.ip, COALESCE(p.name, p.ip) AS name, p.last_seen_epoch,
                   COALESCE(p.paused, 0) AS paused, COALESCE(p.ignored, 0) AS ignored,
                   COUNT(l.id) AS log_count,
                   (SELECT COUNT(*) FROM paused_log_buffer b WHERE b.processor_ip = p.ip) AS buffered_count
            FROM processors p
            LEFT JOIN processor_logs l ON l.processor_ip = p.ip
            GROUP BY p.ip
            ORDER BY LOWER(name), p.ip
        """).fetchall()
    return [dict(row) for row in rows]


def list_logs(processor_ip: str = '', limit: int = 500, search: str = '', after_id: int = 0, ascending: bool = False, severity: str = ''):
    init_log_db()
    limit = max(1, min(int(limit or 500), 5000))
    params: list[Any] = []
    clauses = []
    if processor_ip:
        clauses.append('l.processor_ip = ?')
        params.append(processor_ip)
    if severity != '':
        if str(severity) not in {str(i) for i in range(8)}:
            severity = ''
    if severity != '':
        clauses.append('l.severity = ?')
        params.append(int(severity))
    if search:
        clauses.append('l.message LIKE ?')
        params.append(f'%{search}%')
    if after_id:
        clauses.append('l.id > ?')
        params.append(int(after_id))
    where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
    params.append(limit)
    direction = 'ASC' if ascending else 'DESC'
    with connect() as conn:
        rows = conn.execute(f"""
            SELECT l.*, COALESCE(p.name, l.processor_ip) AS processor_name
            FROM processor_logs l
            LEFT JOIN processors p ON p.ip = l.processor_ip
            {where}
            ORDER BY l.id {direction}
            LIMIT ?
        """, params).fetchall()
    return [dict(row) for row in rows]


def clear_logs(processor_ip: str):
    init_log_db()
    with connect() as conn:
        conn.execute("DELETE FROM processor_logs WHERE processor_ip = ?", (processor_ip,))
        conn.execute("DELETE FROM paused_log_buffer WHERE processor_ip = ?", (processor_ip,))


def clear_all_logs():
    init_log_db()
    with connect() as conn:
        conn.execute("DELETE FROM processor_logs")
        conn.execute("DELETE FROM paused_log_buffer")


def set_processor_paused(processor_ip: str, paused: bool):
    init_log_db()
    with connect() as conn:
        conn.execute("""
            INSERT INTO processors (ip, last_seen_epoch, paused)
            VALUES (?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET paused = excluded.paused
        """, (processor_ip, now_epoch(), 1 if paused else 0))
        if not paused:
            conn.execute("DELETE FROM paused_log_buffer WHERE received_epoch < ?", (now_epoch() - PAUSED_BUFFER_SECONDS,))
            conn.execute("""
                INSERT INTO processor_logs (
                    received_epoch, received_at, processor_ip, transport,
                    priority, facility, severity, processor_time, message_type,
                    message, raw_message
                )
                SELECT received_epoch, received_at, processor_ip, transport,
                       priority, facility, severity, processor_time, message_type,
                       message, raw_message
                FROM paused_log_buffer
                WHERE processor_ip = ?
                ORDER BY received_epoch ASC
            """, (processor_ip,))
            conn.execute("DELETE FROM paused_log_buffer WHERE processor_ip = ?", (processor_ip,))


def set_processor_ignored(processor_ip: str, ignored: bool):
    init_log_db()
    with connect() as conn:
        conn.execute("""
            INSERT INTO processors (ip, last_seen_epoch, ignored, paused)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(ip) DO UPDATE SET
                ignored = excluded.ignored,
                paused = CASE WHEN excluded.ignored = 1 THEN 0 ELSE processors.paused END
        """, (processor_ip, now_epoch(), 1 if ignored else 0))
        if ignored:
            conn.execute("DELETE FROM paused_log_buffer WHERE processor_ip = ?", (processor_ip,))


def format_bytes(size: float) -> str:
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    value = float(size or 0)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1024


def log_storage_summary():
    init_log_db()
    db_bytes = LOG_DB.stat().st_size if LOG_DB.exists() else 0
    usage = shutil.disk_usage(str(BASE))
    with connect() as conn:
        total_payload = conn.execute("""
            SELECT COALESCE(SUM(LENGTH(raw_message) + LENGTH(message) + LENGTH(COALESCE(processor_time, ''))), 0)
            FROM processor_logs
        """).fetchone()[0] or 0
        rows = conn.execute("""
            SELECT p.ip, COALESCE(p.name, p.ip) AS name,
                   COALESCE(p.paused, 0) AS paused,
                   COALESCE(p.ignored, 0) AS ignored,
                   COUNT(l.id) AS log_count,
                   COALESCE(SUM(LENGTH(l.raw_message) + LENGTH(l.message) + LENGTH(COALESCE(l.processor_time, ''))), 0) AS payload_bytes,
                   MIN(l.received_epoch) AS first_epoch,
                   MAX(l.received_epoch) AS last_epoch,
                   (SELECT COUNT(*) FROM paused_log_buffer b WHERE b.processor_ip = p.ip) AS buffered_count
            FROM processors p
            LEFT JOIN processor_logs l ON l.processor_ip = p.ip
            GROUP BY p.ip
            ORDER BY LOWER(name), p.ip
        """).fetchall()
    processors = []
    for row in rows:
        payload = float(row['payload_bytes'] or 0)
        estimated = int(db_bytes * (payload / total_payload)) if total_payload else 0
        count = int(row['log_count'] or 0)
        first_epoch = row['first_epoch']
        last_epoch = row['last_epoch']
        span = (float(last_epoch) - float(first_epoch)) if first_epoch and last_epoch and last_epoch > first_epoch else 0
        per_minute = (count / (span / 60)) if span else float(count)
        processors.append({
            'ip': row['ip'],
            'name': row['name'] or row['ip'],
            'paused': bool(row['paused']),
            'ignored': bool(row['ignored']),
            'log_count': count,
            'buffered_count': int(row['buffered_count'] or 0),
            'estimated_bytes': estimated,
            'estimated_display': format_bytes(estimated),
            'messages_per_minute': per_minute,
        })
    return {
        'db_bytes': db_bytes,
        'db_display': format_bytes(db_bytes),
        'free_bytes': usage.free,
        'free_display': format_bytes(usage.free),
        'total_bytes': usage.total,
        'total_display': format_bytes(usage.total),
        'processors': processors,
    }


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
            SELECT l.received_at, l.processor_time,
                   COALESCE(p.name, l.processor_ip) AS processor_name,
                   l.processor_ip, l.message_type,
                   l.facility, l.severity, l.message, l.raw_message
            FROM processor_logs l
            LEFT JOIN processors p ON p.ip = l.processor_ip
            {where}
            ORDER BY l.received_epoch ASC
        """, params).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['received_at_utc', 'processor_time', 'processor_name', 'processor_ip', 'type', 'facility', 'severity', 'message', 'raw_message'])
    for row in rows:
        writer.writerow([row[k] for k in row.keys()])
    return out.getvalue()
