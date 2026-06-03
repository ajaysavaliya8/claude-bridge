"""Self-contained HTML for the broker's live dashboard (served at /ui).

No build step and no external dependencies — one page with inline CSS + JS that
polls the broker's authenticated /peers and /messages endpoints and renders the
live question/answer feed. Filter by claude session id to follow one peer's
accumulating session ("chat id").
"""

from __future__ import annotations

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-bridge</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         background: #0d1117; color: #c9d1d9; }
  header { position: sticky; top: 0; background: #161b22; border-bottom: 1px solid #30363d;
           padding: 12px 16px; z-index: 10; }
  h1 { margin: 0 0 8px; font-size: 16px; color: #e6edf3; }
  h1 .dot { color: #58a6ff; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  input[type=text], input:not([type]) { background: #0d1117; color: #c9d1d9;
           border: 1px solid #30363d; border-radius: 6px; padding: 5px 8px; font: inherit; }
  #filter { width: 360px; }
  button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px;
           padding: 5px 10px; cursor: pointer; font: inherit; }
  button:hover { background: #30363d; }
  label { display: flex; align-items: center; gap: 4px; }
  .peers { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .peer { padding: 2px 8px; border-radius: 12px; border: 1px solid #30363d; font-size: 12px; }
  .peer::before { content: "\25CF "; }
  .peer.on::before { color: #3fb950; } .peer.off::before { color: #f85149; }
  .status { color: #f85149; font-size: 12px; min-height: 16px; margin-top: 6px; }
  main { padding: 12px 16px; max-width: 1100px; margin: 0 auto; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 10px 12px; margin-bottom: 10px; }
  .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .dir { font-weight: 600; color: #e6edf3; }
  .time { margin-left: auto; color: #6e7681; font-size: 12px; }
  .q { margin: 6px 0; white-space: pre-wrap; word-break: break-word; }
  .answer { margin-top: 6px; padding: 8px 10px; background: #0d1117; border-left: 3px solid #3fb950;
            border-radius: 4px; white-space: pre-wrap; word-break: break-word; }
  .answer.err { border-left-color: #f85149; }
  .muted { color: #6e7681; } .center { text-align: center; padding: 30px; }
  .badge { font-size: 11px; padding: 1px 7px; border-radius: 10px; border: 1px solid #30363d; }
  .req { color: #58a6ff; } .note { color: #d2a8ff; } .ok { color: #3fb950; }
  .err { color: #f85149; } .pend { color: #d29922; } .img { color: #79c0ff; }
  .sid { font-size: 11px; color: #8b949e; cursor: pointer; text-decoration: underline dotted; }
  .sid:hover { color: #58a6ff; }
</style>
</head>
<body>
<header>
  <h1><span class="dot">&#9679;</span> claude-bridge &mdash; live</h1>
  <div class="controls">
    <input id="filter" type="text" placeholder="filter by claude session id (chat id)" autocomplete="off">
    <label><input type="checkbox" id="auto" checked> auto-refresh</label>
    <button id="refresh">refresh now</button>
    <button id="clearFilter">clear filter</button>
  </div>
  <div class="peers" id="peers"></div>
  <div class="status" id="status"></div>
</header>
<main id="feed"></main>
<script>
  const $ = (s) => document.querySelector(s);
  const filterInput = $("#filter"), autoBox = $("#auto");
  const params = new URLSearchParams(location.search);
  if (params.get("session_id")) filterInput.value = params.get("session_id");

  const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const fmtTime = (iso) => iso ? iso.replace("T", " ").slice(0, 19) : "";
  const badge = (t, c) => '<span class="badge ' + c + '">' + esc(t) + '</span>';

  async function getJSON(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error("HTTP " + r.status + " from " + path);
    return r.json();
  }

  async function loadPeers() {
    try {
      const d = await getJSON("/peers");
      $("#peers").innerHTML = (d.peers || []).map((p) =>
        '<span class="peer ' + (p.alive ? "on" : "off") + '">' + esc(p.name) + "</span>"
      ).join("") || '<span class="muted">no peers connected yet</span>';
    } catch (e) { /* surfaced by loadMessages */ }
  }

  function statusBadge(m) {
    if (m.answered) return m.is_error ? badge("error", "err") : badge("answered", "ok");
    return badge(m.status, "pend");
  }

  function setSid(s) { filterInput.value = s; loadMessages(); }
  window.setSid = setSid;

  function renderMsg(m) {
    const kind = badge(m.kind, m.kind === "note" ? "note" : "req");
    const sid = m.session_id
      ? '<span class="sid" title="filter by this session" onclick="setSid(\'' + esc(m.session_id) + '\')">session ' + esc(String(m.session_id).slice(0, 8)) + "…</span>"
      : "";
    const cost = (m.cost_usd != null) ? '<span class="muted">$' + Number(m.cost_usd).toFixed(4) + "</span>" : "";
    const att = (m.attachment_ids && m.attachment_ids.length) ? badge(m.attachment_ids.length + " img", "img") : "";
    const ans = m.answered
      ? '<div class="answer' + (m.is_error ? " err" : "") + '">' + esc(m.answer) + "</div>"
      : '<div class="muted">waiting for answer…</div>';
    return '<div class="card"><div class="row">' +
      '<span class="dir">' + esc(m.sender) + " → " + esc(m.target) + "</span> " +
      kind + " " + statusBadge(m) + " " + att + " " + cost + " " + sid +
      '<span class="time">' + fmtTime(m.created_at) + "</span></div>" +
      '<div class="q">' + esc(m.question) + "</div>" + ans + "</div>";
  }

  async function loadMessages() {
    const sid = filterInput.value.trim();
    try {
      const qs = "/messages?limit=150" + (sid ? "&session_id=" + encodeURIComponent(sid) : "");
      const d = await getJSON(qs);
      $("#status").textContent = "";
      $("#feed").innerHTML = (d.messages || []).map(renderMsg).join("") ||
        '<div class="muted center">no messages yet — ask a peer something</div>';
    } catch (e) { $("#status").textContent = e.message; }
  }

  $("#refresh").addEventListener("click", () => { loadPeers(); loadMessages(); });
  $("#clearFilter").addEventListener("click", () => { filterInput.value = ""; loadMessages(); });
  filterInput.addEventListener("change", loadMessages);
  async function tick() { if (autoBox.checked) { await loadPeers(); await loadMessages(); } }
  loadPeers(); loadMessages();
  setInterval(tick, 2000);
</script>
</body>
</html>
"""
