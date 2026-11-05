"""Verifier: is each supplier real, active and able to deliver?

Every check becomes a fact with its source. A check that cannot be completed
(tool down after retries) is UNVERIFIABLE, never PASS: the Verifier says so
instead of guessing. Critical failures reject the supplier outright.
"""
import datetime
import re
import time

from .. import bank, config
from ..crustdata import Crustdata, CrustdataError, dig
from ..sandbox import SandboxRegistry, transit_days

CRITICAL = {"registry_record", "domain_match", "capacity", "bank_account_holder"}


def _retry(store, fn, what, retries=3):
    for attempt in range(retries + 1):
        try:
            return fn()
        except (TimeoutError, OSError) as e:
            if attempt == retries:
                raise
            store.emit("Verifier", "retry", f"{what}: {e} — retry {attempt + 1}/{retries}")
            time.sleep(0.2 * 2 ** attempt)


def _norm(name: str) -> str:
    out = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    out = re.sub(r"\b(pvt|ltd|private|limited|llp|co|company|inc)\b", " ", out)
    return " ".join(out.split())


def verify_one(store, spec, cand, registry: SandboxRegistry):
    name = cand["company_name"]
    domain = cand["domain"]
    subj = f"supplier:{domain}"
    checks = []

    def check(key, ok, detail, source, value=None, critical=None):
        fid = store.fact(subj, key, value if value is not None else detail, source)
        checks.append({"check": key, "ok": ok, "detail": detail, "source": source, "fact_id": fid,
                       "critical": key in CRITICAL if critical is None else critical})

    src = "sandbox registry (simulated Crustdata record for a fictional company)"
    try:
        rec = _retry(store, lambda: registry.lookup(domain), f"Registry lookup for {domain}")
    except (TimeoutError, OSError) as e:
        check("registry_record", None, f"Registry unreachable after retries: {e}", src)
        return _verdict(store, cand, checks, spec)

    if not rec:
        check("registry_record", False, f"No company registry record for {domain}; claimed registration "
              f"{cand.get('registration_id')} does not resolve", src)
        age = registry.domain_age_days(domain)
        if age is not None:
            check("domain_age", age > 365, f"Website domain is {age} days old", "sandbox WHOIS", age)
        check("web_footprint", False, "0 independent web or news mentions found", "sandbox web search", 0)
    else:
        check("registry_record", True, f"{rec['legal_name']} · {rec['registration_id']} · HQ {rec['hq_city']}", src,
              {"legal_name": rec["legal_name"], "registration_id": rec["registration_id"]})
        known = [_norm(n) for n in [rec["legal_name"], *rec.get("trade_names", [])]]
        names_ok = all(_norm(a) in known for a in cand["aliases"])
        check("domain_match", names_ok,
              f"Bid names {cand['aliases']} {'match' if names_ok else 'do not match'} the registered names for {domain}",
              src)
        age_years = config.today().year - rec["year_founded"]
        check("company_age", age_years >= config.POLICY["min_company_age_years"],
              f"Founded {rec['year_founded']} ({age_years} years)", src, rec["year_founded"])
        shrink = rec["headcount_6mo_ago"] and rec["headcount"] < 0.7 * rec["headcount_6mo_ago"]
        check("active", rec["headcount"] > 0 and not shrink,
              f"Headcount {rec['headcount']} now vs {rec['headcount_6mo_ago']} six months ago", src,
              rec["headcount"], critical=False)
        mentions = rec.get("web_mentions", [])
        check("web_footprint", len(mentions) > 0,
              f"{len(mentions)} recent mentions" + (f", latest: “{mentions[0]['title']}” ({mentions[0]['date']})"
                                                    if mentions else ""), "sandbox web search", mentions)

        # capacity: evidence from headcount vs what this order needs before the deadline
        p = config.POLICY
        evidence_cap = int(rec["headcount"] * p["production_share_of_headcount"] * p["units_per_worker_month"])
        latest = datetime.date.fromisoformat(spec["latest_acceptable_delivery"])
        transit = transit_days(rec["hq_city"], spec["delivery_city"])
        prod_days = (latest - config.today()).days - transit - p["qc_days"]
        need_month = int(spec["quantity"] / max(prod_days, 1) * 30) if prod_days > 0 else 10**9
        claimed = cand["claimed_capacity_month"]
        ratio = claimed / max(evidence_cap, 1)
        cap_ok = evidence_cap >= need_month
        detail = (f"Needs ≈{need_month:,}/month ({spec['quantity']:,} units in {prod_days} production days). "
                  f"Evidence supports ≈{evidence_cap:,}/month ({rec['headcount']} staff × "
                  f"{p['production_share_of_headcount']:.0%} × {p['units_per_worker_month']}/worker). "
                  f"Claimed {claimed:,}/month")
        if ratio > p["capacity_claim_tolerance"]:
            detail += f" — {ratio:.1f}× the evidence: claim unsupported"
        check("capacity", cap_ok, detail, "Verifier capacity model over registry headcount",
              {"needed": need_month, "evidence": evidence_cap, "claimed": claimed, "ratio": round(ratio, 1)})
        if ratio > p["capacity_claim_tolerance"]:
            store.fact(subj, "contradiction", {"claimed_capacity": claimed, "evidence_capacity": evidence_cap},
                       "Verifier: supplier claim vs registry headcount")

    holder = bank.penny_drop(cand["bank_account"], cand["ifsc"])
    legal = rec["legal_name"] if rec else name
    holder_ok = bool(holder) and _norm(holder) == _norm(legal)
    check("bank_account_holder", holder_ok,
          f"Penny drop on a/c …{cand['bank_account'][-4:]} returns '{holder}'"
          + ("" if holder_ok else f", not '{legal}'"), "sandbox bank penny-drop", holder)
    return _verdict(store, cand, checks, spec, rec)


def _verdict(store, cand, checks, spec, rec=None):
    crit_fail = [c for c in checks if c["critical"] and c["ok"] is False]
    unknown = [c for c in checks if c["ok"] is None]
    scored = [c for c in checks if c["ok"] is not None]
    score = round(sum(1 for c in scored if c["ok"]) / max(len(scored), 1), 2)
    if crit_fail:
        verdict = "REJECT"
    elif unknown:
        verdict = "UNVERIFIABLE"
    elif score < config.POLICY["min_verification_score"]:
        verdict = "REJECT"
    else:
        verdict = "PASS"
    reasons = [c["detail"] for c in (crit_fail or unknown or [c for c in checks if c["ok"] is False])]
    out = {"supplier": cand["company_name"], "domain": cand["domain"], "bid_ids": cand["bid_ids"],
           "aliases": cand["aliases"], "verdict": verdict, "score": score, "checks": checks,
           "reasons": reasons, "legal_name": rec["legal_name"] if rec else None,
           "hq_city": rec["hq_city"] if rec else cand.get("city"),
           "registration_id": rec["registration_id"] if rec else None,
           "vendor_master": ({"legal_name": rec["legal_name"], "domain": cand["domain"],
                              "contact_email": cand["contact_email"], "bank_account": cand["bank_account"],
                              "ifsc": cand["ifsc"]} if verdict == "PASS" else None),
           "bid": {k: cand[k] for k in ("unit_price_inr", "claimed_capacity_month", "earliest_delivery",
                                        "advance_pct", "contact_email")}}
    icon = {"PASS": "✅", "REJECT": "⛔", "UNVERIFIABLE": "❔"}[verdict]
    msg = f"{icon} {cand['company_name']}: {verdict} (score {score:.2f})"
    if verdict != "PASS":
        msg += " — " + "; ".join(reasons[:2])
    store.emit("Verifier", "verdict", msg, {"supplier": cand["company_name"], "verdict": verdict,
                                            "checks": checks, "price": cand["unit_price_inr"]})
    return out


def verify_real(store, companies, limit=3):
    """Verify real Crustdata-discovered companies (reference only, never contacted)."""
    cd = Crustdata(on_event=lambda kind, msg: store.emit("Verifier", kind, msg))
    out = []
    for co in companies[:limit]:
        if not co.get("domain"):
            continue
        try:
            data = cd.enrich_domain(co["domain"])
        except CrustdataError as e:
            store.emit("Verifier", "tool_error", f"Crustdata enrich failed for {co['name']}: {e} — marked unverifiable")
            out.append({**co, "verdict": "UNVERIFIABLE"})
            continue
        if not data:
            out.append({**co, "verdict": "NO_RECORD"})
            continue
        subj = f"real:{co['domain']}"
        src, url = "Crustdata /company/enrich", "https://api.crustdata.com/company/enrich"
        hc = dig(data, "headcount.total")
        yf = dig(data, "basic_info.year_founded")
        hq = dig(data, "locations.headquarters")
        for k, v in (("headcount", hc), ("year_founded", yf), ("headquarters", hq)):
            if v is not None:
                store.fact(subj, k, v, src, url)
        active = bool(hc and hc > 0)
        out.append({**co, "headcount": hc, "year_founded": yf, "headquarters": hq,
                    "verdict": "REAL_ACTIVE" if active else "UNCLEAR"})
        store.emit("Verifier", "real_company",
                   f"Real company {co['name']} ({co['domain']}): headcount {hc}, founded {yf}, HQ {hq} — "
                   f"source Crustdata. Reference only, not contacted.", {"company": co["name"]})
    return out


def verify(store, spec, scouted):
    registry = SandboxRegistry()
    results = []
    for cand in sorted(scouted["candidates"], key=lambda c: c["unit_price_inr"]):
        results.append(store.run_step(f"verify:{cand['domain']}", lambda c=cand: verify_one(store, spec, c, registry)))
    real = verify_real(store, scouted.get("real_companies") or []) if scouted.get("real_companies") else []
    passed = [r for r in results if r["verdict"] == "PASS"]
    store.emit("Verifier", "done", f"{len(passed)} of {len(results)} suppliers passed: "
               + ", ".join(r["supplier"] for r in passed))
    return {"results": results, "real_companies": real}
