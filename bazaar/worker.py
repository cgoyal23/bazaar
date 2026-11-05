"""Worker process: `python -m bazaar.worker RUN_ID`.

One worker per run (a lock row with the pid). A killed worker leaves a dead pid
behind; the next worker takes over and resumes from the event log.
"""
import os
import sys
import traceback

from . import db, orchestrator


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def acquire(run_id: str) -> bool:
    with db.tx() as c:
        row = c.execute("SELECT pid FROM workers WHERE run_id=?", (run_id,)).fetchone()
        if row and row["pid"] != os.getpid() and _alive(row["pid"]):
            return False
        c.execute("INSERT OR REPLACE INTO workers VALUES(?,?,?)", (run_id, os.getpid(), db.now()))
    return True


def release(run_id: str):
    db.conn().execute("DELETE FROM workers WHERE run_id=? AND pid=?", (run_id, os.getpid()))


def main(run_id: str) -> int:
    if not acquire(run_id):
        print(f"another worker is already running {run_id}", file=sys.stderr)
        return 2
    try:
        orchestrator.run(run_id)
        return 0
    except Exception as e:
        db.Store(run_id).emit("System", "error", f"Worker error: {type(e).__name__}: {e}. "
                              "State is saved; press Resume to continue.", {"trace": traceback.format_exc()[-1500:]})
        db.set_run_status(run_id, "failed")
        return 1
    finally:
        release(run_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
