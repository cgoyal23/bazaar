"""Contract: writes the purchase order, sends it for countersignature, and checks
the signed copy line by line against the agreed terms before anyone relies on it.

A changed number is caught even when the new total is still inside the budget.
The supplier gets one chance to correct; if it insists, the deal is voided and
the next-best approved deal is tried.
"""
import re

from .. import config
from . import risk


def build_po(spec, deal, vendor, po_number):
    unit = deal["price"]["unit_price"] * 100
    total = unit * spec["quantity"]
    adv_pct = deal["payment"]["advance_pct"]
    advance = total * adv_pct // 100
    terms = {"po_number": po_number, "unit_price_paise": unit, "quantity": spec["quantity"],
             "total_paise": total, "advance_pct": adv_pct, "advance_paise": advance,
             "delivery_date": deal["delivery"]["date"], "delivery_city": spec["delivery_city"],
             "bank_account": vendor["bank_account"], "ifsc": vendor["ifsc"]}
    s = spec["specification"]
    text = "\n".join([
        f"PURCHASE ORDER  {po_number}",
        f"Date: {config.today()}",
        f"Buyer: {config.buyer_name()} (issued by Bazaar procurement agents)",
        f"Supplier: {vendor['legal_name']}  [FICTIONAL sandbox supplier]",
        f"Supplier registration: {deal.get('registration_id') or vendor.get('registration_id', '-')}",
        f"Supplier contact: {vendor['contact_email']}",
        f"Item: {s['item']} — " + "; ".join(s["assumed"][:1]),
        f"Quantity: {spec['quantity']:,} units",
        f"Unit price: INR {config.fmt_inr(unit, '')} (all-inclusive: GST and freight to {spec['delivery_city']})",
        f"Order total: INR {config.fmt_inr(total, '')}",
        f"Delivery: to {spec['delivery_city']} by {deal['delivery']['date']}",
        f"Payment terms: {adv_pct}% advance (INR {config.fmt_inr(advance, '')}) on countersignature; "
        f"balance within 15 days of delivery and QC pass",
        f"Supplier bank account: {vendor['bank_account']} (IFSC {vendor['ifsc']}) — verified by penny drop",
        "Quality: inspection on arrival at AQL 2.5; rejected units replaced at supplier's cost",
        "Late delivery: 0.5% of order value per day, capped at 10%",
        "Changes: any change to price, quantity, dates or bank details requires a new written PO.",
    ])
    return terms, text


def parse_signed(text):
    def grab(pattern, conv=lambda x: x):
        m = re.search(pattern, text)
        return conv(m.group(1)) if m else None
    return {
        "po_number": grab(r"PURCHASE ORDER\s+(PO-[A-Z0-9-]+)"),
        "unit_price_paise": grab(r"Unit price: INR ([\d,]+\.\d{2})", config.parse_inr),
        "quantity": grab(r"Quantity: ([\d,]+) units", lambda x: int(x.replace(",", ""))),
        "total_paise": grab(r"Order total: INR ([\d,]+\.\d{2})", config.parse_inr),
        "advance_pct": grab(r"Payment terms: (\d+)% advance", int),
        "advance_paise": grab(r"advance \(INR ([\d,]+\.\d{2})\)", config.parse_inr),
        "delivery_date": grab(r"Delivery: to \S+ by (\d{4}-\d{2}-\d{2})"),
        "delivery_city": grab(r"Delivery: to (\S+) by"),
        "bank_account": grab(r"Supplier bank account: (\d+)"),
        "ifsc": grab(r"IFSC ([A-Z0-9]+)"),
        "countersigned": "COUNTERSIGNED" in text,
    }


def diff_terms(agreed, signed):
    diffs = []
    for k, v in agreed.items():
        if signed.get(k) != v:
            diffs.append({"field": k, "agreed": v, "in_signed_copy": signed.get(k)})
    if not signed.get("countersigned"):
        diffs.append({"field": "countersignature", "agreed": True, "in_signed_copy": False})
    return diffs


def _fmt_diff(d):
    if d["field"].endswith("_paise"):
        return (f"{d['field'].replace('_paise', '')}: agreed {config.fmt_inr(d['agreed'], '₹')}, "
                f"signed copy says {config.fmt_inr(d['in_signed_copy'], '₹') if d['in_signed_copy'] else 'missing'}")
    return f"{d['field']}: agreed {d['agreed']}, signed copy says {d['in_signed_copy']}"


def contract(store, market, spec, verified, negotiated, reviewed):
    vendors = {v["domain"]: v for v in verified["results"]}
    deals = {d["domain"]: d for d in negotiated["deals"]}
    voided = []
    for n, domain in enumerate(reviewed["ranked"], start=1):
        deal, ver = deals[domain], vendors[domain]
        vendor = dict(ver["vendor_master"], registration_id=ver["registration_id"])
        deal = dict(deal, registration_id=ver["registration_id"])
        po_number = f"PO-{store.run_id[:6].upper()}-{n:02d}"
        terms, text = build_po(spec, deal, vendor, po_number)
        store.put_document(f"{po_number}.draft", "po_draft", text)
        store.emit("Contract", "po", f"Drafted {po_number} for {deal['supplier']}: {spec['quantity']:,} × "
                   f"{config.fmt_inr(terms['unit_price_paise'], '₹')} = {config.fmt_inr(terms['total_paise'], '₹')}, "
                   f"{terms['advance_pct']}% advance, delivery {terms['delivery_date']} — sent for countersignature",
                   {"po_number": po_number, "terms": terms})
        bid_id = deal["bid_ids"][0]
        reply, _ = store.send(f"{store.run_id}:po:{po_number}:sign", bid_id,
                              {"type": "po_for_signature", "po_number": po_number, "text": text},
                              lambda b: market.send(bid_id, b))
        signed = parse_signed(reply["text"])
        diffs = diff_terms(terms, signed)
        if diffs:
            store.emit("Contract", "mismatch",
                       f"⚠️ {deal['supplier']} returned {po_number} marked “{reply.get('note')}”, but it does not "
                       "match what was agreed: " + "; ".join(_fmt_diff(d) for d in diffs),
                       {"po_number": po_number, "diffs": diffs})
            reply2, _ = store.send(f"{store.run_id}:po:{po_number}:challenge", bid_id,
                                   {"type": "po_challenge", "po_number": po_number, "text": text,
                                    "diffs": diffs}, lambda b: market.send(bid_id, b))
            diffs2 = diff_terms(terms, parse_signed(reply2["text"]))
            store.emit("Contract", "challenge", f"Asked {deal['supplier']} to sign the PO as agreed → "
                       f"“{reply2.get('note')}”", {"po_number": po_number})
            if diffs2:
                reason = (f"altered the agreed terms after negotiation ({'; '.join(_fmt_diff(d) for d in diffs2)}) "
                          "and refused to correct them. The new total is still under budget, so only the "
                          "line-by-line check caught it.")
                risk.block_for_bad_faith(store, deal["supplier"], reason)
                store.emit("Contract", "void", f"{po_number} voided. Moving to the next approved deal.",
                           {"po_number": po_number})
                voided.append({"po_number": po_number, "supplier": deal["supplier"], "diffs": diffs2,
                               "reason": reason})
                continue
            reply = reply2
        signed_text = reply["text"]
        digest = store.put_document(po_number, "po_signed", signed_text)
        store.emit("Contract", "signed", f"✍️ {po_number} countersigned by {vendor['legal_name']}. Every term "
                   f"matches the agreed deal. SHA-256 {digest[:16]}…",
                   {"po_number": po_number, "sha256": digest})
        return {"status": "signed", "po_number": po_number, "supplier": deal["supplier"], "domain": domain,
                "bid_id": bid_id, "terms": terms, "signed_text": signed_text, "sha256": digest,
                "vendor": vendor, "voided": voided}
    store.raise_exception("no_contract", "No supplier signed the PO as agreed",
                          {"voided": voided}, f"{store.run_id}:no_contract")
    store.emit("Contract", "exception", "No approved deal produced a matching signed PO — handed to a human")
    return {"status": "no_contract", "voided": voided}
