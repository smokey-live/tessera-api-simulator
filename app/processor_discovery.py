import random
import re
import socket
import threading
import time
import urllib.request
from typing import Any


SLP_MULTICAST = ('239.255.255.253', 427)
DISCOVERY_INTERVAL_SECONDS = 20
ONLINE_SECONDS = 90
API_CHECK_SECONDS = 60
SERVICE_TYPE = 'processor.tessera'
SCOPE = 'DEFAULT'
LANGUAGE = 'en'

DISCOVERY_LOCK = threading.RLock()
DISCOVERED: dict[str, dict[str, Any]] = {}
DISCOVERY_THREAD: threading.Thread | None = None


def now_epoch() -> float:
    return time.time()


def build_slp_header(function: int, xid: int, payload: bytes, flags: int = 0x2000) -> bytes:
    lang = LANGUAGE.encode('ascii')
    length = 14 + len(lang) + len(payload)
    return (
        bytes([2, function])
        + length.to_bytes(3, 'big')
        + flags.to_bytes(2, 'big')
        + b'\x00\x00\x00'
        + xid.to_bytes(2, 'big')
        + len(lang).to_bytes(2, 'big')
        + lang
        + payload
    )


def build_service_request(xid: int, previous_responders: list[str] | None = None) -> bytes:
    prlist = ','.join(previous_responders or []).encode('ascii')
    service = SERVICE_TYPE.encode('ascii')
    scope = SCOPE.encode('ascii')
    payload = (
        len(prlist).to_bytes(2, 'big') + prlist
        + len(service).to_bytes(2, 'big') + service
        + len(scope).to_bytes(2, 'big') + scope
        + b'\x00\x00'
        + b'\x00\x00'
    )
    return build_slp_header(1, xid, payload)


def build_attribute_request(xid: int, url: str) -> bytes:
    url_bytes = url.encode('ascii')
    scope = SCOPE.encode('ascii')
    payload = (
        b'\x00\x00'
        + len(url_bytes).to_bytes(2, 'big') + url_bytes
        + len(scope).to_bytes(2, 'big') + scope
        + b'\x00\x00'
        + b'\x00\x00'
    )
    return build_slp_header(6, xid, payload)


def parse_header(data: bytes) -> tuple[int, int, int, int, int, int] | None:
    if len(data) < 14 or data[0] != 2:
        return None
    function = data[1]
    length = int.from_bytes(data[2:5], 'big')
    xid = int.from_bytes(data[10:12], 'big')
    lang_len = int.from_bytes(data[12:14], 'big')
    offset = 14 + lang_len
    if length > len(data):
        return None
    return function, length, xid, lang_len, offset, int.from_bytes(data[5:7], 'big')


def parse_service_reply(data: bytes) -> list[str]:
    header = parse_header(data)
    if not header:
        return []
    function, length, _, _, offset, _ = header
    if function != 2 or offset + 4 > length:
        return []
    error = int.from_bytes(data[offset:offset + 2], 'big')
    count = int.from_bytes(data[offset + 2:offset + 4], 'big')
    offset += 4
    if error:
        return []
    urls = []
    for _ in range(count):
        if offset + 5 > length:
            break
        offset += 1
        offset += 2
        url_len = int.from_bytes(data[offset:offset + 2], 'big')
        offset += 2
        url = data[offset:offset + url_len].decode('utf-8', errors='replace')
        offset += url_len
        if offset < length:
            auth_count = data[offset]
            offset += 1
            if auth_count:
                break
        urls.append(url)
    return urls


def parse_attribute_reply(data: bytes) -> dict[str, str]:
    header = parse_header(data)
    if not header:
        return {}
    function, length, _, _, offset, _ = header
    if function != 7 or offset + 4 > length:
        return {}
    error = int.from_bytes(data[offset:offset + 2], 'big')
    attr_len = int.from_bytes(data[offset + 2:offset + 4], 'big')
    offset += 4
    if error or offset + attr_len > length:
        return {}
    attr_text = data[offset:offset + attr_len].decode('utf-8', errors='replace')
    return parse_attribute_list(attr_text)


def parse_attribute_list(attr_text: str) -> dict[str, str]:
    attrs = {}
    for entry in attr_text.strip().strip('()').split('),('):
        if '=' not in entry:
            continue
        key, value = entry.split('=', 1)
        attrs[key] = value
    return attrs


def ip_from_url(url: str) -> str:
    match = re.match(r'^service:processor\.tessera://([^/:]+)', url or '', re.I)
    return match.group(1) if match else ''


def check_api(ip: str, timeout: float = 0.8) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            f'http://{ip}/api/system/processor-name',
            headers={'Accept': 'application/json', 'User-Agent': 'tessera-control-and-monitoring'},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(512).decode('utf-8', errors='replace')
        return True, body
    except Exception as ex:
        return False, str(ex)


def remember_processor(ip: str, url: str, source_ip: str, attrs: dict[str, str] | None = None):
    ts = now_epoch()
    attrs = attrs or {}
    with DISCOVERY_LOCK:
        row = DISCOVERED.setdefault(ip, {'ip': ip})
        row.update({
            'ip': ip,
            'url': url or row.get('url') or f'service:processor.tessera://{ip}',
            'source_ip': source_ip,
            'last_seen_epoch': ts,
        })
        if attrs:
            row.setdefault('attributes', {}).update(attrs)
            row['username'] = attrs.get('username') or row.get('username') or ip
            row['project'] = attrs.get('project') or row.get('project') or ''
            row['serial'] = attrs.get('serial') or row.get('serial') or ''
            row['version'] = attrs.get('version') or row.get('version') or ''
            row['tcpport'] = attrs.get('tcpport') or row.get('tcpport') or ''


def refresh_api_state(ip: str, force: bool = False):
    ts = now_epoch()
    with DISCOVERY_LOCK:
        row = DISCOVERED.get(ip)
        if not row:
            return
        if not force and ts - float(row.get('api_checked_epoch') or 0) < API_CHECK_SECONDS:
            return
    available, detail = check_api(ip)
    with DISCOVERY_LOCK:
        row = DISCOVERED.get(ip)
        if row:
            row['api_available'] = available
            row['api_checked_epoch'] = ts
            row['api_error'] = '' if available else detail


def discovery_cycle():
    xid = random.randint(1, 65535)
    found_urls: dict[str, str] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.25)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.bind(('', 0))
        for _ in range(3):
            sock.sendto(build_service_request(xid), SLP_MULTICAST)
            deadline = now_epoch() + 0.45
            while now_epoch() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                for url in parse_service_reply(data):
                    ip = ip_from_url(url)
                    if ip:
                        found_urls[ip] = url
                        remember_processor(ip, url, addr[0])
            time.sleep(0.1)
        for ip, url in list(found_urls.items()):
            attr_xid = random.randint(1, 65535)
            sock.sendto(build_attribute_request(attr_xid, url), SLP_MULTICAST)
            deadline = now_epoch() + 0.35
            while now_epoch() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                attrs = parse_attribute_reply(data)
                if attrs:
                    remember_processor(ip, url, addr[0], attrs)
                    break
            refresh_api_state(ip)


def discovery_loop():
    while True:
        try:
            discovery_cycle()
        except Exception:
            pass
        time.sleep(DISCOVERY_INTERVAL_SECONDS)


def ensure_discovery_running():
    global DISCOVERY_THREAD
    with DISCOVERY_LOCK:
        if DISCOVERY_THREAD and DISCOVERY_THREAD.is_alive():
            return
        DISCOVERY_THREAD = threading.Thread(target=discovery_loop, name='processor-discovery', daemon=True)
        DISCOVERY_THREAD.start()


def list_discovered_processors() -> list[dict[str, Any]]:
    ensure_discovery_running()
    cutoff = now_epoch() - ONLINE_SECONDS
    with DISCOVERY_LOCK:
        rows = [dict(row) for row in DISCOVERED.values() if float(row.get('last_seen_epoch') or 0) >= cutoff]
    for row in rows:
        refresh_api_state(row['ip'])
    with DISCOVERY_LOCK:
        rows = [dict(row) for row in DISCOVERED.values() if float(row.get('last_seen_epoch') or 0) >= cutoff]
    rows.sort(key=lambda row: tuple(int(part) if part.isdigit() else 999 for part in row['ip'].split('.')))
    return rows
