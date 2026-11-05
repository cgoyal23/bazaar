#!/usr/bin/env python3
"""Bazaar command line.

  python3 run.py serve [--port 8000]          live timeline UI (http://localhost:8000)
  python3 run.py demo ["request"]             run one purchase in the terminal
  python3 run.py demo --crash-at-payment      kill the run right after the bank moves money,
                                              then resume it and prove it paid once
  python3 run.py resume RUN_ID                resume an interrupted run
"""
import argparse
import datetime
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

EXAMPLE = "We need 5,000 leather laptop sleeves in Bengaluru by 15 December, under ₹12 lakh."


def print_events(run_id, after=0):
    from bazaar import db
    last = after
    for e in db.conn().execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after)):
        local = datetime.datetime.fromisoformat(e["ts"]).astimezone().strftime("%H:%M:%S")
        print(f"{local}  {e['agent']:<12} {e['message']}")
        last = e["id"]
    return last


def worker(run_id, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.call([sys.executable, "-m", "bazaar.worker", run_id], cwd=ROOT, env=env)


def cmd_demo(args):
    from bazaar import bank, db, orchestrator
    run_id = db.create_run(args.request or EXAMPLE, orchestrator.mode())
    print(f"run {run_id}\n")
    if args.crash_at_payment:
        code = worker(run_id, {"BAZAAR_CRASH_AT": "after_bank_transfer"})
        last = print_events(run_id)
        print(f"\n*** worker exited with code {code} (killed mid-payment). Restarting… ***\n")
        worker(run_id)
        print_events(run_id, last)
    else:
        worker(run_id)
        print_events(run_id)
    c = db.conn()
    po = c.execute("SELECT name, body FROM documents WHERE run_id=? AND kind='po_signed'", (run_id,)).fetchone()
    if po:
        print("\n" + "=" * 72 + "\n" + po["body"] + "\n" + "=" * 72)
        print("bank transfers:", [dict(t) for t in bank.transfers_for_reference(po["name"])])
        print("ledger:", [dict(e) for e in bank.ledger_for_po(po["name"])])
    for e in db.Store(run_id).exceptions():
        print(f"exception #{e['id']} [{e['status']}] {e['title']}")
    print(f"\nstatus: {db.get_run(run_id)['status']}")


def main():
    p = argparse.ArgumentParser(description="Bazaar — an AI procurement department")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    d = sub.add_parser("demo")
    d.add_argument("request", nargs="?")
    d.add_argument("--crash-at-payment", action="store_true")
    r = sub.add_parser("resume")
    r.add_argument("run_id")
    args = p.parse_args()
    if args.cmd == "serve":
        from bazaar import server
        server.serve(args.port)
    elif args.cmd == "demo":
        cmd_demo(args)
    elif args.cmd == "resume":
        worker(args.run_id)
        print_events(args.run_id)


if __name__ == "__main__":
    main()
