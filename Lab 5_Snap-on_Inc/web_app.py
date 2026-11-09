"""Local browser UI for the fixed agent-team command-line workflow.

Run with ``python .\\web_app.py`` and open http://127.0.0.1:8000.
The UI starts the same ``agent_team.run_team`` function used by the CLI; it does
not duplicate the workflow or alter its node order.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import agent_team


ROOT = Path(__file__).parent
RUNS = ROOT / "agent_runs"
STATES: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Equity Research Agent Team</title><style>
:root{font-family:Inter,Segoe UI,Arial,sans-serif;color:#14213d;background:#f6f8fb}body{max-width:1050px;margin:0 auto;padding:32px 20px 60px}h1{margin:0;color:#102a43}.sub{color:#52606d;margin:8px 0 26px}.card{background:white;border:1px solid #d9e2ec;border-radius:12px;padding:20px;margin:16px 0;box-shadow:0 2px 7px #102a4310}.grid{display:grid;grid-template-columns:1fr 1fr 160px;gap:12px}label{font-weight:650;display:grid;gap:6px}input,textarea,button{font:inherit;padding:10px;border-radius:7px;border:1px solid #bcccdc}button{background:#176b87;color:white;border:0;font-weight:700;cursor:pointer}button:disabled{opacity:.6;cursor:wait}.status{font-weight:700}.node{padding:9px 11px;border-left:4px solid #d9e2ec;margin:6px 0;background:#f7fafc}.done{border-color:#2f855a}.active{border-color:#dd6b20}.failed{border-color:#c53030}.error{color:#c53030;white-space:pre-wrap}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#102a43;color:#e6fffa;padding:18px;border-radius:8px;line-height:1.5}.marks{display:grid;gap:10px}textarea{min-height:62px;resize:vertical}@media(max-width:680px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>Equity Research Agent Team</h1><p class="sub">Run the fixed SEC-to-verification workflow without opening a terminal.</p>
<form id="run" class="card"><div class="grid"><label>Ticker<input id="ticker" required maxlength="12" placeholder="SNA"></label><label>Optional peers<input id="peers" placeholder="SWK, ITW"></label><label>Price<input id="price" type="number" min="0" step="0.01" placeholder="357.51"></label></div><p><button id="go">Run team</button></p></form>
<section id="progress" hidden><div class="card"><div id="status" class="status">Preparing run…</div><div id="nodes"></div><div id="error" class="error"></div></div></section>
<section id="result" hidden><div class="card"><h2>Partner review marks</h2><p>Locate an Insight source line and record both hand-checked core claims before sharing the report.</p><div class="marks"><label>Manual mark 1<textarea id="mark1" placeholder="Claim, filing line, and pass/fail"></textarea></label><label>Manual mark 2<textarea id="mark2" placeholder="Claim, filing line, and pass/fail"></textarea></label><button id="save" type="button">Save partner marks</button></div></div><div class="card"><h2>Verified report</h2><pre id="report"></pre></div></section>
<script>
let runId=null, timer=null;
const $=id=>document.getElementById(id);
let nodeState={};
function renderNodes(order){for(const e of order){nodeState[e.node]=e.status} $('nodes').innerHTML='';for(const [node,status] of Object.entries(nodeState)){const d=document.createElement('div');d.className='node '+(status==='completed'?'done':status==='failed'?'failed':'active');d.textContent=node+' — '+status;$('nodes').append(d)}}
async function poll(){if(!runId)return;const data=await fetch('/api/runs/'+runId).then(r=>r.json());$('status').textContent=data.status;renderNodes(data.order||[]);$('error').textContent=data.error||'';if(data.manual_marks){$('mark1').value=data.manual_marks[0]||'';$('mark2').value=data.manual_marks[1]||''}if(data.report){$('report').textContent=data.report;$('result').hidden=false}if(data.finished){clearInterval(timer);$('go').disabled=false}}
$('run').addEventListener('submit',async e=>{e.preventDefault();nodeState={};$('go').disabled=true;$('progress').hidden=false;$('result').hidden=true;$('nodes').textContent='';$('error').textContent='';const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:$('ticker').value,peers:$('peers').value,price:$('price').value})});const data=await response.json();if(!response.ok){$('error').textContent=data.error;$('go').disabled=false;return}runId=data.id;await poll();timer=setInterval(poll,1200)});
$('save').addEventListener('click',async()=>{if(!runId)return;await fetch('/api/runs/'+runId+'/marks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({marks:[$('mark1').value,$('mark2').value]})})});
</script></body></html>"""


def _json_response(handler: BaseHTTPRequestHandler, status: int, value: Any) -> None:
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _run_directory_after(start: float) -> Path | None:
    folders = [folder for folder in RUNS.glob("*") if folder.is_dir() and folder.stat().st_mtime >= start]
    return max(folders, key=lambda folder: folder.stat().st_mtime, default=None)


def _run_worker(run_id: str, ticker: str, peers: list[str], price: float | None) -> None:
    try:
        path = agent_team.run_team(ticker, peers, price)
        with LOCK:
            STATES[run_id].update({"run_dir": path, "finished": True, "status": "Completed"})
    except Exception as exc:
        with LOCK:
            state = STATES[run_id]
            state.update({"run_dir": _run_directory_after(state["started"]), "finished": True, "status": "Failed", "error": str(exc)})


def _snapshot(run_id: str) -> dict[str, Any]:
    with LOCK:
        state = dict(STATES[run_id])
    run_dir = state.get("run_dir") or _run_directory_after(state["started"])
    if run_dir:
        with LOCK:
            STATES[run_id]["run_dir"] = run_dir
        state["run_dir"] = run_dir
    order: list[dict[str, str]] = []
    report = None
    manual_marks: list[str] = []
    if run_dir:
        execution = run_dir / "execution.json"
        if execution.exists():
            order = json.loads(execution.read_text(encoding="utf-8")).get("order", [])
        verified = run_dir / "verified_report.md"
        if verified.exists():
            report = verified.read_text(encoding="utf-8")
        marks = run_dir / "partner_marks.json"
        if marks.exists():
            manual_marks = json.loads(marks.read_text(encoding="utf-8")).get("marks", [])
    return {"status": state["status"], "finished": state["finished"], "error": state.get("error", ""), "order": order, "report": report, "manual_marks": manual_marks}


class App(BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        return

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/":
            body = PAGE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route.startswith("/api/runs/"):
            run_id = route.rsplit("/", 1)[-1]
            if run_id not in STATES:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Unknown run."})
            else:
                _json_response(self, HTTPStatus.OK, _snapshot(run_id))
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON."})
            return
        route = urlparse(self.path).path
        if route == "/api/runs":
            ticker = str(payload.get("ticker", "")).strip().upper()
            peers = [item.strip().upper() for item in re.split(r"\s*(?:,|\band\b)\s*", str(payload.get("peers", "")), flags=re.I) if item.strip()]
            try:
                price = float(payload["price"]) if str(payload.get("price", "")).strip() else None
            except (TypeError, ValueError):
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Price must be a number."})
                return
            if not ticker or not ticker.isalnum():
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Ticker must be letters or digits."})
                return
            run_id = uuid.uuid4().hex
            with LOCK:
                STATES[run_id] = {"started": time.time(), "status": "Starting deterministic SEC steps", "finished": False}
            threading.Thread(target=_run_worker, args=(run_id, ticker, peers, price), daemon=True).start()
            _json_response(self, HTTPStatus.ACCEPTED, {"id": run_id})
            return
        if route.startswith("/api/runs/") and route.endswith("/marks"):
            run_id = route.split("/")[-2]
            marks = payload.get("marks")
            if run_id not in STATES or not isinstance(marks, list) or len(marks) != 2:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Provide exactly two marks for an existing run."})
                return
            state = _snapshot(run_id)
            run_dir = STATES[run_id].get("run_dir")
            if not run_dir:
                _json_response(self, HTTPStatus.CONFLICT, {"error": "The run folder is not available yet."})
                return
            (run_dir / "partner_marks.json").write_text(json.dumps({"marks": [str(mark) for mark in marks]}, indent=2), encoding="utf-8")
            _json_response(self, HTTPStatus.OK, state)
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found."})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), App)
    print("Open http://127.0.0.1:8000")
    server.serve_forever()
