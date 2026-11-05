"""Payments: match the invoice to the signed PO, pay the advance exactly once,
and stop any invoice whose bank details or sender do not match the vendor master.

Exactly-once, in code:
  1. a payment intent is written before money moves (key = PO + milestone)
  2. the bank call carries that idempotency key; the bank returns the same
     transfer for the same key and never moves money twice
  3. the ledger posts one entry per bank transfer id (UNIQUE)
A kill at any point heals on resume: the step re-runs, finds the transfer at the
bank, posts the ledger entry if it is missing, and sends nothing new.
"""
import difflib
import json
import os
import time

from .. import bank, config, db


def _intent(run_id, key, po_number, amount, account):
    c = db.conn()
    c.execute("INSERT OR IGNORE INTO payment_intents VALUES(?,?,?,?,?,?,?,?,?,?)",
              (key, run_id, po_number, "ADVANCE", amount, account, "pending", None, db.now(), db.now()))
    return dict(c.execute("SELECT * FROM payment_intents WHERE key=?", (key,)).fetchone())


def _update_intent(key, status, transfer_id=None):
    db.conn().execute("UPDATE payment_intents SET status=?, transfer_id=COALESCE(?, transfer_id), updated_at=? "
                      "WHERE key=?", (status, transfer_id, db.now(), key))


def check_invoice(inv, contract):
    """Code-only checks of an invoice against the signed PO and vendor master."""
    terms, vendor = contract["terms"], contract["vendor"]
    key = f"{terms['po_number']}:{inv.get('milestone')}"
    sender_domain = inv.get("from_email", "").split("@")[-1]
    sim = difflib.SequenceMatcher(None, sender_domain, vendor["domain"]).ratio()
    already = bank.find_transfer(key)
    holder = bank.penny_drop(inv.get("bank_account", ""), inv.get("ifsc", ""))
    checks = [
        ("po_number", inv.get("po_number") == terms["po_number"],
         f"Invoice cites {inv.get('po_number')}, signed PO is {terms['po_number']}"),
        ("amount", inv.get("amount_paise") == terms["advance_paise"],
         f"Invoice {config.fmt_inr(inv.get('amount_paise') or 0, '₹')} vs "
         f"{terms['advance_pct']}% advance {config.fmt_inr(terms['advance_paise'], '₹')} computed from the PO"),
        ("sender_domain", sender_domain == vendor["domain"],
         f"Sent from {sender_domain}; vendor master domain {vendor['domain']}"
         + (f" — LOOKALIKE ({sim:.0%} similar)" if sender_domain != vendor["domain"] and sim > 0.8 else "")),
        ("bank_account", inv.get("bank_account") == vendor["bank_account"] and inv.get("ifsc") == vendor["ifsc"],
         f"Pay-to a/c …{inv.get('bank_account', '')[-4:]} vs vendor master a/c …{vendor['bank_account'][-4:]} "
         "(verified by penny drop at onboarding)"),
        ("account_holder", holder == vendor["legal_name"],
         f"Penny drop on pay-to account returns '{holder}', vendor is '{vendor['legal_name']}'"),
        ("not_already_paid", already is None,
         "No earlier payment for this PO milestone" if already is None
         else f"Advance for {terms['po_number']} already paid ({already['id']})"),
    ]
    out = [{"check": n, "ok": ok, "detail": d} for n, ok, d in checks]
    return out, all(c["ok"] for c in out)


def pay_advance(store, market, contract):
    terms, vendor = contract["terms"], contract["vendor"]
    po = terms["po_number"]
    bid_id = contract["bid_id"]
    inv, replayed = store.send(f"{store.run_id}:invoice:{po}:ADVANCE", bid_id,
                               {"type": "invoice_request", "po_number": po, "milestone": "ADVANCE",
                                "po_text": contract["signed_text"]}, lambda b: market.send(bid_id, b))
    store.emit("Payments", "invoice", f"Invoice {inv['invoice_no']} from {inv['from_email']}: "
               f"{config.fmt_inr(inv['amount_paise'], '₹')} advance against {inv['po_number']}", {"invoice": inv})
    key = f"{po}:ADVANCE"
    existing = bank.find_transfer(key)
    checks, ok = check_invoice(inv, contract)
    if existing:  # resumed after the bank already moved the money
        checks = [c for c in checks if c["check"] != "not_already_paid"]
        ok = all(c["ok"] for c in checks)
    store.emit("Payments", "match", ("Invoice matches PO and vendor master on all checks" if ok else
                                     "Invoice FAILED checks: " + "; ".join(c["detail"] for c in checks if not c["ok"])),
               {"checks": checks})
    if not ok:
        eid = store.raise_exception("invoice_mismatch", f"Invoice {inv['invoice_no']} does not match {po}",
                                    {"invoice": inv, "checks": checks}, f"{store.run_id}:inv:{inv['invoice_no']}")
        return {"status": "held", "invoice": inv, "checks": checks, "exception_id": eid}

    amount = terms["advance_paise"]
    if amount > config.POLICY["auto_pay_limit_paise"]:
        eid = store.raise_exception("approval_needed", f"Advance {config.fmt_inr(amount, '₹')} exceeds auto-pay limit",
                                    {"invoice": inv}, f"{store.run_id}:approve:{po}")
        store.emit("Payments", "exception", "Amount above auto-pay limit — waiting for a human approver")
        return {"status": "awaiting_approval", "exception_id": eid}
    if terms["advance_pct"] > config.POLICY["max_advance_pct"]:
        raise RuntimeError("advance above policy reached Payments")  # unreachable by construction

    intent = _intent(store.run_id, key, po, amount, vendor["bank_account"])
    store.emit("Payments", "approve", f"Auto-approved under policy: {config.fmt_inr(amount, '₹')} ≤ auto-pay limit "
               f"{config.fmt_inr(config.POLICY['auto_pay_limit_paise'], '₹')}, advance {terms['advance_pct']}% ≤ "
               f"{config.POLICY['max_advance_pct']}%. Payment intent {key} recorded.", {"intent": intent})
    if existing:
        transfer = existing
        store.emit("Payments", "recover", f"♻️ Bank already holds transfer {transfer['id']} for key {key} "
                   f"(made before the interruption). Not sending again.", {"transfer": transfer})
    else:
        transfer, created = bank.transfer(key, amount, vendor["bank_account"], vendor["ifsc"],
                                          vendor["legal_name"], reference=po)
        _update_intent(key, "sent", transfer["id"])
        store.emit("Payments", "transfer", f"🏦 Bank accepted transfer {transfer['id']}: "
                   f"{config.fmt_inr(amount, '₹')} → {vendor['legal_name']} a/c …{vendor['bank_account'][-4:]} "
                   f"(idempotency key {key})", {"transfer": transfer})
        if config.crash_at() == "after_bank_transfer":
            store.emit("System", "crash", "💥 Simulated crash right after the bank moved the money", pace=False)
            os._exit(137)
        wait = config.settle_seconds()
        if wait:
            store.emit("Payments", "settling", f"⏳ Waiting {wait:.0f}s for settlement confirmation before posting "
                       "the ledger — kill the worker now to test crash recovery", {"seconds": wait})
            time.sleep(wait)
    entry, posted = bank.post_ledger(store.run_id, transfer, po, f"{terms['advance_pct']}% advance on {po}")
    _update_intent(key, "settled", transfer["id"])
    store.emit("Payments", "ledger", ("📒 Ledger posted: " if posted else "📒 Ledger entry already present: ")
               + f"Dr Advances to suppliers / Cr Bank {config.fmt_inr(entry['amount_paise'], '₹')} "
               f"({transfer['id']}). Transfers for {po}: {len(bank.transfers_for_reference(po))}.",
               {"ledger": entry})
    return {"status": "paid", "invoice": inv, "checks": checks, "transfer": transfer, "ledger": entry}


def watch_inbox(store, market, contract):
    """Process what arrives after the advance — including the bank-change fraud."""
    terms = contract["terms"]
    mails = market.inbox_after_payment(contract["vendor"]["contact_email"], terms["po_number"], terms["advance_paise"])
    results = []
    for inv in mails:
        store.emit("Payments", "invoice", f"📨 New invoice {inv['invoice_no']} from {inv['from_email']}: "
                   f"“{inv['text']}”", {"invoice": inv})
        checks, ok = check_invoice(inv, contract)
        if ok:  # would only happen for a genuine new milestone
            results.append({"invoice": inv, "status": "ok", "checks": checks})
            continue
        failed = [c for c in checks if not c["ok"]]
        eid = store.raise_exception(
            "bank_change", f"Bank-detail change request on {terms['po_number']} — payment held",
            {"invoice": inv, "checks": checks,
             "recommended_action": "Do not pay. Call the supplier on the phone number captured at verification "
                                   "(not any number in this email) before changing bank details."},
            f"{store.run_id}:bank_change:{inv['invoice_no']}")
        store.emit("Payments", "hold", f"🛑 HELD. Not paying {inv['invoice_no']}: "
                   + "; ".join(c["detail"] for c in failed) + ". Raised to a human.",
                   {"checks": checks, "exception_id": eid})
        results.append({"invoice": inv, "status": "held", "checks": checks, "exception_id": eid})
    return {"processed": results}


def resolve_exception(exc_id: int, action: str):
    """Called by a human from the UI. Even an approval cannot cause a second payment."""
    c = db.conn()
    row = c.execute("SELECT * FROM exceptions WHERE id=?", (exc_id,)).fetchone()
    if not row:
        return {"ok": False, "message": "no such exception"}
    store = db.Store(row["run_id"])
    detail = json.loads(row["detail"])
    if action == "reject":
        c.execute("UPDATE exceptions SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
                  ("Rejected as fraud by a human; supplier alerted via the verified phone number", db.now(), exc_id))
        store.emit("Human", "resolve", f"Human rejected exception #{exc_id}: {row['title']}", pace=False)
        return {"ok": True, "message": "rejected"}
    if action == "approve" and row["kind"] == "bank_change":
        inv = detail["invoice"]
        paid = bank.find_transfer(f"{inv['po_number']}:{inv['milestone']}")
        if paid:
            msg = (f"Refused: the advance for {inv['po_number']} was already paid ({paid['id']}). Approving a "
                   "bank change cannot create a second payment for the same milestone.")
            c.execute("UPDATE exceptions SET resolution=? WHERE id=?", (msg, exc_id))
            store.emit("Payments", "refuse", "🛑 " + msg, pace=False)
            return {"ok": False, "message": msg}
    return {"ok": False, "message": "action not supported for this exception"}
