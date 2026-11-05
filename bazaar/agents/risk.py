"""Risk Officer: reviews every deal against policy and can block it.

All rules are code. A deal that breaks any of them is blocked, with the rule
and the numbers that broke it.
"""
from .. import config


def review_deal(spec, deal, verification):
    p = config.POLICY
    checks = []

    def rule(name, ok, detail):
        checks.append({"rule": name, "ok": bool(ok), "detail": detail})

    price_ok = deal["price"]["status"] == "agreed"
    rule("price_agreed", price_ok, "Price agreed" if price_ok else "No agreed price")
    if price_ok:
        total = deal["total_paise"]
        rule("within_budget", total <= spec["budget_paise"],
             f"Total {config.fmt_inr(total, '₹')} vs budget {config.fmt_inr(spec['budget_paise'], '₹')}")
        rule("unit_ceiling", deal["price"]["unit_price"] * 100 <= spec["unit_ceiling_paise"],
             f"₹{deal['price']['unit_price']}/unit vs ceiling {config.fmt_inr(spec['unit_ceiling_paise'], '₹')}")
    d = deal["delivery"]
    rule("delivery", d["status"] == "agreed" and d["date"] <= spec["latest_acceptable_delivery"],
         f"Delivery {d.get('date')} vs latest acceptable {spec['latest_acceptable_delivery']}")
    adv = deal["payment"].get("advance_pct")
    rule("advance_cap", adv is not None and adv <= p["max_advance_pct"],
         f"Advance {adv}% vs policy cap {p['max_advance_pct']}%")
    rule("verified", verification and verification["verdict"] == "PASS",
         f"Verifier verdict {verification['verdict'] if verification else 'missing'}"
         f" (score {verification['score'] if verification else 0})")
    if verification:
        rule("verification_score", verification["score"] >= p["min_verification_score"],
             f"Score {verification['score']} vs minimum {p['min_verification_score']}")
    blocked = [c for c in checks if not c["ok"]]
    return {"supplier": deal["supplier"], "domain": deal["domain"],
            "decision": "BLOCK" if blocked else "APPROVE", "checks": checks,
            "reasons": [c["detail"] for c in blocked]}


def review(store, spec, verified, negotiated):
    by_domain = {v["domain"]: v for v in verified["results"]}
    reviews = []
    for deal in negotiated["deals"]:
        r = review_deal(spec, deal, by_domain.get(deal["domain"]))
        reviews.append(r)
        if r["decision"] == "BLOCK":
            store.emit("Risk Officer", "block", f"🛑 BLOCKED {deal['supplier']}: " + "; ".join(r["reasons"]),
                       {"review": r})
        else:
            store.emit("Risk Officer", "approve", f"Approved {deal['supplier']} — all "
                       f"{len(r['checks'])} policy rules pass", {"review": r})
    approved = [d for d, r in zip(negotiated["deals"], reviews) if r["decision"] == "APPROVE"]
    ranked = sorted(approved, key=lambda d: (d["total_paise"], d["delivery"]["date"]))
    if ranked:
        store.emit("Risk Officer", "rank", "Ranking of approved deals (lowest total cost first): " + ", ".join(
            f"{i + 1}. {d['supplier']} {config.fmt_inr(d['total_paise'], '₹')}" for i, d in enumerate(ranked)))
    return {"reviews": reviews, "ranked": [d["domain"] for d in ranked]}


def block_for_bad_faith(store, supplier, reason):
    store.emit("Risk Officer", "block", f"🛑 BLOCKED {supplier}: {reason}", {"supplier": supplier, "reason": reason})
