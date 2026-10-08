# Copyright 2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Single-page front-end for the SEDAN GROUP robot console.

Served verbatim by :class:`dimos.web.console.module.RobotConsoleModule` at
``/``. No build step: vanilla HTML/CSS/JS. The page:

* embeds the existing Rerun web viewer by iframe (original Rerun content
  preserved) in the centre,
* renders a control-deck of buttons for the workflow operations on the left,
* renders a ChatGPT-style chat on the right that shows the user's messages,
  the agent's replies, and the internal tool input/output,
* carries a SEDAN GROUP logo and a tech-styled dashboard look.

All live data arrives over a single Server-Sent-Events stream (``/events``);
actions and chat messages go over ``/api/*``.
"""

from __future__ import annotations

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>SEDAN GROUP · Robot Console</title>
<style>
:root {
  --bg: #060a12;
  --bg-2: #0a1119;
  --panel: #0e1723;
  --panel-2: #122032;
  --line: #1b3350;
  --line-2: #24466b;
  --text: #e6f0fb;
  --muted: #7d94ad;
  --dim: #556a82;
  --accent: #21d4c8;      /* teal */
  --accent-2: #3aa0ff;    /* blue */
  --accent-3: #8b5cf6;    /* violet */
  --good: #22d67c;
  --warn: #f6b73c;
  --bad: #ff5d6c;
  --shadow: 0 8px 30px rgba(0,0,0,.55);
  --mono: "SFMono-Regular", "JetBrains Mono", "Roboto Mono", ui-monospace, monospace;
  --sans: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background:
    radial-gradient(1200px 600px at 80% -10%, rgba(58,160,255,.10), transparent 60%),
    radial-gradient(1000px 500px at -10% 110%, rgba(33,212,200,.08), transparent 55%),
    var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
}
/* ---- layout ---- */
.app { display: grid; grid-template-rows: 56px minmax(0,1fr) 30px; height: 100dvh; overflow:hidden; }
main { display: grid; grid-template-columns: 300px minmax(0,1fr) 400px; min-height: 0; overflow:hidden; }
aside.deck { border-right: 1px solid var(--line); background: var(--panel); overflow: auto; }
center.viz { display:flex; flex-direction:column; background: #05090f; min-width:0; min-height:0; text-align:left; }
.viewer { position:relative; flex:1; min-height:0; }
.backend-console { height:220px; flex:none; display:flex; flex-direction:column; border-top:1px solid var(--line-2); background:#08101a; }
.console-heading { display:flex; align-items:center; gap:12px; padding:10px 12px; border-bottom:1px solid var(--line); }
.console-heading h2 { margin:0; font-size:12px; color:var(--accent); }
.console-heading small { flex:1; color:var(--muted); font-size:10px; }
.console-heading button { color:var(--muted); background:var(--panel-2); border:1px solid var(--line); border-radius:6px; padding:4px 8px; cursor:pointer; }
aside.chat { border-left: 1px solid var(--line); background: var(--panel); display: flex; flex-direction: column; min-width:0; min-height:0; overflow:hidden; }

/* ---- header ---- */
header {
  display: flex; align-items: center; gap: 16px;
  padding: 0 18px; border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, #0b1420, #08101a);
}
.logo { display: flex; align-items: center; gap: 10px; }
.logo .mark {
  width: 30px; height: 30px; border-radius: 8px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  display: grid; place-items: center; font-weight: 800; color: #04101a;
  box-shadow: 0 0 18px rgba(33,212,200,.35);
}
.logo .name { font-weight: 800; letter-spacing: .14em; font-size: 15px; }
.logo .name small { display:block; font-weight: 600; letter-spacing:.22em; font-size:9px; color: var(--muted); }
.title { font-weight: 700; font-size: 14px; color: var(--text); letter-spacing:.02em; }
.title .sub { color: var(--muted); font-weight: 500; }
.spacer { flex: 1; }
.status-row { display: flex; gap: 8px; align-items: center; }
.badge {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 5px 11px; border-radius: 999px; font-size: 11.5px; font-weight: 600;
  border: 1px solid var(--line); background: var(--panel-2); color: var(--muted);
}
.badge .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); box-shadow: 0 0 8px currentColor; }
.badge.ok { color: var(--good); border-color: rgba(34,214,124,.35); }
.badge.ok .dot { background: var(--good); color: var(--good); }
.badge.warn { color: var(--warn); border-color: rgba(246,183,60,.35); }
.badge.warn .dot { background: var(--warn); color: var(--warn); }
.badge.bad { color: var(--bad); border-color: rgba(255,93,108,.35); }
.badge.bad .dot { background: var(--bad); color: var(--bad); }
.badge.busy { color: var(--accent); border-color: rgba(33,212,200,.35); }
.badge.busy .dot { background: var(--accent); color: var(--accent); animation: pulse 1s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }

/* ---- control deck ---- */
.deck h2 { font-size: 11px; letter-spacing: .16em; text-transform: uppercase; color: var(--muted); margin: 14px 16px 8px; }
.group { margin: 0 12px 6px; }
.btn {
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 11px 13px; margin: 6px 0; border-radius: 10px; cursor: pointer;
  border: 1px solid var(--line); background: var(--panel-2); color: var(--text);
  font: inherit; font-weight: 600; text-align: left; transition: .12s;
}
.btn:hover { border-color: var(--line-2); background: #16263a; transform: translateY(-1px); }
.btn:active { transform: translateY(0); }
.btn.primary { border-color: rgba(33,212,200,.4); background: linear-gradient(180deg, rgba(33,212,200,.14), rgba(33,212,200,.05)); }
.btn.primary:hover { border-color: var(--accent); }
.btn.human { border-color: rgba(246,183,60,.35); }
.btn .ic { width: 26px; height: 26px; border-radius: 8px; display: grid; place-items: center; background: #0b1622; color: var(--accent); font-size: 15px; flex: none; }
.btn .lbl { flex: 1; }
.deck-tooltip {
  position:fixed; z-index:100; width:320px; max-width:calc(100vw - 24px);
  padding:13px 16px; border:1px solid rgba(33,212,200,.4); border-radius:12px;
  background:linear-gradient(145deg,#132337,#09131f); color:var(--text);
  box-shadow:0 12px 36px rgba(0,0,0,.45); font-size:12px; line-height:1.6;
  pointer-events:none; overflow-wrap:anywhere;
}
.deck-tooltip[hidden] { display:none; }
.deck-tooltip strong { display:block; color:var(--accent); margin-bottom:5px; font-size:11px; letter-spacing:.06em; }
.btn .kind { font-size: 9.5px; color: var(--dim); letter-spacing: .08em; text-transform: uppercase; }
.deck .hint { padding: 6px 16px 0; color: var(--dim); font-size: 11px; }
.log { margin: 8px 12px; border: 1px solid var(--line); border-radius: 10px; background: #08101a; max-height: 180px; overflow: auto; }
.log .row { font-family: var(--mono); font-size: 11px; padding: 5px 9px; border-bottom: 1px solid var(--line); color: var(--muted); }
.log .row .t { color: var(--dim); margin-right: 8px; }
.log .row .k { color: var(--accent-2); }
.log .row.err { color: var(--bad); }
.log .row.ok { color: var(--good); }

/* ---- viz ---- */
.viz iframe { width: 100%; height: 100%; border: 0; background: #05090f; }
.viz .vbar {
  position: absolute; top: 10px; left: 12px; right: 12px;
  display: flex; align-items: center; gap: 10px; pointer-events: none; z-index: 3;
}
.viz .vbar .chip {
  pointer-events: auto; background: rgba(10,17,25,.8); backdrop-filter: blur(6px);
  border: 1px solid var(--line); border-radius: 8px; padding: 6px 11px; font-size: 11.5px; color: var(--muted);
}
.viz .vbar .chip b { color: var(--text); font-weight: 700; }
.viz .vbar .link { pointer-events: auto; color: var(--accent); cursor: pointer; text-decoration: underline; }
.viz .frame-fallback { position:absolute; inset:0; display:none; place-items:center; text-align:center; color:var(--muted); padding:24px; }
.viz .frame-fallback a { color: var(--accent); }

/* ---- chat ---- */
.chat .head {
  padding: 12px 14px; border-bottom: 1px solid var(--line);
  display: flex; align-items: center; gap: 10px;
}
.chat .head .ai { width: 26px; height: 26px; border-radius: 8px; display:grid; place-items:center;
  background: linear-gradient(135deg, var(--accent-3), var(--accent-2)); color:#fff; font-weight:800; font-size:12px; }
.chat .head .who { font-weight: 700; }
.chat .head .who small { display:block; color: var(--muted); font-weight: 500; font-size: 10.5px; }
.chat .msgs { flex: 1; min-height:0; overflow:auto; overflow-wrap:anywhere; padding: 14px 14px 6px; display: flex; flex-direction: column; gap: 12px; }
.chat .msgs > * { flex-shrink:0; }
.chat .head, .chat .in { flex-shrink:0; }
.stream-view { position:absolute; inset:0; background:#05090f; }
.stream-view img { width:100%; height:100%; object-fit:contain; }
.stream-view.pip { inset:54px 14px auto auto; width:30%; height:30%; min-width:120px; z-index:4; border:1px solid var(--accent); border-radius:10px; overflow:hidden; box-shadow:var(--shadow); }
.stream-label { position:absolute; bottom:6px; left:6px; background:rgba(0,0,0,.8); padding:4px 8px; border-radius:5px; color:var(--text); z-index:2; font-size:11px; }
.stream-switch { position:absolute; inset:0; width:100%; border:0; background:transparent; cursor:pointer; z-index:3; }
.stream-view:not(.pip) .stream-switch { display:none; }
.camera-note { position:absolute; inset:0; display:grid; place-items:center; color:var(--muted); pointer-events:none; }
.keyboard-bar { display:flex; align-items:center; gap:8px; padding:6px 12px; flex-wrap:wrap; font-size:11px; color:var(--muted); border-top:1px solid var(--line); flex:none; }
.keyboard-bar .btn { width:auto; padding:5px 10px; margin:0; font-size:11px; }
.msg { display: flex; gap: 10px; max-width: 100%; }
.msg .av { width: 28px; height: 28px; border-radius: 8px; flex: none; display:grid; place-items:center; font-size:11px; font-weight:800; }
.msg.user .av { background: #14324a; color: var(--accent-2); }
.msg.agent .av { background: linear-gradient(135deg, var(--accent-3), var(--accent-2)); color:#fff; }
.msg.system .av { background: #241a10; color: var(--warn); }
.bubble { padding: 9px 12px; border-radius: 12px; border: 1px solid var(--line); background: var(--panel-2); line-height: 1.45; }
.msg.user .bubble { border-radius: 12px 12px 3px 12px; background: #0f2436; border-color: rgba(58,160,255,.3); }
.msg.agent .bubble { border-radius: 12px 12px 12px 3px; }
.msg .meta { font-size: 10px; color: var(--dim); margin-bottom: 3px; letter-spacing:.04em; }
.bubble pre { font-family: var(--mono); font-size: 11.5px; white-space: pre-wrap; word-break: break-word; color: #cfe0f0; }
.bubble .txt { white-space: pre-wrap; word-break: break-word; }
/* tool card */
.tool {
  border: 1px solid var(--line-2); border-radius: 12px; background: #0b1826; overflow: hidden; max-width: 100%;
  border-left: 3px solid var(--accent);
}
.tool .th { display:flex; align-items:center; gap:8px; padding: 8px 12px; border-bottom: 1px solid var(--line); }
.tool .th .ic { width: 22px; height:22px; border-radius: 6px; display:grid; place-items:center; background:#0a1420; color: var(--accent); font-size: 12px; }
.tool .th .name { font-weight: 700; font-size: 12.5px; font-family: var(--mono); color: var(--accent); }
.tool .th .st { margin-left: auto; font-size: 10.5px; color: var(--muted); }
.tool .io { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--line); }
.tool .io > div { background: #0a1420; padding: 8px 10px; }
.tool .io .cap { font-size: 9.5px; text-transform: uppercase; letter-spacing:.1em; color: var(--dim); margin-bottom: 4px; }
.tool .io .in .cap { color: var(--accent-2); }
.tool .io .out .cap { color: var(--accent); }
.tool .io pre { margin: 0; font-family: var(--mono); font-size: 11px; white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow: auto; color: #cfe0f0; }
.tool .full { grid-column: 1 / -1; }
.msg.agent .tool .io { grid-template-columns: 1fr; }
/* typing indicator */
.typing { display: flex; gap: 10px; align-items: center; color: var(--muted); font-size: 12px; padding: 4px 2px; }
.typing .av { background: linear-gradient(135deg, var(--accent-3), var(--accent-2)); color:#fff; }
.typing .dots { display:inline-flex; gap:4px; }
.typing .dots i { width:6px; height:6px; border-radius:50%; background: var(--muted); animation: blink 1.2s infinite; }
.typing .dots i:nth-child(2){ animation-delay:.2s } .typing .dots i:nth-child(3){ animation-delay:.4s }
@keyframes blink { 0%,80%,100%{opacity:.25} 40%{opacity:1} }
/* chat input */
.chat .in { border-top: 1px solid var(--line); padding: 10px 12px; }
.chat .in .row { display: flex; gap: 8px; }
.chat .in textarea {
  flex: 1; resize: none; background: var(--bg-2); color: var(--text);
  border: 1px solid var(--line); border-radius: 10px; padding: 9px 11px; font: inherit; min-height: 42px; max-height: 140px;
}
.chat .in textarea:focus { outline: none; border-color: var(--accent); }
.chat .in .send {
  flex: none; width: 44px; border-radius: 10px; border: 1px solid var(--accent);
  background: linear-gradient(180deg, rgba(33,212,200,.2), rgba(33,212,200,.06)); color: var(--accent);
  cursor: pointer; font-size: 16px;
}
.chat .in .send:hover { background: rgba(33,212,200,.28); }
.chat .in .hint { color: var(--dim); font-size: 10.5px; margin-top: 6px; }

/* ---- footer ---- */
footer { display:flex; align-items:center; gap: 16px; padding: 0 16px; border-top: 1px solid var(--line); background: #08101a; color: var(--dim); font-size: 11px; }
footer .sep { color: var(--line-2); }
footer b { color: var(--muted); font-weight: 600; }
button:disabled { opacity:.45; cursor:not-allowed; transform:none; }
[hidden] { display:none !important; }
.header-btn { width:auto; margin:0; padding:6px 12px; }
dialog { color:var(--text); background:var(--panel); border:1px solid var(--line-2); border-radius:16px; padding:24px; width:min(760px,94vw); max-height:88vh; overflow:auto; box-shadow:var(--shadow); }
dialog::backdrop { background:rgba(0,0,0,.7); backdrop-filter:blur(4px); }
dialog h2 { margin:0 0 12px; }
dialog p, .note { color:var(--muted); line-height:1.5; }
.form-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
.field { display:flex; flex-direction:column; gap:6px; min-width:0; }
.field input, .field select { width:100%; color:var(--text); background:var(--bg-2); border:1px solid var(--line-2); padding:10px; border-radius:8px; font:inherit; }
.field input[type=checkbox] { width:auto; align-self:flex-start; }
.field small { color:var(--muted); }
.actions { display:flex; justify-content:flex-end; gap:12px; margin-top:20px; }
.actions .btn { width:auto; }
.error { color:var(--bad); white-space:pre-wrap; }
.section-title { grid-column:1/-1; font-size:12px; letter-spacing:.08em; color:var(--accent); border-top:1px solid var(--line); padding-top:12px; margin-top:8px; }
#alignment-detail { white-space:pre-wrap; padding:12px; font-size:12px; color:var(--muted); }
#stack-log, #diagnostic-output { white-space:pre-wrap; overflow-wrap:anywhere; max-height:220px; overflow:auto; padding:12px; font:11px var(--mono); }
#stack-log { flex:1; min-height:0; max-height:none; margin:0; text-align:left; color:var(--muted); }
.inventory { margin:12px; overflow:auto; font-size:11px; }
.inventory table { width:100%; border-collapse:collapse; }
.inventory td, .inventory th { padding:7px 4px; border-bottom:1px solid var(--line); text-align:left; overflow-wrap:anywhere; }
.inventory .btn { padding:5px; font-size:11px; }
@media(max-width:1100px) { main { grid-template-columns:260px minmax(0,1fr) 320px; } .title { display:none; } }
@media(max-width:800px) { .app { height:auto; min-height:100vh; grid-template-rows:auto 1fr auto; } header { flex-wrap:wrap; padding:10px; gap:8px; } main { grid-template-columns:1fr; } aside.deck { max-height:45vh; } center.viz { height:50vh; } aside.chat { height:65vh; } footer { flex-wrap:wrap; padding:10px; gap:8px; } .form-grid { grid-template-columns:1fr; } }

/* scrollbar */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: #17293c; border-radius: 8px; }
::-webkit-scrollbar-track { background: transparent; }
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="logo">
      <div class="mark">S</div>
      <div class="name">SEDAN&nbsp;GROUP<small>ROBOT CONSOLE</small></div>
    </div>
    <div class="title">Go2 Persistent Workflow <span class="sub">/ control deck</span></div>
    <div class="spacer"></div>
    <button class="btn header-btn" id="settings-open">Settings</button>
    <button class="btn header-btn" id="fullscreen-toggle">Full screen</button>
    <div class="status-row" id="status">
      <span class="badge" id="b-conn"><span class="dot"></span>connecting</span>
      <span class="badge" id="b-agent"><span class="dot"></span>agent idle</span>
      <span class="badge" id="b-align"><span class="dot"></span>aligning</span>
      <span class="badge" id="b-nav"><span class="dot"></span>nav ?</span>
    </div>
  </header>

  <main>
    <!-- CONTROL DECK -->
    <aside class="deck" id="deck">
      <div id="lifecycle" class="group" hidden>
        <h2>Robot stack</h2>
        <span class="badge" id="b-stack">stopped</span>
        <label class="field" for="stack-profile"><span>Blueprint</span>
          <select id="stack-profile"></select>
        </label>
        <label class="field" for="stack-connection"><span>Connection</span>
          <select id="stack-connection">
            <option value="robot">Real robot</option>
            <option value="replay">Replay (recorded data)</option>
            <option value="simulation">MuJoCo simulation</option>
          </select>
        </label>
        <small id="stack-profile-note" role="status"></small>
        <label class="field" id="stack-map-field" for="stack-map-mode"><span>Map mode</span>
          <select id="stack-map-mode"><option value="restore">Restore saved map</option><option value="new">New map</option></select>
        </label>
        <button class="btn primary" id="stack-start" data-help="Choose New map for the first run and Restore when reconnecting. Restore starts directly; rotation capture may rotate the robot automatically. Automatic tagging may call your model service. Supervise and keep the area clear.">Start robot stack</button>
        <button class="btn human" id="stack-stop" data-help="Save and stop only the robot stack launched by this console. This is not a hardware emergency stop.">Save and stop</button>
        <button class="btn human" id="stack-stop-without-save" data-help="Explicitly stop without a final map save. Any earlier autosaved file is retained. Use only after inspecting the save error.">Stop without saving</button>
        <div id="map-save-detail" role="status"></div>
      </div>
      <h2 id="deck-title">Control deck</h2>
      <div id="deck-groups"></div>
      <div class="group" id="alignment-panel">
        <h2>Alignment <span class="badge" id="alignment-phase">unknown</span></h2>
        <div id="alignment-detail" role="status"></div>
        <div id="alignment-quality"></div>
        <div id="alignment-heading"></div>
        <small>Blue: saved map · Orange: scan · Green: robot forward. Check position AND heading; fitness is not certainty.</small>
        <button class="btn" id="alignment-view" disabled>View candidate</button>
        <button class="btn" id="alignment-world-view">Back to normal view</button>
      </div>
      <div class="inventory" id="inventory"></div>
      <div class="group">
        <button class="btn" id="diagnostics" data-help="Inspect available MCP tools and modules without moving the robot.">MCP tools and modules</button>
        <pre id="diagnostic-output"></pre>
      </div>
      <h2>Operation log</h2>
      <div class="log" id="log"><div class="row"><span class="t">--:--:--</span>ready</div></div>
    </aside>

    <!-- RERUN (preserved) -->
    <center class="viz" id="viz">
      <div class="viewer">
      <div class="vbar">
        <span class="chip">Rerun · <b id="viz-host">live 3D</b></span>
        <span class="chip link" id="viz-open">open in new tab</span>
        <button class="chip link" id="viz-refresh">Refresh viewer</button>
      </div>
      <div class="stream-view" id="world-view">
        <iframe id="rr" title="Rerun 3D" style="display:none"></iframe>
        <span class="stream-label">3D</span>
        <button class="stream-switch" id="world-switch" aria-label="Make 3D the main view"></button>
      </div>
      <div class="stream-view pip" id="camera-view">
        <img id="camera-image" alt="Robot camera" hidden>
        <span class="camera-note" id="camera-note">Camera not ready</span>
        <span class="stream-label">Camera</span>
        <button class="stream-switch" id="camera-switch" aria-label="Make Camera the main view"></button>
      </div>
      <div class="frame-fallback" id="rr-fallback">
        <div>
          <p style="color:var(--text);font-weight:600;margin:0 0 8px">Rerun viewer</p>
          <p>The live Rerun viewer is served by the stack. <a id="rr-link" href="#">Open it in a new tab</a> to view the 3D map and camera feed.</p>
        </div>
      </div>
      </div>
      <div class="keyboard-bar" id="keyboard-bar" hidden>
        <button class="btn human" id="keyboard-enable">Enable keyboard</button>
        <div id="manual-pad" hidden>
          <button class="btn" data-drive="w" aria-label="Hold to move forward">Forward</button>
          <button class="btn" data-drive="s" aria-label="Hold to move backward">Back</button>
          <button class="btn" data-drive="a" aria-label="Hold to strafe left">Strafe left</button>
          <button class="btn" data-drive="d" aria-label="Hold to strafe right">Strafe right</button>
          <button class="btn" data-drive="q" aria-label="Hold to turn left">Turn left</button>
          <button class="btn" data-drive="e" aria-label="Hold to turn right">Turn right</button>
        </div>
        <span id="keyboard-status">Hold Space + W/S forward/back, A/D strafe, Q/E turn. Esc stops.</span>
      </div>
      <section class="backend-console" id="stack-logs" aria-label="Backend console">
        <div class="console-heading">
          <h2>Backend console</h2>
          <small>Stack output, operations and tool progress</small>
          <button type="button" id="console-clear">Clear</button>
        </div>
        <pre id="stack-log" role="log" aria-label="Backend execution output"></pre>
      </section>
    </center>

    <!-- CHAT -->
    <aside class="chat">
      <div class="head">
        <div class="ai">✦</div>
        <div class="who">Agent chat<small>talk to the robot · see tool I/O</small></div>
        <div class="spacer" style="flex:1"></div>
        <button class="chip link" id="speaker-toggle">Go2 speaker: Off</button>
        <span class="badge" id="b-thinking" style="display:none"><span class="dot"></span>thinking</span>
      </div>
      <div class="msgs" id="msgs" role="log" aria-label="Conversation"></div>
      <div id="persistent-controls">
      <label class="hint" for="navigation-speed">Live navigation speed limit:
        <output id="navigation-speed-value">0.55 m/s</output>
        <input id="navigation-speed" type="range" min="0.1" max="0.55" step="0.05" value="0.55"
          aria-label="Live navigation speed limit" style="width:100%" disabled>
        <span>Applies immediately to active navigation, including exploration. Actual speed may be lower; teleop and rotation are unchanged.</span>
      </label>
      <label class="hint" for="navigation-distance">Nearby stop distance:
        <output id="navigation-distance-value">1.0 m</output>
        <input id="navigation-distance" type="range" min="0.3" max="3" step="0.1" value="1"
          aria-label="Nearby stop distance" style="width:100%">
        <span>Applies to the next trip; precise navigation is unchanged.</span>
        <button type="button" class="chip link" id="visual-arrival-toggle">Visual arrival: Off</button>
        <button type="button" class="chip link" id="murmur-toggle" disabled>Murmur: Off</button>
        <span id="puppy-status" role="status">Enable Go2 speaker to start Puppy.</span>
      </label>
      </div>
      <div class="in">
        <div class="row">
          <textarea id="input" placeholder="Message the agent…  (Enter to send, Shift+Enter for newline)" rows="1"></textarea>
          <button class="send" id="send" title="Send">➤</button>
        </div>
        <div class="hint">The agent can call skills (tag · query · navigate) and the control deck mirrors them as buttons.</div>
      </div>
    </aside>
  </main>

  <footer>
    <span>SEDAN&nbsp;GROUP · Robot Console</span><span class="sep">·</span>
    <span>events: <b id="f-events">—</b></span><span class="sep">·</span>
    <span>mcp: <b id="f-mcp">—</b></span><span class="sep">·</span>
    <span>rerun: <b id="f-rr">—</b></span><span class="sep">·</span>
    <span id="f-note"></span>
  </footer>
</div>
<dialog id="operation-dialog">
  <form id="operation-form">
    <h2 id="operation-title"></h2>
    <p id="operation-description"></p>
    <div class="form-grid" id="operation-fields"></div>
    <p class="note" id="operation-warning"></p>
    <div class="actions"><button class="btn" type="button" id="operation-cancel">Cancel</button><button class="btn" type="button" id="operation-restore" hidden>No — use existing map</button><button class="btn primary" type="submit">Confirm</button></div>
  </form>
</dialog>
<dialog id="settings-dialog">
  <form id="settings-form" autocomplete="off">
    <h2>Settings</h2>
    <p>Changes apply on the next startup. Stop the robot stack before editing settings. Ordinary settings are saved locally. Keys are loaded from the project's .env file and are read-only here. Edit .env to change keys, then restart the robot stack. Local vLLM agent model: openai:your-model-name.</p>
    <div class="form-grid" id="settings-fields"></div>
    <p id="settings-error" class="error" role="alert"></p>
    <div class="actions"><button class="btn" type="button" id="settings-cancel">Cancel</button><button class="btn primary" id="settings-save" type="submit">Save settings</button></div>
  </form>
</dialog>

<script>
"use strict";
const $ = (s) => document.querySelector(s);
const el = (t, cls, html) => { const n = document.createElement(t); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const now = () => { const d = new Date(); return d.toTimeString().slice(0,8); };

let CONFIG = {};
const state = { nav: false, stack: null, agent: true, map: true };
const openToolCards = new Map();
const pendingEchoes = new Map();
let refreshing = false;
let sending = false;
let keyboardSocket = null;
let keyboardReady = false;
let driveHeld = false;
let taggingEnabled = null;
let speakerEnabled = false;
const pressedKeys = new Set();
let cameraBusy = false;

// ---------- helpers ----------
function log(kind, key, detail) {
  const row = el("div", "row" + (kind ? " " + kind : ""));
  row.innerHTML = `<span class="t">${now()}</span><span class="k">${esc(key)}</span>${detail ? esc(detail) : ""}`;
  const box = $("#log");
  box.insertBefore(row, box.firstChild);
  while (box.children.length > 60) box.removeChild(box.lastChild);
  appendStackLog(`[${now()}] [${kind || "info"}] ${key}${detail ? ": " + detail : ""}`);
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function setBadge(id, cls, label) { const b = $("#" + id); if (!b) return; b.className = "badge " + cls; b.innerHTML = `<span class="dot"></span>${esc(label)}`; }
function fmtTime() { return now(); }
async function api(url, data) {
  const options = data === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Console-Token":CONFIG.csrf_token || ""},body:JSON.stringify(data)};
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const result = await response.json();
  if (result.ok === false) throw new Error(result.error || "Operation failed");
  return result;
}

// ---------- Rerun embed ----------
function setupRerun(url) {
  const host = new URL(url).host;
  $("#viz-host").textContent = host;
  $("#f-rr").textContent = host;
  const iframe = $("#rr");
  iframe.onerror = () => {
    iframe.style.display = "none";
    const fb = $("#rr-fallback"); fb.style.display = "grid";
  };
  // Use the iframe by default; if the browser blocks it the fallback link remains.
  iframe.src = url;
  iframe.style.display = "block";
  $("#rr-fallback").style.display = "none";
  $("#viz-open").onclick = () => window.open(url, "_blank");
  const link = $("#rr-link"); if (link) link.href = url;
  $("#viz-refresh").onclick = () => setupRerun(url);
}
function showAlignmentView() {
  const url=new URL(CONFIG.rerun_url);
  const sources=url.searchParams.getAll("url").filter(source=>!source.endsWith(".rbl"));
  url.searchParams.delete("url");
  for(const source of sources)url.searchParams.append("url",source);
  url.searchParams.append("url",new URL("/api/view/alignment.rbl",location.href).href);
  setupRerun(url.href);
}

// ---------- operations / control deck ----------
function buildDeck() {
  const wrap = $("#deck-groups");
  wrap.innerHTML = "";
  const byGroup = {};
  for (const op of CONFIG.operations) {
    (byGroup[op.group] = byGroup[op.group] || []).push(op);
  }
  const order = ["Alignment", "Map", "Tagging", "Memory", "Navigation", "Exploration"];
  for (const group of order) {
    const ops = byGroup[group]; if (!ops) continue;
    const g = el("div", "group");
    g.innerHTML = `<h2>${esc(group)}</h2>`;
    for (const op of ops) {
      const btn = el("button", "btn" + (op.primary ? " primary" : "") + (op.human_only ? " human" : ""));
      btn.innerHTML = `
        <span class="ic">${opIcon(op)}</span>
        <span class="lbl">${esc(op.label)}</span>
        <span class="kind">${op.kind === "rpc" ? "rpc" : "skill"}</span>`;
      btn.onclick = () => runOp(op, btn);
      btn.dataset.operation = op.key;
      btn.dataset.help=op.description || "";
      g.appendChild(btn);
    }
    if(group==="Tagging") {
      const toggle=el("button","btn");
      toggle.id="tagging-toggle";toggle.textContent="Automatic tagging: Check status";
      toggle.dataset.help="Enable or pause automatic room and object tagging. Tagging may upload camera images to your model service.";
      toggle.onclick=toggleTagging;g.appendChild(toggle);
    }
    if(group==="Map") {
      const detail=el("p","note");detail.id="fusion-detail";
      detail.textContent="Map fusion status unavailable";g.appendChild(detail);
    }
    if(group==="Exploration") {
      const detail=el("p","note");detail.id="exploration-detail";
      detail.textContent="Exploration not started";g.appendChild(detail);
    }
    wrap.appendChild(g);
  }
}
function opIcon(op) {
  const map = { confirm_alignment: "✓", reject_alignment: "✕", save_map: "💾",
    finish_startup_capture: "⚑", cancel_startup_rotation: "↺",
    alignment_status: "◷", navigation_ready: "⦿",
    tag_object: "◈", tag_location: "⌖", query_memory_tags: "⌕",
    navigate_near_memory_tag: "⤖", navigate_to_memory_tag: "⤖", stop_navigation: "⏹" };
  return map[op.key] || "•";
}
function setupDeckTooltips() {
  const deck=$("#deck"),tip=el("div","deck-tooltip");
  tip.id="deck-tooltip";tip.setAttribute("role","tooltip");tip.hidden=true;
  const heading=el("strong"),text=el("div");tip.append(heading,text);document.body.appendChild(tip);
  let timer=null,target=null;
  const hide=()=>{
    clearTimeout(timer);timer=null;tip.hidden=true;
    if(target)target.removeAttribute("aria-describedby");
    target=null;
  };
  const schedule=button=>{
    if(!button || !button.dataset.help){hide();return;}
    if(target===button)return;
    hide();target=button;
    timer=setTimeout(()=>{
      if(!button.isConnected || document.querySelector("dialog[open]")){hide();return;}
      heading.textContent=button.querySelector(".lbl")?.textContent || button.textContent;
      text.textContent=button.dataset.help;tip.hidden=false;
      button.setAttribute("aria-describedby",tip.id);
      const rect=button.getBoundingClientRect(),box=tip.getBoundingClientRect();
      tip.style.left=Math.max(12,Math.min(rect.right+12,window.innerWidth-box.width-12))+"px";
      tip.style.top=Math.max(12,Math.min(rect.top,window.innerHeight-box.height-12))+"px";
    },1000);
  };
  deck.addEventListener("pointerover",e=>{
    if(e.pointerType==="touch")return;
    schedule(e.target.closest("button[data-help]"));
  });
  deck.addEventListener("pointerout",e=>{
    if(target && !target.contains(e.relatedTarget))hide();
  });
  deck.addEventListener("focusin",e=>schedule(e.target.closest("button[data-help]")));
  deck.addEventListener("focusout",hide);
  deck.addEventListener("pointerdown",hide);
  deck.addEventListener("click",hide);
  deck.addEventListener("scroll",hide);
  window.addEventListener("resize",hide);
  document.addEventListener("keydown",e=>{if(e.key==="Escape")hide();});
}
function requestOperation(op, preset = {}) {
  return new Promise(resolve => {
    const dialog = $("#operation-dialog");
    const form = $("#operation-form");
    $("#operation-title").textContent = op.label;
    $("#operation-description").textContent = op.description || "";
    const restore=$("#operation-restore");restore.hidden=!op.offer_restore;
    $("#operation-form button[type=submit]").textContent=op.confirm_label || (op.offer_restore?"Yes — overwrite":"Confirm");
    $("#operation-warning").textContent = op.human_only ? "Human approval required. Inspect Rerun before alignment approval; stop the robot before finishing capture. Navigation can move the robot." : "";
    const wrap = $("#operation-fields"); wrap.replaceChildren();
    for (const [name, spec] of Object.entries((op.schema || {}).properties || {})) {
      const label = el("label","field"); label.appendChild(el("span", "", esc(name)));
      const input = el(spec.enum?"select":"input"); input.name = name;
      if(spec.enum)for(const value of spec.enum){const option=el("option");option.value=value;option.textContent=value;input.appendChild(option);}
      if(["number","integer"].includes(spec.type)){input.type="number";input.step=spec.type==="integer"?"1":"any";if(spec.minimum!==undefined)input.min=spec.minimum;if(spec.maximum!==undefined)input.max=spec.maximum;}
      input.value = preset[name] ?? spec.default ?? "";
      input.required = ((op.schema || {}).required || []).includes(name);
      label.appendChild(input); const help = el("small"); help.textContent = spec.description || ""; label.appendChild(help); wrap.appendChild(label);
    }
    const finish = result => { dialog.close(); form.onsubmit = null; dialog.oncancel = null; restore.onclick=null;restore.hidden=true;resolve(result); };
    restore.onclick=()=>finish({use_existing_map:true});
    $("#operation-cancel").onclick = () => finish(null);
    dialog.oncancel = e => { e.preventDefault(); finish(null); };
    form.onsubmit = e => { e.preventDefault(); const args = {}; for (const input of wrap.querySelectorAll("input,select")) {const value=input.value.trim();if(input.required && !value) {input.setCustomValidity("Please enter a value");input.reportValidity();input.setCustomValidity("");return;}args[input.name]=input.type==="number"?Number(value):value;} finish(args); };
    dialog.showModal();
  });
}
async function runOp(op, btn, preset = {}) {
  const candidateId=["confirm_alignment","reject_alignment"].includes(op.key)?state.candidateId:null;
  if(["confirm_alignment","reject_alignment"].includes(op.key) && !candidateId){
    addSystem("No current alignment candidate. Wait for matching, then inspect the preview.");return;
  }
  let args = preset;
  const fields = (op.schema && op.schema.properties) || {};
  if (op.human_only || Object.keys(fields).length) {
    args = await requestOperation(op, preset);
    if (args === null) return;
  }
  btn.disabled = true;
  log("running", op.key, op.kind === "rpc" ? "rpc" : "skill");
  try {
    const card = addTool(makeToolCard(op.key, pretty(args), null, "call"));
    try {
      const res = await api("/api/action", {name:op.key,args,confirmed:op.human_only,...(candidateId?{candidate_id:candidateId}:{})});
      card.querySelector(".out pre").textContent = typeof res.result === "string" ? res.result : pretty(res.result);
      card.querySelector(".st").textContent = "result";
      log("ok", op.key, "done");
      if (op.key === "query_memory_tags") renderInventory(res.result);
      await refreshStatus();
    } catch(e) { card.querySelector(".out pre").textContent = e.message; card.querySelector(".st").textContent = "error"; throw e; }
  } catch (e) {
    log("err", op.key, String(e));
    addSystem("✗ " + op.label + " — " + e.message);
  }
  btn.disabled = false;
  updateControls();
}

function renderInventory(result) {
  if (typeof result === "string") { try {result = JSON.parse(result);} catch(e) {return;} }
  if (!result || !Array.isArray(result.tags)) return;
  const wrap = $("#inventory"); wrap.replaceChildren();
  const summary = el("p"); summary.textContent = `${result.matching_tag_count} matching / ${result.total_stored_tags} saved · world`; wrap.appendChild(summary);
  const table = el("table"); const head = el("tr"); for (const title of ["Name / ID","Position","Go"]) {const th=el("th");th.textContent=title;head.appendChild(th);}table.appendChild(head);
  for (const tag of result.tags) {
    const tr = el("tr"); for (const value of [tag.name + "\n" + tag.id, pretty(tag.position)]) {const td=el("td");td.textContent=value;tr.appendChild(td);}
    const td = el("td"); const btn=el("button","btn");btn.textContent="Navigate nearby";btn.dataset.help="Navigate near this saved tag using the configured arrival distance. Supervise robot movement.";btn.dataset.navigation="true";btn.disabled=!state.nav;btn.onclick=()=>runOp(CONFIG.operations.find(op=>op.key==="navigate_near_memory_tag"),btn,{location_id:tag.id});td.appendChild(btn);tr.appendChild(td);table.appendChild(tr);
  }
  wrap.appendChild(table);
}

// ---------- chat ----------
const msgs = () => $("#msgs");
function scrollBottom() { const m = $("#msgs"); m.scrollTop = m.scrollHeight; }
function addBubble(role, content, meta) {
  const wrap = el("div", "msg " + role);
  const av = el("div", "av", role === "user" ? "You" : role === "agent" ? "A" : "S");
  const body = el("div");
  const metaDiv = el("div", "meta", meta || (role === "user" ? "You" : role === "agent" ? "Agent" : "System"));
  const bubble = el("div", "bubble");
  bubble.innerHTML = `<div class="txt">${esc(content)}</div>`;
  body.appendChild(metaDiv); body.appendChild(bubble);
  wrap.appendChild(av); wrap.appendChild(body);
  msgs().appendChild(wrap); scrollBottom();
  return wrap;
}
function addSystem(text) { addBubble("system", text, "Status"); }
function addTool(card) {
  const wrap = el("div", "msg agent");
  const av = el("div", "av", "T");
  const node = el("div");
  node.appendChild(card);
  wrap.appendChild(av); wrap.appendChild(node);
  msgs().appendChild(wrap); scrollBottom();
  return card;
}
function makeToolCard(name, input, output, kind) {
  const card = el("div", "tool");
  card.innerHTML = `
    <div class="th">
      <span class="ic">⚙</span>
      <span class="name">${esc(name)}</span>
      <span class="st">${esc(kind || (output != null ? "result" : "call"))}</span>
    </div>
    <div class="io">
      <div class="in"><div class="cap">input</div><pre>${esc(input || "—")}</pre></div>
      <div class="out"><div class="cap">output</div><pre>${output != null ? esc(output) : "…"}</pre></div>
    </div>`;
  return card;
}
function pretty(obj) {
  try { return JSON.stringify(obj, null, 2); } catch (e) { return String(obj); }
}

// render a normalized message (from /events)
function renderMessage(m) {
  if (m.role === "user" || m.kind === "user") {
    const count = m.source ? 0 : (pendingEchoes.get(m.content) || 0);
    if (count) { if(count===1) pendingEchoes.delete(m.content); else pendingEchoes.set(m.content,count-1); return; }
    addBubble("user", m.content || "", m.source || "You");
    return;
  }
  if (m.kind === "tool" || m.role === "tool") {
    // match a pending tool_call by id, else open a fresh card
    let card = openToolCards.get(m.tool_call_id);
    if (!card) card = addTool(makeToolCard(m.name, "—", null, "result"));
    card.querySelector(".out pre").textContent = m.content || "(no output)";
    card.querySelector(".st").textContent = "result";
    openToolCards.delete(m.tool_call_id);
    return;
  }
  // agent message
  if (m.tool_calls && m.tool_calls.length) {
    for (const tc of m.tool_calls) {
      const card = makeToolCard(tc.name, pretty(tc.args || {}), null, "call");
      addTool(card);
      const id = m.tool_call_id || (tc.id || (tc.args && tc.args._id));
      if (id) openToolCards.set(id, card);
    }
  }
  if (m.content && m.content.trim()) {
    addBubble("agent", m.content, m.source || "Agent");
  } else if (m.tool_calls && m.tool_calls.length) {
    addBubble("agent", "planning…", "Agent · tool call");
  }
}
function renderToolStream(ev) {
  // live tool progress / message from /tool_streams
  const name = ev.name || "tool";
  const text = ev.text || "";
  if (!text) return;
  appendStackLog(`[${now()}] [tool:${name}] ${text}`);
  const card = makeToolCard(name, "", text, "live");
  card.querySelector(".io").classList.add("full");
  addTool(card);
}
function setThinking(on) {
  const b = $("#b-thinking"); b.style.display = on ? "" : "none";
  if (on) {
    setBadge("b-agent", "busy", "thinking");
  } else {
    setBadge("b-agent", "ok", "agent idle");
  }
}

// ---------- status ----------
function applyStatus(s) {
  if($("#exploration-detail"))$("#exploration-detail").textContent=s.exploration_status || "Exploration not started";
  if (s.agent_idle != null) setThinking(!s.agent_idle);
  else {$("#b-thinking").style.display="none";setBadge("b-agent","","agent unknown");}
  state.nav = s.navigation_ready === true;
  if (s.navigation_ready != null) {
    const ready = s.navigation_ready === true;
    setBadge("b-nav", ready ? "ok" : "warn", ready ? "nav ready" : "nav blocked");
  }
  if (s.alignment_status != null && s.alignment_status !== undefined && s.alignment_status !== "error") {
    const st = String(s.alignment_status);
    const aligned = st.startsWith("Ready:");
    setBadge("b-align", aligned ? "ok" : "warn", aligned ? "aligned" : "aligning");
    $("#alignment-detail").textContent=st;
  } else {setBadge("b-align","","alignment unknown");$("#alignment-detail").textContent="";setBadge("b-nav","","nav unknown");}
  const alignment=s.alignment_details;
  state.candidateId=alignment && alignment.phase==="candidate"?alignment.candidate_id:null;
  $("#alignment-phase").textContent=alignment && typeof alignment==="object"?alignment.phase:"unknown";
  $("#alignment-quality").textContent="";
  $("#alignment-heading").textContent="";
  if(alignment && typeof alignment==="object"){
    $("#alignment-detail").textContent=alignment.reason || "";
    const metrics=alignment.metrics || {};
    $("#alignment-quality").textContent=`Candidate: ${state.candidateId?state.candidateId.slice(0,8):"none"} · Attempts ${alignment.attempts || 0} · Scans ${alignment.capture_scans || 0} · Points ${alignment.capture_points || 0}`+
      (Number.isFinite(metrics.fitness)?` · Fitness ${metrics.fitness.toFixed(3)}`:"")+
      (Number.isFinite(metrics.rmse_m)?` · RMSE ${metrics.rmse_m.toFixed(3)} m`:"")+
      (Number.isFinite(alignment.elapsed_s)?` · Matching ${alignment.elapsed_s.toFixed(0)} s`:"");
    const pose=alignment.robot_in_saved_map;
    $("#alignment-heading").textContent=pose?`Saved-map robot position: ${pose.position_m.map(x=>x.toFixed(2)).join(", ")} m · Heading ${pose.yaw_deg.toFixed(1)}° (+X = 0°)`:
      state.candidateId?"Robot pose unavailable; do not approve until heading can be checked.":"";
  }else if(typeof alignment==="string")$("#alignment-detail").textContent=alignment;
  const fusion=s.fusion_status;
  if($("#fusion-detail")) {
    $("#fusion-detail").textContent=fusion && typeof fusion==="object"
      ? `${fusion.fusion_enabled ? "Fusion active" : "Fusion paused"}: ${fusion.reason}. Accepted ${fusion.accepted_frames}, skipped ${fusion.skipped_frames}. Live pose is not frozen.`
      : `Map fusion status unavailable${typeof fusion==="string" ? ": "+fusion : ""}`;
  }
  if(s.stack) applyStack(s.stack);
  updateControls();
}

function applyStack(s) {
  const previous = state.stack;
  state.stack = s.state;
  setBadge("b-stack",s.state==="running"?"ok":s.state==="failed"?"bad":"warn",s.state);
  $("#stack-start").disabled=["starting","running","stopping"].includes(s.state);
  $("#stack-stop").disabled=!s.pid;
  if(previous !== s.state && s.error) addSystem(s.error);
  if(previous !== "running" && s.state==="running") setupRerun(CONFIG.rerun_url);
  updateControls();
  if(s.state!=="running") {
    if($("#fusion-detail"))$("#fusion-detail").textContent="Robot stack is not running.";
    disableKeyboard();
    speakerEnabled=false;$("#speaker-toggle").textContent="Go2 speaker: Off";
    taggingEnabled=null;
    if($("#tagging-toggle"))$("#tagging-toggle").textContent="Automatic tagging: Check status";
    $("#camera-image").hidden=true;
    $("#camera-note").textContent="Camera not ready";$("#camera-note").style.display="grid";
  }
}
function updateControls() {
  const stackBusy=state.lifecycleBusy || ["starting","running","stopping"].includes(state.stack);
  $("#stack-start").disabled=stackBusy;
  $("#stack-map-mode").disabled=stackBusy;
  $("#stack-profile").disabled=stackBusy;
  $("#stack-connection").disabled=stackBusy;
  if(state.lifecycleBusy)$("#stack-stop").disabled=true;
  $("#stack-stop-without-save").disabled=state.lifecycleBusy || !["running","starting","stopping"].includes(state.stack);
  const ready = !CONFIG.standalone || state.stack==="running";
  for (const btn of document.querySelectorAll("[data-operation]")) {
    btn.disabled=!ready || (["tag_object","tag_location","navigate_near_memory_tag","navigate_to_memory_tag","return_to_starting_location","begin_demo_exploration"].includes(btn.dataset.operation) && !state.nav);
    if(["confirm_alignment","reject_alignment"].includes(btn.dataset.operation))btn.disabled=!ready || !state.candidateId;
  }
  $("#alignment-view").disabled=!ready || !state.candidateId;
  if($("#tagging-toggle"))$("#tagging-toggle").disabled=!ready;
  $("#speaker-toggle").disabled=!ready || !CONFIG.standalone;
  $("#navigation-distance").disabled=!ready;
  $("#navigation-speed").disabled=!ready || !state.speedEnabled || $("#navigation-speed").dataset.saving==="true";
  $("#visual-arrival-toggle").disabled=!ready;
  if(!ready){$("#murmur-toggle").disabled=true;$("#puppy-status").textContent="Robot stack is not running.";}
  for(const btn of document.querySelectorAll("[data-navigation]")) btn.disabled=!ready || !state.nav;
  $("#send").disabled=!ready || sending || !state.agent;
  $("#input").disabled=!ready || !state.agent;
}

// ---------- events (SSE) ----------
function connect() {
  const es = new EventSource("/events");
  es.addEventListener("status", e => {
    try { applyStatus(JSON.parse(e.data)); } catch (err) {log("err","status event",err.message);}
  });
  es.addEventListener("message", e => {
    try { renderMessage(JSON.parse(e.data)); } catch (err) {log("err","message event",err.message);}
  });
  es.addEventListener("tool", e => {
    try { renderToolStream(JSON.parse(e.data)); } catch (err) {log("err","tool event",err.message);}
  });
  es.addEventListener("stack",e=>{try{applyStack(JSON.parse(e.data));}catch(err){log("err","stack event",err.message);}});
  es.addEventListener("stack_log",e=>{try{appendStackLog(JSON.parse(e.data).text);}catch(err){log("err","stack log",err.message);}});
  es.addEventListener("open", () => { setBadge("b-conn", "ok", "connected"); $("#f-events").textContent = "live"; });
  es.addEventListener("error", () => { setBadge("b-conn", "bad", "disconnected"); $("#f-events").textContent="reconnecting"; });
}

// ---------- chat send ----------
async function send() {
  const ta = $("#input"); const text = ta.value.trim();
  if (!text || sending) return;
  sending=true;updateControls();
  ta.value = "";
  addBubble("user", text, "You");
  pendingEchoes.set(text,(pendingEchoes.get(text)||0)+1);
  setThinking(true);
  log("chat", "→ agent", text.length > 40 ? text.slice(0, 40) + "…" : text);
  try {
    await api("/api/chat",{message:text});
  } catch (e) { pendingEchoes.delete(text);ta.value=text;addSystem("send failed: " + e.message);await refreshStatus(); }
  finally {sending=false;updateControls();ta.focus();}
}

// ---------- boot ----------
async function boot() {
  setupDeckTooltips();
  try {
    CONFIG = await api("/api/config");
    $("#f-mcp").textContent = (CONFIG.mcp_url || "").replace(/^http:\/\//, "");
    buildDeck();
    setupRerun(CONFIG.rerun_url);
    $("#settings-open").hidden=!CONFIG.standalone;
    $("#lifecycle").hidden=!CONFIG.standalone;
    $("#keyboard-bar").hidden=!CONFIG.standalone;
    if(CONFIG.standalone) {
      await syncStackSelectors();
      const logs=await api("/api/stack/logs");for(const line of logs.lines)appendStackLog(line);
    }
  } catch (e) {
    log("err", "config", String(e));
    addSystem("Could not reach the console API: " + e);
  }
  connect();
  refreshStatus();
  setInterval(refreshStatus, 5000);
  $("#send").onclick = send;
  $("#settings-open").onclick = openSettings;
  $("#fullscreen-toggle").onclick = async () => {
    try {
      if(document.fullscreenElement)await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    }catch(e){addSystem("Full screen unavailable: "+e.message);}
  };
  document.addEventListener("fullscreenchange",()=>{$("#fullscreen-toggle").textContent=document.fullscreenElement?"Exit full screen":"Full screen";});
  $("#console-clear").onclick = () => {$("#stack-log").textContent="";};
  $("#camera-switch").onclick = () => swapViews(true);
  $("#world-switch").onclick = () => swapViews(false);
  $("#keyboard-enable").onclick = enableKeyboard;
  $("#speaker-toggle").onclick = toggleSpeaker;
  $("#visual-arrival-toggle").onclick = async () => {
    const button=$("#visual-arrival-toggle");button.disabled=true;
    try {
      const current=(await api("/api/visual-arrival")).result;
      const enabled=!current.enabled;
      if(enabled && await requestOperation({label:"Enable visual arrival search",human_only:true,description:"After reaching the nearby threshold, stop forward movement and turn slowly in place (0.15 rad/s, up to 20 seconds) to match the selected tag's saved image. Local visual matching; no cloud upload. Keep the area clear. Missing images, stale camera, failed match or timeout stop without claiming arrival. Applies to the next trip."})===null)return;
      const result=(await api("/api/visual-arrival",{enabled,confirmed:true})).result;
      button.textContent="Visual arrival: "+(result.enabled?"On":"Off");
    }catch(e){addSystem(e.message);}finally{updateControls();}
  };
  $("#murmur-toggle").onclick = async () => {
    const button=$("#murmur-toggle");button.disabled=true;
    try {
      const status=(await api("/api/murmur")).result;
      if(!status.puppy)throw new Error("Puppy is not running. Restart the stack and enable Go2 speaker.");
      const result=(await api("/api/murmur",{enabled:!status.puppy.murmur})).result;
      applyPuppyStatus(result);
    }catch(e){addSystem(e.message);}finally{await refreshStatus();}
  };
  $("#navigation-distance").oninput = () => {
    $("#navigation-distance-value").textContent=Number($("#navigation-distance").value).toFixed(1)+" m";
  };
  $("#navigation-speed").oninput = () => {
    $("#navigation-speed-value").textContent=Number($("#navigation-speed").value).toFixed(2)+" m/s";
  };
  $("#navigation-speed").onchange = async () => {
    const slider=$("#navigation-speed");slider.disabled=true;slider.dataset.saving="true";
    try {
      const result=(await api("/api/navigation-speed",{speed_mps:Number(slider.value)})).result;
      slider.dataset.confirmed=result.speed_mps;slider.value=result.speed_mps;
      $("#navigation-speed-value").textContent=Number(result.speed_mps).toFixed(2)+" m/s";
      log("ok","navigation","Live speed limit: "+result.speed_mps+" m/s");
    }catch(e){
      addSystem(e.message);slider.value=slider.dataset.confirmed || "0.55";
      $("#navigation-speed-value").textContent=Number(slider.value).toFixed(2)+" m/s";
    }finally{delete slider.dataset.saving;updateControls();}
  };
  $("#navigation-distance").onchange = async () => {
    const slider=$("#navigation-distance");slider.disabled=true;slider.dataset.saving="true";
    try {
      const result=(await api("/api/navigation-distance",{distance_m:Number(slider.value)})).result;
      slider.dataset.confirmed=result.distance_m;slider.value=result.distance_m;
      $("#navigation-distance-value").textContent=Number(result.distance_m).toFixed(1)+" m";
      log("ok","navigation","Next nearby trip stops within "+result.distance_m+" m");
    }catch(e){
      addSystem(e.message);slider.value=slider.dataset.confirmed || "1";
      $("#navigation-distance-value").textContent=Number(slider.value).toFixed(1)+" m";
    }finally{delete slider.dataset.saving;updateControls();}
  };
  for(const button of document.querySelectorAll("[data-drive]")) {
    button.style.touchAction="none";
    button.onpointerdown=e=>{
      if(!keyboardReady)return;
      e.preventDefault();button.setPointerCapture(e.pointerId);
      driveHeld=true;pressedKeys.clear();pressedKeys.add(" ");pressedKeys.add(button.dataset.drive);
      sendKeyboard();
    };
    const release=()=>{driveHeld=false;pressedKeys.clear();sendKeyboard();};
    button.onpointerup=release;button.onpointercancel=release;button.onlostpointercapture=release;
  }
  setInterval(refreshCamera, 150);
  setInterval(sendKeyboard, 100);
  window.addEventListener("keydown", keyboardDown);
  window.addEventListener("keyup", keyboardUp);
  window.addEventListener("blur", disableKeyboard);
  document.addEventListener("visibilitychange",()=>{if(document.hidden)disableKeyboard();});
  $("#operation-dialog").addEventListener("close",()=>pressedKeys.clear());
  window.addEventListener("beforeunload", disableKeyboard);
  $("#settings-cancel").onclick = () => {$("#settings-form").reset();$("#settings-dialog").close();};
  $("#settings-dialog").addEventListener("close",()=>$("#settings-form").reset());
  $("#settings-form").onsubmit = saveSettings;
  $("#stack-profile").onchange = onStackChoice;
  $("#stack-connection").onchange = onStackChoice;
  $("#stack-start").onclick = () => lifecycle("start");
  $("#stack-stop").onclick = () => lifecycle("stop");
  $("#stack-stop-without-save").onclick = () => lifecycle("stop",false);
  $("#alignment-view").onclick=showAlignmentView;
  $("#alignment-world-view").onclick=()=>setupRerun(CONFIG.rerun_url);
  $("#diagnostics").onclick = async () => {try{$("#diagnostic-output").textContent=pretty(await api("/api/diagnostics"));}catch(e){addSystem(e.message);}};
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  // periodic refresh keeps the Rerun viewer fresh
  window.addEventListener("beforeunload", () => {});
}
async function refreshStatus() {
  if(refreshing) return;
  refreshing=true;
  try {
    const s = await api("/api/refresh-status");
    applyStatus(s);
    const slider=$("#navigation-distance");
    if((!CONFIG.standalone || state.stack==="running") &&
       document.activeElement!==slider && !slider.dataset.saving) {
      const result=(await api("/api/navigation-distance")).result;
      if(!result || !Number.isFinite(result.distance_m))throw new Error("Invalid nearby distance status");
      slider.value=result.distance_m;slider.dataset.confirmed=result.distance_m;
      $("#navigation-distance-value").textContent=Number(result.distance_m).toFixed(1)+" m";
    }
    if(!CONFIG.standalone || state.stack==="running") {
      const speed=(await api("/api/navigation-speed")).result;
      state.speedEnabled=speed && speed.enabled===true && Number.isFinite(speed.speed_mps);
      const speedSlider=$("#navigation-speed");
      if(state.speedEnabled && document.activeElement!==speedSlider && !speedSlider.dataset.saving){
        speedSlider.value=speed.speed_mps;speedSlider.dataset.confirmed=speed.speed_mps;
        $("#navigation-speed-value").textContent=Number(speed.speed_mps).toFixed(2)+" m/s";
      }
      updateControls();
      const visual=(await api("/api/visual-arrival")).result;
      $("#visual-arrival-toggle").textContent="Visual arrival: "+(visual.enabled?"On":"Off")+(visual.searching?" (searching)":"");
      if(CONFIG.standalone)applyPuppyStatus((await api("/api/murmur")).result);
    }
  } catch (e) {setBadge("b-nav","bad","status unavailable");log("err","status",e.message);}
  finally {refreshing=false;}
}

const settingFields = [
  ["Robot connection", [
    ["robot_ip","Robot IP","text"], ["replay","Replay (no real robot)","checkbox"],
    ["replay_db","Replay dataset","text"], ["obstacle_avoidance","Onboard obstacle avoidance","checkbox"],
    ["openai_api_key","OPENAI_API_KEY (from .env, read-only)","password"], ["unitree_aes_128_key","UNITREE_AES_128_KEY (from .env, read-only)","password"]
  ]],
  ["Agent & vision", [
    ["agent_url","Agent API base URL (including /v1)","url"], ["agent_model","Agent model","text"],
    ["vlm_url","Vision API base URL","url"], ["vlm_model","Vision model","text"],
    ["puppy_noise_reduction","Puppy microphone noise reduction (restart; keep quiet for first second when enabled)","checkbox"]
  ]],
  ["Mapping & tagging", [
    ["auto_pause_fusion","Auto-pause permanent map on low-speed odometry (restart; does not fix pose drift)","checkbox"],
    ["fusion_window","Motion window (s)","number"],
    ["fusion_stationary_duration","Stationary dwell (s)","number"],
    ["fusion_sensor_timeout","Odometry timeout (s; >= window)","number"],
    ["fusion_stop_speed","Stationary speed threshold (m/s)","number"],
    ["fusion_resume_speed","Resume speed threshold (m/s; > stationary)","number"],
    ["fusion_stop_rotation_deg","Stationary rotation threshold (deg/s)","number"],
    ["fusion_resume_rotation_deg","Resume rotation threshold (deg/s; > stationary)","number"],
    ["pgo_enabled","PGO loop correction (fixed world; applies on restart)","checkbox"],
    ["nearby_arrival_distance","Nearby stop distance (m; 0.3-3.0; restart default)","number"],
    ["planner_robot_width","Demo planner width (m; 0.05-1.00; restart; below actual footprint risks collisions)","number"],
    ["navigation_speed_limit","Navigation speed limit (m/s; 0.10-0.55; restart default; live slider is session-only)","number"],
    ["scene_map_dir","Scene directory (maps and tags)","text"],
    ["capture_mode","Restore capture mode","select",["manual","rotation"]],
    ["rotation_speed","Rotation speed (rad/s; max 0.3)","number"],
    ["rotation_duration","Rotation duration (s; max 60)","number"],
    ["place_tagging","Automatic room tagging","checkbox"],
    ["object_tagging","Automatic object tagging","checkbox"],
    ["vlm_distance_m","Tagging distance threshold (m)","number"],
    ["object_segmenter","Object segmenter","select",["yolo","auto","vlm"]]
  ]],
  ["Local services", [
    ["mcp_port","MCP port","number"], ["rerun_web_port","Rerun viewer port","number"],
    ["rerun_grpc_port","Rerun data port (gRPC; change if 9877 is occupied)","number"]
  ]]
];
async function openSettings() {
  try {
    const data=await api("/api/settings");
    const wrap=$("#settings-fields");wrap.replaceChildren();
    for(const [group,fields] of settingFields) {
      const heading=el("div","section-title");heading.textContent=group;wrap.appendChild(heading);
      for(const [key,title,type,choices] of fields) {
        const label=el("label","field");const caption=el("span");caption.textContent=title;label.appendChild(caption);
        const input=el(type==="select"?"select":"input");input.name=key;input.id="setting-"+key;
        if(type==="select") for(const value of choices){const opt=el("option");opt.value=value;opt.textContent=value;input.appendChild(opt);}
        else input.type=type;
        if(type==="checkbox")input.checked=data.settings[key];else input.value=type==="password"?"":data.settings[key];
        if(type==="password"){input.readOnly=true;input.value=data.secrets[key.toUpperCase()]?"********":"";input.placeholder=data.secrets[key.toUpperCase()]?"Configured in .env":"Not configured in .env";input.required=false;}
        else if(type!=="checkbox")input.required=true;
        if(type==="number")input.step=key.endsWith("port")?"1":"any";
        if(key==="planner_robot_width"){input.min="0.05";input.max="1.00";input.step="0.01";}
        if(key==="navigation_speed_limit"){input.min="0.10";input.max="0.55";input.step="0.05";}
        label.appendChild(input);wrap.appendChild(label);
      }
    }
    $("#settings-error").textContent="";
    $("#settings-save").disabled=["starting","running","stopping"].includes(state.stack);
    $("#settings-dialog").showModal();
  }catch(e){addSystem("Settings: "+e.message);}
}
async function saveSettings(e) {
  e.preventDefault();
  const payload={settings:{}};
  payload.settings.map_mode=$("#stack-map-mode").value;
  payload.settings.profile=$("#stack-profile").value;
  for(const input of $("#settings-fields").querySelectorAll("input,select")) {
    if(input.type==="password") continue;
    payload.settings[input.name]=input.type==="checkbox"?input.checked:input.type==="number"?Number(input.value):input.value;
  }
  // The Replay checkbox here and the Connection selector describe one choice; replay wins.
  payload.settings.simulation=$("#stack-connection").value==="simulation" && !payload.settings.replay;
  $("#settings-save").disabled=true;
  try {
    await api("/api/settings",payload);
    $("#settings-form").reset();$("#settings-dialog").close();
    CONFIG=await api("/api/config");buildDeck();setupRerun(CONFIG.rerun_url);await syncStackSelectors();updateControls();
    addSystem("Settings saved. Restart the robot stack to apply. Keys are loaded from .env.");
  }catch(err){$("#settings-error").textContent=err.message;}
  finally{$("#settings-save").disabled=false;}
}
const CONNECTION_NOTES = {
  robot: " Uses the Robot IP in Settings; keep the area clear and supervise.",
  replay: " Plays recorded data, so the robot does not respond to commands.",
  simulation: " A MuJoCo window opens on this computer."
};
function connectionOf(settings) { return settings.simulation?"simulation":settings.replay?"replay":"robot"; }
function updateStackPanel(profile, connection) {
  state.agent=profile.has_agent;
  state.map=profile.has_map;
  const simulated=connection==="simulation";
  $("#stack-map-field").hidden=!profile.has_map || simulated;
  $("#stack-profile-note").textContent=profile.description+(CONNECTION_NOTES[connection]||"")+(profile.has_map && simulated?" A new map is built in a fresh folder each start.":"");
  $("#stack-stop").textContent=profile.has_map?"Save and stop":"Stop";
  $("#stack-stop-without-save").hidden=!profile.has_map;
  $("#alignment-panel").hidden=!profile.has_map;
  $("#persistent-controls").hidden=!profile.has_map;
  $("#speaker-toggle").hidden=!profile.has_agent;
  $("#diagnostics").hidden=!profile.has_agent;
  $("#diagnostic-output").hidden=!profile.has_agent;
  $("#deck-title").hidden=!(CONFIG.operations||[]).length;
  $("#input").placeholder=profile.has_agent?"Message the agent…  (Enter to send, Shift+Enter for newline)":"This stack has no agent to chat with";
  updateControls();
}
async function syncStackSelectors() {
  if(!CONFIG.standalone) return;
  const current=(await api("/api/settings")).settings;
  const profiles=CONFIG.profiles||[];
  const select=$("#stack-profile");
  if(select.options.length!==profiles.length) {
    select.innerHTML="";
    for(const item of profiles) {const option=document.createElement("option");option.value=item.key;option.textContent=item.label;select.appendChild(option);}
  }
  const profile=profiles.find(item=>item.key===current.profile)||profiles[0];
  if(!profile) return;
  select.value=profile.key;
  const connection=$("#stack-connection");
  for(const option of connection.options) option.disabled=!profile.connections.includes(option.value);
  connection.value=connectionOf(current);
  $("#stack-map-mode").value=current.map_mode;
  updateStackPanel(profile, connectionOf(current));
}
async function onStackChoice() {
  if(state.lifecycleBusy) return;
  const profile=(CONFIG.profiles||[]).find(item=>item.key===$("#stack-profile").value);
  if(!profile) return;
  let connection=$("#stack-connection").value;
  // A demo that only runs in simulation moves the Connection selector for you.
  if(!profile.connections.includes(connection)) connection=profile.connections[0];
  try {
    const current=(await api("/api/settings")).settings;
    await api("/api/settings",{settings:{...current,profile:profile.key,replay:connection==="replay",simulation:connection==="simulation"}});
    CONFIG=await api("/api/config");buildDeck();setupRerun(CONFIG.rerun_url);
  } catch(e) {addSystem(e.message);}
  await syncStackSelectors();
}
async function lifecycle(action,save=true) {
  if(state.lifecycleBusy)return;
  state.lifecycleBusy=true;updateControls();
  let overwriteToken;
  let useExistingMap=false;
  try {
  if(action==="start") {
      const mode=$("#stack-map-mode").value;
      const current=await api("/api/settings");
      if(current.settings.map_mode!==mode)
        await api("/api/settings",{settings:{...current.settings,map_mode:mode}});
      const plan=await api("/api/stack/prepare",{});
      if(plan.confirm_new_map) {
        const overwrite=await requestOperation({label:plan.overwrite_required?"Overwrite existing scene?":"Create new map?",human_only:true,offer_restore:plan.restore_available,confirm_label:plan.overwrite_required?"Yes — overwrite":"Yes — create new map",description:`${plan.overwrite_required?"Yes: replace maps and tags, keeping a sibling backup.":"Yes: create a new map."} Scene: ${plan.scene_directory}. ${plan.restore_available?"No — use existing map: preserve the scene and start Restore for alignment.":"No saved map is available to restore; use Cancel to abort."} Rotation capture may rotate the robot when restoring. Automatic tagging may call your configured model service. Keep the area clear. Cancel: do not start.`});
        if(overwrite===null)return;
        useExistingMap=overwrite.use_existing_map===true;
        if(!useExistingMap)overwriteToken=plan.overwrite_token;
      }
  } else {
    const approved=await requestOperation({label:!state.map?"Stop robot stack":save?"Save and stop robot stack":"Stop WITHOUT saving",human_only:true,description:!state.map?"Stop this console's robot stack. This is not a hardware emergency stop.":save?"Save the updated scene map first, then stop this console's stack. If saving fails, the stack stays running and the error is shown.":"Stop this console's stack without a final map write. New unsaved observations will be discarded; earlier autosaves are retained. This is not an emergency stop."});
    if(approved===null)return;
  }
  const result=await api("/api/stack/"+action,{confirmed:true,overwrite_token:overwriteToken,...(useExistingMap?{use_existing_map:true}:{}),...(action==="stop"?{save,confirm_without_save:!save}:{})});
  if(result.map_save){
    $("#map-save-detail").textContent=`${result.map_save.state}: ${result.map_save.path}. Accepted ${result.map_save.accepted_frames}, skipped ${result.map_save.skipped_frames}. ${result.map_save.reason}`;
    addSystem($("#map-save-detail").textContent);
  }
  applyStack(result);await refreshStatus();
  }
  catch(e){if(action==="stop")$("#map-save-detail").textContent="Save/stop failed: "+e.message;addSystem(e.message);}
  finally{state.lifecycleBusy=false;await refreshStatus();updateControls();}
}
function appendStackLog(text) {
  const box=$("#stack-log");
  const follow=box.scrollHeight-box.scrollTop-box.clientHeight<32;
  const lines=box.textContent ? box.textContent.split("\n") : [];
  lines.push(...String(text).split("\n"));box.textContent=lines.slice(-200).join("\n");
  if(follow)box.scrollTop=box.scrollHeight;
}
function swapViews(cameraMain) {
  $("#camera-view").classList.toggle("pip",!cameraMain);
  $("#world-view").classList.toggle("pip",cameraMain);
}
async function refreshCamera() {
  if(cameraBusy || (CONFIG.standalone && state.stack!=="running")) return;
  cameraBusy=true;
  try {
    const response=await fetch("/api/camera.jpg");
    if(!response.ok)throw new Error(await response.text());
    const image=$("#camera-image");
    const previous=image.dataset.blob;
    const blob=URL.createObjectURL(await response.blob());
    image.src=blob;image.dataset.blob=blob;image.hidden=false;
    if(previous)URL.revokeObjectURL(previous);
    $("#camera-note").style.display="none";
  }catch(e){$("#camera-image").hidden=true;$("#camera-note").textContent=e.message;$("#camera-note").style.display="grid";}
  finally{cameraBusy=false;}
}
function disableKeyboard() {
  keyboardReady=false;driveHeld=false;$("#manual-pad").hidden=true;
  pressedKeys.clear();
  if(keyboardSocket) {
    if(keyboardSocket.readyState===WebSocket.OPEN) keyboardSocket.send(JSON.stringify({keys:[]}));
    keyboardSocket.close();keyboardSocket=null;
  }
  $("#keyboard-enable").textContent="Enable keyboard";
  $("#keyboard-status").textContent="Disabled. Click Enable keyboard, then hold Space + movement keys on this page (not inside Rerun).";
}
async function enableKeyboard() {
  if(keyboardSocket){disableKeyboard();return;}
  if(state.stack!=="running"){addSystem("Start the robot stack before enabling keyboard control.");return;}
  const approved=await requestOperation({label:"Enable keyboard control",human_only:true,description:"Supervise the robot and clear its surroundings. Manual control cancels navigation and is available during alignment capture. Hold Space while pressing movement keys. This is not a hardware emergency stop."});
  if(approved===null)return;
  const socket=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/api/teleop?token=${encodeURIComponent(CONFIG.csrf_token)}`);
  keyboardSocket=socket;
  socket.onopen=()=>{$("#keyboard-enable").textContent="Preparing controls...";document.activeElement.blur();};
  socket.onmessage=e=>{
    const result=JSON.parse(e.data);
    if(result.error){addSystem(result.error);disableKeyboard();return;}
    if(result.ready){keyboardReady=true;$("#manual-pad").hidden=false;$("#keyboard-enable").textContent="Disable keyboard";$("#keyboard-status").textContent="Enabled: hold Space + W/S/A/D/Q/E, or hold a direction button. Clicking Rerun disables controls for safety.";log("ok","keyboard","Go2 joystick enabled; control channel ready");}
  };
  socket.onerror=()=>{addSystem("Keyboard connection failed");};
  socket.onclose=()=>{if(keyboardSocket===socket){disableKeyboard();log("info","keyboard","disconnected; stop sent");}};
}
function keyboardDown(e) {
  if(!keyboardReady || !keyboardSocket || keyboardSocket.readyState!==WebSocket.OPEN)return;
  if(e.key==="Escape"){e.preventDefault();disableKeyboard();return;}
  if(e.target.closest("input,textarea,select,button") || document.querySelector("dialog[open]")) {pressedKeys.clear();return;}
  const key=e.key.toLowerCase();
  if(["w","a","s","d","q","e"," "].includes(key)){e.preventDefault();pressedKeys.add(key);}
}
function keyboardUp(e) {
  pressedKeys.delete(e.key.toLowerCase());
  if(keyboardSocket && ["w","a","s","d","q","e"," "].includes(e.key.toLowerCase())) {e.preventDefault();sendKeyboard();}
}
function sendKeyboard() {
  if(keyboardSocket && keyboardSocket.readyState===WebSocket.OPEN) {
    if(!keyboardReady || document.querySelector("dialog[open]") || (!driveHeld && document.activeElement.matches("input,textarea,select,button")))pressedKeys.clear();
    keyboardSocket.send(JSON.stringify({keys:[...pressedKeys]}));
  }
}
async function toggleTagging() {
  const button=$("#tagging-toggle");button.disabled=true;
  try {
    const current=(await api("/api/tagging")).result;
    if(taggingEnabled===null){taggingEnabled=current.enabled;button.textContent=`Automatic tagging: ${current.enabled?"On":"Off"} (click to toggle)`;return;}
    const enabled=!current.enabled;
    if(enabled && await requestOperation({label:"Resume automatic tagging",description:"Resumes the place/object modes selected in Settings and may call your model service."})===null)return;
    const result=(await api("/api/tagging",{enabled})).result;
    taggingEnabled=result.enabled;button.textContent=`Automatic tagging: ${result.enabled?"On":"Off"} (click to toggle)`;
    log("ok","tagging",result.enabled?"automatic tagging resumed":"automatic tagging paused; manual tagging remains available");
  }catch(e){addSystem(e.message);}
  finally{button.disabled=false;}
}
async function toggleSpeaker() {
  const button=$("#speaker-toggle");button.disabled=true;
  try {
    const enabled=!(await api("/api/speaker")).result.enabled;
    if(enabled && await requestOperation({label:"Enable Go2 speaker + Puppy murmur",human_only:true,description:"MAXIMUM volume (10/10). Warn nearby people. Enables Go2 microphone with LOCAL Whisper recognition and cute camera comments approximately every 10 seconds. With noise reduction enabled, keep quiet for the first second to learn Go2 background noise. Camera frames and recognized text use the configured OpenAI-compatible service (gpt-4o-mini); raw microphone audio stays local. Do not enable around private conversations or sensitive camera content. First use may download the local Whisper base model. Speech playback pauses listening; voice chat cannot move the robot. Playback unavailable in replay/simulation."})===null)return;
    const result=(await api("/api/speaker",{enabled,confirmed:true})).result;
    speakerEnabled=result.enabled;button.textContent=`Go2 speaker: ${result.enabled?"On (max) · Puppy mic ON":"Off"}`;
    await refreshStatus();
    log("ok","Go2 speaker",result.enabled?"enabled at maximum volume":"disabled; queued replies cancelled");
  }catch(e){addSystem(e.message);}
  finally{button.disabled=false;}
}
function applyPuppyStatus(status) {
  const puppy=status.puppy;
  $("#murmur-toggle").disabled=!status.enabled || !puppy || !puppy.running;
  $("#murmur-toggle").textContent="Murmur: "+(puppy && puppy.murmur?"On":"Off");
  $("#speaker-toggle").textContent="Go2 speaker: "+(status.enabled?"On (max)"+(status.microphone?" · Puppy mic ON":""):"Off");
  $("#puppy-status").textContent=!status.puppy_configured ?
    "Old/non-Puppy stack. Restart console and robot stack to enable murmur." :
    !puppy ? "Enable Go2 speaker to start Puppy (first Whisper load may take time)." :
    puppy.last_error ? "Puppy error: "+puppy.last_error :
    puppy.noise_calibrating ? "Microphone noise calibration: keep quiet for one second." :
    status.camera_age_s===null || status.camera_age_s>3 ? "Murmur waiting: no fresh Go2 camera frame." :
    puppy.busy ? "Puppy speaker busy; commentary waits." :
    puppy.murmur ? "Murmur active · "+puppy.model+" · "+(puppy.stage || "approximately every 10 s while idle") :
    "Murmur paused; microphone conversation remains enabled.";
  if(puppy && !puppy.noise_calibrating)
    $("#puppy-status").textContent+=" · Noise reduction: "+(puppy.noise_reduction?"On":"Off")+" · Half-duplex";
}
boot();
</script>
</body>
</html>"""
