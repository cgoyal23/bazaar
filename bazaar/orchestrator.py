"""The procurement workflow. Each stage is a durable step: a finished step is
never run again, so a killed run resumes exactly where it stopped."""
import datetime
import os

from . import config, db, llm
from .crustdata import Crustdata
from .sandbox import Marketplace
from .agents import auditor, contract as contract_agent, negotiator, payments, planner, risk, scout, verifier


def mode():
    return {"crustdata": "live" if Crustdata().live else "offline (sandbox registry)",
            "llm": f"Claude ({llm.MODEL})" if llm.available() else "off (deterministic parser)"}


def _hand_over(store, run_id, kind, title, detail):
    store.raise_exception(kind, title, detail, f"{run_id}:{kind}")
    store.emit("System", "exception", f"{title} — handed to a human with every reason attached", detail)
    db.set_run_status(run_id, "needs_human")
    return "needs_human"


def run(run_id: str):
    store = db.Store(run_id)
    r = db.get_run(run_id)
    done = store.done_steps()
    if done:
        top = [s for s in done if ":" not in s]
        store.emit("System", "resume", f"Worker restarted (pid {os.getpid()}). Resuming from the event log — "
                   f"{len(top)} stage(s) already done: {', '.join(top) or 'none'}.", {"done": done})
    else:
        store.emit("System", "start", f"Run {run_id} started. Crustdata: {mode()['crustdata']}. LLM: {mode()['llm']}.")
    db.set_run_status(run_id, "running")

    spec = store.run_step("plan", lambda: planner.plan(store, r["request"]))
    if spec["status"] != "ok":
        store.raise_exception("needs_input", "Planner needs more information", {"questions": spec["questions"]},
                              f"{run_id}:needs_input")
        db.set_run_status(run_id, "needs_input")
        return "needs_input"

    market = Marketplace(datetime.date.fromisoformat(spec["today"]), spec["delivery_city"])
    scouted = store.run_step("scout", lambda: scout.scout(store, spec, market))
    if not scouted["candidates"]:
        return _hand_over(store, run_id, "no_supplier",
                          f"No supplier in the marketplace makes '{spec['item']}'",
                          {"declines": scouted.get("declines", [])})
    verified = store.run_step("verify", lambda: verifier.verify(store, spec, scouted))
    if not any(v["verdict"] == "PASS" for v in verified["results"]):
        return _hand_over(store, run_id, "no_verified_supplier",
                          "No supplier passed verification",
                          {"results": [{"supplier": v["supplier"], "verdict": v["verdict"], "reasons": v["reasons"]}
                                       for v in verified["results"]]})
    negotiated = store.run_step("negotiate", lambda: negotiator.negotiate(store, market, spec, verified))
    reviewed = store.run_step("risk_review", lambda: risk.review(store, spec, verified, negotiated))
    if not reviewed["ranked"]:
        store.raise_exception("no_deal", "No deal passed the Risk Officer",
                              {"reviews": reviewed["reviews"]}, f"{run_id}:no_deal")
        store.emit("System", "exception", "No approved deal — handed to a human with every reason attached")
        db.set_run_status(run_id, "needs_human")
        return "needs_human"
    signed = store.run_step("contract", lambda: contract_agent.contract(store, market, spec, verified, negotiated, reviewed))
    if signed["status"] != "signed":
        db.set_run_status(run_id, "needs_human")
        return "needs_human"
    paid = store.run_step("payment_advance", lambda: payments.pay_advance(store, market, signed))
    inbox = store.run_step("inbox_watch", lambda: payments.watch_inbox(store, market, signed)) \
        if paid["status"] == "paid" else {"processed": []}
    store.run_step("audit", lambda: auditor.audit(store, spec, scouted, verified, negotiated, reviewed,
                                                  signed, paid, inbox))
    status = "completed" if paid["status"] == "paid" else "needs_human"
    db.set_run_status(run_id, status)
    store.emit("System", "done", f"✅ Run {status}: {signed['po_number']} signed with {signed['supplier']}, "
               f"advance {'paid once' if paid['status'] == 'paid' else paid['status']}, audit trail written.")
    return status
