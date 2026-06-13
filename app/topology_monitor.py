import json
import os
import re
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


BASE = Path(os.environ.get('TESSERA_SIM_BASE', '/var/lib/tessera-sim'))
TOPOLOGY_FILE = BASE / 'topology_monitors.json'
MAX_MONITORS = 20
DEFAULT_INTERVAL = 10
SUPPORTED_TYPE = 'sx40'


def now_epoch() -> float:
    return time.time()


def now_text(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_epoch()).astimezone().isoformat(timespec='seconds')


def html_escape(s: Any) -> str:
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


def normalize_ip(ip: str) -> str:
    value = (ip or '').strip().replace('http://', '').replace('https://', '').strip('/')
    if '/' in value:
        value = value.split('/', 1)[0]
    return value


def load_config():
    BASE.mkdir(parents=True, exist_ok=True)
    if not TOPOLOGY_FILE.exists():
        return {'monitors': []}
    try:
        with TOPOLOGY_FILE.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get('monitors'), list):
            return data
    except Exception:
        pass
    return {'monitors': []}


def save_config(config):
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = TOPOLOGY_FILE.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(config, handle, indent=2)
    tmp.replace(TOPOLOGY_FILE)


def list_monitors():
    return load_config().get('monitors', [])


def add_monitor(ip: str, interval: int = DEFAULT_INTERVAL):
    ip = normalize_ip(ip)
    if not ip:
        raise ValueError('Missing processor IP address')
    interval = max(1, int(interval or DEFAULT_INTERVAL))
    config = load_config()
    monitors = config.setdefault('monitors', [])
    for monitor in monitors:
        if monitor.get('ip') == ip:
            monitor['interval'] = interval
            save_config(config)
            update_monitor(monitor['id'])
            return monitor
    if len(monitors) >= MAX_MONITORS:
        raise ValueError(f'Maximum of {MAX_MONITORS} processors can be monitored')
    monitor = {
        'id': uuid.uuid4().hex[:12],
        'ip': ip,
        'interval': interval,
        'name': ip,
        'processor_type': '',
        'loop1_state': '',
        'loop2_state': '',
        'last_poll_epoch': 0,
        'last_poll_at': '',
        'last_status': 'pending',
        'last_error': '',
    }
    monitors.append(monitor)
    save_config(config)
    update_monitor(monitor['id'])
    return monitor


def remove_monitor(monitor_id: str):
    config = load_config()
    config['monitors'] = [m for m in config.get('monitors', []) if m.get('id') != monitor_id]
    save_config(config)


def reorder_monitors(monitor_ids):
    config = load_config()
    monitors = config.get('monitors', [])
    by_id = {m.get('id'): m for m in monitors}
    ordered = [by_id[mid] for mid in monitor_ids if mid in by_id]
    ordered.extend([m for m in monitors if m.get('id') not in monitor_ids])
    config['monitors'] = ordered
    save_config(config)


def fetch_endpoint(ip: str, path: str, timeout: float = 4.0):
    req = urllib.request.Request(
        f'http://{ip}{path}',
        headers={'Accept': 'application/json', 'User-Agent': 'tessera-control-and-monitoring'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        obj = json.loads(response.read().decode('utf-8'))
    key = path.strip('/').split('/')[-1]
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        if 'data' in obj:
            return obj['data']
    return obj


def update_monitor(monitor_id: str):
    config = load_config()
    changed = False
    for monitor in config.get('monitors', []):
        if monitor.get('id') != monitor_id:
            continue
        ts = now_epoch()
        try:
            ip = monitor['ip']
            name = fetch_endpoint(ip, '/api/system/processor-name')
            processor_type = fetch_endpoint(ip, '/api/system/processor-type')
            monitor['name'] = str(name or ip)
            monitor['processor_type'] = str(processor_type or '').lower()
            if monitor['processor_type'] == SUPPORTED_TYPE:
                monitor['loop1_state'] = fetch_endpoint(ip, '/api/output/network/cable-redundancy/loops/1/state') or ''
                monitor['loop2_state'] = fetch_endpoint(ip, '/api/output/network/cable-redundancy/loops/2/state') or ''
            else:
                monitor['loop1_state'] = ''
                monitor['loop2_state'] = ''
            monitor['last_status'] = 'ok'
            monitor['last_error'] = ''
        except Exception as ex:
            monitor['last_status'] = 'error'
            monitor['last_error'] = str(ex)
        monitor['last_poll_epoch'] = ts
        monitor['last_poll_at'] = now_text(ts)
        changed = True
        break
    if changed:
        save_config(config)


def poll_due_monitors():
    config = load_config()
    due = []
    current = now_epoch()
    for monitor in config.get('monitors', []):
        interval = max(1, int(monitor.get('interval') or DEFAULT_INTERVAL))
        if current - float(monitor.get('last_poll_epoch') or 0) >= interval:
            due.append(monitor.get('id'))
    for monitor_id in due:
        update_monitor(monitor_id)


def parse_loop_state(raw: Any):
    if raw is None:
        return []
    text = str(raw).strip()
    if not text or text.lower() in ('null', 'none'):
        return []
    entries = []
    pattern = re.compile(r'(loop-found|no-loop-found|incorrect-loop-found|one-to-many-error)\s*:\s*([A-D]\d{1,2})(?:\s*->\s*([A-D]\d{1,2}))?', re.I)
    for match in pattern.finditer(text):
        entries.append({
            'state': match.group(1).lower(),
            'start': match.group(2).upper(),
            'end': (match.group(3) or '').upper(),
        })
    return entries


def port_number(port: str) -> int | None:
    match = re.match(r'^[A-D](\d{1,2})$', port or '')
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 10 else None


def expected_mate(start: str) -> str:
    if not start:
        return ''
    letter = start[0]
    number = start[1:]
    return {'A': 'B', 'B': 'A', 'C': 'D', 'D': 'C'}.get(letter, '') + number


def topology_svg(monitor):
    name = html_escape(monitor.get('name') or monitor.get('ip') or 'Processor')
    marker_suffix = re.sub(r'[^a-zA-Z0-9_-]+', '-', str(monitor.get('id') or monitor.get('ip') or 'processor'))
    ok_marker = f'arrow-ok-{marker_suffix}'
    bad_marker = f'arrow-bad-{marker_suffix}'
    processor_type = str(monitor.get('processor_type') or '').lower()
    unsupported = processor_type and processor_type != SUPPORTED_TYPE
    loop1 = parse_loop_state(monitor.get('loop1_state'))
    loop2 = parse_loop_state(monitor.get('loop2_state'))
    show_cd = bool(loop2)
    view_w = 640
    view_h = 492 if show_cd else 292
    frame_h = view_h - 24
    rows = {'A': 78, 'B': 176, 'C': 286, 'D': 384} if show_cd else {'A': 78, 'B': 176}
    x0 = 68
    gap = 54
    row_box_x = 36
    row_box_w = 568
    row_box_h = 42

    def x_for(port):
        number = port_number(port)
        return x0 + (number - 1) * gap if number else None

    row_markup = []
    for letter in ('A', 'B', 'C', 'D') if show_cd else ('A', 'B'):
        y = rows[letter]
        labels = []
        for number in range(1, 11):
            labels.append(f'<text x="{x0 + (number - 1) * gap}" y="{y + 29}" text-anchor="middle">{letter}{number}</text>')
        row_markup.append(f'<rect class="port-row" x="{row_box_x}" y="{y}" width="{row_box_w}" height="{row_box_h}"/>{"".join(labels)}')

    arrows = []
    if not unsupported:
        for loop in loop1 + loop2:
            start = loop['start']
            end = loop['end']
            start_x = x_for(start)
            if start_x is None or start[0] not in rows:
                continue
            start_letter = start[0]
            good = loop['state'] == 'loop-found' and end == expected_mate(start)
            css = 'ok' if good else 'bad'
            if end and x_for(end) is not None and end[0] in rows:
                end_x = x_for(end)
                y1 = rows[start_letter] + row_box_h
                y2 = rows[end[0]]
                marker = f'marker-start="url(#{ok_marker})" marker-end="url(#{ok_marker})"' if good else f'marker-end="url(#{bad_marker})"'
                arrows.append(f'<line class="arrow {css}" x1="{start_x}" y1="{y1}" x2="{end_x}" y2="{y2}" {marker}/>')
            else:
                y1 = rows[start_letter] + row_box_h
                direction = 48 if start_letter in ('A', 'C') else -48
                y2 = y1 + direction
                marker = f'marker-start="url(#{ok_marker})" marker-end="url(#{ok_marker})"' if good else f'marker-end="url(#{bad_marker})"'
                arrows.append(f'<line class="arrow {css}" x1="{start_x}" y1="{y1}" x2="{start_x}" y2="{y2}" {marker}/>')

    status = ''
    if unsupported:
        label = html_escape(monitor.get('processor_type') or 'unknown')
        status = f'<text class="unsupported" x="{view_w / 2}" y="{view_h / 2}" text-anchor="middle">Loop monitoring not currently supported for {label}</text>'
    elif monitor.get('last_status') == 'error':
        status = f'<text class="unsupported" x="{view_w / 2}" y="{view_h / 2}" text-anchor="middle">{html_escape(monitor.get("last_error") or "Polling error")}</text>'

    return f"""<svg class="topology-svg" viewBox="0 0 {view_w} {view_h}" role="img" aria-label="Topology for {name}">
<defs>
  <marker id="{ok_marker}" markerWidth="7" markerHeight="7" refX="3.5" refY="3.5" orient="auto-start-reverse"><path d="M0,0 L7,3.5 L0,7 Z" fill="#00ff30"/></marker>
  <marker id="{bad_marker}" markerWidth="7" markerHeight="7" refX="3.5" refY="3.5" orient="auto-start-reverse"><path d="M0,0 L7,3.5 L0,7 Z" fill="#ff2828"/></marker>
</defs>
<rect class="frame" x="12" y="12" width="{view_w - 24}" height="{frame_h}"/>
<text class="title" x="{view_w / 2}" y="48" text-anchor="middle">{name}</text>
{''.join(row_markup)}
{''.join(arrows)}
{status}
</svg>"""
