"""
ui/dashboard.py — Optional web dashboard for JARVIS.
Runs a lightweight FastAPI + HTML interface showing live status,
command history, and manual text input.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from config.settings import Settings


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JARVIS Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;600;900&display=swap" rel="stylesheet">
<style>
  :root {
    --cyan: #00f5ff;
    --blue: #0066ff;
    --dark: #020a14;
    --panel: #040e1e;
    --border: rgba(0,245,255,0.2);
    --glow: 0 0 20px rgba(0,245,255,0.3);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--dark);
    color: var(--cyan);
    font-family: 'Share Tech Mono', monospace;
    min-height: 100vh;
    overflow-x: hidden;
  }
  body::before {
    content: '';
    position: fixed; inset: 0;
    background: repeating-linear-gradient(
      0deg, transparent, transparent 2px,
      rgba(0,245,255,0.015) 2px, rgba(0,245,255,0.015) 4px
    );
    pointer-events: none; z-index: 0;
  }
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 1.5rem 2rem;
    border-bottom: 1px solid var(--border);
    background: rgba(0,245,255,0.04);
    position: relative; z-index: 1;
  }
  .logo { font-family: 'Exo 2', sans-serif; font-weight: 900; font-size: 2rem; letter-spacing: 0.3em; }
  .logo span { color: white; }
  .status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #00ff88; box-shadow: 0 0 10px #00ff88;
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .status-label { font-size: 0.75rem; margin-left: 0.5rem; color: #00ff88; }
  .grid { display: grid; grid-template-columns: 1fr 380px; gap: 1.5rem; padding: 1.5rem; position: relative; z-index: 1; }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 4px;
    box-shadow: var(--glow);
  }
  .panel-header {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.7rem; letter-spacing: 0.2em; color: rgba(0,245,255,0.6);
  }
  #log {
    height: 420px; overflow-y: auto;
    padding: 1rem; font-size: 0.82rem; line-height: 1.8;
  }
  #log::-webkit-scrollbar { width: 4px; }
  #log::-webkit-scrollbar-thumb { background: var(--border); }
  .log-entry { border-left: 2px solid var(--border); padding-left: 0.75rem; margin-bottom: 0.5rem; }
  .log-entry.success { border-color: #00ff88; }
  .log-entry.error { border-color: #ff4444; color: #ff8888; }
  .log-entry.info { border-color: var(--cyan); }
  .log-ts { font-size: 0.68rem; opacity: 0.5; }
  .input-area { padding: 1rem; }
  #cmd-input {
    width: 100%; background: rgba(0,245,255,0.05);
    border: 1px solid var(--border); border-radius: 2px;
    color: var(--cyan); font-family: inherit; font-size: 0.9rem;
    padding: 0.75rem 1rem; outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
  }
  #cmd-input:focus { border-color: var(--cyan); box-shadow: var(--glow); }
  #cmd-input::placeholder { color: rgba(0,245,255,0.3); }
  #send-btn {
    width: 100%; margin-top: 0.75rem;
    background: transparent; border: 1px solid var(--cyan);
    color: var(--cyan); font-family: 'Exo 2', sans-serif;
    font-weight: 600; letter-spacing: 0.2em;
    padding: 0.75rem; cursor: pointer; border-radius: 2px;
    transition: all 0.2s;
  }
  #send-btn:hover { background: rgba(0,245,255,0.1); box-shadow: var(--glow); }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; padding: 1rem; }
  .stat { text-align: center; }
  .stat-val { font-family: 'Exo 2', sans-serif; font-size: 2rem; font-weight: 900; }
  .stat-lbl { font-size: 0.65rem; opacity: 0.5; letter-spacing: 0.15em; }
  .history { height: 200px; overflow-y: auto; padding: 0.75rem 1rem; font-size: 0.78rem; }
  .history-item { padding: 0.3rem 0; border-bottom: 1px solid rgba(0,245,255,0.07); cursor: pointer; }
  .history-item:hover { color: white; }
  #voice-indicator {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.75rem 1rem; font-size: 0.75rem; color: rgba(0,245,255,0.5);
  }
  .bars { display: flex; align-items: center; gap: 2px; height: 20px; }
  .bar { width: 3px; background: var(--cyan); border-radius: 1px; }
  .bar:nth-child(1) { animation: bar1 0.8s ease infinite; }
  .bar:nth-child(2) { animation: bar2 0.9s ease infinite 0.1s; }
  .bar:nth-child(3) { animation: bar3 0.7s ease infinite 0.2s; }
  .bar:nth-child(4) { animation: bar2 1.0s ease infinite 0.3s; }
  .bar:nth-child(5) { animation: bar1 0.8s ease infinite 0.1s; }
  @keyframes bar1 { 0%,100%{height:4px} 50%{height:18px} }
  @keyframes bar2 { 0%,100%{height:8px} 50%{height:14px} }
  @keyframes bar3 { 0%,100%{height:12px} 50%{height:6px} }
</style>
</head>
<body>
<header>
  <div class="logo"><span>J</span>ARVIS</div>
  <div style="display:flex;align-items:center;gap:1rem">
    <div id="voice-indicator">
      <div class="bars">
        <div class="bar"></div><div class="bar"></div><div class="bar"></div>
        <div class="bar"></div><div class="bar"></div>
      </div>
      LISTENING
    </div>
    <div style="display:flex;align-items:center">
      <div class="status-dot"></div>
      <span class="status-label">ONLINE</span>
    </div>
  </div>
</header>

<div class="grid">
  <div>
    <div class="panel">
      <div class="panel-header">▸ ACTIVITY LOG</div>
      <div id="log"></div>
    </div>
  </div>

  <div style="display:flex;flex-direction:column;gap:1rem">
    <div class="panel">
      <div class="panel-header">▸ MANUAL INPUT</div>
      <div class="input-area">
        <input id="cmd-input" placeholder="Type a command..." autocomplete="off">
        <button id="send-btn" onclick="sendCommand()">EXECUTE</button>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">▸ SESSION STATS</div>
      <div class="stats">
        <div class="stat"><div class="stat-val" id="cmd-count">0</div><div class="stat-lbl">COMMANDS</div></div>
        <div class="stat"><div class="stat-val" id="success-rate">—</div><div class="stat-lbl">SUCCESS</div></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">▸ HISTORY</div>
      <div class="history" id="history"></div>
    </div>
  </div>
</div>

<script>
  let cmdCount = 0, successCount = 0;

  function ts() {
    return new Date().toLocaleTimeString('en-US', {hour12: false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
  }

  function addLog(msg, type='info') {
    const log = document.getElementById('log');
    const div = document.createElement('div');
    div.className = `log-entry ${type}`;
    div.innerHTML = `<span class="log-ts">${ts()}</span>  ${msg}`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function addHistory(cmd) {
    const hist = document.getElementById('history');
    const div = document.createElement('div');
    div.className = 'history-item';
    div.textContent = `> ${cmd}`;
    div.onclick = () => { document.getElementById('cmd-input').value = cmd; };
    hist.insertBefore(div, hist.firstChild);
  }

  async function sendCommand() {
    const input = document.getElementById('cmd-input');
    const cmd = input.value.trim();
    if (!cmd) return;
    input.value = '';

    addLog(`⚡ ${cmd}`, 'info');
    addHistory(cmd);
    cmdCount++;
    document.getElementById('cmd-count').textContent = cmdCount;

    try {
      const resp = await fetch('/command', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({command: cmd})
      });
      const data = await resp.json();
      if (data.success) {
        successCount++;
        addLog(`✓ ${data.result || 'Done'}`, 'success');
      } else {
        addLog(`✗ ${data.error || 'Failed'}`, 'error');
      }
      const rate = Math.round((successCount / cmdCount) * 100);
      document.getElementById('success-rate').textContent = rate + '%';
    } catch(e) {
      addLog(`✗ Connection error: ${e.message}`, 'error');
    }
  }

  document.getElementById('cmd-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') sendCommand();
  });

  // Poll for new log entries
  async function pollLogs() {
    try {
      const resp = await fetch('/logs');
      const data = await resp.json();
      data.entries.forEach(e => addLog(e.message, e.type));
    } catch(e) {}
    setTimeout(pollLogs, 1000);
  }

  addLog('🤖 JARVIS control panel online.', 'success');
  addLog('Speak a command or type below.', 'info');
  pollLogs();
</script>
</body>
</html>"""


class Dashboard:
    """
    Lightweight web dashboard served via aiohttp.
    Exposes /command POST endpoint and /logs polling endpoint.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._log_queue: list[dict] = []
        self._planner = None
        self._executor = None

    def attach(self, planner, executor):
        """Connect planner and executor for text command execution."""
        self._planner = planner
        self._executor = executor

    def push_log(self, message: str, log_type: str = "info"):
        self._log_queue.append({
            "message": message,
            "type": log_type,
            "timestamp": time.time(),
        })
        # Keep last 200 entries
        if len(self._log_queue) > 200:
            self._log_queue = self._log_queue[-200:]

    async def start(self):
        try:
            from aiohttp import web
        except ImportError:
            print("⚠️  aiohttp not installed. Dashboard disabled. Run: pip install aiohttp")
            return

        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_post("/command", self._handle_command)
        app.router.add_get("/logs", self._handle_logs)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.settings.dashboard_host, self.settings.dashboard_port)
        await site.start()
        print(f"🌐 Dashboard: http://{self.settings.dashboard_host}:{self.settings.dashboard_port}")

    async def _handle_index(self, request):
        from aiohttp import web
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _handle_command(self, request):
        from aiohttp import web
        try:
            body = await request.json()
            command = body.get("command", "")
            if self._planner and self._executor:
                plan = await self._planner.plan(command)
                results = []
                for step in plan.steps:
                    r = await self._executor.execute(step)
                    results.append(r)
                self.push_log(f"✓ Completed: {plan.goal}", "success")
                return web.json_response({"success": True, "result": plan.goal})
            return web.json_response({"success": False, "error": "Planner not attached"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_logs(self, request):
        from aiohttp import web
        entries = self._log_queue.copy()
        self._log_queue.clear()
        return web.json_response({"entries": entries})
