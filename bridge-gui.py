"""
Claude <-> Feishu Bridge — Web Dashboard
FastAPI server for managing multi-bot configurations, profiles, and live log streaming.
Usage: python bridge-gui.py [--port 8080]
"""
import json
import os
import shutil
import sys
import time
import asyncio
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

BRIDGE_DIR = Path(__file__).parent
CONFIG_PATH = BRIDGE_DIR / "bridge-config.json"

# Resolve lark-cli
_LARK_CLI = shutil.which("lark-cli") or shutil.which("lark-cli.cmd")
if not _LARK_CLI:
    _npm_root = os.path.expandvars(r"%APPDATA%\npm")
    for _c in ["lark-cli.cmd", "lark-cli"]:
        _p = os.path.join(_npm_root, _c)
        if os.path.exists(_p):
            _LARK_CLI = _p
            break
LARK_CLI = _LARK_CLI or "lark-cli"

app = FastAPI(title="Claude-Feishu Bridge", version="2.0")

# ── Global state ──
bridge_process: subprocess.Popen | None = None
log_clients: list[WebSocket] = []
log_buffer: list[str] = []
MAX_LOG_BUFFER = 500

tz_utc8 = timezone(timedelta(hours=8))


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def now_str():
    return datetime.now(tz_utc8).strftime("%H:%M:%S")


def broadcast_log(line: str):
    """Send a log line to all connected WebSocket clients."""
    ts = now_str()
    entry = f"[{ts}] {line}"
    log_buffer.append(entry)
    if len(log_buffer) > MAX_LOG_BUFFER:
        log_buffer[:] = log_buffer[-MAX_LOG_BUFFER:]
    gone = []
    for ws in log_clients:
        try:
            asyncio.create_task(ws.send_text(entry))
        except Exception:
            gone.append(ws)
    for ws in gone:
        log_clients.remove(ws)


def run_lark(*args) -> tuple[int, str, str]:
    """Run a lark-cli command, return (code, stdout, stderr)."""
    try:
        r = subprocess.run(
            [LARK_CLI, *args],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


# ═══════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════

@app.get("/api/status")
async def api_status():
    global bridge_process
    running = bridge_process is not None and bridge_process.poll() is None
    cfg = load_config()
    bots = []
    for name, bot_cfg in cfg.get("bots", {}).items():
        bots.append({
            "name": name,
            "profile": bot_cfg.get("feishu_profile", ""),
            "app_id": bot_cfg.get("feishu_app_id", ""),
            "model": bot_cfg.get("claude", {}).get("models", {}).get("default", ""),
            "thinking_model": bot_cfg.get("claude", {}).get("models", {}).get("thinking", ""),
        })
    return {
        "running": running,
        "pid": bridge_process.pid if running else None,
        "bot_count": len(bots),
        "bots": bots,
    }


@app.get("/api/config")
async def api_get_config():
    return load_config()


@app.post("/api/config/bots/{name}")
async def api_add_bot(name: str, data: dict):
    cfg = load_config()
    if name in cfg.setdefault("bots", {}):
        raise HTTPException(409, f"Bot '{name}' already exists")
    cfg["bots"][name] = data
    save_config(cfg)
    broadcast_log(f"[GUI] Bot added: {name}")
    return {"ok": True}


@app.put("/api/config/bots/{name}")
async def api_update_bot(name: str, data: dict):
    cfg = load_config()
    if name not in cfg.get("bots", {}):
        raise HTTPException(404, f"Bot '{name}' not found")
    cfg["bots"][name] = data
    save_config(cfg)
    broadcast_log(f"[GUI] Bot updated: {name}")
    return {"ok": True}


@app.delete("/api/config/bots/{name}")
async def api_remove_bot(name: str):
    cfg = load_config()
    if name not in cfg.get("bots", {}):
        raise HTTPException(404, f"Bot '{name}' not found")
    del cfg["bots"][name]
    save_config(cfg)
    broadcast_log(f"[GUI] Bot removed: {name}")
    return {"ok": True}


@app.post("/api/bridge/start")
async def api_start_bridge():
    global bridge_process
    if bridge_process and bridge_process.poll() is None:
        return {"ok": False, "error": "Bridge is already running"}

    broadcast_log("[GUI] Starting bridge...")

    bridge_py = str(BRIDGE_DIR / "bridge.py")
    try:
        bridge_process = subprocess.Popen(
            [sys.executable, "-u", bridge_py],
            cwd=str(BRIDGE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception as e:
        broadcast_log(f"[GUI] Failed to start bridge: {e}")
        return {"ok": False, "error": str(e)}

    # Background reader
    def _read_bridge_output():
        global bridge_process
        for line in iter(bridge_process.stdout.readline, ""):
            if line:
                broadcast_log(line.rstrip())
        broadcast_log("[GUI] Bridge process exited")
        bridge_process = None

    import threading
    t = threading.Thread(target=_read_bridge_output, daemon=True)
    t.start()

    return {"ok": True, "pid": bridge_process.pid}


@app.post("/api/bridge/stop")
async def api_stop_bridge():
    global bridge_process
    if not bridge_process or bridge_process.poll() is not None:
        return {"ok": False, "error": "Bridge is not running"}

    broadcast_log("[GUI] Stopping bridge...")
    bridge_process.terminate()
    try:
        bridge_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        bridge_process.kill()
        bridge_process.wait()
    bridge_process = None
    broadcast_log("[GUI] Bridge stopped")
    return {"ok": True}


@app.get("/api/profiles")
async def api_list_profiles():
    code, stdout, stderr = run_lark("profile", "list")
    if code != 0:
        return {"profiles": [], "error": stderr}
    try:
        profiles = json.loads(stdout)
        return {"profiles": profiles}
    except json.JSONDecodeError:
        return {"profiles": [], "error": stdout}


@app.delete("/api/profiles/{name}")
async def api_remove_profile(name: str):
    code, stdout, stderr = run_lark("profile", "remove", name)
    if code != 0:
        raise HTTPException(400, stderr or stdout)
    return {"ok": True}


@app.post("/api/profiles")
async def api_add_profile(data: dict):
    name = data.get("name", "")
    app_id = data.get("app_id", "")
    app_secret = data.get("app_secret", "")
    brand = data.get("brand", "feishu")
    if not name or not app_id or not app_secret:
        raise HTTPException(400, "name, app_id, app_secret required")

    r = subprocess.run(
        [LARK_CLI, "profile", "add", "--name", name, "--app-id", app_id, "--brand", brand, "--use", "--app-secret-stdin"],
        input=app_secret, capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        raise HTTPException(400, r.stderr or r.stdout)
    return {"ok": True}


@app.get("/api/event-status")
async def api_event_status():
    code, stdout, stderr = run_lark("event", "status")
    return {"code": code, "output": stdout, "error": stderr}


# ═══════════════════════════════════════════════════
# WebSocket — live log streaming
# ═══════════════════════════════════════════════════

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    log_clients.append(ws)
    # Send recent buffer
    for entry in log_buffer[-100:]:
        try:
            await ws.send_text(entry)
        except Exception:
            break
    try:
        while True:
            await ws.receive_text()  # keep-alive pings from client
    except WebSocketDisconnect:
        pass
    finally:
        if ws in log_clients:
            log_clients.remove(ws)


# ═══════════════════════════════════════════════════
# Dashboard HTML
# ═══════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude-Feishu Bridge</title>
<style>
  :root { --bg:#1a1a2e; --card:#16213e; --border:#0f3460; --accent:#e94560; --text:#eee; --text2:#aaa; --green:#00d2a0; --yellow:#f0c040; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:'Segoe UI',system-ui,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }
  header { background:#0d0d1a; border-bottom:2px solid var(--accent); padding:12px 24px; display:flex; align-items:center; justify-content:space-between; }
  header h1 { font-size:1.3em; display:flex; align-items:center; gap:10px; }
  .status-dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  .status-dot.on { background:var(--green); box-shadow:0 0 8px var(--green); }
  .status-dot.off { background:#555; }
  nav { display:flex; gap:4px; padding:0 24px; background:#0d0d1a; border-bottom:1px solid var(--border); }
  nav button { background:none; border:none; color:var(--text2); padding:10px 18px; cursor:pointer; font-size:0.9em; border-bottom:2px solid transparent; transition:all .2s; }
  nav button:hover { color:var(--text); }
  nav button.active { color:var(--accent); border-bottom-color:var(--accent); }
  main { padding:24px; max-width:1200px; margin:0 auto; }
  .tab { display:none; }
  .tab.active { display:block; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:20px; margin-bottom:16px; }
  .card h3 { margin-bottom:12px; font-size:1em; color:var(--text2); text-transform:uppercase; letter-spacing:1px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .grid3 { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }
  .stat { text-align:center; }
  .stat .num { font-size:2em; font-weight:bold; color:var(--accent); }
  .stat .label { font-size:0.8em; color:var(--text2); margin-top:4px; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:0.85em; }
  th { color:var(--text2); font-weight:600; }
  .btn { padding:6px 14px; border:none; border-radius:4px; cursor:pointer; font-size:0.85em; transition:all .2s; }
  .btn-primary { background:var(--accent); color:#fff; }
  .btn-primary:hover { opacity:0.85; }
  .btn-sm { padding:4px 10px; font-size:0.78em; }
  .btn-outline { background:none; border:1px solid var(--border); color:var(--text); }
  .btn-outline:hover { border-color:var(--accent); color:var(--accent); }
  .btn-danger { background:#c0392b; color:#fff; }
  .btn-success { background:var(--green); color:#000; }
  .btn-group { display:flex; gap:8px; margin-top:12px; }
  input,select,textarea { width:100%; padding:8px 10px; background:#0d0d1a; border:1px solid var(--border); border-radius:4px; color:var(--text); font-size:0.85em; font-family:inherit; }
  input:focus,select:focus,textarea:focus { outline:none; border-color:var(--accent); }
  label { display:block; margin-bottom:4px; font-size:0.82em; color:var(--text2); }
  .form-group { margin-bottom:12px; }
  .form-row { display:flex; gap:12px; }
  .form-row > * { flex:1; }
  .log-viewer { background:#0a0a16; border:1px solid var(--border); border-radius:6px; height:500px; overflow-y:auto; padding:12px; font-family:'Cascadia Code','Consolas',monospace; font-size:0.8em; line-height:1.5; }
  .log-viewer .log-line { white-space:pre-wrap; word-break:break-all; }
  .modal-overlay { display:none; position:fixed; top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6); z-index:100; align-items:center; justify-content:center; }
  .modal-overlay.show { display:flex; }
  .modal { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:24px; width:90%; max-width:560px; max-height:90vh; overflow-y:auto; }
  .modal h3 { margin-bottom:16px; }
  .toast { position:fixed; bottom:24px; right:24px; padding:10px 20px; border-radius:6px; font-size:0.85em; z-index:200; animation:fadein .3s; }
  .toast-ok { background:var(--green); color:#000; }
  .toast-err { background:var(--accent); color:#fff; }
  @keyframes fadein { from{opacity:0;transform:translateY(10px);} to{opacity:1;transform:translateY(0);} }
  .empty { text-align:center; padding:40px; color:var(--text2); }
  .mask { font-family:monospace; color:var(--text2); }
</style>
</head>
<body>

<header>
  <h1><span class="status-dot off" id="statusDot"></span>Claude-Feishu Bridge</h1>
  <div>
    <button class="btn btn-success btn-sm" onclick="startBridge()" id="btnStart">Start Bridge</button>
    <button class="btn btn-danger btn-sm" onclick="stopBridge()" id="btnStop" style="display:none">Stop Bridge</button>
  </div>
</header>

<nav>
  <button class="active" data-tab="tab-dashboard">Dashboard</button>
  <button data-tab="tab-bots">Bots</button>
  <button data-tab="tab-profiles">Profiles</button>
  <button data-tab="tab-logs">Logs</button>
</nav>

<main>
<!-- DASHBOARD -->
<div class="tab active" id="tab-dashboard">
  <div class="grid3" style="margin-bottom:16px">
    <div class="card stat"><div class="num" id="statBots">-</div><div class="label">Bots</div></div>
    <div class="card stat"><div class="num" id="statStatus">-</div><div class="label">Status</div></div>
    <div class="card stat"><div class="num" id="statPID">-</div><div class="label">PID</div></div>
  </div>
  <div class="card">
    <h3>Bot Overview</h3>
    <table><thead><tr><th>Name</th><th>Profile</th><th>App ID</th><th>Default Model</th><th>Thinking Model</th></tr></thead>
    <tbody id="botTable"></tbody></table>
    <div class="empty" id="botEmpty" style="display:none">No bots configured. Go to the Bots tab to add one.</div>
  </div>
</div>

<!-- BOTS -->
<div class="tab" id="tab-bots">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
    <h2 style="font-size:1.1em">Bot Configurations</h2>
    <button class="btn btn-primary" onclick="showBotModal()">+ Add Bot</button>
  </div>
  <div class="card" id="botCards"></div>
  <div class="empty" id="botCardsEmpty" style="display:none">No bots. Click "+ Add Bot" to create one.</div>
</div>

<!-- PROFILES -->
<div class="tab" id="tab-profiles">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
    <h2 style="font-size:1.1em">lark-cli Profiles</h2>
    <button class="btn btn-primary" onclick="showProfileModal()">+ Add Profile</button>
  </div>
  <div class="card" id="profileCards"></div>
  <div class="empty" id="profileCardsEmpty" style="display:none">No profiles. Click "+ Add Profile" to register a Feishu app.</div>
</div>

<!-- LOGS -->
<div class="tab" id="tab-logs">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
    <h2 style="font-size:1.1em">Live Logs</h2>
    <button class="btn btn-outline btn-sm" onclick="clearLogs()">Clear</button>
  </div>
  <div class="log-viewer" id="logViewer"></div>
</div>
</main>

<!-- Bot Modal -->
<div class="modal-overlay" id="botModalOverlay">
  <div class="modal">
    <h3 id="botModalTitle">Add Bot</h3>
    <input type="hidden" id="botEditName">
    <div class="form-row">
      <div class="form-group"><label>Bot Name</label><input id="bmName" placeholder="e.g. default"></div>
      <div class="form-group"><label>Feishu Profile</label><input id="bmProfile" placeholder="lark-cli profile name"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Feishu App ID</label><input id="bmAppId" placeholder="cli_xxx"></div>
      <div class="form-group"><label>Feishu App Secret</label><input id="bmAppSecret" type="password" placeholder="App Secret"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>API Key</label><input id="bmApiKey" placeholder="sk-xxx or tp-xxx"></div>
      <div class="form-group"><label>Base URL</label><input id="bmBaseUrl" placeholder="https://..."></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Default Model</label><input id="bmModel" placeholder="mimo-v2.5"></div>
      <div class="form-group"><label>Thinking Model</label><input id="bmThinkModel" placeholder="mimo-v2.5-pro"></div>
      <div class="form-group"><label>Max Tokens</label><input id="bmMaxTokens" type="number" value="8192"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>System Prompt</label><textarea id="bmSysPrompt" rows="2" placeholder="你是一个通过飞书与用户交流的AI助手。回答简洁清晰。"></textarea></div>
      <div class="form-group"><label>Max Context</label><input id="bmMaxCtx" type="number" value="20"></div>
    </div>
    <div class="btn-group" style="justify-content:flex-end">
      <button class="btn btn-outline" onclick="closeBotModal()">Cancel</button>
      <button class="btn btn-primary" onclick="saveBot()">Save</button>
    </div>
  </div>
</div>

<!-- Profile Modal -->
<div class="modal-overlay" id="profileModalOverlay">
  <div class="modal">
    <h3>Add Lark-CLI Profile</h3>
    <div class="form-row">
      <div class="form-group"><label>Profile Name</label><input id="pmName" placeholder="e.g. my-bot"></div>
      <div class="form-group"><label>Brand</label><select id="pmBrand"><option value="feishu">Feishu</option><option value="lark">Lark</option></select></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>App ID</label><input id="pmAppId" placeholder="cli_xxx"></div>
      <div class="form-group"><label>App Secret</label><input id="pmAppSecret" type="password" placeholder="App Secret"></div>
    </div>
    <div class="btn-group" style="justify-content:flex-end">
      <button class="btn btn-outline" onclick="closeProfileModal()">Cancel</button>
      <button class="btn btn-primary" onclick="addProfile()">Add</button>
    </div>
  </div>
</div>

<script>
// ── Navigation ──
document.querySelectorAll('nav button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById(btn.dataset.tab).classList.add('active');
  });
});

// ── Toast ──
function toast(msg, ok=true) {
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'toast-ok' : 'toast-err');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ── API helpers ──
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (!r.ok) { const e = await r.json(); throw new Error(e.detail || r.statusText); }
  return r.json();
}

// ── Status polling ──
let bridgeRunning = false;
async function pollStatus() {
  try {
    const s = await api('GET', '/api/status');
    bridgeRunning = s.running;
    document.getElementById('statusDot').className = 'status-dot ' + (s.running ? 'on' : 'off');
    document.getElementById('statBots').textContent = s.bot_count;
    document.getElementById('statStatus').textContent = s.running ? 'Running' : 'Stopped';
    document.getElementById('statPID').textContent = s.pid || '-';
    document.getElementById('btnStart').style.display = s.running ? 'none' : '';
    document.getElementById('btnStop').style.display = s.running ? '' : 'none';

    // Bot table
    const tb = document.getElementById('botTable');
    const empty = document.getElementById('botEmpty');
    tb.innerHTML = s.bots.map(b => `<tr><td><strong>${esc(b.name)}</strong></td><td>${esc(b.profile)}</td><td class="mask">${esc(b.app_id).slice(0,12)}...</td><td>${esc(b.model)}</td><td>${esc(b.thinking_model)}</td></tr>`).join('');
    empty.style.display = s.bots.length ? 'none' : '';
  } catch(e) { console.error(e); }
}

// ── Bot management ──
async function loadBots() {
  try {
    const cfg = await api('GET', '/api/config');
    const bots = cfg.bots || {};
    const container = document.getElementById('botCards');
    const empty = document.getElementById('botCardsEmpty');
    const entries = Object.entries(bots);
    container.innerHTML = entries.map(([name,b]) => {
      const cc = b.claude || {};
      return `<div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <h3 style="margin:0">${esc(name)} <span class="mask">(${esc(b.feishu_profile)} &rarr; ${esc(b.feishu_app_id||'').slice(0,12)}...)</span></h3>
          <div class="btn-group" style="margin:0">
            <button class="btn btn-outline btn-sm" onclick="editBot('${esc(name)}')">Edit</button>
            <button class="btn btn-danger btn-sm" onclick="deleteBot('${esc(name)}')">Delete</button>
          </div>
        </div>
        <div class="grid2" style="margin-top:12px">
          <div><div class="label">Default Model</div>${esc(cc.models?.default||'-')}</div>
          <div><div class="label">Thinking Model</div>${esc(cc.models?.thinking||'-')}</div>
          <div><div class="label">Max Tokens</div>${cc.max_tokens||'-'}</div>
          <div><div class="label">Max Context</div>${b.max_context_messages||'-'}</div>
        </div>
        <div style="margin-top:8px"><div class="label">System Prompt</div><span style="font-size:0.85em;color:var(--text2)">${esc((cc.system_prompt||'').slice(0,120))}${(cc.system_prompt||'').length>120?'...':''}</span></div>
      </div>`;
    }).join('');
    empty.style.display = entries.length ? 'none' : '';
  } catch(e) { console.error(e); }
}

function showBotModal(name) {
  document.getElementById('botModalTitle').textContent = name ? 'Edit Bot' : 'Add Bot';
  document.getElementById('botEditName').value = name || '';
  document.getElementById('botModalOverlay').classList.add('show');
  if (name) {
    api('GET','/api/config').then(cfg => {
      const b = (cfg.bots||{})[name];
      if (!b) return;
      const cc = b.claude || {};
      document.getElementById('bmName').value = name;
      document.getElementById('bmProfile').value = b.feishu_profile || '';
      document.getElementById('bmAppId').value = b.feishu_app_id || '';
      document.getElementById('bmAppSecret').value = b.feishu_app_secret || '';
      document.getElementById('bmApiKey').value = cc.api_key || '';
      document.getElementById('bmBaseUrl').value = cc.base_url || '';
      document.getElementById('bmModel').value = cc.models?.default || '';
      document.getElementById('bmThinkModel').value = cc.models?.thinking || '';
      document.getElementById('bmMaxTokens').value = cc.max_tokens || 8192;
      document.getElementById('bmSysPrompt').value = cc.system_prompt || '';
      document.getElementById('bmMaxCtx').value = b.max_context_messages || 20;
    });
  } else {
    ['bmName','bmProfile','bmAppId','bmAppSecret','bmApiKey','bmBaseUrl','bmModel','bmThinkModel','bmSysPrompt'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('bmMaxTokens').value = 8192;
    document.getElementById('bmMaxCtx').value = 20;
  }
}

function closeBotModal() { document.getElementById('botModalOverlay').classList.remove('show'); }

async function saveBot() {
  const editName = document.getElementById('botEditName').value;
  const name = document.getElementById('bmName').value.trim();
  if (!name) { toast('Bot name is required', false); return; }
  const bot = {
    feishu_profile: document.getElementById('bmProfile').value.trim(),
    feishu_app_id: document.getElementById('bmAppId').value.trim(),
    feishu_app_secret: document.getElementById('bmAppSecret').value.trim(),
    claude: {
      api_key: document.getElementById('bmApiKey').value.trim(),
      base_url: document.getElementById('bmBaseUrl').value.trim(),
      models: {
        default: document.getElementById('bmModel').value.trim(),
        thinking: document.getElementById('bmThinkModel').value.trim()
      },
      max_tokens: parseInt(document.getElementById('bmMaxTokens').value) || 8192,
      system_prompt: document.getElementById('bmSysPrompt').value.trim()
    },
    max_context_messages: parseInt(document.getElementById('bmMaxCtx').value) || 20
  };
  try {
    if (editName) {
      await api('PUT', '/api/config/bots/' + editName, bot);
      if (name !== editName) {
        // Rename: delete old + add new
        await api('DELETE', '/api/config/bots/' + editName);
        await api('POST', '/api/config/bots/' + name, bot);
      }
    } else {
      await api('POST', '/api/config/bots/' + name, bot);
    }
    closeBotModal();
    loadBots();
    pollStatus();
    toast(editName ? 'Bot updated' : 'Bot added');
  } catch(e) { toast(e.message, false); }
}

async function editBot(name) { showBotModal(name); }

async function deleteBot(name) {
  if (!confirm(`Delete bot "${name}"?`)) return;
  try {
    await api('DELETE', '/api/config/bots/' + name);
    loadBots();
    pollStatus();
    toast('Bot deleted');
  } catch(e) { toast(e.message, false); }
}

// ── Profiles ──
async function loadProfiles() {
  try {
    const data = await api('GET', '/api/profiles');
    const profiles = data.profiles || [];
    const container = document.getElementById('profileCards');
    const empty = document.getElementById('profileCardsEmpty');
    container.innerHTML = profiles.map(p => `<div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div><strong>${esc(p.name)}</strong> ${p.active ? '<span style="color:var(--green);font-size:0.8em">(active)</span>' : ''}</div>
        <div><span class="mask">${esc(p.appId||'')}</span> &middot; ${esc(p.brand||'feishu')}</div>
      </div>
    </div>`).join('');
    empty.style.display = profiles.length ? 'none' : '';
  } catch(e) { toast(e.message, false); }
}

function showProfileModal() { document.getElementById('profileModalOverlay').classList.add('show'); }
function closeProfileModal() { document.getElementById('profileModalOverlay').classList.remove('show'); }

async function addProfile() {
  const data = {
    name: document.getElementById('pmName').value.trim(),
    app_id: document.getElementById('pmAppId').value.trim(),
    app_secret: document.getElementById('pmAppSecret').value.trim(),
    brand: document.getElementById('pmBrand').value
  };
  if (!data.name || !data.app_id || !data.app_secret) { toast('All fields required', false); return; }
  try {
    await api('POST', '/api/profiles', data);
    closeProfileModal();
    loadProfiles();
    toast('Profile added');
  } catch(e) { toast(e.message, false); }
}

// ── Logs ──
let logSocket = null;
function connectLogs() {
  if (logSocket) { logSocket.close(); }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  logSocket = new WebSocket(proto + '//' + location.host + '/ws/logs');
  logSocket.onmessage = (e) => {
    const viewer = document.getElementById('logViewer');
    const atBottom = viewer.scrollTop + viewer.clientHeight >= viewer.scrollHeight - 10;
    const div = document.createElement('div');
    div.className = 'log-line';
    div.textContent = e.data;
    viewer.appendChild(div);
    if (atBottom) viewer.scrollTop = viewer.scrollHeight;
    // Keep max 1000 lines in DOM
    while (viewer.children.length > 1000) viewer.firstChild.remove();
  };
  logSocket.onclose = () => { setTimeout(connectLogs, 3000); };
}

function clearLogs() {
  document.getElementById('logViewer').innerHTML = '';
}

// ── Bridge control ──
async function startBridge() {
  try {
    const r = await api('POST', '/api/bridge/start');
    if (r.ok) { pollStatus(); toast('Bridge started'); }
    else { toast(r.error, false); }
  } catch(e) { toast(e.message, false); }
}

async function stopBridge() {
  try {
    await api('POST', '/api/bridge/stop');
    pollStatus();
    toast('Bridge stopped');
  } catch(e) { toast(e.message, false); }
}

function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// ── Init ──
pollStatus();
loadBots();
loadProfiles();
connectLogs();
setInterval(pollStatus, 5000);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


def main():
    parser = argparse.ArgumentParser(description="Claude-Feishu Bridge Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}")
        print("Create bridge-config.json first, or copy from bridge-config.example.json")
        sys.exit(1)

    print(f" Dashboard: http://{args.host}:{args.port}")
    print(f" Bridge dir: {BRIDGE_DIR}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
