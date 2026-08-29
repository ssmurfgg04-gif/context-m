"""Web trajectory viewer (Reddit 40 mentions: UI / dashboard / viewer).

Lean and simple: a single self-contained HTML file served by a
tiny stdlib HTTP server. No React, no Vite, no CDN. Open the URL
in any browser; the audit log renders as a step-by-step timeline
with click-to-expand payloads.

Usage::

    cortexm trajectory-view --db /path/to/mem.db --port 8901
    # → http://localhost:8901

API endpoints (all JSON):
    GET  /                  → the viewer HTML
    GET  /api/trajectory?n=200&user_id=alice  → audit-log events
    GET  /api/stats         → memory stats
    GET  /api/facts?user_id=alice&limit=200  → fact dump

The HTML fetches /api/trajectory on load and re-renders the
timeline. Each event has a step number, timestamp, kind, user_id,
and a click-to-expand payload. Keyboard: ↑/↓ to navigate events,
J/K same, / to filter.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sys
import urllib.parse
from typing import Any


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cortexm trajectory viewer</title>
<style>
  :root{
    --bg:#0d1117; --fg:#c9d1d9; --muted:#8b949e;
    --accent:#58a6ff; --warn:#f0883e; --ok:#3fb950; --err:#f85149;
    --card:#161b22; --border:#30363d;
  }
  *{box-sizing:border-box} body{margin:0;background:var(--bg);
    color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,
    'Segoe UI',Helvetica,Arial,sans-serif;display:flex;flex-direction:
    column;height:100vh}
  header{background:var(--card);border-bottom:1px solid var(--border);
    padding:10px 16px;display:flex;align-items:center;gap:12px}
  header h1{font-size:14px;margin:0;font-weight:600}
  header .muted{color:var(--muted);font-size:12px}
  header input{flex:1;background:var(--bg);color:var(--fg);border:
    1px solid var(--border);border-radius:6px;padding:6px 10px;font:inherit}
  header button{background:var(--accent);color:var(--bg);border:0;border-radius:
    6px;padding:6px 12px;font:inherit;font-weight:600;cursor:pointer}
  header button:hover{filter:brightness(1.1)}
  .layout{flex:1;display:grid;grid-template-columns:1fr 1fr;overflow:hidden}
  .timeline{overflow:auto;border-right:1px solid var(--border)}
  .detail{overflow:auto;padding:16px;background:var(--bg)}
  .ev{padding:10px 16px;border-bottom:1px solid var(--border);cursor:pointer;
    display:grid;grid-template-columns:50px 60px 1fr auto;gap:8px;align-items:center}
  .ev:hover{background:var(--card)}
  .ev.active{background:var(--card);border-left:3px solid var(--accent);
    padding-left:13px}
  .ev .step{color:var(--muted);font-size:11px;font-variant-numeric:
    tabular-nums;text-align:right}
  .ev .kind{font-size:10px;color:var(--accent);text-transform:uppercase;
    letter-spacing:.05em;font-weight:600}
  .ev .summary{font-size:12px;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap}
  .ev .ts{color:var(--muted);font-size:11px;font-variant-numeric:
    tabular-nums}
  pre{background:var(--card);padding:12px;border-radius:6px;border:1px solid
    var(--border);overflow:auto;font:12px/1.4 'SFMono-Regular',Consolas,
    'Liberation Mono',Menlo,monospace;color:var(--fg)}
  h2{font-size:13px;margin:0 0 8px 0;color:var(--muted);text-transform:
    uppercase;letter-spacing:.05em}
  .stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:6px;
    padding:8px 12px}
  .stat .n{font-size:18px;font-weight:600}
  .stat .lbl{color:var(--muted);font-size:11px;text-transform:uppercase}
  .empty{color:var(--muted);padding:40px;text-align:center;font-size:13px}
  .scroll-btns{position:fixed;bottom:16px;right:16px;display:flex;flex-direction:
    column;gap:6px}
  .scroll-btns button{background:var(--card);color:var(--fg);border:1px solid
    var(--border);border-radius:6px;width:36px;height:36px;cursor:pointer;
    font:inherit;font-size:16px}
  .scroll-btns button:hover{border-color:var(--accent)}
</style>
</head>
<body>
<header>
  <h1>cortexm trajectory</h1>
  <span class="muted" id="hdr-stats">…</span>
  <input id="filter" placeholder="filter events (kind, user_id, payload)…">
  <button onclick="reload()">reload</button>
</header>
<div class="layout">
  <div class="timeline" id="timeline"></div>
  <div class="detail" id="detail">
    <div class="empty">select an event on the left to inspect its payload</div>
  </div>
</div>
<div class="scroll-btns">
  <button onclick="nav(-1)" title="up">↑</button>
  <button onclick="nav(+1)" title="down">↓</button>
</div>
<script>
let events=[]; let activeIdx=-1;
async function fetch_(url){const r=await fetch(url);return r.json();}
async function reload(){
  const f=document.getElementById('filter').value;
  let url='/api/trajectory?n=500';
  if(f) url+='&q='+encodeURIComponent(f);
  const r=await fetch_(url);
  events=r.events||[];
  document.getElementById('hdr-stats').textContent=
    r.n_events+' events · '+r.user_id;
  render();
}
function render(){
  const tl=document.getElementById('timeline');
  tl.innerHTML='';
  if(!events.length){
    tl.innerHTML='<div class="empty">no events — load some memory first</div>';
    return;
  }
  events.forEach((e,i)=>{
    const d=document.createElement('div');
    d.className='ev'+(i===activeIdx?' active':'');
    d.onclick=()=>select(i);
    d.innerHTML=`
      <span class="step">${e.step}</span>
      <span class="kind">${e.kind}</span>
      <span class="summary">${escapeHtml(e.payload_summary||e.kind)}</span>
      <span class="ts">${e.ts?e.ts.slice(11,19):''}</span>`;
    tl.appendChild(d);
  });
}
function select(i){
  activeIdx=i;
  render();
  const e=events[i];
  const detail=document.getElementById('detail');
  detail.innerHTML=`
    <h2>event ${e.step}</h2>
    <div class="stats">
      <div class="stat"><div class="n">${e.kind}</div><div class="lbl">kind</div></div>
      <div class="stat"><div class="n">${e.user_id||'—'}</div><div class="lbl">user_id</div></div>
      <div class="stat"><div class="n">${e.ts||'—'}</div><div class="lbl">ts</div></div>
      <div class="stat"><div class="n">${e.id?e.id.slice(0,8):'—'}</div><div class="lbl">event_id</div></div>
    </div>
    <h2>payload</h2>
    <pre>${escapeHtml(JSON.stringify(e.payload||{},null,2))}</pre>
    <h2>raw</h2>
    <pre>${escapeHtml(JSON.stringify(e,null,2))}</pre>`;
}
function nav(d){if(!events.length)return;activeIdx=Math.max(0,
  Math.min(events.length-1,activeIdx+d));render();
  document.querySelectorAll('.ev')[activeIdx]?.scrollIntoView({block:'center'});
  select(activeIdx);}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);}
document.getElementById('filter').addEventListener('input',reload);
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.key==='ArrowUp'||e.key==='k')nav(-1);
  if(e.key==='ArrowDown'||e.key==='j')nav(+1);
  if(e.key==='/')e.preventDefault(),document.getElementById('filter').focus();
});
reload();
</script>
</body>
</html>"""


class ViewerHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, mem, *a, **kw):
        self.mem = mem
        super().__init__(*a, **kw)

    def log_message(self, *a, **kw):
        pass  # silence default stderr logging

    def _json(self, obj, status=200):
        body = json.dumps(obj, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body, status=200):
        b = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            return self._html(HTML)
        if u.path == "/api/trajectory":
            n = int((q.get("n") or ["500"])[0])
            f = (q.get("q") or [""])[0].lower()
            out = self.mem.trajectory(n=n)
            if f:
                kept = [e for e in out["events"] if f in (
                    str(e.get("kind", "")).lower() + " " +
                    str(e.get("user_id", "")).lower() + " " +
                    str(e.get("payload_summary", "")).lower())]
                out = {"user_id": out["user_id"], "n_events": len(kept),
                       "events": kept}
            return self._json(out)
        if u.path == "/api/stats":
            return self._json(self.mem.stats())
        if u.path == "/api/facts":
            user_id = (q.get("user_id") or ["default"])[0]
            limit = int((q.get("limit") or ["200"])[0])
            return self._json(self.mem.get_all(user_id=user_id,
                                                limit=limit))
        return self._json({"error": "not found"}, 404)


class ThreadedHTTPServer(socketserver.ThreadingMixIn,
                        http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="cortexm trajectory-view",
        description="web viewer for the audit-log timeline")
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — set 0.0.0.0 for LAN)")
    ap.add_argument("--port", type=int, default=8901)
    args = ap.parse_args(argv)

    from cortexm.api.memory import Memory
    from cortexm.config import Config
    cfg = Config.from_env()
    cfg.db_path = args.db
    mem = Memory(cfg)

    def handler_factory(*a, **kw):
        return ViewerHandler(mem, *a, **kw)
    httpd = ThreadedHTTPServer((args.host, args.port), handler_factory)
    url = f"http://{args.host}:{args.port}/"
    print(f"cortexm trajectory viewer → {url}", flush=True)
    print("  Ctrl-C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstop.", flush=True)
    finally:
        mem.close()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
