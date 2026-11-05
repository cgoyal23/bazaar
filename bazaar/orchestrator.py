"""The procurement workflow. Each stage is a durable step: a finished step is
never run again, so a killed run resumes exactly where it stopped."""
import datetime
import json
import os

from . import config, db, llm
from .crustdata import Crustdata
from . import sandbox
from .sandbox import Marketplace
from .agents import auditor, contract as contract_agent, negotiator, payments, planner, risk, scout, verifier
from .agents import advisor, vendor_check


def mode():
    return {"crustdata": "live" if Crustdata().live else "offline (sandbox registry)",
            "llm": f"Claude ({llm.MODEL})" if llm.available() else "off (deterministic parser)"}


def _advise(store, run_id, kind, compute):
    """Work out ways forward once (a durable step), and attach them to the exception."""
    def step():
        options = compute()
        advisor.offer(store, run_id, kind, options)
        return options
    store.run_step(f"advice:{kind}", step)


def option(run_id: str, option_id: str):
    """Look up a way forward the advisor offered on this run."""
    for e in db.Store(run_id).exceptions():
        for o in json.loads(e["detail"] or "{}").get("options", []):
            if o["id"] == option_id:
                return o
    return None


def revise(run_id: str, answers: dict, label: str):
    """Start a revised run from a stopped one: same request, plus the human's chosen
    changes as Planner answers. A new run keeps the outbox, POs and ledger of each
    attempt separate; the old run records which revision replaced it."""
    parent = db.get_run(run_id)
    if not parent:
        return {"ok": False, "message": "no such run"}
    if parent["status"] not in ("needs_human", "needs_input", "failed"):
        return {"ok": False, "message": f"run is {parent['status']}; only a stopped run can be revised"}
    clean = {k: str(v).strip()[:300] for k, v in (answers or {}).items() if k in planner.ASK and str(v).strip()}
    if not clean:
        return {"ok": False, "message": "no changes given"}
    store = db.Store(run_id)
    merged = {**store.answers(), **clean}
    child = db.create_run(parent["request"], {**mode(), "revision_of": run_id, "revision": label})
    db.Store(child).set_answers(merged)
    db.conn().execute("UPDATE exceptions SET status='resolved', resolution=?, resolved_at=? "
                      "WHERE run_id=? AND status='open' AND kind IN "
                      "('no_verified_supplier','no_deal','no_contract','no_supplier','needs_input')",
                      (f"Revised as run {child}: {label}", db.now(), run_id))
    store.emit("Human", "revise", f"Chose a way forward: {label} → revised run {child}", {"child": child,
                                                                                          "answers": clean},
               pace=False)
    db.set_run_status(run_id, "revised")
    return {"ok": True, "run_id": child}


def _hand_over(store, run_id, kind, title, detail):
    store.raise_exception(kind, title, detail, f"{run_id}:{kind}")
    store.emit("System", "exception", f"{title} — handed to a human with every reason attached", detail)
    db.set_run_status(run_id, "needs_human")
    return "needs_human"


def answer(run_id: str, answers: dict):
    """A human answers the Planner's open questions. The answers are stored, the
    plan step is reset, and the same run continues (the caller restarts the worker)."""
    r = db.get_run(run_id)
    if not r:
        return {"ok": False, "message": "no such run"}
    if r["status"] != "needs_input":
        return {"ok": False, "message": f"run is {r['status']}, not waiting for answers"}
    answers = {k: str(v).strip()[:300] for k, v in (answers or {}).items()
               if k in planner.ASK and str(v).strip()}
    if not answers:
        return {"ok": False, "message": "no answers given"}
    store = db.Store(run_id)
    store.set_answers(answers)
    c = db.conn()
    c.execute("UPDATE exceptions SET status='resolved', resolution=?, resolved_at=? "
              "WHERE run_id=? AND kind='needs_input' AND status='open'",
              ("Answered: " + "; ".join(f"{k} = {v}" for k, v in answers.items()), db.now(), run_id))
    c.execute("DELETE FROM steps WHERE run_id=? AND name='plan'", (run_id,))
    store.emit("Human", "answer", "Answered the Planner: " + "; ".join(f"{k}: “{v}”" for k, v in answers.items()),
               {"answers": answers}, pace=False)
    db.set_run_status(run_id, "queued")
    return {"ok": True}


def _run_vendor_check(store, run_id, request):
    """Onboarding check on one vendor: no purchase, no money moves."""
    rep = store.run_step("vendor_check", lambda: vendor_check.check_vendor(store, request))
    ok, bad, n = db.verify_chain(run_id)
    cited = all(c.get("fact_id") for c in rep["checks"])
    store.emit("Auditor", "audit", f"Audit: {len(rep['checks'])} findings, every one stored as a fact with its source "
               f"({'yes' if cited else 'NO'}); event log hash chain {'intact' if ok else f'broken at {bad}'} ({n} events)",
               {"cited": cited, "chain_ok": ok})
    if rep["verdict"] in ("HIGH_RISK", "UNVERIFIABLE") or rep.get("needs_review"):
        store.raise_exception("vendor_review", f"Vendor {rep['verdict'].replace('_', ' ').lower()}: "
                              f"{rep['vendor']['name'] or rep['vendor']['domain']}",
                              {"vendor": rep["vendor"], "reasons": rep["reasons"],
                               "recommended_action": rep["headline"]}, f"{run_id}:vendor_review")
    db.set_run_status(run_id, "completed")
    store.emit("System", "done", f"✅ Vendor check complete: {rep['verdict'].replace('_', ' ')} — {rep['headline']}.")
    return "completed"


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
        m = json.loads(r.get("mode") or "{}")
        if m.get("revision_of"):
            store.emit("System", "revision", f"Revision of run {m['revision_of']}: {m.get('revision')}. "
                       f"Changes: " + "; ".join(f"{k} = {v}" for k, v in store.answers().items()), {"parent": m["revision_of"]})
    db.set_run_status(run_id, "running")

    if vendor_check.detect(r["request"]):
        return _run_vendor_check(store, run_id, r["request"])

    spec = store.run_step("plan", lambda: planner.plan(store, r["request"]))
    if spec["status"] != "ok":
        n = sum(1 for e in store.exceptions() if e["kind"] == "needs_input")
        store.raise_exception("needs_input", "Planner needs more information",
                              {"questions": spec["questions"], "ask": spec["ask"]}, f"{run_id}:needs_input:{n}")
        db.set_run_status(run_id, "needs_input")
        return "needs_input"

    ref_price, basis = sandbox.reference_price(spec)
    market = Marketplace(datetime.date.fromisoformat(spec["today"]), spec["delivery_city"],
                         spec.get("category", "leather_goods"), ref_price)
    scouted = store.run_step("scout", lambda: scout.scout(store, spec, market))
    if not scouted["candidates"]:
        return _hand_over(store, run_id, "no_supplier",
                          f"No supplier in the marketplace makes '{spec['item']}'",
                          {"declines": scouted.get("declines", [])})
    verified = store.run_step("verify", lambda: verifier.verify(store, spec, scouted))
    if not any(v["verdict"] == "PASS" for v in verified["results"]):
        out = _hand_over(store, run_id, "no_verified_supplier",
                         "No supplier passed verification",
                         {"results": [{"supplier": v["supplier"], "verdict": v["verdict"], "reasons": v["reasons"]}
                                      for v in verified["results"]]})
        _advise(store, run_id, "no_verified_supplier", lambda: advisor.after_verification(store, spec, verified))
        return out
    negotiated = store.run_step("negotiate", lambda: negotiator.negotiate(store, market, spec, verified))
    reviewed = store.run_step("risk_review", lambda: risk.review(store, spec, verified, negotiated))
    if not reviewed["ranked"]:
        store.raise_exception("no_deal", "No deal passed the Risk Officer",
                              {"reviews": reviewed["reviews"]}, f"{run_id}:no_deal")
        store.emit("System", "exception", "No approved deal — handed to a human with every reason attached")
        db.set_run_status(run_id, "needs_human")
        _advise(store, run_id, "no_deal", lambda: advisor.after_negotiation(store, spec, negotiated, reviewed))
        return "needs_human"
    signed = store.run_step("contract", lambda: contract_agent.contract(store, market, spec, verified, negotiated, reviewed))
    if signed["status"] != "signed":
        db.set_run_status(run_id, "needs_human")
        _advise(store, run_id, "no_contract", lambda: advisor.after_negotiation(
            store, spec, negotiated, reviewed, voided=[v["supplier"] for v in signed.get("voided", [])]))
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
