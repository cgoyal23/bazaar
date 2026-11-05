"""Riptide server: a background loop keeps the feed live (rescans Crustdata and
reads watcher runs on a schedule), and a small JSON API feeds the dashboard."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from bazaar import config
from bazaar.crustdata import CrustdataError
from . import engine, store

STATE = {"last_scan": None, "next_scan": None, "scanning": False, "last_error": None,
         "every": int(os.environ.get("RIPTIDE_SCAN_MINUTES", "10")),
         "since_days": int(os.environ.get("RIPTIDE_SINCE_DAYS", "60")),
         "country": os.environ.get("RIPTIDE_COUNTRY", "India")}
LOCK = threading.Lock()


def run_cycle():
    if not LOCK.acquire(blocking=False):
        return {"ok": False, "message": "a scan is already running"}
    STATE["scanning"] = True
    try:
        out = engine.scan(STATE["since_days"], STATE["country"])
        out["watch_signals"] = engine.poll_watches()
        STATE["last_error"] = None
        return {"ok": True, **out}
    except CrustdataError as e:
        STATE["last_error"] = str(e)
        store.event("error", f"Scan failed: {e}")
        return {"ok": False, "message": str(e)}
    finally:
        from bazaar.db import now
        STATE["last_scan"] = now()
        STATE["next_scan"] = time.time() + STATE["every"] * 60
        STATE["scanning"] = False
        LOCK.release()


def loop():
    while True:
        if STATE["next_scan"] is None or time.time() >= STATE["next_scan"]:
            run_cycle()
        time.sleep(5)


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
        if u.path in ("/", "/index.html"):
            with open(os.path.join(config.ROOT, "web", "riptide.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/feed":
            nxt = STATE["next_scan"]
            self._json({**engine.feed(), "state": {**STATE, "next_in": max(0, int(nxt - time.time())) if nxt else None}})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/api/scan":
            self._json(run_cycle())
        elif u.path == "/api/watch":
            try:
                w = engine.create_watch(STATE["since_days"], STATE["country"], body.get("webhook_url") or None)
                time.sleep(3)
                n = engine.poll_watches()
                self._json({"ok": True, "watch": w.get("id"), "status": w.get("status"), "new_signals": n})
            except CrustdataError as e:
                store.event("error", f"Watch create failed: {e}")
                self._json({"ok": False, "message": str(e)}, 400)
        elif u.path == "/api/poll":
            self._json({"ok": True, "new_signals": engine.poll_watches()})
        elif u.path.startswith("/api/signals/") and u.path.endswith("/deep-check"):
            self._json(engine.deep_check(u.path.split("/")[3]))
        elif u.path == "/webhooks/crustdata":
            self._json({"ok": True, "new_signals": engine.ingest_webhook(body)})
        else:
            self._json({"error": "not found"}, 404)


def serve(port=8100, live=True):
    store.conn()
    if live:
        threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Riptide running at http://localhost:{port}  (rescans every {STATE['every']} min)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
