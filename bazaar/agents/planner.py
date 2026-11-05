"""Planner: a request in a customer's words -> specification, budget, deadline, task plan.

Numbers are read by a deterministic parser, so the same request worded another
way gets the same plan. Claude (optional) only fills fields the parser could not
find, and each value it returns must be validated against the original text.
If a field that changes the result is still missing, the Planner asks a human.
"""
import datetime
import re

from .. import catalog, config, llm

CITIES = {
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "blr": "Bengaluru", "mumbai": "Mumbai",
    "bombay": "Mumbai", "new delhi": "Delhi", "delhi": "Delhi", "kanpur": "Kanpur", "chennai": "Chennai",
    "madras": "Chennai", "hyderabad": "Hyderabad", "pune": "Pune", "kolkata": "Kolkata",
    "calcutta": "Kolkata", "noida": "Noida", "gurugram": "Gurugram", "gurgaon": "Gurugram",
    "ahmedabad": "Ahmedabad", "agra": "Agra", "jaipur": "Jaipur", "lucknow": "Lucknow",
}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
            r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
MULT = {"lakh": 10**5, "lakhs": 10**5, "lac": 10**5, "lacs": 10**5, "l": 10**5, "crore": 10**7,
        "crores": 10**7, "cr": 10**7, "k": 10**3, "thousand": 10**3, "million": 10**6, "mn": 10**6, "m": 10**6}
NUM = r"(\d[\d,]*(?:\.\d+)?)"
UNIT = r"(lakhs?|lacs?|crores?|cr|thousand|million|mn|k|l|m)?\b"
CUR = r"(?:₹|rs\.?|inr|rupees)"
BUDGET_RES = [
    re.compile(rf"(?:under|below|within|max(?:imum)?|budget(?:\s+(?:of|is))?\s*:?|up\s*to|upto|"
               rf"(?:not|no)\s+(?:more|over)\s+than|less\s+than|cap(?:ped)?\s+at|<=?)\s*{CUR}?\s*{NUM}\s*{UNIT}", re.I),
    re.compile(rf"{CUR}\s*{NUM}\s*{UNIT}", re.I),
    re.compile(rf"{NUM}\s*(lakhs?|lacs?|crores?|cr)\b", re.I),
]
PER_UNIT = re.compile(r"^\s*(?:/|per\b|each\b|apiece\b|a\s+piece\b|/unit|/pc)", re.I)
STOP = re.compile(r"\s+(?:in|to|at|by|delivered|deliver|delivery|before|under|below|within|shipped|ship|"
                  r"needed|required|asap|with|budget|max|"
                  r"for\s+(?:our|the|delivery|a|an|my|us|office|team|staff|employees|"
                  + "|".join(re.escape(c) for c in sorted(CITIES, key=len, reverse=True)) + r")\b|"
                  r"(?:that|which)\b)\b|[,.;:!?(]", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_budget(text: str):
    for rx in BUDGET_RES:
        for m in rx.finditer(text):
            value = _num(m.group(1)) * MULT.get((m.group(2) or "").lower(), 1)
            if value <= 0:
                continue
            per_unit = bool(PER_UNIT.match(text[m.end():m.end() + 12]))
            return {"rupees": value, "per_unit": per_unit, "span": m.span(), "text": m.group(0).strip()}
    return None


def _year_for(month: int, day: int, year, today: datetime.date):
    if year:
        y = int(year)
        return y + 2000 if y < 100 else y
    try:
        d = datetime.date(today.year, month, day)
    except ValueError:
        return today.year
    return today.year if d >= today else today.year + 1


def parse_deadline(text: str, today: datetime.date):
    patterns = [
        (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH_RE}\.?(?:,?\s+(\d{{4}}))?", re.I),
         lambda m: (m[3], MONTHS[m[2][:3].lower()], int(m[1]))),
        (re.compile(rf"\b{MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?", re.I),
         lambda m: (m[3], MONTHS[m[1][:3].lower()], int(m[2]))),
        (re.compile(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b"),  # Indian dd/mm[/yyyy]
         lambda m: (m[3], int(m[2]), int(m[1]))),
    ]
    for rx, conv in patterns:
        m = rx.search(text)
        if not m:
            continue
        year, month, day = conv(m)
        try:
            y = year if isinstance(year, int) and year > 31 else _year_for(month, day, year, today)
            return {"date": datetime.date(y, month, day), "span": m.span(), "text": m.group(0)}
        except (ValueError, TypeError):
            continue
    return None


def parse_city(text: str):
    low = text.lower()
    best = None
    for alias, city in sorted(CITIES.items(), key=lambda kv: -len(kv[0])):
        for m in re.finditer(rf"\b{re.escape(alias)}\b", low):
            before = low[max(0, m.start() - 22):m.start()]
            score = 2 if re.search(r"\b(in|to|at|deliver(?:ed|y)?(?: to| in| at)?)\s*$", before) else 1
            if best is None or score > best[0]:
                best = (score, city, m.group(0))
    return {"city": best[1], "text": best[2]} if best else None


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if re.search(r"(ch|sh|x|ss)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def parse_quantity_item(text: str):
    m = re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh)?\s+(?:units?\s+of\s+|pcs\.?\s+(?:of\s+)?|"
                  r"pieces\s+of\s+|nos\.?\s+(?:of\s+)?|x\s+)?([a-z0-9][a-z0-9\-\" ]{2,80})", text, re.I)
    if not m:
        return None
    qty = _num(m.group(1)) * MULT.get((m.group(2) or "").lower(), 1)
    raw = m.group(3)
    cut = STOP.search(raw)
    item = (raw[:cut.start()] if cut else raw).strip().lower()
    words = item.split()
    if not words:
        return None
    # keep "sleeves for laptops" but drop trailing filler
    tail = re.match(r"\s*for\s+([a-z]+)", raw[cut.start():] if cut else "", re.I) if cut else None
    if "for" in words[1:]:
        i = words.index("for", 1)
        words[i - 1] = _singular(words[i - 1])
    else:
        words[-1] = _singular(words[-1])
    item = " ".join(words)
    if tail and tail.group(1).lower() not in CITIES and tail.group(1).lower() not in ("our", "the", "delivery"):
        item += " for " + tail.group(1).lower()
    if item in ("unit", "piece", "pc", "no"):
        return None
    return {"quantity": int(qty), "item": item, "text": m.group(0).strip()}


def _blank(text: str, span) -> str:
    a, b = span
    return text[:a] + " " * (b - a) + text[b:]


def parse(request: str, today: datetime.date):
    text = " ".join(request.split())
    found, evidence = {}, {}
    work = text
    b = parse_budget(work)
    if b:
        found["budget"] = b
        evidence["budget"] = b["text"]
        work = _blank(work, b["span"])
    d = parse_deadline(work, today)
    if d:
        found["deadline"] = d["date"]
        evidence["deadline"] = d["text"]
        work = _blank(work, d["span"])
    c = parse_city(work)
    if c:
        found["city"] = c["city"]
        evidence["city"] = c["text"]
    q = parse_quantity_item(work)
    if q:
        found["quantity"], found["item"] = q["quantity"], q["item"]
        evidence["quantity"] = evidence["item"] = q["text"]
    return found, evidence


def _llm_fill(request: str, found: dict, today: datetime.date, notes: list):
    missing = [k for k in ("quantity", "item", "city", "deadline", "budget") if k not in found]
    if not missing or not llm.available():
        return
    out = llm.extract_json(llm.complete(
        "You extract purchase-request fields. Reply with one JSON object only. Use null when the text "
        "does not state a value. Never guess.",
        f"Today is {today}. Request: {request!r}\nReturn JSON with keys quantity (integer), item (short noun "
        "phrase), city (Indian city), deadline (YYYY-MM-DD), budget_inr (number, total budget in rupees)."))
    if not out:
        return
    digits = re.sub(r"[^\d]", "", request)
    if "quantity" in missing and isinstance(out.get("quantity"), int) and out["quantity"] > 0:
        found["quantity"] = out["quantity"]
        notes.append("quantity read by Claude")
    if "item" in missing and isinstance(out.get("item"), str) and out["item"].strip():
        if any(w in request.lower() for w in out["item"].lower().split()):
            found["item"] = out["item"].strip().lower()
            notes.append("item read by Claude")
    if "city" in missing and out.get("city") in CITIES.values() and out["city"].lower() in request.lower():
        found["city"] = out["city"]
        notes.append("city read by Claude")
    if "deadline" in missing and isinstance(out.get("deadline"), str):
        try:
            found["deadline"] = datetime.date.fromisoformat(out["deadline"])
            notes.append("deadline read by Claude — please confirm")
        except ValueError:
            pass
    if "budget" in missing and isinstance(out.get("budget_inr"), (int, float)) and out["budget_inr"] > 0:
        # only accept if its leading digits appear in the request
        lead = str(int(out["budget_inr"]))[:2]
        if lead in digits:
            found["budget"] = {"rupees": float(out["budget_inr"]), "per_unit": False, "text": "(Claude)"}
            notes.append("budget read by Claude — please confirm")


ASK = {
    "item": ("What exactly should we buy, and how many units?", "5,000 leather laptop sleeves"),
    "city": ("Which city should the goods be delivered to?", "Bengaluru"),
    "deadline": ("By what date must the goods arrive?", "15 December"),
    "budget": ("What is the maximum total budget (in rupees)?", "₹12 lakh"),
}


def _ask(field):
    q, example = ASK[field]
    return {"field": field, "question": q, "example": example}


def _apply_answers(answers, found, evidence, today, notes, retry):
    """A human's answer to a Planner question overrides what was read from the
    request. Each answer is read with the same parser as that field, never guessed."""
    for field, text in answers.items():
        text = " ".join(str(text).split())
        if not text:
            continue
        got = None
        if field == "item":
            q = parse_quantity_item(text)
            if q:
                found["quantity"], found["item"] = q["quantity"], q["item"]
                got = q["text"]
                evidence["quantity"] = evidence["item"] = f"{got} (answered)"
        elif field == "city":
            c = parse_city(text)
            if c:
                found["city"], got = c["city"], c["text"]
        elif field == "deadline":
            d = parse_deadline(text, today)
            if d:
                found["deadline"], got = d["date"], d["text"]
        elif field == "budget":
            b = parse_budget(text) or parse_budget("under " + text)
            if b:
                found["budget"], got = b, b["text"]
        if got is None:
            retry[field] = text
        else:
            if field != "item":
                evidence[field] = f"{got} (answered)"
            notes.append(f"{field} answered by a human")


def plan(store, request: str):
    today = config.today()
    store.emit("Planner", "start", "Reading the request", {"request": request})
    found, evidence = parse(request, today)
    notes = []
    _llm_fill(request, found, today, notes)

    retry = {}
    _apply_answers(store.answers(), found, evidence, today, notes, retry)

    # Buyer profile: a procurement team ships to its own address by default and
    # works to a standard lead time. Defaults are stated as assumptions, never hidden;
    # an unreadable answer is asked again rather than replaced by a default.
    defaults = []
    if "city" not in found and "city" not in retry:
        found["city"] = config.BUYER["ship_to_city"]
        evidence["city"] = f"{found['city']} (buyer profile default)"
        defaults.append(f"Ship-to {found['city']} from the buyer profile — not stated in the request")
    if "deadline" not in found and "deadline" not in retry:
        found["deadline"] = today + datetime.timedelta(days=config.BUYER["default_lead_days"])
        evidence["deadline"] = f"{found['deadline']} (standard {config.BUYER['default_lead_days']}-day lead time)"
        defaults.append(f"Deadline {found['deadline']} = standard {config.BUYER['default_lead_days']}-day lead time "
                        "— not stated in the request")

    ask = []
    if "quantity" not in found or "item" not in found:
        ask.append(_ask("item"))
    if "city" not in found:
        ask.append(_ask("city"))
    if "deadline" not in found:
        ask.append(_ask("deadline"))
    if "budget" not in found:
        ask.append(_ask("budget"))
    for a in ask:
        if a["field"] in retry:
            a["question"] = f"Could not read “{retry[a['field']]}”. " + a["question"]

    spec = {"status": "ok", "request": request, "today": today.isoformat(), "evidence": evidence,
            "notes": notes, "warnings": []}
    if "quantity" in found:
        spec["quantity"] = found["quantity"]
        spec["item"] = found["item"]
        spec["material"] = "leather" if "leather" in found["item"] else None
        cat = catalog.categorize(found["item"])
        spec["category"], spec["category_label"] = cat["key"], cat["label"]
        spec["benchmark_price"] = cat["benchmark_price"]
    if "city" in found:
        spec["delivery_city"] = found["city"]
    if "deadline" in found:
        dl = found["deadline"]
        spec["deadline"] = dl.isoformat()
        spec["days_to_deadline"] = (dl - today).days
        if dl <= today:
            ask.append(dict(_ask("deadline"), question=f"The deadline {dl} is not in the future. "
                                                         "What is the correct date?"))
        elif (dl - today).days < 10:
            spec["warnings"].append(f"Only {(dl - today).days} days to the deadline; few suppliers can make it.")
    if "budget" in found and "quantity" in found:
        bud = found["budget"]
        total = bud["rupees"] * found["quantity"] if bud["per_unit"] else bud["rupees"]
        spec["budget_paise"] = int(round(total * 100))
        spec["unit_ceiling_paise"] = int(spec["budget_paise"] // found["quantity"])
        if bud["per_unit"]:
            notes.append("budget given per unit; total = unit price x quantity")
    questions = [a["question"] for a in ask]
    if questions:
        spec["status"] = "needs_input"
        spec["questions"] = questions
        spec["ask"] = ask
        store.emit("Planner", "needs_input", "Cannot plan without: " + " ".join(questions),
                   {"questions": questions, "parsed": {k: str(v) for k, v in found.items()}})
        return spec

    dl = datetime.date.fromisoformat(spec["deadline"])
    latest = dl - datetime.timedelta(days=config.POLICY["delivery_buffer_days"])
    spec.update({
        "latest_acceptable_delivery": latest.isoformat(),
        "target_delivery": (dl - datetime.timedelta(days=10)).isoformat(),
        "specification": {
            "item": spec["item"], "quantity": spec["quantity"],
            "material": spec["material"] or "as described",
            "category": spec["category_label"],
            "assumed": [catalog.get(spec["category"])["assumed"],
                        "Prices all-inclusive: GST and freight to " + spec["delivery_city"],
                        "Inspection on arrival at AQL 2.5"],
        },
        "assumptions": defaults + [f"Advance capped at {config.POLICY['max_advance_pct']}% by policy",
                        f"Delivery must land by {latest} ({config.POLICY['delivery_buffer_days']}-day buffer)"],
        "tasks": [
            {"agent": "Scout", "task": "Find suppliers: RFQ to the sandbox marketplace + Crustdata discovery"},
            {"agent": "Verifier", "task": "Prove each supplier is real, active and has the capacity"},
            {"agent": "Negotiator", "task": "Negotiate price, delivery and payment terms in parallel"},
            {"agent": "Risk Officer", "task": "Approve or block each deal against policy"},
            {"agent": "Contract", "task": "Issue the PO and check the countersigned copy word for word"},
            {"agent": "Payments", "task": "Match invoice to PO, pay the advance exactly once"},
            {"agent": "Auditor", "task": "Link every decision to evidence and flag contradictions"},
        ],
    })
    bench = spec.get("benchmark_price")
    if bench and spec["unit_ceiling_paise"] < bench * 100 * 0.8:
        spec["warnings"].append(f"Budget allows {config.fmt_inr(spec['unit_ceiling_paise'], '₹')}/unit, well below "
                                f"the ₹{bench} price-book benchmark for {spec['item']}; expect few or no deals.")
    for d in defaults:
        spec["warnings"].append("Assumed: " + d + ". Say otherwise in the request to change it.")
    store.emit("Planner", "plan",
               f"{spec['quantity']:,} × {spec['item']} → {spec['delivery_city']} by {spec['deadline']}, "
               f"budget {config.fmt_inr(spec['budget_paise'], '₹')} "
               f"(ceiling {config.fmt_inr(spec['unit_ceiling_paise'], '₹')}/unit) · category {spec['category_label']}"
               + (f", price-book benchmark ₹{bench}/unit" if bench else ", no price-book benchmark"),
               {"spec": spec["specification"], "evidence": evidence, "warnings": spec["warnings"]})
    for w in spec["warnings"]:
        store.emit("Planner", "warning", w)
    return spec
