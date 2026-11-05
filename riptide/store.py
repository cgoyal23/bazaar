"""Riptide's state: one SQLite file. Every signal keeps the raw Crustdata record it
came from, and every profile keeps a history of snapshots, so growth over time
(connections, followers) can be measured instead of guessed."""
import json
import os
import sqlite3
import threading

from bazaar import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  id TEXT PRIMARY KEY,                 -- person id + new company + start date: one move, one signal
  person_id INTEGER, name TEXT, kind TEXT, score INTEGER, trust TEXT,
  first_seen TEXT, last_seen TEXT, source TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS snapshots(
  person_id INTEGER, ts TEXT, connections INTEGER, followers INTEGER, title TEXT, company TEXT,
  PRIMARY KEY(person_id, ts));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS watches(
  id INTEGER PRIMARY KEY, created_at TEXT, filters TEXT, status TEXT, last_polled TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS seen_runs(watch_id INTEGER, run_id INTEGER, PRIMARY KEY(watch_id, run_id));
CREATE TABLE IF NOT EXISTS usage(ts TEXT, endpoint TEXT, credits REAL, note TEXT);
"""

_tl = threading.local()


def path():
    return os.environ.get("RIPTIDE_DB") or os.path.join(config.ROOT, "data", "riptide.db")


def conn():
    p = path()
    c = getattr(_tl, "c", None)
    if c is None or getattr(_tl, "p", None) != p:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        c = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        _tl.c, _tl.p = c, p
    return c


def event(kind, message, data=None, ts=None):
    from bazaar.db import now
    conn().execute("INSERT INTO events(ts,kind,message,data) VALUES(?,?,?,?)",
                   (ts or now(), kind, message, json.dumps(data or {}, default=str)))


def usage(endpoint, credits, note=""):
    from bazaar.db import now
    conn().execute("INSERT INTO usage VALUES(?,?,?,?)", (now(), endpoint, credits, note))
