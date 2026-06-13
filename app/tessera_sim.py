#!/usr/bin/env python3
import asyncio, base64, hashlib, json, os, re, time, socket, uuid, urllib.request, urllib.error
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, FileResponse, HTMLResponse, RedirectResponse
from log_store import clear_logs, export_logs_csv, list_logs, list_processors, refresh_processor_name
from topology_monitor import add_monitor, list_monitors, poll_due_monitors, remove_monitor, reorder_monitors, topology_svg

BASE = Path(os.environ.get('TESSERA_SIM_BASE','/var/lib/tessera-sim'))
APPDIR = Path(__file__).resolve().parent
STATE_FILE = BASE/'state.json'
FILES_DIR = BASE/'files'
PRESETS_DIR = BASE/'presets'
LIVE_FILE = BASE/'live_read.json'
START = time.time()

APP_NAME = 'Tessera Control and Monitoring'

app = FastAPI(title=APP_NAME, version='3.5.2')

NAV_CSS = ".nav{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.nav a{color:#8cc7ff;text-decoration:none}.nav a:hover{text-decoration:underline}.nav .active{color:#fff;font-weight:700}"

def page_nav(active: str = '') -> str:
    items = [
        ('Home', '/'),
        ('API Contents', '/api-contents'),
        ('God Mode', '/god'),
        ('Processor Logs', '/logs'),
        ('Topology Monitoring', '/topology'),
    ]
    links = []
    for label, href in items:
        klass = ' class="active"' if label == active else ''
        links.append(f'<a{klass} href="{href}">{label}</a>')
    return '<nav class="nav">' + ' · '.join(links) + '</nav>'

def load_json(p):
    with open(p,'r',encoding='utf-8') as f: return json.load(f)
ENDPOINTS = load_json(APPDIR/'endpoints.json')
META_BY_PATH = {e['path'].strip('/').lower(): e for e in ENDPOINTS}
REGEX_META=[]
for e in ENDPOINTS:
    pat='^'+re.escape(e['path'].strip('/').lower())+'$'
    pat=pat.replace(re.escape('{number}'), r'([^/]+)').replace(re.escape('{serial}'), r'([^/]+)')
    pat=pat.replace(re.escape('{hdmi-port-number}'), r'([^/]+)').replace(re.escape('{sdi-port-number}'), r'([^/]+)')
    pat=pat.replace(re.escape('{dvi-port-number}'), r'([^/]+)').replace(re.escape('{panel-type}'), r'([^/]+)')
    pat=pat.replace(re.escape('{loop-number}'), r'([^/]+)').replace(re.escape('{frame}'), r'([^/]+)')
    pat=pat.replace(re.escape('{frame-user-number}'), r'([^/]+)')
    REGEX_META.append((re.compile(pat),e))

def init_state():
    BASE.mkdir(parents=True, exist_ok=True); FILES_DIR.mkdir(parents=True, exist_ok=True); PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        state = load_json(APPDIR/'default_state.json')
        with open(STATE_FILE,'w',encoding='utf-8') as f: json.dump(state,f,indent=2)

def read_state():
    init_state()
    with open(STATE_FILE,'r',encoding='utf-8') as f: return json.load(f)

def write_state(state):
    tmp=STATE_FILE.with_suffix('.tmp')
    with open(tmp,'w',encoding='utf-8') as f: json.dump(state,f,indent=2)
    tmp.replace(STATE_FILE)

def norm_parts(path):
    path=(path or '').strip('/').lower()
    return [] if not path else [p for p in path.split('/') if p]

def find_key(d, key):
    if not isinstance(d, dict): return None
    lk=key.lower()
    for k in d.keys():
        if str(k).lower()==lk: return k
    return None

def get_node(state, parts):
    node=state.get('api', state)
    if parts and parts[0].lower()=='api': parts=parts[1:]
    for p in parts:
        k=find_key(node,p)
        if k is None: raise KeyError('/'.join(parts))
        node=node[k]
    return node

def set_node(state, parts, value):
    node=state.get('api', state)
    if parts and parts[0].lower()=='api': parts=parts[1:]
    for p in parts[:-1]:
        k=find_key(node,p)
        if k is None or not isinstance(node[k],dict): raise KeyError('/'.join(parts))
        node=node[k]
    k=find_key(node,parts[-1]) if parts else None
    if k is None: raise KeyError('/'.join(parts))
    node[k]=value
    return k, value

def metadata(path):
    p=path.strip('/').lower()
    if p.startswith('api/'): p=p[4:]
    if p in META_BY_PATH: return META_BY_PATH[p]
    for rg,e in REGEX_META:
        if rg.match(p): return e
    return None

def access(e):
    a=(e or {}).get('access','').lower()
    if 'readwrite' in a: return 'rw'
    if 'readonly' in a: return 'ro'
    if 'writeonly' in a: return 'wo'
    return 'unknown'

def err(msg, status=400):
    return JSONResponse({'response-code': msg}, status_code=status)

def parse_range(r):
    if not r: return None
    m=re.search(r'(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)', str(r))
    return (float(m.group(1)), float(m.group(2))) if m else None

def coerce_and_validate(e, value):
    typ=(e or {}).get('type','').lower()
    if typ in ('int','integer'):
        if isinstance(value,bool): raise ValueError('Bad input parameter type')
        try: value=int(value)
        except Exception: raise ValueError('Bad input parameter type')
    elif typ=='float':
        if isinstance(value,bool): raise ValueError('Bad input parameter type')
        try: value=float(value)
        except Exception: raise ValueError('Bad input parameter type')
        if 'decimal_places' in e:
            try: value=round(value,int(re.search(r'\d+', e['decimal_places']).group(0)))
            except Exception: pass
    elif typ=='bool':
        if isinstance(value,bool): pass
        elif isinstance(value,str) and value.lower() in ('true','1','yes','on'): value=True
        elif isinstance(value,str) and value.lower() in ('false','0','no','off'): value=False
        else: raise ValueError('Bad input parameter type')
    elif typ=='string':
        if not isinstance(value,str): value=str(value)
        if len(value.encode('utf-8'))>128: raise ValueError('Bad input parameter value')
    elif typ=='enum':
        if not isinstance(value,str): raise ValueError('Bad input parameter type')
        vals=[v.lower() for v in e.get('supported_values',[])]
        if vals and value.lower() not in vals: raise ValueError('Bad input parameter value')
    elif typ=='testpatterntype':
        vals=[v.lower() for v in e.get('supported_values',[])]
        if isinstance(value,int) or (isinstance(value,str) and value.isdigit()):
            n=int(value); rng=parse_range(e.get('range'))
            if rng and not (rng[0]<=n<=rng[1]): raise ValueError('Bad input parameter value')
            return n
        if not isinstance(value,str): raise ValueError('Bad input parameter type')
        if vals and value.lower() not in vals: raise ValueError('Bad input parameter value')
    # bytearray accepted as str/list/dict; file storage handled elsewhere
    rng=parse_range((e or {}).get('range'))
    if rng and isinstance(value,(int,float)) and not (rng[0] <= float(value) <= rng[1]):
        raise ValueError('Bad input parameter value')
    return value

def cpu_temp():
    vals=[]
    for p in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
        try:
            v=float(p.read_text().strip())
            if v>1000: v/=1000.0
            if 0<v<200: vals.append(v)
        except Exception: pass
    return round(sum(vals)/len(vals),3) if vals else 45.0

def uptime_string():
    s=int(time.time()-START); d,s=divmod(s,86400); h,s=divmod(s,3600); m,s=divmod(s,60)
    out=[]
    if d: out.append(f'{d}d')
    if h: out.append(f'{h}h')
    if m: out.append(f'{m}m')
    out.append(f'{s}s')
    return ' '.join(out)

def apply_live_values(state):
    sys=state.setdefault('api',{}).setdefault('system',{})
    if live_read_active():
        return
    sys['current-date-time']=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sys['uptime']=uptime_string()
    temp=cpu_temp()
    t=sys.setdefault('temperature',{})
    for k in ['ambient','cpu','dsp','fpga','front','gpu','main','psu','rear']:
        t[k]=temp
    t.setdefault('ethernet',{}).setdefault('copper',{})
    t.setdefault('ethernet',{}).setdefault('sfp',{})
    for k in ['a','b']: t['ethernet']['copper'][k]=temp
    for k in ['a','b','c','d']: t['ethernet']['sfp'][k]=temp

def result_for_path(path, state):
    parts=norm_parts(path)
    node=get_node(state, parts)
    if parts:
        return {parts[-1]: node} if not isinstance(node, dict) else node
    return state

def file_path_for_api(path, name='payload.bin'):
    safe=re.sub(r'[^a-zA-Z0-9_.-]+','_',path.strip('/'))[:180] or 'root'
    return FILES_DIR/f'{safe}__{os.path.basename(name)}'

def maybe_store_file(state, path, data, request_filename=None):
    e=metadata(path)
    if not e or (e.get('type') or '').lower()!='bytearray': return data
    raw=b''
    if isinstance(data, str):
        try: raw=base64.b64decode(data, validate=True)
        except Exception: raw=data.encode('utf-8')
    elif isinstance(data, list): raw=bytes(data)
    else: raw=json.dumps(data).encode('utf-8')
    fname=request_filename or 'payload.bin'
    fp=file_path_for_api(path, fname); fp.write_bytes(raw)
    # leave data readback as text if decodable, else base64
    try: stored=raw.decode('utf-8')
    except Exception: stored=base64.b64encode(raw).decode('ascii')
    # update nearby filename when present
    parts=norm_parts(path)
    try:
        parent=get_node(state, parts[:-1])
        fk=find_key(parent,'filename') or find_key(parent,'file-name')
        if fk: parent[fk]=os.path.basename(fname)
    except Exception: pass
    return stored

def set_recursive(state, base_parts, data):
    if isinstance(data, dict):
        out={}
        for k,v in data.items():
            out[k]=set_recursive(state, base_parts+[str(k)], v)
        return out
    path='/'.join(base_parts)
    e=metadata(path)
    if not e: raise KeyError(path)
    if access(e)=='ro': raise PermissionError('Bad operation')
    if access(e)=='unknown': raise PermissionError('Bad operation')
    val=maybe_store_file(state,path, coerce_and_validate(e,data))
    key,val=set_node(state, base_parts, val)
    return val

@app.api_route('/api/{path:path}', methods=['GET','PUT','POST'])
async def api_route(path: str='', request: Request=None):
    state=read_state(); apply_live_values(state)
    q=dict(request.query_params)
    if 'list' in q:
        try: node=get_node(state, norm_parts(path))
        except KeyError: return err('Path not found',404)
        return node if isinstance(node,dict) else {path.split('/')[-1]: node}
    if 'help' in q:
        prefix=path.strip('/').lower()
        items={e['path'].split('/')[-1]: {'Name':e.get('name'), 'Access Specifier':{'rw':'R/W','ro':'R/O','wo':'W/O'}.get(access(e),e.get('access')), 'Type':e.get('type'), 'Range':e.get('range'), 'Supported values':e.get('supported_values'), 'Details':e.get('description')} for e in ENDPOINTS if e['path'].lower().startswith(prefix)}
        return items or {'response-code':'Path not found'}
    if request.method=='GET' and 'set' not in q:
        e=metadata(path)
        if e and access(e)=='wo': return err('Bad operation')
        try: return JSONResponse(result_for_path(path, state))
        except KeyError: return err('Path not found',404)
    # writes via PUT/POST or GET ?set=
    try:
        if request.method=='GET':
            data=q['set']
            # multi-set when ?set=1&child=value
            if data=='1' and len(q)>1:
                data={k:v for k,v in q.items() if k!='set'}
        else:
            ctype=request.headers.get('content-type','')
            body=await request.body()
            if 'application/json' in ctype:
                obj=json.loads(body.decode('utf-8') or '{}')
                data=obj.get('data', obj)
            else:
                data=body
        base=norm_parts(path)
        if isinstance(data,(bytes,bytearray)):
            e=metadata(path)
            if not e or (e.get('type') or '').lower()!='bytearray': return err('Bad input parameter type')
            if access(e)=='ro': return err('Bad operation')
            val=maybe_store_file(state,path, list(data), request.headers.get('x-filename','payload.bin'))
            set_node(state, base, val)
            out={base[-1]: val}
        elif isinstance(data,dict):
            out=set_recursive(state, base, data)
        else:
            e=metadata(path)
            if not e: return err('Path not found',404)
            if access(e)=='ro' or access(e)=='unknown': return err('Bad operation')
            val=maybe_store_file(state,path, coerce_and_validate(e,data))
            key,val=set_node(state, base, val)
            out={key: val}
        write_state(state)
        return JSONResponse(out)
    except KeyError: return err('Path not found',404)
    except PermissionError as ex: return err(str(ex))
    except ValueError as ex: return err(str(ex))
    except Exception as ex: return err('Operation failed')

@app.get('/')
async def root():
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{APP_NAME}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111;color:#eee;margin:0;padding:32px}}
main{{max-width:920px;margin:0 auto}} h1{{margin:0 0 6px;font-size:34px}} .sub{{color:#aaa;margin-bottom:28px}}
.actions{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}
.action{{display:block;background:#181818;border:1px solid #333;border-radius:8px;padding:18px;text-decoration:none;color:#eee}}
.action:hover{{border-color:#5797d6;background:#1d1d1d}} .action b{{display:block;font-size:19px;margin-bottom:8px;color:#fff}}
.action span{{display:block;color:#aaa;line-height:1.45}} .meta{{margin-top:28px;color:#888;font-size:13px}}
a{{color:#8cc7ff}}
</style></head>
<body><main>
<h1>{APP_NAME}</h1>
<div class="sub">Local tools for working with Brompton Tessera processor API data.</div>
<div class="actions">
  <a class="action" href="/api-contents"><b>View API Contents</b><span>Browse the simulator's current API state as a searchable table.</span></a>
  <a class="action" href="/god"><b>God Mode</b><span>Edit API endpoints directly to simulate different processor states.</span></a>
  <a class="action" href="/logs"><b>Processor Logs</b><span>View syslog messages received from Tessera processors.</span></a>
  <a class="action" href="/topology"><b>Topology Monitoring</b><span>Monitor SX40 cable redundancy loop status.</span></a>
</div>
<div class="meta">Raw API data is still available at <a href="/api/">/api/</a>.</div>
</main></body></html>""")

@app.get('/api-contents', response_class=HTMLResponse)
async def api_contents(q: str = ''):
    state = read_state(); apply_live_values(state)
    api = state.get('api', state)
    ql = (q or '').lower().strip()
    rows = []
    for path, value in flatten_values(api):
        val_text = json.dumps(value) if isinstance(value, (dict, list, bool, int, float)) else str(value)
        if ql and ql not in path.lower() and ql not in val_text.lower():
            continue
        e = metadata(path) or {}
        access_label = {'rw':'R/W','ro':'R/O','wo':'W/O'}.get(access(e), e.get('access',''))
        rows.append(f"""
        <tr>
          <td class="path"><code>/api/{html_escape(path)}</code></td>
          <td>{html_escape(e.get('type',''))}</td>
          <td>{html_escape(access_label)}</td>
          <td>{html_escape(e.get('range','') or '')}</td>
          <td><code>{html_escape(val_text)}</code></td>
        </tr>""")
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>API Contents - {APP_NAME}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111;color:#eee;margin:0;padding:24px}}
h1{{margin:0 0 4px}} .sub{{color:#aaa;margin-bottom:18px}} a{{color:#8cc7ff}} code{{color:#b8e1ff}}
{NAV_CSS}
.top{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}}
.search{{display:flex;gap:8px;margin:18px 0}} input{{background:#1d1d1d;color:#fff;border:1px solid #555;border-radius:5px;padding:7px}}
.search input{{width:420px;max-width:60vw}} button{{background:#2f74c0;color:#fff;border:0;border-radius:5px;padding:7px 12px;cursor:pointer}}
table{{width:100%;border-collapse:collapse;font-size:14px}} th{{position:sticky;top:0;background:#202020;text-align:left;z-index:2}}
th,td{{border-bottom:1px solid #333;padding:8px;vertical-align:top}} tr:hover{{background:#191919}}
.path{{width:34%}} td:last-child code{{white-space:pre-wrap;word-break:break-word}}
</style></head>
<body>
<div class="top"><div><h1>API Contents</h1><div class="sub">{len(rows)} current endpoint values shown.</div></div>{page_nav('API Contents')}</div>
<form class="search" method="get" action="/api-contents"><input name="q" value="{html_escape(q)}" placeholder="Filter by path or current value"><button>Filter</button><a href="/api-contents">Clear</a></form>
<table><thead><tr><th>Path</th><th>Type</th><th>Access</th><th>Range</th><th>Current Value</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</body></html>"""
    return HTMLResponse(html)

@app.get('/logs', response_class=HTMLResponse)
async def processor_logs(ip: str = '', limit: int = 500, msg: str = ''):
    processors = list_processors()
    known_ips = {p['ip'] for p in processors}
    selected_ip = ip if ip in known_ips else ''
    for processor in processors:
        refresh_processor_name(processor['ip'])
    processors = list_processors()
    rows = []
    for row in list_logs(selected_ip, limit):
        rows.append(f"""
        <tr>
          <td>{html_escape(row['received_at'])}</td>
          <td>{html_escape(row['processor_name'])}<div class="desc">{html_escape(row['processor_ip'])}</div></td>
          <td>{html_escape(row['transport'])}</td>
          <td>{html_escape(row['message'])}</td>
        </tr>""")
    buttons = ['<a class="tab active" href="/logs">All Processors</a>' if not selected_ip else '<a class="tab" href="/logs">All Processors</a>']
    for processor in processors:
        active = ' active' if processor['ip'] == selected_ip else ''
        label = html_escape(processor['name'] or processor['ip'])
        count = int(processor['log_count'] or 0)
        buttons.append(f'<a class="tab{active}" href="/logs?ip={html_escape(processor["ip"])}">{label}<span>{count}</span></a>')
    clear_form = ''
    if selected_ip:
        clear_form = f"""<form method="post" action="/logs/clear" onsubmit="return confirm('Clear logs for this processor?');">
          <input type="hidden" name="ip" value="{html_escape(selected_ip)}">
          <button class="danger">Clear This Processor</button>
        </form>"""
    flash = f'<div class="flash">{html_escape(msg)}</div>' if msg else ''
    export_ip = f'<input type="hidden" name="ip" value="{html_escape(selected_ip)}">' if selected_ip else ''
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Processor Logs - {APP_NAME}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111;color:#eee;margin:0;padding:24px}}
h1{{margin:0 0 4px}} .sub,.desc{{color:#aaa}} .desc{{font-size:12px;margin-top:3px}} a{{color:#8cc7ff}} code{{color:#b8e1ff}}
{NAV_CSS}
.top{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:16px}}
.tabs{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 18px}} .tab{{display:inline-flex;gap:8px;align-items:center;border:1px solid #333;background:#181818;color:#eee;text-decoration:none;border-radius:8px;padding:9px 12px}}
.tab:hover,.tab.active{{border-color:#5797d6;background:#1d1d1d}} .tab span{{color:#aaa;font-size:12px}}
.tools{{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:16px 0}} label{{display:block;color:#aaa;font-size:12px;margin-bottom:4px}}
input{{background:#1d1d1d;color:#fff;border:1px solid #555;border-radius:5px;padding:7px}} button{{background:#2f74c0;color:#fff;border:0;border-radius:5px;padding:8px 12px;cursor:pointer}}
.danger{{background:#a43b3b}} .flash{{background:#0d2a16;border:1px solid #2f8b4b;padding:10px;border-radius:6px;margin:12px 0;color:#baffc9}}
table{{width:100%;border-collapse:collapse;font-size:14px}} th{{position:sticky;top:0;background:#202020;text-align:left;z-index:2}}
th,td{{border-bottom:1px solid #333;padding:8px;vertical-align:top}} tr:hover{{background:#191919}} td:last-child{{white-space:pre-wrap;word-break:break-word}}
</style></head>
<body>
<div class="top"><div><h1>Processor Logs</h1><div class="sub">Server-timestamped syslog messages received on UDP/TCP port 514.</div></div>{page_nav('Processor Logs')}</div>
{flash}
<div class="tabs">{''.join(buttons)}</div>
<div class="tools">
  <form method="get" action="/logs/export">
    {export_ip}
    <label>Export minutes back from now</label>
    <input name="minutes" type="number" min="1" max="10080" value="60">
    <button>Export CSV</button>
  </form>
  {clear_form}
</div>
<table><thead><tr><th>Server Time</th><th>Processor</th><th>Transport</th><th>Message</th></tr></thead><tbody>
{''.join(rows) if rows else '<tr><td colspan="4" class="sub">No logs received yet.</td></tr>'}
</tbody></table>
</body></html>"""
    return HTMLResponse(html)

@app.get('/logs/export')
async def logs_export(minutes: int = 60, ip: str = ''):
    csv_text = export_logs_csv(minutes, ip)
    suffix = ip.replace('.', '-') if ip else 'all'
    headers = {'Content-Disposition': f'attachment; filename="processor-logs-{suffix}-{minutes}m.csv"'}
    return Response(csv_text, media_type='text/csv', headers=headers)

@app.post('/logs/clear')
async def logs_clear(request: Request):
    form = await request.form()
    ip = str(form.get('ip', '')).strip()
    if ip:
        clear_logs(ip)
    from urllib.parse import urlencode
    qs = urlencode({'ip': ip, 'msg': f'Cleared logs for {ip}.'}) if ip else ''
    return RedirectResponse('/logs' + (('?' + qs) if qs else ''), status_code=303)

def topology_card_payload(monitor):
    status = html_escape(monitor.get('last_status') or 'pending')
    error = html_escape(monitor.get('last_error') or '')
    last = html_escape(monitor.get('last_poll_at') or 'not polled yet')
    processor_type = html_escape(monitor.get('processor_type') or 'unknown')
    monitor_id = html_escape(monitor.get('id'))
    svg = topology_svg(monitor)
    signature_src = json.dumps({
        'id': monitor.get('id'),
        'name': monitor.get('name'),
        'ip': monitor.get('ip'),
        'interval': monitor.get('interval'),
        'processor_type': monitor.get('processor_type'),
        'loop1_state': monitor.get('loop1_state'),
        'loop2_state': monitor.get('loop2_state'),
        'last_status': monitor.get('last_status'),
        'last_error': monitor.get('last_error'),
    }, sort_keys=True)
    signature = hashlib.sha256(signature_src.encode('utf-8')).hexdigest()
    html = f"""
        <section class="monitor" draggable="true" data-id="{monitor_id}" data-signature="{signature}">
          <div class="monitor-head">
            <div><h2>{html_escape(monitor.get('name') or monitor.get('ip'))}</h2><div class="sub">{html_escape(monitor.get('ip'))} · {processor_type} · every {int(monitor.get('interval') or 10)}s · last poll {last}</div></div>
            <div class="monitor-actions">
              <span class="drag-handle" title="Drag to reorder">Drag</span>
              <form method="post" action="/topology/remove" onsubmit="return confirm('Remove this processor from topology monitoring?');">
                <input type="hidden" name="id" value="{monitor_id}">
                <button class="danger">Remove</button>
              </form>
            </div>
          </div>
          <div class="sub">Status: {status}{(' · ' + error) if error else ''}</div>
          {svg}
        </section>"""
    return {'id': monitor.get('id'), 'signature': signature, 'html': html}

def topology_cards_payload():
    return [topology_card_payload(monitor) for monitor in list_monitors()]

@app.get('/topology', response_class=HTMLResponse)
async def topology_page(msg: str = '', level: str = 'info'):
    cards = topology_cards_payload()
    flash = f'<div class="flash {html_escape(level)}">{html_escape(msg)}</div>' if msg else ''
    remaining = max(0, 20 - len(cards))
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Topology Monitoring - {APP_NAME}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111;color:#eee;margin:0;padding:24px}}
h1{{margin:0 0 4px}} h2{{margin:0 0 4px;font-size:18px}} .sub{{color:#aaa}} a{{color:#8cc7ff}}
{NAV_CSS}
.top{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:16px}}
.panel{{background:#181818;border:1px solid #333;border-radius:8px;padding:14px;margin:14px 0}} .monitor-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px;align-items:start}} .monitor{{background:#181818;border:1px solid #333;border-radius:8px;padding:10px;min-width:0;transition:border-color .12s,opacity .12s}}
.monitor-head{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;margin-bottom:10px}}
.monitor-actions{{display:flex;gap:8px;align-items:center}} .drag-handle{{color:#aaa;border:1px solid #444;border-radius:5px;padding:7px 9px;cursor:grab;user-select:none}} .dragging{{opacity:.45}} .drag-over{{border-color:#5797d6}}
.add{{display:flex;gap:10px;align-items:end;flex-wrap:wrap}} label{{display:block;color:#aaa;font-size:12px;margin-bottom:4px}}
input{{background:#1d1d1d;color:#fff;border:1px solid #555;border-radius:5px;padding:8px}} button{{background:#2f74c0;color:#fff;border:0;border-radius:5px;padding:8px 12px;cursor:pointer}}
.danger{{background:#a43b3b}} .flash{{background:#0d2a16;border:1px solid #2f8b4b;padding:10px;border-radius:6px;margin:12px 0;color:#baffc9}} .flash.error{{background:#2b1111;border-color:#933;color:#ffd0d0}}
.topology-svg{{display:block;width:100%;margin:8px auto 0;background:#000;border-radius:4px}}
.topology-svg text{{fill:#fff;font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:20px}} .topology-svg .title{{font-size:22px}}
.topology-svg .frame,.topology-svg .port-row{{fill:none;stroke:#fff;stroke-width:1.5}} .topology-svg .port-row{{stroke-width:1}}
.topology-svg .arrow{{stroke-width:2.2;fill:none}} .topology-svg .arrow.ok{{stroke:#00ff30}} .topology-svg .arrow.bad{{stroke:#ff2828}}
.topology-svg .unsupported{{fill:#ffcc66;font-size:16px}}
</style></head>
<script>
let topologyDragging = null;
function bindTopologyDrag() {{
  document.querySelectorAll('.monitor').forEach(function(card) {{
    if (card.dataset.dragBound === '1') return;
    card.dataset.dragBound = '1';
    card.addEventListener('dragstart', function(event) {{
      topologyDragging = card;
      card.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', card.dataset.id);
    }});
    card.addEventListener('dragend', function() {{
      card.classList.remove('dragging');
      document.querySelectorAll('.drag-over').forEach(function(el) {{ el.classList.remove('drag-over'); }});
      topologyDragging = null;
      saveTopologyOrder();
    }});
    card.addEventListener('dragover', function(event) {{
      event.preventDefault();
      if (!topologyDragging || topologyDragging === card) return;
      card.classList.add('drag-over');
      const grid = document.getElementById('monitor-grid');
      const rect = card.getBoundingClientRect();
      const before = event.clientY < rect.top + rect.height / 2;
      grid.insertBefore(topologyDragging, before ? card : card.nextSibling);
    }});
    card.addEventListener('dragleave', function() {{ card.classList.remove('drag-over'); }});
    card.addEventListener('drop', function(event) {{
      event.preventDefault();
      card.classList.remove('drag-over');
    }});
  }});
}}
function saveTopologyOrder() {{
  const ids = Array.from(document.querySelectorAll('#monitor-grid .monitor')).map(function(card) {{ return card.dataset.id; }});
  fetch('/topology/reorder', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ids: ids}})}}).catch(function() {{}});
}}
async function refreshTopologyCards() {{
  if (topologyDragging) return;
  try {{
    const response = await fetch('/topology/data', {{cache:'no-store'}});
    const data = await response.json();
    const grid = document.getElementById('monitor-grid');
    const empty = document.getElementById('monitor-empty');
    if (!grid) return;
    const existing = new Map(Array.from(grid.querySelectorAll('.monitor')).map(function(card) {{ return [card.dataset.id, card]; }}));
    const existingIds = Array.from(existing.keys()).join(',');
    const incomingIds = data.cards.map(function(card) {{ return card.id; }}).join(',');
    if (existingIds !== incomingIds) {{
      grid.innerHTML = data.cards.map(function(card) {{ return card.html; }}).join('');
      if (empty) empty.style.display = data.cards.length ? 'none' : '';
      bindTopologyDrag();
      return;
    }}
    data.cards.forEach(function(card) {{
      const node = existing.get(card.id);
      if (node && node.dataset.signature !== card.signature) {{
        node.outerHTML = card.html;
      }}
    }});
    if (empty) empty.style.display = data.cards.length ? 'none' : '';
    bindTopologyDrag();
  }} catch (error) {{}}
}}
document.addEventListener('DOMContentLoaded', function() {{
  bindTopologyDrag();
  setInterval(refreshTopologyCards, 5000);
}});
</script>
<body>
<div class="top"><div><h1>Topology Monitoring</h1><div class="sub">Polls only processor name, processor type, and cable redundancy loop state endpoints.</div></div>{page_nav('Topology Monitoring')}</div>
{flash}
<section class="panel">
  <form class="add" method="post" action="/topology/add">
    <div><label>Processor IP address</label><input name="ip" placeholder="192.168.0.101" required></div>
    <div><label>Polling frequency seconds</label><input name="interval" type="number" min="1" value="10"></div>
    <button {'disabled' if remaining == 0 else ''}>Add Processor</button>
  </form>
  <div class="sub">{remaining} monitoring slots available.</div>
</section>
<div id="monitor-grid" class="monitor-grid">{''.join(card['html'] for card in cards)}</div>
<section id="monitor-empty" class="panel" style="display:{'none' if cards else 'block'}"><div class="sub">No processors are being monitored yet.</div></section>
</body></html>"""
    return HTMLResponse(html)

@app.post('/topology/add')
async def topology_add(request: Request):
    from urllib.parse import urlencode
    form = await request.form()
    try:
        add_monitor(str(form.get('ip', '')), int(form.get('interval') or 10))
        qs = urlencode({'msg': 'Processor added to topology monitoring.', 'level': 'info'})
    except Exception as ex:
        qs = urlencode({'msg': str(ex), 'level': 'error'})
    return RedirectResponse('/topology?' + qs, status_code=303)

@app.get('/topology/data')
async def topology_data():
    return JSONResponse({'cards': topology_cards_payload()})

@app.post('/topology/reorder')
async def topology_reorder(request: Request):
    try:
        body = await request.json()
        ids = body.get('ids', []) if isinstance(body, dict) else []
        if isinstance(ids, list):
            reorder_monitors([str(mid) for mid in ids])
    except Exception:
        pass
    return JSONResponse({'ok': True})

@app.post('/topology/remove')
async def topology_remove(request: Request):
    form = await request.form()
    remove_monitor(str(form.get('id', '')))
    return RedirectResponse('/topology', status_code=303)

async def handle_tcp(reader, writer):
    writer.write(b'Tessera Control and Monitoring ready. Commands: get/set/list/help <path> [value]\n'); await writer.drain()
    state=read_state()
    while True:
        line=await reader.readline()
        if not line: break
        try:
            s=line.decode().strip();
            if not s: continue
            cmd,*rest=s.split(' ',2); cmd=cmd.lower()
            path=rest[0] if rest else ''
            state=read_state(); apply_live_values(state)
            if cmd=='get': resp=result_for_path(path,state)
            elif cmd=='list': resp=get_node(state,norm_parts(path))
            elif cmd=='help': resp=[e for e in ENDPOINTS if e['path'].lower().startswith(path.lower())][:50]
            elif cmd=='set':
                if len(rest)<2: resp={'response-code':'Missing input parameter'}
                else:
                    e=metadata(path)
                    val=json.loads(rest[1]) if rest[1][:1] in '[{"' or rest[1] in ('true','false') else rest[1]
                    if not e or access(e)=='ro': resp={'response-code':'Bad operation'}
                    else:
                        val=coerce_and_validate(e,val); set_node(state,norm_parts(path),val); write_state(state); resp=result_for_path(path,state)
            else: resp={'response-code':'Bad operation'}
        except Exception: resp={'response-code':'Operation failed'}
        writer.write((json.dumps(resp)+'\n').encode()); await writer.drain()
    writer.close()


# -------------------------
# God Mode editor endpoints
# -------------------------
def is_god_protected(path: str) -> bool:
    """Values that remain absolutely read-only, even in God Mode."""
    p = path.strip('/').lower()
    if p.startswith('api/'):
        p = p[4:]
    if p in ('system/current-date-time', 'system/uptime'):
        return True
    if p.startswith('system/temperature/'):
        return True
    return False

def flatten_values(obj: Any, prefix: str = ''):
    if isinstance(obj, dict):
        for k in sorted(obj.keys(), key=lambda x: str(x).lower()):
            newp = f'{prefix}/{k}' if prefix else str(k)
            yield from flatten_values(obj[k], newp)
    else:
        yield prefix, obj

def parse_god_value(raw: str):
    raw = raw if raw is not None else ''
    s = raw.strip()
    if s == '':
        return ''
    try:
        return json.loads(s)
    except Exception:
        sl = s.lower()
        if sl == 'true': return True
        if sl == 'false': return False
        if re.fullmatch(r'-?(0|[1-9][0-9]*)', s):
            try: return int(s)
            except Exception: pass
        if re.fullmatch(r'-?(0|[1-9][0-9]*)\.[0-9]+', s):
            try: return float(s)
            except Exception: pass
        return raw

def god_set_node(state, path: str, value: Any):
    parts = norm_parts(path)
    if parts and parts[0].lower() == 'api':
        parts = parts[1:]
    if not parts:
        raise KeyError(path)
    node = state.setdefault('api', {})
    for p in parts[:-1]:
        k = find_key(node, p)
        if k is None:
            node[p] = {}
            k = p
        if not isinstance(node[k], dict):
            node[k] = {}
        node = node[k]
    k = find_key(node, parts[-1]) or parts[-1]
    node[k] = value
    return '/'.join(parts), value

def html_escape(s: Any) -> str:
    return (str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
            .replace('\"','&quot;').replace("'", '&#39;'))


def redirect_god(msg: str = '', level: str = 'info'):
    from urllib.parse import urlencode
    qs = urlencode({'msg': msg, 'level': level}) if msg else ''
    return RedirectResponse('/god' + (('?' + qs) if qs else ''), status_code=303)

def endpoint_is_present(api, template_path: str) -> bool:
    parts = norm_parts(template_path)
    node = api
    for p in parts:
        if p.startswith('{') and p.endswith('}'):
            return False
        k = find_key(node, p) if isinstance(node, dict) else None
        if k is None:
            return False
        node = node[k]
    return not isinstance(node, dict)

def documented_missing_endpoints(api, q: str = ''):
    ql=(q or '').lower().strip()
    out=[]
    for e in ENDPOINTS:
        path=e.get('path','').strip('/')
        if not path or '(deprecated)' in path.lower() or 'deprecated' in (e.get('description','') or '').lower():
            continue
        if endpoint_is_present(api, path):
            continue
        if ql and ql not in path.lower() and ql not in (e.get('name','') or '').lower() and ql not in (e.get('description','') or '').lower():
            continue
        out.append(e)
    return out




def locked_path(path: str) -> bool:
    return is_god_protected(path)

def remove_locked_from_api(api_obj: Any):
    api = deepcopy(api_obj)
    def pop_path(root, path):
        parts = norm_parts(path)
        node = root
        for p in parts[:-1]:
            k = find_key(node, p) if isinstance(node, dict) else None
            if k is None: return
            node = node[k]
        if isinstance(node, dict):
            k = find_key(node, parts[-1])
            if k is not None: node.pop(k, None)
    pop_path(api, 'system/current-date-time')
    pop_path(api, 'system/uptime')
    sys = api.get('system') if isinstance(api, dict) else None
    if isinstance(sys, dict):
        tk = find_key(sys, 'temperature')
        if tk: sys.pop(tk, None)
    return api

def replace_api_with_preset(state, preset_api):
    # Preset recall is intentionally destructive for the simulator state.
    # Saved presets do not contain locked live values; those are restored dynamically by apply_live_values().
    state['api'] = deepcopy(preset_api) if isinstance(preset_api, dict) else {}
    if not live_read_active():
        apply_live_values(state)
    return state

def preset_path(pid: str):
    safe = re.sub(r'[^a-zA-Z0-9_.-]+','_', pid)
    return PRESETS_DIR / f'{safe}.json'

def list_presets():
    init_state()
    out=[]
    for p in sorted(PRESETS_DIR.glob('*.json'), key=lambda x: x.stat().st_mtime, reverse=True):
        try: out.append(load_json(p))
        except Exception: pass
    return out

def save_preset(name: str, notes: str, api_snapshot: Any):
    init_state()
    pid = datetime.now().strftime('%Y%m%d%H%M%S') + '-' + uuid.uuid4().hex[:8]
    preset = {'id': pid, 'name': (name or 'Untitled Preset')[:128], 'notes': notes or '', 'created': datetime.now().isoformat(timespec='seconds'), 'state': {'api': remove_locked_from_api(api_snapshot.get('api', api_snapshot))}}
    preset_path(pid).write_text(json.dumps(preset, indent=2), encoding='utf-8')
    return preset

def extract_api_from_upload(obj: Any):
    if isinstance(obj, dict) and 'state' in obj and isinstance(obj['state'], dict): obj = obj['state']
    if isinstance(obj, dict) and 'api' in obj and isinstance(obj['api'], dict): return obj['api']
    if isinstance(obj, dict): return obj
    raise ValueError('Uploaded JSON did not contain an object/API tree')

def fetch_processor_api(ip: str, timeout=8):
    ip = (ip or '').strip().replace('http://','').replace('https://','').strip('/')
    if not ip: raise ValueError('Missing IP address')
    url = f'http://{ip}/api/'
    req = urllib.request.Request(url, headers={'Accept':'application/json','User-Agent':'tessera-sim'})
    with urllib.request.urlopen(req, timeout=timeout) as r: raw = r.read()
    obj = json.loads(raw.decode('utf-8'))
    api = extract_api_from_upload(obj)
    if not isinstance(api, dict) or not api: raise ValueError('No API data returned')
    return api

def live_config():
    init_state()
    if LIVE_FILE.exists():
        try: return load_json(LIVE_FILE)
        except Exception: pass
    return {'active': False, 'ip': '', 'interval': 5, 'last_status': 'stopped'}

def write_live_config(cfg):
    init_state(); LIVE_FILE.write_text(json.dumps(cfg, indent=2), encoding='utf-8')

def live_read_active():
    try: return bool(live_config().get('active'))
    except Exception: return False

async def live_read_loop():
    while True:
        cfg = live_config()
        if not cfg.get('active'):
            await asyncio.sleep(1); continue
        ip = cfg.get('ip','')
        interval = max(1, int(cfg.get('interval') or 5))
        try:
            api = await asyncio.to_thread(fetch_processor_api, ip, min(interval, 8))
            state = read_state(); state['api'] = api; write_state(state)
            cfg.update({'last_status':'ok', 'last_success': datetime.now().isoformat(timespec='seconds'), 'last_error': ''}); write_live_config(cfg)
        except Exception as ex:
            cfg.update({'last_status':'error', 'last_error': str(ex), 'last_attempt': datetime.now().isoformat(timespec='seconds')}); write_live_config(cfg)
        await asyncio.sleep(interval)

async def topology_monitor_loop():
    while True:
        await asyncio.to_thread(poll_due_monitors)
        await asyncio.sleep(1)

@app.on_event('startup')
async def start_live_task():
    asyncio.create_task(live_read_loop())
    asyncio.create_task(topology_monitor_loop())

def preset_panel_html():
    presets = list_presets(); cfg = live_config(); cards=[]
    for pr in presets:
        pid=html_escape(pr.get('id','')); name=html_escape(pr.get('name','Untitled')); notes=html_escape(pr.get('notes','')); created=html_escape(pr.get('created',''))
        cards.append(f"""<div class='preset-card'><b>{name}</b><div class='desc'>{created}</div><div>{notes}</div>
<form method='post' action='/god/presets/recall' onsubmit="return confirm('Are you sure you want to recall this preset and overwrite the current state of the simulator?');"><input type='hidden' name='id' value='{pid}'><button>Recall</button></form>
<form method='post' action='/god/presets/delete' onsubmit="return confirm('Are you sure you want to delete this preset?');"><input type='hidden' name='id' value='{pid}'><button class='danger'>Delete</button></form></div>""")
    status=html_escape(cfg.get('last_status','stopped')); ip=html_escape(cfg.get('ip','')); err=html_escape(cfg.get('last_error',''))
    errtxt = (' · '+err) if err else ''
    cards_html = ''.join(cards) if cards else '<div class="desc">No presets saved yet.</div>'
    return f"""<div class='panelgrid'>
<section class='panel'><h2>Save Current State as Preset</h2><form method='post' action='/god/presets/save'><input name='name' placeholder='Preset name' required><textarea name='notes' placeholder='Notes'></textarea><button>Save Preset</button></form></section>
<section class='panel'><h2>Load Preset From File</h2><div class='desc'>This imports the JSON into a saved preset only. It does not change the active SIM data until you recall that preset.</div><form method='post' enctype='multipart/form-data' action='/god/presets/upload'><input type='file' name='file' accept='.json,application/json' required><input name='name' placeholder='Preset name' required><textarea name='notes' placeholder='Description'></textarea><button>Load JSON as Preset</button></form></section>
<section class='panel'><h2>Load Preset From Live Processor</h2><form method='post' action='/god/presets/from-processor'><input name='ip' placeholder='Processor IP address' required><input name='name' placeholder='Preset name' required><textarea name='notes' placeholder='Description'></textarea><button>Load</button></form></section>
<section class='panel'><h2>Live Read Real Processor</h2><div class='caution'>Caution: overwrites current SIM data</div><form method='post' action='/god/live/start'><input name='ip' value='{ip}' placeholder='Processor IP address' required><input name='interval' type='number' min='1' step='1' placeholder='refresh interval in seconds (default: 5)'><button>Start Live Read</button></form><form method='post' action='/god/live/stop'><button class='danger'>Stop Live Read</button></form><div class='desc'>Status: {status}{errtxt}</div></section>
</div><h2>Saved Presets</h2><div class='presets'>{cards_html}</div>"""

@app.get('/god', response_class=HTMLResponse)
async def god_page(q: str = '', msg: str = '', level: str = 'info'):
    state = read_state(); apply_live_values(state)
    api = state.get('api', state)
    rows = []
    ql = (q or '').lower().strip()
    for path, value in flatten_values(api):
        if ql and ql not in path.lower() and ql not in str(value).lower():
            continue
        e = metadata(path) or {}
        protected = is_god_protected(path)
        access_label = {'rw':'R/W','ro':'R/O','wo':'W/O'}.get(access(e), e.get('access',''))
        type_label = e.get('type','')
        range_label = e.get('range','') or ''
        desc = e.get('description','') or ''
        val_text = json.dumps(value) if isinstance(value, (dict, list, bool, int, float)) else str(value)
        disabled = 'disabled' if protected else ''
        button = '<button type="submit">Save</button>' if not protected else '<span class="lock">LIVE</span>'
        klass = 'protected' if protected else ''
        rows.append(f"""
        <tr class="{klass}">
          <td class="path"><code>/api/{html_escape(path)}</code><div class="desc">{html_escape(desc)}</div></td>
          <td>{html_escape(type_label)}</td>
          <td>{html_escape(access_label)}</td>
          <td>{html_escape(range_label)}</td>
          <td>
            <form method="post" action="/god/set">
              <input type="hidden" name="path" value="{html_escape(path)}">
              <input class="value" name="value" value="{html_escape(val_text)}" {disabled}>
              {button}
            </form>
          </td>
        </tr>""")
    missing_rows=[]
    for e in documented_missing_endpoints(api, q):
        path=e.get('path','')
        typ=(e.get('type','') or '').lower()
        access_label={'rw':'R/W','ro':'R/O','wo':'W/O'}.get(access(e), e.get('access',''))
        desc=e.get('description','') or ''
        range_label=e.get('range','') or ''
        upload = typ == 'bytearray'
        if upload:
            control=f"""<form method='post' enctype='multipart/form-data' action='/god/add-file'>
              <input type='hidden' name='template_path' value='{html_escape(path)}'>
              <input class='value' name='path' value='{html_escape(path)}'>
              <input type='file' name='file' required>
              <button>Create / Upload</button>
            </form>"""
        else:
            control=f"""<form method='post' action='/god/add'>
              <input type='hidden' name='template_path' value='{html_escape(path)}'>
              <input class='value' name='path' value='{html_escape(path)}'>
              <input class='value' name='value' placeholder='value'>
              <button>Create</button>
            </form>"""
        missing_rows.append(f"""
        <tr class='missing'>
          <td class='path'><code>/api/{html_escape(path)}</code><div class='desc'>{html_escape(desc)} {'Replace {dynamic} path sections before creating.' if '{' in path else ''}</div></td>
          <td>{html_escape(typ)}</td><td>{html_escape(access_label)}</td><td>{html_escape(range_label)}</td><td>{control}</td>
        </tr>""")
    flash = f"<div class='flash {html_escape(level)}'>{html_escape(msg)}</div>" if msg else ''
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{APP_NAME} God Mode</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111;color:#eee;margin:0;padding:24px}}
h1{{margin:0 0 4px}} .sub{{color:#aaa;margin-bottom:18px}}
a{{color:#8cc7ff}} code{{color:#b8e1ff}}
{NAV_CSS}
.search{{display:flex;gap:8px;margin:18px 0}} input{{background:#1d1d1d;color:#fff;border:1px solid #555;border-radius:5px;padding:7px}}
.search input{{width:420px}} button{{background:#2f74c0;color:#fff;border:0;border-radius:5px;padding:7px 12px;cursor:pointer}}
table{{width:100%;border-collapse:collapse;font-size:14px}} th{{position:sticky;top:0;background:#202020;text-align:left;z-index:2}}
th,td{{border-bottom:1px solid #333;padding:8px;vertical-align:top}} tr:hover{{background:#191919}}
.path{{width:36%}} .desc{{color:#999;font-size:12px;margin-top:3px;max-width:650px}} .value{{width:78%;font-family:ui-monospace,Menlo,Consolas,monospace}}
.protected{{opacity:.62}} .lock{{display:inline-block;color:#ffcc66;font-weight:700;margin-left:8px}}
.notice{{background:#241b08;border:1px solid #73581d;padding:10px;border-radius:6px;margin:12px 0;color:#ffd98a}} .flash{{background:#0d2a16;border:1px solid #2f8b4b;padding:10px;border-radius:6px;margin:12px 0;color:#baffc9}} .flash.error{{background:#2b1111;border-color:#933;color:#ffd0d0}} .caution{{color:#ff5757;font-weight:800;margin:6px 0 10px}} .missing{{background:#141316}}
.panelgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin:16px 0}} .panel,.preset-card{{background:#181818;border:1px solid #333;border-radius:8px;padding:12px}} .panel h2{{font-size:16px;margin:0 0 8px}} textarea{{display:block;width:95%;min-height:58px;background:#1d1d1d;color:#fff;border:1px solid #555;border-radius:5px;padding:7px;margin:8px 0}} .panel input{{display:block;width:95%;margin:8px 0}} .presets{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;margin-bottom:18px}} .preset-card form{{display:inline-block;margin:8px 6px 0 0}} .danger{{background:#a43b3b}}
</style></head>
<body>
<h1>{APP_NAME} God Mode</h1>
<div class="sub">Edit simulator state directly. API read-only rules are bypassed here for testing clients.</div>
<div class="notice">Still locked in God Mode: <code>/api/system/current-date-time</code>, <code>/api/system/uptime</code>, and everything under <code>/api/system/temperature/</code>. Live Read Real Processor intentionally overwrites those values while live read is active.</div>
{flash}
{preset_panel_html()}
<form class="search" method="get" action="/god"><input name="q" value="{html_escape(q)}" placeholder="Filter by path or current value"><button>Filter</button><a href="/god">Clear</a></form>
<div class="sub">{page_nav('God Mode')} · API root: <a href="/api/">/api/</a> · JSON dump: <a href="/god/state">/god/state</a></div>
<table><thead><tr><th>Path</th><th>Type</th><th>API Access</th><th>Range</th><th>Value</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<h2>Documented Endpoints Not Currently In SIM Data</h2><div class='sub'>These are available to add. Dynamic placeholders like <code>{{number}}</code>, <code>{{serial}}</code>, and port numbers must be replaced with real values before creating. Unset endpoints are not returned by API calls.</div><table><thead><tr><th>Path</th><th>Type</th><th>API Access</th><th>Range</th><th>Create Value</th></tr></thead><tbody>{''.join(missing_rows)}</tbody></table>
</body></html>"""
    return HTMLResponse(html)

@app.post('/god/set')
async def god_set(request: Request):
    form = await request.form()
    path = str(form.get('path','')).strip('/ ')
    if not path:
        return RedirectResponse('/god', status_code=303)
    if is_god_protected(path):
        return HTMLResponse('<h1>Locked</h1><p>This live system value cannot be edited.</p><p><a href="/god">Back</a></p>', status_code=403)
    value = parse_god_value(str(form.get('value','')))
    state = read_state()
    god_set_node(state, path, value)
    write_state(state)
    return RedirectResponse('/god?q=' + path, status_code=303)



@app.post('/god/presets/save')
async def god_preset_save(request: Request):
    form = await request.form(); state=read_state(); apply_live_values(state)
    save_preset(str(form.get('name','Untitled')), str(form.get('notes','')), state)
    return redirect_god('Preset saved.', 'info')

@app.post('/god/presets/recall')
async def god_preset_recall(request: Request):
    form = await request.form(); pid=str(form.get('id','')); p=preset_path(pid)
    if not p.exists(): return HTMLResponse('<h1>Preset not found</h1><p><a href="/god">Back</a></p>', status_code=404)
    preset=load_json(p); api=extract_api_from_upload(preset.get('state',{})); state=read_state(); state.setdefault('api',{})
    replace_api_with_preset(state, api); write_state(state)
    return redirect_god('Preset recalled. Active SIM data was replaced by the preset.', 'info')

@app.post('/god/presets/delete')
async def god_preset_delete(request: Request):
    form = await request.form(); p=preset_path(str(form.get('id','')))
    if p.exists(): p.unlink()
    return RedirectResponse('/god', status_code=303)

@app.post('/god/presets/upload')
async def god_preset_upload(request: Request):
    form = await request.form(); upload = form.get('file')
    try:
        raw = await upload.read(); obj=json.loads(raw.decode('utf-8')); api=extract_api_from_upload(obj)
        save_preset(str(form.get('name','Untitled')), str(form.get('notes','')), {'api': api})
        return redirect_god('JSON file was saved as a preset only. Active SIM data was not changed.', 'info')
    except Exception as ex:
        return HTMLResponse(f'<h1>Upload failed</h1><p>{html_escape(ex)}</p><p><a href="/god">Back</a></p>', status_code=400)

@app.post('/god/presets/from-processor')
async def god_preset_from_processor(request: Request):
    form = await request.form()
    try:
        api = await asyncio.to_thread(fetch_processor_api, str(form.get('ip','')))
        save_preset(str(form.get('name','Untitled')), str(form.get('notes','')), {'api': api})
        return HTMLResponse('<h1>Success</h1><p>Preset created from live processor. Active SIM data was not changed.</p><p><a href="/god">Back to God Mode</a></p>')
    except Exception as ex:
        return HTMLResponse(f'<h1>Error</h1><p>No preset was created.</p><pre>{html_escape(ex)}</pre><p><a href="/god">Back</a></p>', status_code=400)

@app.post('/god/live/start')
async def god_live_start(request: Request):
    form = await request.form(); ip=str(form.get('ip','')).strip()
    try: interval=max(1, int(form.get('interval') or 5))
    except Exception: interval=5
    try:
        api = await asyncio.to_thread(fetch_processor_api, ip); state=read_state(); state['api']=api; write_state(state)
        write_live_config({'active': True, 'ip': ip, 'interval': interval, 'last_status': 'ok', 'last_success': datetime.now().isoformat(timespec='seconds'), 'last_error': ''})
        return RedirectResponse('/god', status_code=303)
    except Exception as ex:
        write_live_config({'active': False, 'ip': ip, 'interval': interval, 'last_status': 'error', 'last_error': str(ex)})
        return HTMLResponse(f'<h1>Live Read failed</h1><p>Live Read was not started.</p><pre>{html_escape(ex)}</pre><p><a href="/god">Back</a></p>', status_code=400)

@app.post('/god/live/stop')
async def god_live_stop(request: Request):
    cfg=live_config(); cfg['active']=False; cfg['last_status']='stopped'; write_live_config(cfg)
    return RedirectResponse('/god', status_code=303)


@app.post('/god/add')
async def god_add(request: Request):
    form=await request.form(); path=str(form.get('path','')).strip('/ ')
    if not path or '{' in path or '}' in path:
        return redirect_god('Replace all dynamic {placeholders} in the endpoint path before creating it.', 'error')
    if is_god_protected(path):
        return redirect_god('That endpoint is locked and cannot be created or edited in God Mode.', 'error')
    e=metadata(path)
    if not e:
        return redirect_god('Endpoint path is not in the documented API list.', 'error')
    try:
        value=parse_god_value(str(form.get('value','')))
        # validate against documented type/range, but allow read-only endpoints in God Mode
        value=coerce_and_validate(e, value)
        state=read_state(); god_set_node(state, path, value); write_state(state)
        return redirect_god(f'Created /api/{path}.', 'info')
    except Exception as ex:
        return redirect_god(f'Could not create endpoint: {ex}', 'error')

@app.post('/god/add-file')
async def god_add_file(request: Request):
    form=await request.form(); path=str(form.get('path','')).strip('/ '); upload=form.get('file')
    if not path or '{' in path or '}' in path:
        return redirect_god('Replace all dynamic {placeholders} in the endpoint path before uploading.', 'error')
    if is_god_protected(path):
        return redirect_god('That endpoint is locked and cannot be edited in God Mode.', 'error')
    e=metadata(path)
    if not e or (e.get('type') or '').lower()!='bytearray':
        return redirect_god('Endpoint is not a documented bytearray upload endpoint.', 'error')
    try:
        raw=await upload.read()
        filename=getattr(upload, 'filename', None) or 'payload.bin'
        state=read_state()
        stored=maybe_store_file(state, path, list(raw), filename)
        god_set_node(state, path, stored)
        write_state(state)
        return redirect_god(f'Uploaded file into /api/{path}.', 'info')
    except Exception as ex:
        return redirect_god(f'Upload failed: {ex}', 'error')

@app.get('/god/state')
async def god_state():
    state = read_state(); apply_live_values(state)
    return JSONResponse(state)

if __name__=='__main__':
    import uvicorn
    init_state()
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT','8080')))
