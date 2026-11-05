"""The single source of truth: one SQLite file.

* events     append-only, hash-chained timeline (tamper-evident)
* steps      durable step runner: a finished step is never run again
* outbox     every message to a counterparty, keyed, so a resumed run replays
             the recorded reply instead of sending again
* facts      every fact with its source and retrieval time
* payment_intents / bank_transfers / ledger   exactly-once money movement
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
import datetime
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, mode TEXT);
CREATE TABLE IF NOT EXISTS steps(
  run_id TEXT, name TEXT, status TEXT, output TEXT, attempts INTEGER DEFAULT 0,
  started_at TEXT, finished_at TEXT, PRIMARY KEY(run_id, name));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, agent TEXT, kind TEXT,
  message TEXT, data TEXT, prev_hash TEXT, hash TEXT);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, subject TEXT, key TEXT, value TEXT,
  source TEXT, url TEXT, retrieved_at TEXT);
CREATE TABLE IF NOT EXISTS outbox(
  key TEXT PRIMARY KEY, run_id TEXT, counterparty TEXT, body TEXT, reply TEXT,
  sent_at TEXT, replied_at TEXT);
CREATE TABLE IF NOT EXISTS documents(
  run_id TEXT, name TEXT, kind TEXT, body TEXT, sha256 TEXT, created_at TEXT,
  PRIMARY KEY(run_id, name));
CREATE TABLE IF NOT EXISTS exceptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, title TEXT, detail TEXT,
  status TEXT, resolution TEXT, created_at TEXT, resolved_at TEXT, dedupe_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS payment_intents(
  key TEXT PRIMARY KEY, run_id TEXT, po_number TEXT, milestone TEXT, amount_paise INTEGER,
  account TEXT, status TEXT, transfer_id TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS bank_transfers(
  id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, amount_paise INTEGER NOT NULL,
  account TEXT NOT NULL, ifsc TEXT, beneficiary TEXT, reference TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, transfer_id TEXT UNIQUE NOT NULL,
  po_number TEXT, debit TEXT, credit TEXT, amount_paise INTEGER, memo TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS api_cache(key TEXT PRIMARY KEY, endpoint TEXT, response TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS answers(run_id TEXT, field TEXT, value TEXT, ts TEXT, PRIMARY KEY(run_id, field));
CREATE TABLE IF NOT EXISTS workers(run_id TEXT PRIMARY KEY, pid INTEGER, started_at TEXT);
"""

_tl = threading.local()


def db_path() -> str:
    return os.environ.get("BAZAAR_DB") or os.path.join(config.ROOT, "data", "bazaar.db")


def conn() -> sqlite3.Connection:
    path = db_path()
    c = getattr(_tl, "conn", None)
    if c is None or getattr(_tl, "path", None) != path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(SCHEMA)
        _tl.conn, _tl.path = c, path
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def event_hash(prev, ts, agent, kind, message, data) -> str:
    return sha256("|".join([prev, ts, agent, kind, message, data]))


# ---------------------------------------------------------------- runs

def create_run(request: str, mode: dict) -> str:
    run_id = uuid.uuid4().hex[:10]
    t = now()
    conn().execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                   (run_id, request, "queued", t, t, dumps(mode)))
    return run_id


def get_run(run_id: str):
    row = conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def set_run_status(run_id: str, status: str):
    conn().execute("UPDATE runs SET status=?, updated_at=? WHERE id=?", (status, now(), run_id))


def list_runs(limit=20):
    return [dict(r) for r in conn().execute(
        "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]


def verify_chain(run_id: str):
    """Recompute the hash chain. Returns (ok, first_bad_event_id or None, count)."""
    prev = "GENESIS"
    n = 0
    for r in conn().execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)):
        n += 1
        expect = event_hash(prev, r["ts"], r["agent"], r["kind"], r["message"], r["data"])
        if r["prev_hash"] != prev or r["hash"] != expect:
            return False, r["id"], n
        prev = r["hash"]
    return True, None, n


class Store:
    """Everything an agent may write goes through here, scoped to one run."""

    def __init__(self, run_id: str):
        self.run_id = run_id

    # -- timeline
    def emit(self, agent: str, kind: str, message: str, data=None, pace=True) -> int:
        payload = dumps(data or {})
        with tx() as c:
            row = c.execute("SELECT hash FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1",
                            (self.run_id,)).fetchone()
            prev = row["hash"] if row else "GENESIS"
            ts = now()
            h = event_hash(prev, ts, agent, kind, message, payload)
            cur = c.execute(
                "INSERT INTO events(run_id,ts,agent,kind,message,data,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
                (self.run_id, ts, agent, kind, message, payload, prev, h))
            event_id = cur.lastrowid
        if pace and config.pace():
            time.sleep(config.pace())
        return event_id

    # -- durable steps
    def step_done(self, name: str):
        row = conn().execute("SELECT * FROM steps WHERE run_id=? AND name=?",
                             (self.run_id, name)).fetchone()
        return json.loads(row["output"]) if row and row["status"] == "done" else None

    def done_steps(self):
        return [r["name"] for r in conn().execute(
            "SELECT name FROM steps WHERE run_id=? AND status='done' ORDER BY finished_at", (self.run_id,))]

    def run_step(self, name: str, fn):
        c = conn()
        row = c.execute("SELECT * FROM steps WHERE run_id=? AND name=?", (self.run_id, name)).fetchone()
        if row and row["status"] == "done":
            return json.loads(row["output"])
        attempts = (row["attempts"] if row else 0) + 1
        c.execute(
            "INSERT INTO steps(run_id,name,status,attempts,started_at) VALUES(?,?,'running',?,?) "
            "ON CONFLICT(run_id,name) DO UPDATE SET status='running', attempts=excluded.attempts, "
            "started_at=excluded.started_at", (self.run_id, name, attempts, now()))
        if attempts > 1:
            self.emit("System", "recover",
                      f"Re-entering interrupted step '{name}' (attempt {attempts}). "
                      "Every side effect is keyed, so nothing is sent or paid twice.",
                      {"step": name, "attempt": attempts})
        out = fn()
        c.execute("UPDATE steps SET status='done', output=?, finished_at=? WHERE run_id=? AND name=?",
                  (dumps(out), now(), self.run_id, name))
        return out

    # -- facts with provenance
    def fact(self, subject, key, value, source, url=None, retrieved_at=None) -> int:
        cur = conn().execute(
            "INSERT INTO facts(run_id,subject,key,value,source,url,retrieved_at) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, subject, key, dumps(value), source, url, retrieved_at or now()))
        return cur.lastrowid

    def facts(self, subject=None):
        q = "SELECT * FROM facts WHERE run_id=?"
        args = [self.run_id]
        if subject:
            q += " AND subject=?"
            args.append(subject)
        return [dict(r) for r in conn().execute(q + " ORDER BY id", args)]

    # -- outbox: exactly-once messaging
    def send(self, key: str, counterparty: str, body: dict, deliver):
        """Send `body` once. Returns (reply, replayed). A resumed run gets the recorded reply."""
        c = conn()
        row = c.execute("SELECT * FROM outbox WHERE key=?", (key,)).fetchone()
        if row and row["reply"] is not None:
            return json.loads(row["reply"]), True
        if not row:
            c.execute("INSERT INTO outbox(key,run_id,counterparty,body,sent_at) VALUES(?,?,?,?,?)",
                      (key, self.run_id, counterparty, dumps(body), now()))
        reply = deliver(body)
        c.execute("UPDATE outbox SET reply=?, replied_at=? WHERE key=?", (dumps(reply), now(), key))
        return reply, False

    # -- a human's answers to the Planner's questions
    def answers(self):
        return {r["field"]: r["value"] for r in conn().execute(
            "SELECT field, value FROM answers WHERE run_id=?", (self.run_id,))}

    def set_answers(self, answers: dict):
        with tx() as c:
            for field, value in answers.items():
                c.execute("INSERT OR REPLACE INTO answers VALUES(?,?,?,?)", (self.run_id, field, value, now()))

    # -- documents
    def put_document(self, name, kind, body) -> str:
        digest = sha256(body)
        conn().execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?)",
                       (self.run_id, name, kind, body, digest, now()))
        return digest

    def document(self, name):
        row = conn().execute("SELECT * FROM documents WHERE run_id=? AND name=?",
                             (self.run_id, name)).fetchone()
        return dict(row) if row else None

    # -- human exceptions
    def raise_exception(self, kind, title, detail, dedupe_key) -> int:
        c = conn()
        c.execute("INSERT OR IGNORE INTO exceptions(run_id,kind,title,detail,status,created_at,dedupe_key) "
                  "VALUES(?,?,?,?, 'open', ?, ?)", (self.run_id, kind, title, dumps(detail), now(), dedupe_key))
        return c.execute("SELECT id FROM exceptions WHERE dedupe_key=?", (dedupe_key,)).fetchone()["id"]

    def exceptions(self):
        return [dict(r) for r in conn().execute(
            "SELECT * FROM exceptions WHERE run_id=? ORDER BY id", (self.run_id,))]
