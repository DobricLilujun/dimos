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
.app { display: grid; grid-template-rows: 56px 1fr 30px; height: 100vh; }
main { display: grid; grid-template-columns: 300px 1fr 400px; min-height: 0; }
aside.deck { border-right: 1px solid var(--line); background: var(--panel); overflow: auto; }
center.viz { position: relative; background: #05090f; min-width: 0; }
aside.chat { border-left: 1px solid var(--line); background: var(--panel); display: flex; flex-direction: column; min-width: 0; }

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
.btn .lbl small { display: block; color: var(--muted); font-weight: 500; font-size: 11px; }
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
.chat .msgs { flex: 1; overflow: auto; padding: 14px 14px 6px; display: flex; flex-direction: column; gap: 12px; }
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
      <h2>Control deck</h2>
      <div id="deck-groups"></div>
      <h2>Operation log</h2>
      <div class="log" id="log"><div class="row"><span class="t">--:--:--</span>ready</div></div>
    </aside>

    <!-- RERUN (preserved) -->
    <center class="viz" id="viz">
      <div class="vbar">
        <span class="chip">Rerun · <b id="viz-host">live 3D</b></span>
        <span class="chip link" id="viz-open">open in new tab</span>
      </div>
      <iframe id="rr" title="Rerun" style="display:none"></iframe>
      <div class="frame-fallback" id="rr-fallback">
        <div>
          <p style="color:var(--text);font-weight:600;margin:0 0 8px">Rerun viewer</p>
          <p>The live Rerun viewer is served by the stack. <a id="rr-link" href="#">Open it in a new tab</a> to view the 3D map and camera feed.</p>
        </div>
      </div>
    </center>

    <!-- CHAT -->
    <aside class="chat">
      <div class="head">
        <div class="ai">✦</div>
        <div class="who">Agent chat<small>talk to the robot · see tool I/O</small></div>
        <div class="spacer" style="flex:1"></div>
        <span class="badge" id="b-thinking" style="display:none"><span class="dot"></span>thinking</span>
      </div>
      <div class="msgs" id="msgs"></div>
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

<script>
"use strict";
const $ = (s) => document.querySelector(s);
const el = (t, cls, html) => { const n = document.createElement(t); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const now = () => { const d = new Date(); return d.toTimeString().slice(0,8); };

let CONFIG = {};
const state = { agentIdle: false, align: null, nav: null };
let openToolCards = {};   // tool_call_id -> { inputEl }  (to pair agent tool_call -> tool result)
let lastToolOut = {};     // name -> last output (for cards without matching id)

// ---------- helpers ----------
function log(kind, key, detail) {
  const row = el("div", "row" + (kind ? " " + kind : ""));
  row.innerHTML = `<span class="t">${now()}</span><span class="k">${esc(key)}</span>${detail ? esc(detail) : ""}`;
  const box = $("#log");
  box.insertBefore(row, box.firstChild);
  while (box.children.length > 60) box.removeChild(box.lastChild);
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function setBadge(id, cls, label) { const b = $(id); if (!b) return; b.className = "badge " + cls; b.innerHTML = `<span class="dot"></span>${esc(label)}`; }
function fmtTime() { return now(); }

// ---------- Rerun embed ----------
function setupRerun(url) {
  const host = url.replace(/^http:\/\//, "").replace(/\/$/, "");
  $("#viz-host").textContent = host;
  $("#f-rr").textContent = host;
  const iframe = $("#rr");
  let failed = false;
  iframe.onload = () => { try { const d = iframe.contentDocument; } catch (e) {} };
  iframe.onerror = () => { showFallback(); };
  // some viewers refuse framing; surface a manual-open fallback after a short delay
  const t = setTimeout(() => {
    // If the iframe is still blank / blocked, show the fallback link.
    try {
      iframe.contentWindow.location.href; // throws if cross-origin blocked
    } catch (e) { /* cross-origin: likely fine, keep iframe */ }
  }, 3500);
  function showFallback() {
    failed = true;
    iframe.style.display = "none";
    const fb = $("#rr-fallback"); fb.style.display = "grid";
  }
  // Use the iframe by default; if the browser blocks it the fallback link remains.
  iframe.src = url;
  iframe.style.display = "block";
  $("#rr-fallback").style.display = "none";
  $("#viz-open").onclick = () => window.open(url, "_blank");
  const link = $("#rr-link"); if (link) link.href = url;
  // Detect a blocked frame (0x0 or not rendering) and fall back to a link.
  setTimeout(() => {
    if (!iframe.complete && iframe.getAttribute("src") === url) {
      // leave the iframe; browsers will show it if framing is allowed
    }
  }, 3500);
}

// ---------- operations / control deck ----------
function buildDeck() {
  const wrap = $("#deck-groups");
  wrap.innerHTML = "";
  const byGroup = {};
  for (const op of CONFIG.operations) {
    (byGroup[op.group] = byGroup[op.group] || []).push(op);
  }
  const order = ["Alignment", "Map", "Tagging", "Memory", "Navigation"];
  for (const group of order) {
    const ops = byGroup[group]; if (!ops) continue;
    const g = el("div", "group");
    g.innerHTML = `<h2>${esc(group)}</h2>`;
    for (const op of ops) {
      const btn = el("button", "btn" + (op.primary ? " primary" : "") + (op.human_only ? " human" : ""));
      btn.innerHTML = `
        <span class="ic">${opIcon(op)}</span>
        <span class="lbl">${esc(op.label)}<small>${esc(op.description || "")}</small></span>
        <span class="kind">${op.kind === "rpc" ? "rpc" : "skill"}</span>`;
      btn.onclick = () => runOp(op, btn);
      g.appendChild(btn);
    }
    wrap.appendChild(g);
  }
}
function opIcon(op) {
  const map = { confirm_alignment: "✓", reject_alignment: "✕", save_map: "💾",
    finish_startup_capture: "⚑", cancel_startup_rotation: "↺",
    alignment_status: "◷", navigation_ready: "⦿",
    tag_object: "◈", tag_location: "⌖", query_memory_tags: "⌕",
    navigate_to_memory_tag: "⤖", stop_navigation: "⏹" };
  return map[op.key] || "•";
}
async function runOp(op, btn) {
  let args = {};
  const fields = (op.schema && op.schema.properties) || {};
  const required = (op.schema && op.schema.required) || [];
  if (required.length || Object.keys(fields).length) {
    for (const [name, spec] of Object.entries(fields)) {
      const def = spec.default != null ? spec.default : (spec.type === "boolean" ? false : "");
      const v = prompt(`${op.label} — ${esc(name)}` + (spec.description ? ` (${esc(spec.description)})` : "") + ":", def);
      if (v === null) { log("", op.key, "cancelled"); return; }
      args[name] = spec.type === "string" ? v.trim() : v;
    }
  }
  btn.style.opacity = ".5";
  log("running", op.key, op.kind === "rpc" ? "rpc" : "skill");
  try {
    const res = await fetch("/api/action", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: op.key, args }),
    }).then(r => r.json());
    if (res.ok) {
      log("ok", op.key, "done");
      addSystem(`✓ ${esc(op.label)} — ${esc(String(res.result ?? "ok"))}`);
      if (op.key === "alignment_status" || op.key === "navigation_ready") refreshStatus();
    } else {
      log("err", op.key, res.error || "failed");
      addSystem(`✗ ${esc(op.label)} — ${esc(res.error || "failed")}`);
    }
  } catch (e) {
    log("err", op.key, String(e));
  }
  btn.style.opacity = "1";
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
    addBubble("user", m.content || "", "You");
    return;
  }
  if (m.kind === "tool" || m.role === "tool") {
    // match a pending tool_call by id, else open a fresh card
    const card = makeToolCard(m.name, "…", m.content, "result");
    addTool(card);
    const io = card.querySelector(".out pre");
    if (io) io.textContent = m.content || "(no output)";
    if (m.tool_call_id && openToolCards[m.tool_call_id]) {
      const inEl = openToolCards[m.tool_call_id];
      if (inEl) inEl.textContent = m.input || "…";
      delete openToolCards[m.tool_call_id];
    }
    return;
  }
  // agent message
  if (m.tool_calls && m.tool_calls.length) {
    for (const tc of m.tool_calls) {
      const card = makeToolCard(tc.name, pretty(tc.args || {}), null, "call");
      addTool(card);
      const id = m.tool_call_id || (tc.id || (tc.args && tc.args._id));
      if (id) openToolCards[id] = card.querySelector(".in pre");
    }
  }
  if (m.content && m.content.trim()) {
    addBubble("agent", m.content, "Agent");
  } else if (m.tool_calls && m.tool_calls.length) {
    addBubble("agent", "planning…", "Agent · tool call");
  }
}
function renderToolStream(ev) {
  // live tool progress / message from /tool_streams
  const name = ev.name || "tool";
  const text = ev.text || "";
  if (!text) return;
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
  if (s.agent_idle != null) setThinking(!!s.agent_idle);
  if (s.navigation_ready != null) {
    const ready = typeof s.navigation_ready === "boolean" ? s.navigation_ready : /true/i.test(String(s.navigation_ready));
    setBadge("b-nav", ready ? "ok" : "warn", ready ? "nav ready" : "nav blocked");
  }
  if (s.alignment_status != null && s.alignment_status !== undefined && s.alignment_status !== "error") {
    const st = String(s.alignment_status);
    const aligned = /confirmed|ready|aligned|true/i.test(st) && !/no alignment|not|waiting/i.test(st);
    setBadge("b-align", aligned ? "ok" : "warn", aligned ? "aligned" : "aligning");
  }
}

// ---------- events (SSE) ----------
function connect() {
  const es = new EventSource("/events");
  es.addEventListener("status", e => {
    try { applyStatus(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener("message", e => {
    try { renderMessage(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener("tool", e => {
    try { renderToolStream(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener("open", () => { setBadge("b-conn", "ok", "connected"); $("#f-events").textContent = "live"; });
  es.addEventListener("error", () => { setBadge("b-conn", "bad", "disconnected"); });
}

// ---------- chat send ----------
async function send() {
  const ta = $("#input"); const text = ta.value.trim();
  if (!text) return;
  ta.value = "";
  addBubble("user", text, "You");
  log("chat", "→ agent", text.length > 40 ? text.slice(0, 40) + "…" : text);
  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    }).then(r => r.json());
    if (!res.ok) addSystem("send failed: " + (res.error || "unknown"));
  } catch (e) { addSystem("send failed: " + e); }
}

// ---------- boot ----------
async function boot() {
  try {
    CONFIG = await fetch("/api/config").then(r => r.json());
    $("#f-mcp").textContent = (CONFIG.mcp_url || "").replace(/^http:\/\//, "");
    buildDeck();
    setupRerun(CONFIG.rerun_url);
  } catch (e) {
    log("err", "config", String(e));
    addSystem("Could not reach the console API: " + e);
  }
  connect();
  refreshStatus();
  setInterval(refreshStatus, 5000);
  $("#send").onclick = send;
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  // periodic refresh keeps the Rerun viewer fresh
  window.addEventListener("beforeunload", () => {});
}
async function refreshStatus() {
  try {
    const s = await fetch("/api/refresh-status").then(r => r.json());
    applyStatus(s);
  } catch (_) {}
}
boot();
</script>
</body>
</html>"""