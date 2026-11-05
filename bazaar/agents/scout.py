"""Scout: find suppliers.

1. Broadcast an RFQ to the sandbox marketplace (fictional suppliers, who reply with bids).
2. With a Crustdata key, also discover *real* Indian manufacturers. Real companies are
   used for discovery and verification only; they are never contacted, because the
   handbook keeps agents in the room.
3. Resolve entities: two bids from the same company under two names become one record.
"""
from .. import catalog, config
from ..crustdata import Crustdata, CrustdataError, dig


def _domain(email_or_url: str) -> str:
    s = email_or_url.split("@")[-1]
    s = s.replace("https://", "").replace("http://", "").split("/")[0]
    return s.lower().removeprefix("www.")


def scout(store, spec, market):
    rfq = {"type": "rfq", "item": spec["item"], "quantity": spec["quantity"],
           "deliver_to": spec["delivery_city"], "deliver_by": spec["latest_acceptable_delivery"],
           "terms": "all-inclusive price, max 30% advance"}
    store.emit("Scout", "rfq", f"RFQ posted to the sandbox marketplace: {spec['quantity']:,} × {spec['item']} "
               f"to {spec['delivery_city']} by {spec['latest_acceptable_delivery']}", rfq)
    replies, replayed = store.send(f"{store.run_id}:rfq", "marketplace", rfq, market.broadcast_rfq)
    bids = [b for b in replies if b.get("type") == "bid"]
    declines = [d for d in replies if d.get("type") == "decline"]
    for d in declines:
        store.emit("Scout", "decline", f"{d['company_name']} <{d['contact_email']}> declined: {d['text']}",
                   {"decline": d, "fictional": True})
    for b in bids:
        store.emit("Scout", "bid", f"Bid from {b['company_name']} <{b['contact_email']}>: "
                   f"₹{b['unit_price_inr']}/unit, claims {b['claimed_capacity_month']:,}/month, "
                   f"{b['advance_pct']}% advance", {"bid": b, "fictional": True})

    # entity resolution: same web domain or same claimed registration = same company
    merged = {}
    for b in bids:
        key = _domain(b["contact_email"])
        reg = b.get("registration_id")
        hit = next((k for k, c in merged.items() if k == key or (reg and c["registration_id"] == reg)), None)
        if hit:
            c = merged[hit]
            c["aliases"].append(b["company_name"])
            c["bid_ids"].append(b["bid_id"])
            if b["unit_price_inr"] < c["unit_price_inr"]:
                c.update({k: b[k] for k in ("unit_price_inr",)})
            store.emit("Scout", "dedupe",
                       f"Merged '{b['company_name']}' into '{c['company_name']}': same domain {key} and "
                       f"registration {reg}. One company, one record, one negotiation.",
                       {"aliases": c["aliases"], "domain": key, "registration_id": reg})
        else:
            merged[key] = dict(b, domain=key, aliases=[b["company_name"]], bid_ids=[b["bid_id"]], fictional=True)
    candidates = list(merged.values())

    real = []
    cd = Crustdata(on_event=lambda kind, msg: store.emit("Scout", kind, msg))
    if cd.live:
        cat = spec.get("category") or "general"
        query = catalog.search_query(cat, spec["item"])
        store.emit("Scout", "crustdata", f"Crustdata Company Search (India, {catalog.get(cat)['label']}): “{query}”")
        try:
            dropped = []
            for co in cd.search_companies(query, "IND", 10):
                name = dig(co, "basic_info.name")
                domain = dig(co, "basic_info.primary_domain")
                desc = dig(co, "basic_info.description") or ""
                if not name:
                    continue
                if not catalog.matches_category(cat, f"{name} {desc}"):
                    dropped.append(name)
                    continue
                if len(real) >= 6:
                    continue
                real.append({"crustdata_company_id": co.get("crustdata_company_id"), "name": name,
                             "domain": domain, "year_founded": dig(co, "basic_info.year_founded"),
                             "headcount": dig(co, "headcount.total"),
                             "headquarters": dig(co, "locations.headquarters"),
                             "description": desc[:200]})
            if dropped:
                store.emit("Scout", "crustdata",
                           f"Dropped {len(dropped)} search result(s) whose description is not about "
                           f"{catalog.get(cat)['label']}: {', '.join(dropped[:5])}",
                           {"dropped": dropped})
            store.emit("Scout", "crustdata",
                       f"Crustdata returned {len(real)} real Indian companies in {catalog.get(cat)['label']}. Used for "
                       "market discovery and verification only — not contacted (handbook: agents stay in the room).",
                       {"companies": real})
        except CrustdataError as e:
            store.emit("Scout", "tool_error", f"Crustdata search failed: {e}. Continuing with marketplace bids only.")
    else:
        store.emit("Scout", "crustdata", "CRUSTDATA_API_KEY not set — skipping real-company discovery. "
                   "Supplier checks run against the sandbox registry.")
    store.emit("Scout", "done", f"{len(bids)} bids → {len(candidates)} distinct suppliers"
               + (f", plus {len(real)} real companies for reference" if real else ""))
    return {"candidates": candidates, "real_companies": real, "bids_received": len(bids),
            "declines": [{"supplier": d["company_name"], "reason": d["text"]} for d in declines]}
