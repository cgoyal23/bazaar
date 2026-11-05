"""Tiny stdlib web server: serves the live timeline and runs each purchase in a
separate worker process, so the demo can SIGKILL the worker mid-payment and
restart it while the UI keeps watching."""
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, db, orchestrator
from .agents import payments
from .sandbox import Marketplace

WORKERS = {}  # run_id -> Popen
LOCK = threading.Lock()


def spawn(run_id):
    with LOCK:
        p = WORKERS.get(run_id)
        if p and p.poll() is None:
            return False
        env = dict(os.environ)
        env.setdefault("BAZAAR_PACE", "0.35")
        env.setdefault("BAZAAR_SETTLE_SECONDS", "8")
        env.pop("BAZAAR_CRASH_AT", None)
        WORKERS[run_id] = subprocess.Popen([sys.executable, "-m", "bazaar.worker", run_id], cwd=config.ROOT, env=env)
        return True


def alive(run_id):
    p = WORKERS.get(run_id)
    return bool(p and p.poll() is None)


def kill(run_id):
    p = WORKERS.get(run_id)
    if not p or p.poll() is not None:
        return False
    os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=5)
    db.Store(run_id).emit("System", "crash", f"💥 Worker pid {p.pid} killed with SIGKILL (no clean-up, no warning)",
                          pace=False)
    db.set_run_status(run_id, "killed")
    return True


def state(run_id, after):
    c = db.conn()
    run = db.get_run(run_id)
    if not run:
        return None
    steps = {r["name"]: dict(r) for r in c.execute("SELECT name,status,attempts,output FROM steps WHERE run_id=?", (run_id,))}
    outputs = {k: json.loads(v["output"]) for k, v in steps.items() if v["status"] == "done" and ":" not in k}
    events = [dict(e) for e in c.execute("SELECT id,ts,agent,kind,message,data FROM events WHERE run_id=? AND id>? "
                                         "ORDER BY id", (run_id, after))]
    for e in events:
        e["data"] = json.loads(e["data"] or "{}")
    prefix = f"PO-{run_id[:6].upper()}-%"
    transfers = [dict(r) for r in c.execute("SELECT * FROM bank_transfers WHERE reference LIKE ? ORDER BY created_at", (prefix,))]
    ledger = [dict(r) for r in c.execute("SELECT * FROM ledger WHERE run_id=? ORDER BY id", (run_id,))]
    exceptions = db.Store(run_id).exceptions()
    for x in exceptions:
        x["detail"] = json.loads(x["detail"] or "{}")
    ok, bad, n = db.verify_chain(run_id)
    return {"run": run, "events": events, "outputs": outputs,
            "steps": [{"name": k, "status": v["status"], "attempts": v["attempts"]} for k, v in steps.items()],
            "transfers": transfers, "ledger": ledger, "exceptions": exceptions, "alive": alive(run_id),
            "chain": {"ok": ok, "events": n}, "mode": json.loads(run["mode"] or "{}")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if u.path in ("/", "/index.html"):
            with open(os.path.join(config.ROOT, "web", "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parts == ["api", "runs"]:
            self._json({"runs": db.list_runs(), "mode": orchestrator.mode()})
        elif parts == ["api", "testworld"]:
            self._json({"suppliers": Marketplace.reveal()})
        elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "state":
            after = int(parse_qs(u.query).get("after", ["0"])[0])
            s = state(parts[2], after)
            self._json(s if s else {"error": "not found"}, 200 if s else 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = self._body()
        if parts == ["api", "runs"]:
            req = (body.get("request") or "").strip()
            if not req:
                return self._json({"error": "empty request"}, 400)
            run_id = db.create_run(req, orchestrator.mode())
            spawn(run_id)
            self._json({"run_id": run_id})
        elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "kill":
            self._json({"killed": kill(parts[2])})
        elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "resume":
            self._json({"started": spawn(parts[2])})
        elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "answer":
            if alive(parts[2]):
                return self._json({"ok": False, "message": "worker is still running"}, 409)
            out = orchestrator.answer(parts[2], body.get("answers"))
            if out["ok"]:
                spawn(parts[2])
            self._json(out, 200 if out["ok"] else 400)
        elif len(parts) == 4 and parts[:2] == ["api", "exceptions"] and parts[3] == "resolve":
            out = payments.resolve_exception(int(parts[2]), body.get("action", ""))
            if out.get("resume"):
                spawn(out["resume"])
            self._json(out)
        else:
            self._json({"error": "not found"}, 404)


def serve(port=8000):
    db.conn()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Bazaar running at http://localhost:{port}  (Crustdata: {orchestrator.mode()['crustdata']}; "
          f"LLM: {orchestrator.mode()['llm']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
