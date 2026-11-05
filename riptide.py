#!/usr/bin/env python3
"""Riptide: live talent-flow signals on Crustdata.

  python3 riptide.py serve [--port 8100]   live dashboard; rescans Crustdata every 10 min
  python3 riptide.py scan                  one live scan, printed
  python3 riptide.py watch [--webhook URL] save the move filter as a Crustdata Discovery Watch
  python3 riptide.py poll                  read new watcher runs
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    p = argparse.ArgumentParser(description="Riptide — live talent-flow signals on Crustdata")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8100)))
    sub.add_parser("scan")
    w = sub.add_parser("watch")
    w.add_argument("--webhook")
    sub.add_parser("poll")
    a = p.parse_args()
    from riptide import engine, server
    if a.cmd == "serve":
        server.serve(a.port)
    elif a.cmd == "scan":
        print(engine.scan())
        for sig in engine.feed()["signals"][:15]:
            print(f"{sig['score']:>3}  {sig['trust']:<11} {sig['kind_label']:<20} {sig['name']}: ex-{sig['from']['company']} "
                  f"{sig['from']['title']} → {sig['to']['title']} @ {sig['to']['company']}")
    elif a.cmd == "watch":
        print(engine.create_watch(webhook_url=a.webhook))
    elif a.cmd == "poll":
        print("new signals:", engine.poll_watches())


if __name__ == "__main__":
    main()
