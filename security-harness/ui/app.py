#!/usr/bin/env python3
"""security-harness live dashboard — trigger workflows and watch the swarm in real time.

Stdlib only (no new dependencies, no build step): a threaded http.server backend that
  * launches the ./sast / ./scan / ./assess / ./swarm entrypoints as subprocesses,
  * reads workflow status from the Conductor API, and
  * reads the blackboard facts + agent chatter straight off disk,
and serves one embedded HTML page (Run / Progress / Message Board tabs) that polls it.

Run:   python3 ui/app.py          # then open http://127.0.0.1:8700
Env:   SC_UI_PORT (default 8700), CONDUCTOR_SERVER_URL (default http://localhost:8080/api)
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys as _sys  # noqa: E402
_sys.path.insert(0, os.path.join(ROOT, "workers"))
try:  # SFW red-team callsigns so the board reads like a real op, not a wall of UUIDs
    from common.codename import callsign as _callsign, label as _cs_label
except Exception:  # keep the UI working even if the module is unavailable
    def _callsign(x):
        return (str(x or "?")[:8])

    def _cs_label(x):
        return str(x or "?")
PORT = int(os.environ.get("SC_UI_PORT", "8700"))
CONDUCTOR = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")
MODES = {"sast": "./sast", "scan": "./scan", "assess": "./assess", "swarm": "./swarm"}
ROOT_WF = {"swarm": "swarm_assess", "assess": "deep_assess", "scan": "security_scan", "sast": "sast_report"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# In-memory registry of launches started from the UI: token -> {mode, status, cid, line, started}
_LAUNCHES: dict[str, dict] = {}
_LOCK = threading.Lock()


# ── Conductor API + blackboard readers ────────────────────────────────────────────────
def _api(path: str):
    try:
        with urllib.request.urlopen(CONDUCTOR + path, timeout=8) as r:
            return json.load(r)
    except Exception:
        return None


def _state_base(cid: str) -> str | None:
    """The blackboard root for a campaign — repo-root state/ (ensure-stack) or workers/state/."""
    for base in (os.path.join(ROOT, "state"), os.path.join(ROOT, "workers", "state")):
        if os.path.isdir(os.path.join(base, "blackboard", "facts", cid)):
            return base
    return None


def _facts_dir(cid: str) -> str | None:
    base = _state_base(cid)
    return os.path.join(base, "blackboard", "facts", cid) if base else None


def _count(fd: str, kind: str) -> int:
    return len(glob.glob(os.path.join(fd, kind, "*.json"))) if fd else 0


def _load_kind(fd: str, kind: str, limit: int = 0) -> list[dict]:
    files = glob.glob(os.path.join(fd, kind, "*.json"))
    if limit:
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        files = files[:limit]
    out = []
    for f in files:
        try:
            out.append(json.load(open(f)))
        except Exception:
            pass
    return out


def _report_pdf(cid: str, pdf_hint: str | None = None) -> str | None:
    """Resolve the on-disk report PDF for a campaign, across the state-dir variants and the
    worker-payload path the workflow records. Returns an absolute path or None."""
    cands = [os.path.join(ROOT, d, cid, name)
             for name in ("report_with_reproduction.pdf", "report.pdf")
             for d in ("reports", os.path.join("workers", "reports"))]
    if pdf_hint is None:
        wf = _api(f"/workflow/{cid}?includeTasks=false") or {}
        pdf_hint = (wf.get("output") or {}).get("pdf")
    if isinstance(pdf_hint, str) and pdf_hint.startswith("file://"):
        cands.append(unquote(pdf_hint[7:]))
    for d in (os.path.join(ROOT, "reports", cid), os.path.join(ROOT, "workers", "reports", cid)):
        cands += sorted(glob.glob(os.path.join(d, "*.pdf")))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def status(cid: str) -> dict:
    wf = _api(f"/workflow/{cid}?includeTasks=false") or {}
    fd = _facts_dir(cid)
    findings = _load_kind(fd, "finding") if fd else []
    confirmed = sum(1 for f in findings if (f.get("data") or {}).get("confirmed") is True)
    cov = _load_kind(fd, "coverage") if fd else []
    cov_by = {}
    for c in cov:
        s = (c.get("data") or {}).get("status") or ("untested" if (c.get("data") or {}).get("untested") else "?")
        cov_by[s] = cov_by.get(s, 0) + 1
    started = wf.get("startTime") or 0
    out = wf.get("output") or {}
    return {
        "cid": cid,
        "found": bool(wf),
        "workflowType": wf.get("workflowType") or wf.get("workflowName") or (wf.get("workflowDefinition") or {}).get("name"),
        "wf_status": wf.get("status"),
        "elapsed_s": int((time.time() * 1000 - started) / 1000) if started else None,
        "rounds_run": out.get("rounds_run"),
        "counts": {k: _count(fd, k) for k in
                   ("hypothesis", "attempt", "attempt_lesson", "message", "finding", "observation", "coverage")} if fd else {},
        "confirmed": confirmed,
        "coverage_by_status": cov_by,
        "report_dir": out.get("report_dir"),
        "pdf": out.get("pdf"),
        "pdf_available": bool(_report_pdf(cid, out.get("pdf"))),
    }


def messages(cid: str, limit: int = 400) -> list[dict]:
    fd = _facts_dir(cid)
    if not fd:
        return []
    msgs = _load_kind(fd, "message", limit=limit)
    rows = []
    for m in msgs:
        d = m.get("data") or {}
        _from = d.get("from") or m.get("author") or "?"
        rows.append({
            "ts": m.get("ts") or 0,
            "from": _callsign(_from),
            "from_id": str(_from)[:8],
            "from_role": d.get("from_role") or "?",
            "to": _cs_label(d.get("to") or "?"),
            "intent": d.get("intent") or "share",
            "thread": (d.get("thread") or "")[:28],
            "body": d.get("body") or "",
            "msg_id": d.get("msg_id") or "",
        })
    rows.sort(key=lambda r: r["ts"])
    return rows


def recent_runs() -> list[dict]:
    q = "workflowType IN (swarm_assess,deep_assess,security_scan,sast_report)"
    from urllib.parse import quote
    res = _api(f"/workflow/search?query={quote(q)}&size=25&sort=startTime:DESC") or {}
    out = []
    for w in res.get("results", []) or []:
        cid = w.get("workflowId")
        out.append({"cid": cid, "type": w.get("workflowType"), "status": w.get("status"),
                    "startTime": w.get("startTime"), "pdf": bool(_report_pdf(cid, ""))})
    return out


# ── launch ────────────────────────────────────────────────────────────────────────────
def _launch(token: str, argv: list[str]):
    with _LOCK:
        _LAUNCHES[token] = {"status": "launching", "cid": None, "line": "starting…", "argv": " ".join(argv)}
    try:
        proc = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:  # type: ignore
            line = line.rstrip()
            with _LOCK:
                _LAUNCHES[token]["line"] = line[:200]
            m = None
            if "campaign id" in line.lower() or "/execution/" in line or "correlation id" in line.lower():
                found = _UUID.findall(line)
                if found:
                    m = found[-1]
            if m:
                with _LOCK:
                    _LAUNCHES[token].update(status="running", cid=m)
                break
        # --no-wait scripts exit after printing the id; keep reading briefly so it doesn't zombie
        threading.Thread(target=lambda: proc.wait(), daemon=True).start()
        with _LOCK:
            if _LAUNCHES[token]["cid"] is None:
                _LAUNCHES[token]["status"] = "no-id (see line)"
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            _LAUNCHES[token] = {"status": "error", "cid": None, "line": str(e)[:200]}


def launch(params: dict) -> str:
    mode = params.get("mode", "swarm")
    script = MODES.get(mode)
    if not script:
        raise ValueError("bad mode")
    argv = [script, params.get("target", "").strip()]
    if params.get("authorized"):
        argv.append("--authorized")
    if params.get("capability"):
        argv += ["--capability", str(params["capability"])]
    if mode == "swarm":
        for flag in ("width", "rounds", "budget"):
            if params.get(flag):
                argv += [f"--{flag}", str(params[flag])]
    if params.get("docs"):
        argv += ["--docs", params["docs"].strip()]
    if params.get("source"):
        argv += ["--source", params["source"].strip()]
    if mode in ("swarm", "assess"):
        argv.append("--no-wait")
    if params.get("extra"):
        argv += shlex.split(params["extra"])
    argv = [a for a in argv if a != ""]
    token = os.urandom(8).hex()
    threading.Thread(target=_launch, args=(token, argv), daemon=True).start()
    return token


# ── HTTP ──────────────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(HTML.encode(), ctype="text/html; charset=utf-8")
        if u.path == "/api/runs":
            return self._send({"runs": recent_runs()})
        if u.path == "/api/status":
            cid = (q.get("cid") or [""])[0]
            return self._send(status(cid) if cid else {"found": False})
        if u.path == "/api/messages":
            cid = (q.get("cid") or [""])[0]
            return self._send({"messages": messages(cid) if cid else []})
        if u.path == "/api/report":
            cid = (q.get("cid") or [""])[0]
            if not _UUID.fullmatch(cid or ""):
                return self._send({"error": "bad cid"}, 400)
            path = _report_pdf(cid)
            if not path:
                return self._send({"error": "no report pdf for this run"}, 404)
            data = open(path, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{cid[:8]}-report.pdf"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/launch_status":
            tok = (q.get("token") or [""])[0]
            with _LOCK:
                return self._send(_LAUNCHES.get(tok) or {"status": "unknown"})
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/launch":
            return self._send({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            params = json.loads(self.rfile.read(n) or b"{}")
            token = launch(params)
            return self._send({"token": token})
        except Exception as e:  # noqa: BLE001
            return self._send({"error": str(e)}, 400)


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Swarm Console</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--mut:#8b949e;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center}
h1{font-size:16px;margin:0;font-weight:650}.dot{width:8px;height:8px;border-radius:50%;background:var(--mut);display:inline-block;margin-right:6px}
.tabs{display:flex;gap:4px;margin-left:auto}.tab{padding:6px 14px;border-radius:6px;cursor:pointer;color:var(--mut)}
.tab.on{background:var(--panel);color:var(--fg)}
main{padding:18px;max-width:1100px;margin:0 auto}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
label{display:block;font-size:12px;color:var(--mut);margin:8px 0 3px}
input,select,textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font:inherit}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
button{background:var(--acc);color:#04101f;border:0;border-radius:6px;padding:9px 16px;font-weight:650;cursor:pointer;margin-top:12px}
button.ghost{background:transparent;color:var(--acc);border:1px solid var(--line)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.card{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px}
.card .n{font-size:24px;font-weight:700}.card .l{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.runs{max-height:200px;overflow:auto}.run{padding:7px 9px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;cursor:pointer;display:flex;gap:8px;align-items:center;font-size:13px}
.run:hover{border-color:var(--acc)}.run.sel{border-color:var(--acc);background:#1b2838}
.pill{font-size:10px;padding:2px 7px;border-radius:20px;border:1px solid var(--line);color:var(--mut)}
.RUNNING{color:var(--ok);border-color:var(--ok)}.COMPLETED{color:var(--acc);border-color:var(--acc)}.TERMINATED,.FAILED{color:var(--bad);border-color:var(--bad)}
.feed{max-height:62vh;overflow:auto;display:flex;flex-direction:column;gap:8px}
.msg{border:1px solid var(--line);border-left:3px solid var(--mut);border-radius:6px;padding:8px 10px;background:var(--bg)}
.msg .h{font-size:12px;color:var(--mut);margin-bottom:3px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.msg .b{font-size:13px;white-space:pre-wrap;word-break:break-word}
.intent{font-size:10px;padding:1px 7px;border-radius:20px;font-weight:700;text-transform:uppercase}
.alert{background:#3d1d1d;color:var(--bad)}.handoff{background:#1d2d3d;color:var(--acc)}.answer{background:#1d3d2a;color:var(--ok)}
.share{background:#3d341d;color:var(--warn)}.ask{background:#2d1d3d;color:#bc8cff}
.msg.alert{border-left-color:var(--bad)}.msg.answer{border-left-color:var(--ok)}.msg.share{border-left-color:var(--warn)}
.mut{color:var(--mut)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
a{color:var(--acc)}.hide{display:none}
</style></head><body>
<header><h1>🐝 Swarm Console</h1>
<span id="sel" class="mut mono"></span>
<div class="tabs"><div class="tab on" data-t="run">Run</div><div class="tab" data-t="progress">Progress</div><div class="tab" data-t="messages">Message Board</div></div>
</header>
<main>
 <section id="run" class="tab-pane">
  <div class="panel">
   <b>Launch a workflow</b>
   <div class="row">
    <div><label>Mode</label><select id="mode"><option value="swarm">swarm — collaborative deep hunt</option><option value="assess">assess — deep single agent</option><option value="scan">scan — surface</option><option value="sast">sast — source only</option></select></div>
    <div><label>Target (URL, or source path for sast)</label><input id="target" placeholder="https://app.example.com"></div>
    <div><label>Capability</label><select id="capability"><option value="">default (1)</option><option>0</option><option>1</option><option>2</option><option>3</option><option>4</option></select></div>
   </div>
   <div class="row" id="swarmflags">
    <div><label>Width</label><input id="width" type="number" placeholder="24"></div>
    <div><label>Rounds</label><input id="rounds" type="number" placeholder="4"></div>
    <div><label>Budget</label><input id="budget" type="number" placeholder="5000"></div>
   </div>
   <div class="row">
    <div><label>Docs URL (optional)</label><input id="docs" placeholder="https://docs.example.com/"></div>
    <div><label>Source path (optional)</label><input id="source" placeholder="/path/to/code"></div>
   </div>
   <label>Extra flags (identities go here, e.g. <span class="mono">--id 'u=token:…,tenant:a' --oob http://…</span>)</label>
   <textarea id="extra" rows="2" placeholder="--id 'userA=token:eyJ...'"></textarea>
   <label><input type="checkbox" id="authorized" checked style="width:auto"> --authorized (required to pass the gate)</label>
   <button onclick="doLaunch()">▶ Launch</button>
   <span id="launchmsg" class="mut mono"></span>
  </div>
  <div class="panel"><b>Recent runs</b> <span class="mut">(click to watch)</span><div id="runs" class="runs"></div></div>
 </section>

 <section id="progress" class="tab-pane hide">
  <div class="panel" id="progsummary"><span class="mut">Select a run from the Run tab.</span></div>
  <div class="panel"><b>Blackboard</b><div id="cards" class="cards" style="margin-top:10px"></div></div>
  <div class="panel"><b>Coverage</b><div id="cov" style="margin-top:8px"></div></div>
 </section>

 <section id="messages" class="tab-pane hide">
  <div class="panel" style="display:flex;gap:10px;align-items:center">
   <b>Message Board</b><span id="mcount" class="mut mono">select a run →</span>
   <select id="mfilter" style="width:auto;margin-left:auto"><option value="">all intents</option><option>alert</option><option>handoff</option><option>answer</option><option>share</option><option>ask</option></select>
   <label style="margin:0"><input type="checkbox" id="autoscroll" checked style="width:auto"> auto-scroll</label>
  </div>
  <div id="feed" class="feed"><div class="mut" style="padding:24px">← Pick a run on the <b>Run</b> tab to watch its message board.</div></div>
 </section>
</main>
<script>
let CID=null, tab="run", allMsgs=[], msgIds={};
const $=s=>document.querySelector(s);
function showTab(name){tab=name;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x.dataset.t===name));
  document.querySelectorAll(".tab-pane").forEach(p=>p.classList.toggle("hide",p.id!==name));
  if(name==="messages")renderFeed(); tick();}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>showTab(t.dataset.t));
$("#mode").onchange=()=>{$("#swarmflags").style.display=$("#mode").value==="swarm"?"grid":"none"};
$("#mfilter").onchange=renderFeed;    // re-filter the whole board immediately
function cidShort(){return CID?CID.slice(0,8):""}
function pick(cid){CID=cid;allMsgs=[];msgIds={};$("#sel").textContent="▶ watching "+cid.slice(0,8);
  document.querySelectorAll(".run").forEach(r=>r.classList.toggle("sel",r.dataset.cid===cid));
  renderFeed();showTab("progress");}  // clicking a run selects it AND jumps to its live view
function fmtTs(ts){if(!ts)return"";const d=new Date(ts*1000);return d.toLocaleTimeString([], {hour12:false})+"."+String(d.getMilliseconds()).padStart(3,"0").slice(0,2);}
function fmtTsFull(ts){return ts?new Date(ts*1000).toLocaleString():"";}
function msgHTML(m){return `<div class="msg ${m.intent}"><div class="h">`+
  `<span class="mut mono" title="${fmtTsFull(m.ts)}">${fmtTs(m.ts)}</span> `+
  `<span class="intent ${m.intent}">${m.intent}</span> `+
  `<span class="mono" title="${m.from_id||''}">${m.from}</span><span class="mut">(${m.from_role})</span>→<span class="mono">${m.to}</span>`+
  `${m.thread?` <span class="mut">«${m.thread}»</span>`:""}</div><div class="b">${(m.body||"").replace(/</g,"&lt;")}</div></div>`;}
function renderFeed(){const feed=$("#feed");if(!feed)return;
  if(!CID){feed.innerHTML=`<div class="mut" style="padding:24px">← Pick a run on the Run tab to watch its message board.</div>`;$("#mcount").textContent="select a run →";return;}
  const f=$("#mfilter").value, rows=allMsgs.filter(m=>!f||m.intent===f).slice(-800);
  const atBottom=Math.abs(feed.scrollHeight-feed.clientHeight-feed.scrollTop)<40, keep=feed.scrollTop;
  feed.innerHTML=rows.length?rows.map(msgHTML).join(""):`<div class="mut" style="padding:24px">No ${f||""} messages yet for this run.</div>`;
  $("#mcount").textContent=allMsgs.length+" messages"+(f?` · ${rows.length} ${f}`:"")+" · run "+cidShort();
  feed.scrollTop=($("#autoscroll").checked||atBottom)?feed.scrollHeight:keep;}
async function doLaunch(){
  const p={mode:$("#mode").value,target:$("#target").value,capability:$("#capability").value,
    width:$("#width").value,rounds:$("#rounds").value,budget:$("#budget").value,
    docs:$("#docs").value,source:$("#source").value,extra:$("#extra").value,authorized:$("#authorized").checked};
  $("#launchmsg").textContent="launching…";
  const r=await fetch("/api/launch",{method:"POST",body:JSON.stringify(p)}); const j=await r.json();
  if(j.error){$("#launchmsg").textContent="error: "+j.error;return;}
  const poll=setInterval(async()=>{
    const s=await(await fetch("/api/launch_status?token="+j.token)).json();
    $("#launchmsg").textContent=s.status+" · "+(s.line||"");
    if(s.cid){clearInterval(poll);pick(s.cid);$("#launchmsg").textContent="running "+s.cid;loadRuns();}
  },1500);
}
async function loadRuns(){
  const j=await(await fetch("/api/runs")).json();
  $("#runs").innerHTML=(j.runs||[]).map(r=>`<div class="run ${r.cid===CID?'sel':''}" data-cid="${r.cid}">
    <span class="pill ${r.status}">${r.status}</span><b>${r.type}</b><span class="mut mono">${r.cid.slice(0,8)}</span>
    ${r.pdf?`<a href="/api/report?cid=${r.cid}" download onclick="event.stopPropagation()" style="margin-left:auto;font-weight:650">&#8595; pdf</a>`:`<span class="mut" style="margin-left:auto">watch &#8594;</span>`}</div>`).join("")||"<span class=mut>no runs yet</span>";
  document.querySelectorAll("#runs .run").forEach(el=>el.onclick=()=>pick(el.dataset.cid));  // robust attached handler
}
async function tick(){
  if(CID){
    try{
      const s=await(await fetch("/api/status?cid="+CID)).json();
      const dot=s.wf_status==="RUNNING"?"var(--ok)":"var(--mut)";
      $("#progsummary").innerHTML=`<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
        <span><span class="dot" style="background:${dot}"></span><b>${s.workflowType||"?"}</b></span>
        <span class="pill ${s.wf_status}">${s.wf_status||"?"}</span>
        <span class="mut">elapsed ${s.elapsed_s!=null?Math.floor(s.elapsed_s/60)+"m":"—"}</span>
        <span class="mut">rounds ${s.rounds_run??"—"}</span>
        <span style="color:var(--ok)">✔ ${s.confirmed} confirmed</span>
        ${s.pdf_available?`<a href="/api/report?cid=${CID}" download style="font-weight:650">⬇ Download report.pdf</a>`:(s.wf_status==="COMPLETED"?`<span class="mut">report pending…</span>`:"")}</div>
        <div class="mono mut" style="margin-top:6px">${CID}</div>`;
      const c=s.counts||{};
      $("#cards").innerHTML=["hypothesis","attempt","attempt_lesson","message","observation","finding"].map(k=>
        `<div class="card"><div class="n">${c[k]??0}</div><div class="l">${k.replace('_',' ')}</div></div>`).join("");
      const cb=s.coverage_by_status||{};const tot=Object.values(cb).reduce((a,b)=>a+b,0)||1;
      $("#cov").innerHTML=Object.entries(cb).map(([k,v])=>
        `<div style="margin:4px 0"><span class="mut" style="display:inline-block;width:90px">${k}</span>
         <span style="display:inline-block;height:10px;background:var(--acc);border-radius:4px;width:${Math.round(v/tot*300)}px;vertical-align:middle"></span> ${v}</div>`).join("")||"<span class=mut>—</span>";
    }catch(e){}
    if(tab==="messages"){
      try{
        const j=await(await fetch("/api/messages?cid="+CID)).json();
        let nnew=0;(j.messages||[]).forEach(m=>{if(!msgIds[m.msg_id]){msgIds[m.msg_id]=1;allMsgs.push(m);nnew++;}});
        if(nnew)renderFeed();
      }catch(e){}
    }
  }
}
loadRuns();setInterval(loadRuns,5000);setInterval(tick,2000);$("#mode").onchange();
</script></body></html>"""


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Swarm Console → http://127.0.0.1:{PORT}  (Conductor: {CONDUCTOR}, root: {ROOT})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
