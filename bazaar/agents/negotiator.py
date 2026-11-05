"""Negotiator: separate negotiations on price, delivery and payment terms, run in
parallel across every verified supplier (one thread per supplier per track).

Strategy is deterministic code, so a resumed run reaches the same deal. Every
message goes through the outbox, so after a crash the recorded replies are
replayed and nothing is sent twice.
"""
import datetime
from concurrent.futures import ThreadPoolExecutor

from .. import config


def _send(store, market, bid_id, track, n, body):
    reply, replayed = store.send(f"{store.run_id}:nego:{bid_id}:{track}:{n}", bid_id, body,
                                 lambda b: market.send(bid_id, b))
    return reply


def price_track(store, market, spec, sup):
    name, bid_id = sup["supplier"], sup["bid_ids"][0]
    ceiling = spec["unit_ceiling_paise"] // 100
    bid = sup["bid"]["unit_price_inr"]
    # The concession path is set by the supplier's quote, not by the budget, so a
    # bigger budget never makes us pay more for the same deal. The budget only caps it.
    anchor, target = round(0.8 * bid), round(0.9 * bid)
    step = (target - anchor) / 5
    offer, rounds, agreed = min(ceiling, anchor), [], None
    for r in range(6):
        final = r == 5
        reply = _send(store, market, bid_id, "price", r, {"type": "price_offer", "round": r,
                                                          "unit_price": offer, "final": final})
        rounds.append({"round": r, "buyer": offer, "supplier": reply.get("unit_price"), "reply": reply["type"]})
        store.emit("Negotiator", "nego", f"[{name} · price] R{r + 1}: we offer ₹{offer} → {reply['text']}",
                   {"supplier": name, "track": "price", "round": r, "buyer": offer,
                    "supplier_value": reply.get("unit_price"), "reply": reply["type"]})
        if reply["type"] == "accept":
            agreed = reply["unit_price"]
            break
        if reply["type"] == "reject":
            break
        counter = reply["unit_price"]
        nxt = min(ceiling, round(anchor + step * (r + 1)))
        # Late in the talks, a counter inside the budget ceiling is taken rather than lost.
        if counter <= nxt or (r >= 4 and counter <= ceiling):
            _send(store, market, bid_id, "price", f"{r}-accept", {"type": "accept_counter", "unit_price": counter})
            agreed = counter
            why = (f"our next step would have been ₹{nxt}" if counter <= nxt
                   else f"their counter has stopped moving and is inside our ₹{ceiling} ceiling")
            store.emit("Negotiator", "nego", f"[{name} · price] We accept their ₹{counter} ({why})",
                       {"supplier": name, "track": "price", "round": r, "buyer": counter,
                        "supplier_value": counter, "reply": "agreed"})
            break
        offer = nxt
    status = "agreed" if agreed is not None else "no_deal"
    if agreed is None:
        store.emit("Negotiator", "nego", f"[{name} · price] No deal within the ₹{ceiling} ceiling — walking away",
                   {"supplier": name, "track": "price", "reply": "no_deal"})
    return {"status": status, "unit_price": agreed, "opening_bid": bid, "rounds": rounds,
            "saving_vs_bid_pct": round((bid - agreed) / bid * 100, 1) if agreed else None}


def delivery_track(store, market, spec, sup):
    name, bid_id = sup["supplier"], sup["bid_ids"][0]
    target = spec["target_delivery"]
    latest = spec["latest_acceptable_delivery"]
    reply = _send(store, market, bid_id, "delivery", 0, {"type": "delivery_proposal", "date": target})
    store.emit("Negotiator", "nego", f"[{name} · delivery] We ask for {target} → {reply['text']}",
               {"supplier": name, "track": "delivery", "round": 0, "buyer": target,
                "supplier_value": reply.get("date"), "reply": reply["type"]})
    date = reply["date"]
    if reply["type"] == "counter":
        if date <= latest:
            _send(store, market, bid_id, "delivery", 1, {"type": "delivery_proposal", "date": date})
            store.emit("Negotiator", "nego", f"[{name} · delivery] {date} is inside our limit ({latest}) — agreed",
                       {"supplier": name, "track": "delivery", "round": 1, "buyer": date,
                        "supplier_value": date, "reply": "agreed"})
        else:
            store.emit("Negotiator", "nego", f"[{name} · delivery] {date} is after our limit {latest} — no deal",
                       {"supplier": name, "track": "delivery", "reply": "no_deal"})
            return {"status": "no_deal", "date": date}
    return {"status": "agreed", "date": date}


def payment_track(store, market, spec, sup):
    name, bid_id = sup["supplier"], sup["bid_ids"][0]
    cap = config.POLICY["max_advance_pct"]
    reply = _send(store, market, bid_id, "payment", 0, {"type": "payment_proposal", "advance_pct": 20})
    store.emit("Negotiator", "nego", f"[{name} · payment] We propose 20% advance, balance 15 days after QC "
               f"→ {reply['text']}", {"supplier": name, "track": "payment", "round": 0, "buyer": 20,
                                       "supplier_value": reply.get("advance_pct"), "reply": reply["type"]})
    if reply["type"] == "accept":
        return {"status": "agreed", "advance_pct": 20}
    want = reply["advance_pct"]
    if want <= cap:
        _send(store, market, bid_id, "payment", 1, {"type": "payment_proposal", "advance_pct": want})
        store.emit("Negotiator", "nego", f"[{name} · payment] {want}% is within policy — agreed",
                   {"supplier": name, "track": "payment", "round": 1, "buyer": want,
                    "supplier_value": want, "reply": "agreed"})
        return {"status": "agreed", "advance_pct": want}
    reply = _send(store, market, bid_id, "payment", 1, {"type": "payment_proposal", "advance_pct": cap, "final": True})
    store.emit("Negotiator", "nego", f"[{name} · payment] Final offer {cap}% → {reply['text']}",
               {"supplier": name, "track": "payment", "round": 1, "buyer": cap,
                "supplier_value": reply.get("advance_pct"), "reply": reply["type"]})
    if reply["type"] == "accept":
        return {"status": "agreed", "advance_pct": cap}
    return {"status": "supplier_final", "advance_pct": reply["advance_pct"],
            "note": f"Supplier insists on {reply['advance_pct']}%; above our {cap}% cap"}


TRACKS = {"price": price_track, "delivery": delivery_track, "payment": payment_track}


def negotiate_supplier(store, market, spec, sup):
    store.emit("Negotiator", "open", f"Opening three parallel negotiations with {sup['supplier']}",
               {"supplier": sup["supplier"]})
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {t: ex.submit(store.run_step, f"negotiate:{sup['domain']}:{t}",
                             lambda fn=fn: fn(store, market, spec, sup)) for t, fn in TRACKS.items()}
        res = {t: f.result() for t, f in futs.items()}
    deal = {"supplier": sup["supplier"], "domain": sup["domain"], "bid_ids": sup["bid_ids"], **res}
    if res["price"]["status"] == "agreed":
        deal["total_paise"] = res["price"]["unit_price"] * 100 * spec["quantity"]
    store.emit("Negotiator", "deal",
               f"{sup['supplier']}: " + (f"₹{res['price']['unit_price']}/unit "
                                         f"(total {config.fmt_inr(deal['total_paise'], '₹')})"
                                         if "total_paise" in deal else "no price agreed")
               + f", delivery {res['delivery'].get('date')}, advance {res['payment'].get('advance_pct')}%",
               {"deal": deal})
    return deal


def negotiate(store, market, spec, verified):
    passed = [v for v in verified["results"] if v["verdict"] == "PASS"]
    store.emit("Negotiator", "start", f"Negotiating with {len(passed)} verified suppliers in parallel "
               f"(price, delivery and payment tracks each)")
    with ThreadPoolExecutor(max_workers=max(1, len(passed))) as ex:
        futs = [ex.submit(store.run_step, f"negotiate:{v['domain']}",
                          lambda v=v: negotiate_supplier(store, market, spec, v)) for v in passed]
        deals = [f.result() for f in futs]
    return {"deals": deals}
