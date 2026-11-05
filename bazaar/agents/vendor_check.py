"""Vendor check: "here is a new vendor's name and website — is it a real, trading
business, who runs it, how long has it existed, and does anything look off?"

This is the step a procurement team runs before onboarding any supplier. For a
real domain every fact comes from Crustdata (Identify, Enrich with people and
news, Web Search) and is stored with its source. For a fictional `.example`
domain the sandbox registry plays Crustdata's part. Nothing is guessed: a fact
that cannot be confirmed is reported as unconfirmed, and an identity that cannot
be pinned down makes the whole verdict UNVERIFIABLE rather than a pass or a fail.
"""
import datetime
import difflib
import json
import re

from .. import config
from ..crustdata import Crustdata, CrustdataError, dig
from ..sandbox import SandboxRegistry
from .. import catalog

DOMAIN_RE = re.compile(r"(?:https?://)?(?:www\.)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?:/[^\s,]*)?",
                       re.I)
VENDOR_WORDS = re.compile(r"\b(vendor|supplier|trading business|real business|is it real|legit|genuine|who runs|"
                          r"check (?:this|the|a) (?:company|vendor|supplier)|verify|onboard|kyb|due diligence|"
                          r"shell)\b", re.I)
NEGATIVE = ["fraud", "scam", "raid", "arrest", "cheat", "fake invoice", "default", "insolven", "nclt",
            "shell compan", "money laundering", "gst evasion", "tax evasion", "blacklist", "penalty", "sebi order",
            "wilful defaulter", "liquidation", "ponzi", "seized"]
_STOP = re.compile(r"\b(pvt|private|ltd|limited|llp|co|company|inc|the|and|india)\b")


def _norm(name):
    return " ".join(_STOP.sub(" ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())).split())


def _similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def detect(request: str):
    """Return {"name", "domain"} if the request asks to check a vendor, else None."""
    text = " ".join(request.split())
    m = None
    for cand in DOMAIN_RE.finditer(text):
        dom = cand.group(1).lower()
        if re.fullmatch(r"\d+(\.\d+)+", dom) or dom.endswith((".jpg", ".png", ".pdf")):
            continue
        m = cand
        break
    if not m:
        return None
    has_qty = re.search(r"\b\d[\d,]*\s*(?:k|units?|pcs|pieces|nos)?\s+[a-z]", text[:m.start()] + text[m.end():], re.I)
    if not VENDOR_WORDS.search(text) and has_qty:
        return None
    domain = m.group(1).lower()
    before = text[:m.start()]
    trail = re.compile(r"[\s,:;(\-–—]*(?:website|web site|site|url|domain|at|and|is|on)?[\s,:;(\-–—]*$", re.I)
    for _ in range(3):
        before = trail.sub("", before)
    name = None
    nm = re.search(r"(?:vendor|supplier|company)(?:['’]s)?\s*(?:name)?(?:\s+(?:and|&)\s+website)?\s*(?:is|:|-)\s*"
                   r"(.+)$", before, re.I)
    cand = nm.group(1) if nm else re.split(r"[:.;?!]\s+", before)[-1]
    lead = re.compile(r"^(?:here\s+is|please|pls|kindly|can you|could you|check|verify|is|this|the|a|new|"
                      r"vendor|supplier|company|name|called|named)\b[\s:,'’s]*", re.I)
    for _ in range(6):
        cand = lead.sub("", cand.strip(" \"'“”,"))
    cand = cand.strip(" \"'“”,")
    if cand and 1 <= len(cand.split()) <= 7 and re.search(r"[A-Z0-9]", cand) and not VENDOR_WORDS.search(cand):
        name = cand
    return {"name": name, "domain": domain}


class Report:
    def __init__(self, store, subject):
        self.store, self.subject, self.checks, self.facts = store, subject, [], {}

    def fact(self, key, value, source, url=None):
        fid = self.store.fact(self.subject, key, value, source, url)
        self.facts[key] = fid
        return fid

    def check(self, key, ok, detail, source, severity="caution", value=None):
        """ok: True, False, or None (could not be checked). severity of a failure: 'critical' or 'caution'."""
        fid = self.fact(key, value if value is not None else detail, source)
        self.checks.append({"check": key, "ok": ok, "detail": detail, "source": source, "fact_id": fid,
                            "severity": severity})
        icon = {True: "✅", False: "⛔" if severity == "critical" else "⚠️", None: "❔"}[ok]
        if ok is False and severity == "review":
            icon = "🔎"
        self.store.emit("Verifier", "vendor_fact", f"{icon} {detail} — {source}",
                        {"check": key, "ok": ok, "severity": severity, "fact_id": fid})


def _years(year):
    return config.today().year - int(year) if year else None


def _people(rec):
    ppl = rec.get("people") or {}
    seen, out = set(), []
    for group in ("founders", "cxos", "decision_makers"):
        for p in (ppl.get(group) or []) if isinstance(ppl, dict) else []:
            bp = p.get("basic_profile") or p
            name = bp.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "title": bp.get("current_title") or bp.get("headline") or group[:-1],
                        "role": group})
    return out[:6]


def _six_months_ago(rec):
    series = dig(rec, "headcount.timeseries") or []
    cutoff = (config.today() - datetime.timedelta(days=182)).isoformat()
    past = [p for p in series if isinstance(p, dict) and p.get("date", "") <= cutoff and p.get("employee_count")]
    return (past[-1]["employee_count"], past[-1]["date"]) if past else (None, None)


def _name_lookup(cd, rep, name, domain):
    """Free Identify by the given name: what does the name resolve to on its own?"""
    if not name:
        return
    try:
        hits = cd.identify(name=name)
    except CrustdataError:
        return
    if not hits:
        rep.check("name_lookup", None, f"No Crustdata record under the name “{name}”",
                  "Crustdata /company/identify", "caution")
        return
    top = [(d.get("basic_info") or {}) for _, d in hits[:3]]
    listed = "; ".join(f"{t.get('name')} ({t.get('primary_domain') or 'no website'})" for t in top if t.get("name"))
    owns = any(domain in [str(x).lower() for x in (t.get("all_domains") or [])] or t.get("primary_domain") == domain
               for t in top)
    rep.check("name_lookup", True if owns else None,
              f"The name “{name}” resolves to: {listed}" + ("" if owns else f" — none of them lists {domain}"),
              "Crustdata /company/identify", "caution", top)


def _check_real(store, rep, name, domain):
    cd = Crustdata(on_event=lambda kind, msg: store.emit("Verifier", kind, msg))
    src_id, src_en, src_web = "Crustdata /company/identify", "Crustdata /company/enrich", "Crustdata /web/search/live"
    try:
        rec = cd.enrich_domain(domain, fields=("basic_info", "headcount", "locations", "people", "news"))
    except CrustdataError as e:
        rep.check("identity", None, f"Crustdata unavailable for {domain}: {e}", src_en, "critical")
        return {}
    if not rec:
        rep.check("identity", None, f"No company record for {domain} on Crustdata", src_en, "critical")
        alt = []
        if name:
            try:
                alt = cd.identify(name=name)
            except CrustdataError:
                alt = []
        if alt:
            top = [d.get("basic_info", {}).get("name") for _, d in alt[:3]]
            rep.check("name_lookup", None, f"Records found under the name “{name}”: {', '.join(t for t in top if t)} "
                      f"— none lists {domain}", src_id, "caution", top)
        return {}
    registered = dig(rec, "basic_info.name")
    if not rec.get("_domain_match"):
        rep.check("identity", None, f"Crustdata's best match for {domain} is “{registered}”, which does not list "
                  f"{domain} as its own domain — identity not confirmed", src_en, "critical",
                  {"matched": registered})
        _name_lookup(cd, rep, name, domain)
        return {"registered_name": registered}
    rep.check("identity", True, f"{domain} belongs to “{registered}” (Crustdata id {rec.get('crustdata_company_id')}, "
              f"confidence {rec.get('_confidence')})", src_en, "critical",
              {"name": registered, "crustdata_company_id": rec.get("crustdata_company_id")})
    if name:
        # A different name is common (renames, trade names, group companies), so it is a
        # caution to resolve, not a rejection: an honest business must not be turned away.
        sim = _similar(name, registered)
        rep.check("name_match", sim >= 0.6, f"Name given “{name}” vs registered “{registered}” ({sim:.0%} similar)"
                  + ("" if sim >= 0.6 else " — confirm which legal entity will sign and invoice"),
                  src_en, "caution", {"given": name, "registered": registered})
        if sim < 0.6:
            _name_lookup(cd, rep, name, domain)
    status = dig(rec, "basic_info.status")
    rep.check("status", status == "active" if status else None,
              f"Status: {status or 'not on record'}", src_en, "critical", status)
    yf = dig(rec, "basic_info.year_founded")
    age = _years(yf)
    rep.check("age", None if age is None else age >= config.POLICY["min_company_age_years"],
              f"Founded {yf} ({age} years ago)" if yf else "Founding year not on record", src_en, "caution", yf)
    hc = dig(rec, "headcount.total")
    hc6, d6 = _six_months_ago(rec)
    trend = f", {hc6} on {d6}" if hc6 else ""
    if hc is None:
        rep.check("headcount", None, "Headcount not on record", src_en, "caution")
    else:
        falling = bool(hc6 and hc < 0.5 * hc6)
        tiny = hc < 5
        rep.check("headcount", hc > 0 and not falling and not tiny,
                  f"Headcount {hc}{trend}" + (" — more than halved in six months" if falling else
                                              " — very small team on record; may undercount staff" if tiny else ""),
                  src_en, "critical" if falling else "caution", {"now": hc, "six_months_ago": hc6})
    people = _people(rec)
    rep.check("leadership", bool(people) or None,
              "Run by: " + "; ".join(f"{p['name']} ({p['title']})" for p in people) if people
              else "No founders or executives on record", src_en, "caution", people)
    hq = dig(rec, "locations.headquarters")
    if hq:
        rep.fact("headquarters", hq, src_en)
    news = rec.get("news") or []
    query = f'"{registered}"'
    try:
        results = cd.web_search(query, sources=("web", "news"))
    except CrustdataError as e:
        results = None
        rep.check("web", None, f"Web search failed: {e}", src_web, "caution")
    if results is not None:
        tokens = [t for t in _norm(registered).split() if len(t) > 2] or [_norm(registered)]
        relevant = [r for r in results if isinstance(r, dict) and (
            domain in (r.get("url") or "") or all(t in f"{r.get('title', '')} {r.get('snippet', '')}".lower()
                                                   for t in tokens))]
        items = relevant + [{"title": n.get("article_title"), "url": n.get("article_url"), "snippet": ""}
                            for n in news if isinstance(n, dict)]
        bad = [r for r in items if any(w in f"{r.get('title', '')} {r.get('snippet', '')}".lower() for w in NEGATIVE)]
        brand = (tokens or [""])[0]
        victim = [r for r in bad if re.search(rf"\b(fake|counterfeit|duplicate|spurious|imitation)\s+{re.escape(brand)}|"
                                                r"impersonat|in the name of", f"{r.get('title', '')} "
                                                f"{r.get('snippet', '')}".lower())]
        other = [r for r in bad if r not in victim]
        rep.check("web_footprint", bool(relevant) or None,
                  f"{len(relevant)} web/news results about {registered}" if relevant
                  else f"No web or news results clearly about {registered}", src_web, "caution",
                  [{"title": r.get("title"), "url": r.get("url")} for r in relevant[:5]])
        if victim:
            rep.fact("counterfeit_reports", [{"title": r.get("title"), "url": r.get("url")} for r in victim[:5]], src_web)
            store.emit("Verifier", "vendor_fact", f"ℹ️ {len(victim)} report(s) of counterfeits or impersonation name "
                       f"{registered} as the victim, not the wrongdoer: “{victim[0].get('title')}” — {src_web}")
        # Adverse news is for a person to read, not a reason to reject on keywords alone.
        rep.check("adverse_news", not other,
                  "No adverse news about the company itself (fraud, raids, insolvency, defaults, penalties)"
                  if not other else "Adverse mentions to review: " + "; ".join(
                      f"“{r.get('title')}” {r.get('url') or ''}" for r in other[:3]),
                  src_web, "review", [{"title": r.get("title"), "url": r.get("url")} for r in other[:5]])
    return {"registered_name": registered, "year_founded": yf, "age_years": age, "headcount": hc,
            "headcount_6mo_ago": hc6, "headquarters": hq, "status": status, "people": people,
            "crustdata_company_id": rec.get("crustdata_company_id")}


def _check_sandbox(store, rep, name, domain):
    src = "sandbox registry (simulated Crustdata record for a fictional company)"
    rec, people = None, []
    for cat in [*catalog.CATEGORIES, "general"]:
        reg = SandboxRegistry(cat)
        rec = reg.record(domain)
        if rec:
            age_days = rec.get("domain_age_days")
            break
        age_days = reg.domain_age_days(domain)
        if age_days is not None:
            break
    if not rec:
        rep.check("identity", False, f"No company registry record for {domain}", src, "critical")
        if age_days is not None:
            rep.check("domain_age", age_days > 365, f"Website domain registered {age_days} days ago",
                      "sandbox WHOIS", "critical", age_days)
        rep.check("web_footprint", False, "0 independent web or news mentions", "sandbox web search", "caution", 0)
        return {}
    registered = rec["legal_name"]
    rep.check("identity", True, f"{domain} belongs to “{registered}” · {rec['registration_id']}", src, "critical",
              {"name": registered, "registration_id": rec["registration_id"]})
    if name:
        known = [registered, *rec.get("trade_names", [])]
        sim = max(_similar(name, k) for k in known)
        rep.check("name_match", sim >= 0.6, f"Name given “{name}” vs registered names {known} ({sim:.0%} similar)",
                  src, "critical")
    age = _years(rec["year_founded"])
    rep.check("age", age >= config.POLICY["min_company_age_years"], f"Founded {rec['year_founded']} ({age} years ago)",
              src, "caution", rec["year_founded"])
    hc, hc6 = rec["headcount"], rec.get("headcount_6mo_ago")
    rep.check("headcount", hc > 0 and not (hc6 and hc < 0.5 * hc6), f"Headcount {hc}, {hc6} six months ago",
              src, "caution", {"now": hc, "six_months_ago": hc6})
    people = rec.get("people") or []
    rep.check("leadership", bool(people) or None,
              "Run by: " + "; ".join(f"{p['name']} ({p['title']})" for p in people) if people
              else "No founders or executives on record", src, "caution", people)
    mentions = rec.get("web_mentions") or []
    rep.check("web_footprint", bool(mentions), f"{len(mentions)} web mentions" +
              (f", latest “{mentions[0]['title']}”" if mentions else ""), "sandbox web search", "caution", mentions)
    rep.check("adverse_news", True, "No adverse news found", "sandbox web search", "critical")
    return {"registered_name": registered, "year_founded": rec["year_founded"], "age_years": age, "headcount": hc,
            "headcount_6mo_ago": hc6, "headquarters": rec["hq_city"], "people": people, "status": "active"}


def check_vendor(store, request):
    target = detect(request)
    name, domain = target["name"], target["domain"]
    store.emit("Planner", "plan", f"Vendor check: “{name or '(name not given)'}” at {domain}. Question: is it a real, "
               "trading business, who runs it, how long has it existed, and what looks off?",
               {"vendor": target})
    rep = Report(store, f"vendor:{domain}")
    fictional = domain.endswith(".example")
    if fictional:
        profile = _check_sandbox(store, rep, name, domain)
    elif Crustdata().live:
        profile = _check_real(store, rep, name, domain)
    else:
        rep.check("identity", None, "CRUSTDATA_API_KEY not set — cannot look up a real company", "Bazaar", "critical")
        profile = {}

    crit_fail = [c for c in rep.checks if c["ok"] is False and c["severity"] == "critical"]
    unknown_identity = any(c["check"] == "identity" and c["ok"] is None for c in rep.checks)
    cautions = [c for c in rep.checks if c["ok"] is False and c["severity"] in ("caution", "review")]
    unknowns = [c for c in rep.checks if c["ok"] is None and c["check"] != "identity"]
    if crit_fail:
        verdict, headline = "HIGH_RISK", "Do not onboard"
    elif unknown_identity:
        verdict, headline = "UNVERIFIABLE", "Identity not confirmed — ask the vendor for registration documents"
    elif cautions:
        verdict, headline = "CAUTION", "Real, but check the flagged points before onboarding"
    else:
        verdict, headline = "REAL_AND_TRADING", "Real, trading business"
    reasons = [c["detail"] for c in (crit_fail or [c for c in rep.checks if c["check"] == "identity" and c["ok"] is None]
                                     or cautions)]
    if unknowns and verdict in ("REAL_AND_TRADING", "CAUTION"):
        reasons += ["Not confirmed: " + c["detail"] for c in unknowns]
    needs_review = any(c["ok"] is False and c["severity"] == "review" for c in rep.checks)
    if needs_review and verdict == "CAUTION":
        headline = "Real, but a person should read the adverse mentions before onboarding"
    out = {"vendor": target, "fictional": fictional, "verdict": verdict, "headline": headline,
           "needs_review": needs_review,
           "reasons": reasons, "profile": profile, "checks": rep.checks}
    icon = {"HIGH_RISK": "⛔", "UNVERIFIABLE": "❔", "CAUTION": "⚠️", "REAL_AND_TRADING": "✅"}[verdict]
    store.emit("Verifier", "vendor_verdict", f"{icon} {name or domain}: {verdict.replace('_', ' ')} — {headline}"
               + (". " + "; ".join(reasons[:3]) if reasons else ""), {"verdict": verdict})
    store.put_document(f"vendor-check-{domain}.json", "vendor_report", json.dumps(out, default=str))
    return out
