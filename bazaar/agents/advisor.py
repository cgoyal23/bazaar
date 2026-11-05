"""Recovery advisor: when a run cannot finish, work out how it could.

A procurement team that hits a wall does not just report failure; it comes back
with options. The advisor reads the evidence the run already gathered (verified
capacity, transit times, the prices suppliers actually offered, why deals were
blocked) and turns it into concrete revisions: a later deadline a verified
supplier can meet, the budget the lowest real offer needs, how many units fit the
current budget, or a combination. Each option is a set of answers for the
Planner; choosing one starts a revised run, so every limit is enforced again in
code and nothing is paid on a guess. Options never relax policy (advance cap,
verification), only the buyer's own request.
"""
import datetime
import json
import math

from .. import config, db
from ..sandbox import transit_days


def _d(s):
    return datetime.date.fromisoformat(s)


def _opt(oid, label, why, answers):
    return {"id": oid, "label": label, "why": why, "answers": answers}


def _fmt_rupees(r):
    return config.fmt_inr(int(r) * 100, "₹").removesuffix(".00")


def _capacity_values(store):
    """fact id -> capacity value {needed, evidence, claimed, ratio}."""
    return {f["id"]: json.loads(f["value"]) for f in store.facts() if f["key"] == "capacity"}


def _deadline_for(spec, earliest_landing: datetime.date):
    """The deadline to ask for so that `earliest_landing` sits inside the safety buffer."""
    # The Planner keeps min(3, days_to_deadline // 4) days of buffer; pick the
    # earliest deadline whose buffer still leaves `earliest_landing` inside it.
    for b in range(config.POLICY["delivery_buffer_days"] + 1):
        dl = earliest_landing + datetime.timedelta(days=b)
        if min(config.POLICY["delivery_buffer_days"], max(0, (dl - config.today()).days // 4)) <= b:
            return dl
    return earliest_landing + datetime.timedelta(days=config.POLICY["delivery_buffer_days"])


def _budget_options(spec, asks, prefix, extra_answers=None):
    """asks: sorted [(price, supplier)] of real offers from suppliers that could otherwise be approved."""
    out, qty = [], spec["quantity"]
    budget = spec["budget_paise"] // 100
    extra = dict(extra_answers or {})
    if not asks:
        return out
    p1, s1 = asks[0]
    need = p1 * qty
    if need > budget:
        out.append(_opt(f"{prefix}budget", f"Raise the budget to {_fmt_rupees(need)}" + (" and " + extra["_label"]
                                                                                       if extra.get("_label") else ""),
                        f"Lowest price a verified supplier offered: ₹{p1}/unit ({s1}). "
                        f"{qty:,} × ₹{p1} = {_fmt_rupees(need)}.",
                        {**{k: v for k, v in extra.items() if not k.startswith("_")}, "budget": str(need)}))
    if len(asks) > 1 and asks[1][0] * qty > budget:
        p2, s2 = asks[1]
        out.append(_opt(f"{prefix}budget2", f"Raise the budget to {_fmt_rupees(p2 * qty)} (safer)"
                        + (" and " + extra["_label"] if extra.get("_label") else ""),
                        f"Covers the two cheapest verified suppliers ({s1} at ₹{p1}, {s2} at ₹{p2}), so the order "
                        "still goes through if one backs out or changes terms at signing.",
                        {**{k: v for k, v in extra.items() if not k.startswith("_")}, "budget": str(p2 * qty)}))
    units = budget // p1
    if 0 < units < qty:
        out.append(_opt(f"{prefix}qty", f"Buy {units:,} units within {_fmt_rupees(budget)}"
                        + (" and " + extra["_label"] if extra.get("_label") else ""),
                        f"At the lowest verified price (₹{p1}/unit) the current budget covers {units:,} of "
                        f"{qty:,}; order the rest later or from stock.",
                        {**{k: v for k, v in extra.items() if not k.startswith("_")},
                         "item": f"{units} {spec['item']}"}))
    return out


def after_verification(store, spec, verified):
    """No supplier passed verification. Suppliers that failed only on time/capacity
    (genuine, but cannot make it by the date) are the way forward."""
    caps = _capacity_values(store)
    qty, today = spec["quantity"], config.today()
    latest = _d(spec["latest_acceptable_delivery"])
    p = config.POLICY
    feasible = []
    for r in verified["results"]:
        failed = [c for c in r["checks"] if c["ok"] is False]
        if not failed or any(c["check"] != "capacity" for c in failed if c["critical"]):
            continue  # fake, mismatched or unverifiable suppliers are never a way forward
        cap = next((caps.get(c["fact_id"]) for c in r["checks"] if c["check"] == "capacity"), None)
        if not cap or cap.get("ratio", 99) > p["capacity_claim_tolerance"] or not cap.get("evidence"):
            continue  # an unsupported capacity claim is misrepresentation, not a timing problem
        transit = transit_days(r.get("hq_city"), spec["delivery_city"])
        prod = math.ceil(qty * 30 / cap["evidence"])
        landing = max(today + datetime.timedelta(days=prod + transit + p["qc_days"]),
                      _d(r["bid"]["earliest_delivery"]))
        units_now = 0
        prod_avail = (latest - today).days - transit - p["qc_days"]
        if prod_avail > 0 and _d(r["bid"]["earliest_delivery"]) <= latest:
            units_now = int(cap["evidence"] * prod_avail / 30)
        feasible.append({"supplier": r["supplier"], "landing": landing, "units_now": units_now,
                         "price": r["bid"]["unit_price_inr"]})
    if not feasible:
        return []
    feasible.sort(key=lambda f: f["landing"])
    best = feasible[0]
    new_deadline = _deadline_for(spec, best["landing"])
    others = [f["supplier"] for f in feasible if f["landing"] <= best["landing"] + datetime.timedelta(days=0)]
    why = (f"{', '.join(others)} passed every check except time: verified capacity and transit put the earliest "
           f"delivery at {best['landing']}. {len(feasible)} genuine supplier(s) can deliver by "
           f"{_deadline_for(spec, feasible[-1]['landing'])}.")
    opts = [_opt("deadline", f"Move the deadline to {new_deadline:%d %b %Y}", why, {"deadline": new_deadline.isoformat()})]
    more = max(feasible, key=lambda f: f["units_now"])
    if 0 < more["units_now"] < qty:
        opts.append(_opt("partial", f"Buy {more['units_now']:,} units by the original deadline",
                         f"{more['supplier']} can verifiably make {more['units_now']:,} before "
                         f"{spec['latest_acceptable_delivery']}; order the rest separately.",
                         {"item": f"{more['units_now']} {spec['item']}"}))
    # Look ahead: if the budget is also below every quote, fix both in one step.
    bench = spec.get("benchmark_price")
    floor_quotes = sorted((f["price"], f["supplier"]) for f in feasible)
    ceiling = spec["unit_ceiling_paise"] // 100
    if floor_quotes and ceiling < min(q for q, _ in floor_quotes) * 0.8:
        target = max(bench or 0, int(min(q for q, _ in floor_quotes) * 0.9))
        budget = target * qty
        opts.append(_opt("deadline_budget", f"Move the deadline to {new_deadline:%d %b} and raise the budget to "
                         f"{_fmt_rupees(budget)}",
                         f"The budget (₹{ceiling}/unit) is also well below every quote "
                         f"(lowest ₹{floor_quotes[0][0]}/unit from {floor_quotes[0][1]})"
                         + (f" and the ₹{bench} price-book benchmark" if bench else "")
                         + ", so changing only the date would stop again at negotiation.",
                         {"deadline": new_deadline.isoformat(), "budget": str(budget)}))
    return opts


def after_negotiation(store, spec, negotiated, reviewed, voided=()):
    """No deal could be approved (or every approved one was voided at signing)."""
    voided = set(voided)
    reviews = {r["supplier"]: r for r in reviewed["reviews"]}
    asks, late = [], []
    for d in negotiated["deals"]:
        if d["supplier"] in voided:
            continue
        r = reviews.get(d["supplier"], {})
        hard = [c for c in r.get("checks", []) if not c["ok"] and c["rule"] in ("advance_cap", "verified",
                                                                                 "verification_score")]
        if hard:
            continue  # policy is not negotiable from here
        offers = [x["supplier"] for x in d["price"].get("rounds", []) if x.get("supplier") is not None]
        price = d["price"].get("unit_price") or (min(offers) if offers else None)
        if price:
            asks.append((price, d["supplier"]))
        if d["delivery"].get("status") != "agreed" or (d["delivery"].get("date") or "") > spec["latest_acceptable_delivery"]:
            late.append((d["delivery"].get("date"), d["supplier"]))
    asks.sort()
    opts = []
    extra = None
    late_dates = [x for x in late if x[0]]
    if late_dates:
        latest_needed = max(_d(x[0]) for x in late_dates)
        dl = _deadline_for(spec, latest_needed)
        opts.append(_opt("deadline", f"Move the deadline to {dl:%d %b %Y}",
                         f"{', '.join(s for _, s in late_dates)} can deliver by {latest_needed}, after the current "
                         f"limit {spec['latest_acceptable_delivery']}.", {"deadline": dl.isoformat()}))
        extra = {"deadline": dl.isoformat(), "_label": f"move the deadline to {dl:%d %b}"}
    opts += _budget_options(spec, asks, "", None)
    if extra:
        opts += _budget_options(spec, asks, "late_", extra)[:1]
    return opts


def offer(store, run_id, kind, options):
    """Attach options to the open exception and put them on the timeline."""
    if not options:
        store.emit("Advisor", "advice", "No safe way forward found automatically: every supplier failed a check "
                   "that a revision cannot fix (fake, misrepresented capacity, or policy). Adjust the request "
                   "by hand below, or widen the search.")
        return
    c = db.conn()
    row = c.execute("SELECT id, detail FROM exceptions WHERE run_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                    (run_id, kind)).fetchone()
    if row:
        detail = json.loads(row["detail"] or "{}")
        detail["options"] = options
        c.execute("UPDATE exceptions SET detail=? WHERE id=?", (json.dumps(detail, default=str), row["id"]))
    store.emit("Advisor", "advice", f"{len(options)} way(s) forward: " + " · ".join(o["label"] for o in options),
               {"options": options})
