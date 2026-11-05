"""Auditor: links every decision to its evidence and flags contradictions.

It re-derives the numbers from the raw records instead of trusting other
agents' summaries, and verifies the hash chain of the event log.
"""
import json

from .. import bank, config, db


def audit(store, spec, scouted, verified, negotiated, reviewed, contract, payment, inbox):
    flags, checks, decisions = [], [], []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            flags.append(f"{name}: {detail}")

    ok, bad, n = db.verify_chain(store.run_id)
    check("event_log_integrity", ok, f"{n} events, hash chain intact" if ok else f"chain broken at event {bad}")

    facts = {f["id"]: f for f in store.facts()}

    def cite(ids):
        return [{"fact_id": i, "key": facts[i]["key"], "value": json.loads(facts[i]["value"]),
                 "source": facts[i]["source"], "retrieved_at": facts[i]["retrieved_at"]} for i in ids if i in facts]

    for v in verified["results"]:
        ev = [c["fact_id"] for c in v["checks"] if v["verdict"] == "PASS" or c["ok"] is not True]
        decisions.append({"agent": "Verifier", "decision": f"{v['verdict']} {v['supplier']}",
                          "reasons": v["reasons"] or ["all checks passed"], "evidence": cite(ev)})
        check(f"evidence:{v['domain']}", len(ev) > 0, f"{v['supplier']} decision cites {len(ev)} facts")

    for rc in verified.get("real_companies") or []:
        ev = [f["id"] for f in facts.values() if f["subject"] == f"real:{rc.get('key') or rc.get('domain')}"]
        decisions.append({"agent": "Verifier", "decision": f"{rc['verdict']} {rc['name']} (real company, reference only)",
                          "reasons": rc.get("reasons") or ["active, capacity covers the order"], "evidence": cite(ev)})

    for r in reviewed["reviews"]:
        decisions.append({"agent": "Risk Officer", "decision": f"{r['decision']} {r['supplier']}",
                          "reasons": r["reasons"] or ["all policy rules pass"],
                          "evidence": [{"rule": c["rule"], "detail": c["detail"]} for c in r["checks"]]})
    for v in contract.get("voided", []):
        decisions.append({"agent": "Contract", "decision": f"VOID {v['po_number']} ({v['supplier']})",
                          "reasons": [v["reason"]], "evidence": v["diffs"]})

    for f in store.facts():
        if f["key"] == "contradiction":
            val = json.loads(f["value"])
            sup = f["subject"].split(":", 1)[1]
            rejected = any(v["domain"] == sup and v["verdict"] != "PASS" for v in verified["results"])
            checks.append({"check": f"contradiction:{sup}", "ok": rejected,
                           "detail": f"Claimed {val['claimed_capacity']:,}/month vs evidence {val['evidence_capacity']:,}/month"
                           + (" — resolved: supplier rejected" if rejected else " — UNRESOLVED")})
            if not rejected:
                flags.append(f"Unresolved contradiction for {sup}")

    merged = [c for c in scouted["candidates"] if len(c["aliases"]) > 1]
    for c in merged:
        checks.append({"check": "entity_resolution", "ok": True,
                       "detail": f"{' / '.join(c['aliases'])} kept as one record ({c['domain']})"})

    if contract.get("status") == "signed":
        t = contract["terms"]
        check("po_arithmetic", t["unit_price_paise"] * t["quantity"] == t["total_paise"],
              f"{t['quantity']:,} × {config.fmt_inr(t['unit_price_paise'], '₹')} = {config.fmt_inr(t['total_paise'], '₹')}")
        check("po_within_budget", t["total_paise"] <= spec["budget_paise"],
              f"{config.fmt_inr(t['total_paise'], '₹')} ≤ {config.fmt_inr(spec['budget_paise'], '₹')}")
        doc = store.document(t["po_number"])
        check("po_document_hash", doc and doc["sha256"] == contract["sha256"] == db.sha256(doc["body"]),
              f"Signed PO SHA-256 {contract['sha256'][:16]}… matches stored document")
        transfers = bank.transfers_for_reference(t["po_number"])
        ledger = bank.ledger_for_po(t["po_number"])
    if contract.get("status") == "signed" and payment.get("status") != "paid":
        check("no_payment_without_approval", len(transfers) == 0 and len(ledger) == 0,
              f"Advance {payment.get('status', 'not paid').replace('_', ' ')}: {len(transfers)} bank transfer(s), "
              f"{len(ledger)} ledger entr(y/ies) — nothing moves until a human decides")
    if contract.get("status") == "signed" and payment.get("status") == "paid":
        check("exactly_one_payment", len(transfers) == 1,
              f"{len(transfers)} bank transfer(s) for {t['po_number']}")
        check("ledger_matches_bank", len(ledger) == len(transfers) and
              sum(e["amount_paise"] for e in ledger) == sum(x["amount_paise"] for x in transfers),
              f"{len(ledger)} ledger entr(y/ies) totalling {config.fmt_inr(sum(e['amount_paise'] for e in ledger), '₹')}")
        check("advance_matches_po", sum(e["amount_paise"] for e in ledger) == t["advance_paise"],
              f"Paid {config.fmt_inr(sum(e['amount_paise'] for e in ledger), '₹')} = {t['advance_pct']}% of PO")
        check("paid_to_vendor_master", all(x["account"] == contract["vendor"]["bank_account"] for x in transfers),
              f"All transfers to the penny-drop-verified account …{contract['vendor']['bank_account'][-4:]}")
        held = [r for r in inbox.get("processed", []) if r["status"] == "held"]
        for h in held:
            checks.append({"check": "fraud_held", "ok": True,
                           "detail": f"{h['invoice']['invoice_no']} from {h['invoice']['from_email']} held, not paid"})

    open_exc = [e for e in store.exceptions() if e["status"] == "open"]
    summary = {"checks": checks, "flags": flags, "decisions": decisions,
               "open_exceptions": [{"id": e["id"], "title": e["title"]} for e in open_exc]}
    store.put_document("audit.json", "audit", db.dumps(summary))
    passed = sum(1 for c in checks if c["ok"])
    store.emit("Auditor", "audit", f"Audit: {passed}/{len(checks)} checks pass, {len(flags)} contradictions, "
               f"{len(decisions)} decisions each linked to evidence, {len(open_exc)} open human exception(s)",
               {"flags": flags})
    for f in flags:
        store.emit("Auditor", "flag", "🚩 " + f)
    return summary
