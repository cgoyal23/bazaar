"""Riptide engine: detect people leaving big companies for startups, the moment
Crustdata sees the move, and turn each move into a scored, trust-checked signal.

Sources (all Crustdata; nothing is scraped from LinkedIn):
  * Person Search    — a live scan for recent big-company -> startup moves
  * Discovery Watch  — the same filter saved as a Crustdata watcher; each run's
                       newly matching people become signals (pull-only, or by webhook)
  * Person Enrich    — on demand, Crustdata's own authenticity verdict for a profile
"""
import datetime
import hashlib
import json
import re

from bazaar import config
from bazaar.crustdata import Crustdata, CrustdataError
from bazaar.db import now
from . import store

BIG_COMPANIES = ["Google", "Microsoft", "Amazon", "Amazon Web Services (AWS)", "Meta", "Apple", "Netflix",
                 "Flipkart", "Uber", "Goldman Sachs", "JPMorgan Chase & Co.", "Adobe", "Salesforce", "Oracle",
                 "Walmart Global Tech India", "PhonePe", "Swiggy", "Zomato", "Paytm", "Razorpay", "Atlassian",
                 "McKinsey & Company", "Boston Consulting Group (BCG)", "Bain & Company", "Tata Consultancy Services",
                 "Infosys", "Nvidia", "Intel Corporation", "Qualcomm", "Samsung Electronics"]
BIG_HEADCOUNT = 5000
SMALL_HEADCOUNT = 50
FIELDS = ["basic_profile", "experience", "professional_network", "social_handles"]

SENIOR = re.compile(r"\b(chief|cxo|ceo|cto|coo|cfo|cpo|vp|vice president|head|director|general manager|gm|partner)\b", re.I)
MID = re.compile(r"\b(senior|sr\.?|lead|principal|staff|manager|architect|specialist ii|sde ?(ii|iii|3|2))\b", re.I)
JUNIOR = re.compile(r"\b(intern|internship|trainee|student|apprentice|fellow|fellowship|mentee|scholar|"
                    r"research assistant|ambassador|volunteer)\b", re.I)
FOUNDER = re.compile(r"\b(founder|co-?founder|founding)\b", re.I)


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _years(start, end):
    a, b = _date(start), _date(end) or config.today()
    return round((b - a).days / 365.25, 1) if a and b and b >= a else 0.0


NOT_A_COMPANY = re.compile(r"education|universit|college|school|hospital|health care|government|"
                           r"public administration|armed forces|military|non-?profit", re.I)
SOLO = re.compile(r"\b(self[- ]employed|freelanc\w*|independent|consultant|career break|sabbatical|"
                  r"looking for|open to work)\b", re.I)


def _is_big(role):
    name = (role.get("name") or "").lower()
    if any(name == b.lower() or name.startswith(b.lower() + " ") for b in BIG_COMPANIES):
        return True
    inds = " ".join(role.get("company_industries") or []) + " " + (role.get("company_type") or "")
    return (role.get("company_headcount_latest") or 0) >= BIG_HEADCOUNT and not NOT_A_COMPANY.search(inds)


def filters(since_days=60, country="India", big=None):
    since = (config.today() - datetime.timedelta(days=since_days)).isoformat()
    conds = [
        {"field": "experience.employment_details.past.company_name", "type": "in", "value": big or BIG_COMPANIES},
        {"field": "experience.employment_details.current.start_date", "type": "=>", "value": since},
        {"field": "experience.employment_details.current.company_headcount_latest", "type": "=<",
         "value": SMALL_HEADCOUNT},
    ]
    if country:
        conds.append({"field": "professional_network.location.country", "type": "=", "value": country})
    return {"op": "and", "conditions": conds}


# ------------------------------------------------------------------ one profile -> one move

def parse_move(p, since_days=120):
    """The move that matters: the newest small-company role that started recently, and
    the big-company stint the person left. None when the profile is not such a move."""
    ex = (p.get("experience") or {}).get("employment_details") or {}
    current = [r for r in (ex.get("current") or []) if isinstance(r, dict)]
    past = [r for r in (ex.get("past") or []) if isinstance(r, dict)]
    cutoff = config.today() - datetime.timedelta(days=since_days)
    fresh = [r for r in current if (_date(r.get("start_date")) or datetime.date.min) >= cutoff
             and (r.get("company_headcount_latest") or 0) <= SMALL_HEADCOUNT and not _is_big(r)
             and not SOLO.search(f"{r.get('name') or ''} {r.get('title') or ''}")
             and not NOT_A_COMPANY.search(" ".join(r.get("company_industries") or []) + " " + (r.get("name") or ""))]
    if not fresh:
        return None
    new = max(fresh, key=lambda r: (_date(r.get("start_date")), bool(r.get("is_default"))))
    bigs = [r for r in past if _is_big(r) and not JUNIOR.search(r.get("title") or "")]
    if not bigs:
        return None  # an internship at Google does not make a big-company leaver
    left = max(bigs, key=lambda r: _date(r.get("end_date")) or datetime.date.min)
    previous = max(past, key=lambda r: _date(r.get("end_date")) or datetime.date.min) if past else None
    bp = p.get("basic_profile") or {}
    pn = p.get("professional_network") or {}
    url = (((p.get("social_handles") or {}).get("professional_network_identifier") or {}).get("profile_url"))
    return {
        "person_id": p.get("crustdata_person_id"), "name": bp.get("name"), "headline": bp.get("headline"),
        "location": (bp.get("location") or {}).get("full_location") or (bp.get("location") or {}).get("raw"),
        "profile_url": url, "picture": bool(bp.get("profile_picture_permalink")),
        "connections": pn.get("connections"), "followers": pn.get("followers"),
        "to": {"company": new.get("name"), "title": new.get("title"), "start": str(new.get("start_date"))[:10],
               "headcount": new.get("company_headcount_latest") or 0, "company_id": new.get("crustdata_company_id"),
               "website": new.get("company_website") or "", "industries": new.get("company_industries") or [],
               "seniority": new.get("seniority_level") or ""},
        "from": {"company": left.get("name"), "title": left.get("title"), "start": str(left.get("start_date"))[:10],
                 "end": str(left.get("end_date"))[:10] if left.get("end_date") else None,
                 "years": _years(left.get("start_date"), left.get("end_date")),
                 "headcount": left.get("company_headcount_latest") or 0, "company_id": left.get("crustdata_company_id"),
                 "email_verified": bool(left.get("business_email_verified"))},
        "direct": previous is left,  # big company was the last job, not an earlier one
        "concurrent_roles": len([r for r in current if (r.get("employment_type") or "Full-time") == "Full-time"]),
        "any_email_verified": any(r.get("business_email_verified") for r in current + past),
    }


def classify(m):
    t, co = m["to"]["title"] or "", m["to"]["company"] or ""
    if "stealth" in co.lower() or "stealth" in t.lower():
        return "stealth_founder"
    own = m["to"]["headcount"] <= 10 and (re.search(r"\b(ceo|cto|managing director|owner|proprietor)\b", t, re.I)
                                         or "owner" in (m["to"]["seniority"] or "").lower())
    if FOUNDER.search(t) or own:
        return "founder_leap"
    if SENIOR.search(t):
        return "senior_startup_hire"
    return "startup_hire"


KIND_LABEL = {"stealth_founder": "Stealth founder", "founder_leap": "Founder leap",
              "senior_startup_hire": "Senior startup hire", "startup_hire": "Startup hire"}

PLAYBOOK = {  # who buys this signal, and the next step a Riptide customer takes (drafts only; nobody is contacted)
    "stealth_founder": [("VC", "Pre-seed lead before any announcement: book an intro this week")],
    "founder_leap": [("VC", "Founder left {from_co} to build {to_co}: add to deal flow, intro before the round"),
                     ("Sales", "New company buying its first tools: offer a founder plan")],
    "senior_startup_hire": [("Sales", "New {title} at {to_co} owns a fresh budget for ~90 days: reach out now"),
                            ("VC", "{to_co} is pulling senior talent from {from_co}: a traction signal")],
    "startup_hire": [("Recruiting", "{from_co} alumni are moving to startups: a hiring window for similar roles")],
}


def score(m, kind):
    """0-100, every point explained."""
    parts = []
    f, t = m["from"], m["to"]
    prestige = 30 if any((f["company"] or "").lower().startswith(b.lower()) for b in BIG_COMPANIES) else (
        25 if f["headcount"] >= 10000 else 18)
    parts.append(("Left a big company", prestige, f"{f['company']} ({f['headcount']:,} staff)" if f["headcount"]
                  else f["company"]))
    sen = 25 if SENIOR.search(f["title"] or "") else 15 if MID.search(f["title"] or "") else 8
    parts.append(("Seniority there", sen, f["title"]))
    tenure = min(10, int(f["years"] * 2))
    parts.append(("Tenure there", tenure, f"{f['years']} years"))
    dest = {"stealth_founder": 20, "founder_leap": 20}.get(kind, 15 if t["headcount"] <= 10 else 10)
    parts.append(("Where they went", dest, f"{t['title']} at {t['company']} ({t['headcount'] or 'new, no'} staff)"))
    days = (config.today() - (_date(t["start"]) or config.today())).days
    rec = 15 if days <= 14 else 10 if days <= 31 else 5 if days <= 62 else 0
    parts.append(("Freshness", rec, f"started {days} days ago"))
    if m["direct"]:
        parts.append(("Direct move", 5, "the big company was their last job"))
    total = min(100, sum(p[1] for p in parts))
    return total, [{"factor": a, "points": b, "why": c} for a, b, c in parts]


# ------------------------------------------------------------------ trust: is the profile behind the signal real?

def growth(person_id):
    rows = store.conn().execute("SELECT ts, connections, followers FROM snapshots WHERE person_id=? ORDER BY ts",
                                (person_id,)).fetchall()
    return [dict(r) for r in rows]


def trust(m, history=None):
    """Checks a fake or inflated profile tends to fail, from data Crustdata already
    returned (free). Each check says what it saw. A verdict is a flag for a human,
    never an accusation."""
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": ok, "detail": detail})

    conn, fol = m.get("connections") or 0, m.get("followers") or 0
    add("verified_email", True if m["any_email_verified"] else None,
        "A business email on one of their roles is verified" if m["any_email_verified"]
        else "No verified business email on record (common, not a red flag on its own)")
    add("photo", m["picture"], "Profile photo present" if m["picture"] else "No profile photo")
    add("employer_linked", bool(m["from"]["company_id"]),
        f"{m['from']['company']} role is linked to the real company page" if m["from"]["company_id"]
        else f"{m['from']['company']} role is not linked to any company page — a common sign of an invented employer")
    thin = conn < 100 and SENIOR.search(m["from"]["title"] or "")
    add("network_fits_claim", not thin, f"{conn:,} connections for a claimed “{m['from']['title']}”"
        + (" — thin network for that seniority" if thin else ""))
    lopsided = conn and fol > 15 * conn and conn < 500
    add("audience_shape", not lopsided, f"{fol:,} followers vs {conn:,} connections"
        + (" — followers far outrun connections (bought-audience pattern)" if lopsided else ""))
    many = m["concurrent_roles"] > 3
    add("concurrent_roles", not many, f"{m['concurrent_roles']} full-time current role(s)"
        + (" — more than three at once" if many else ""))
    hist = history if history is not None else growth(m["person_id"])
    if len(hist) >= 2:
        a, b = hist[0], hist[-1]
        days = max(1, (datetime.datetime.fromisoformat(b["ts"]) - datetime.datetime.fromisoformat(a["ts"])).days or 1)
        dc, df = (b["connections"] or 0) - (a["connections"] or 0), (b["followers"] or 0) - (a["followers"] or 0)
        spike = df > max(1000, 0.3 * (a["followers"] or 1)) or dc > max(500, 0.3 * (a["connections"] or 1))
        add("growth_velocity", not spike, f"+{dc:,} connections, +{df:,} followers over {days} day(s) across "
            f"{len(hist)} snapshots" + (" — growth spike" if spike else ""))
    else:
        add("growth_velocity", None, "Growth is measured from the second snapshot on (each scan stores one)")
    bad = [c for c in checks if c["ok"] is False]
    if m.get("crustdata_verdict") in ("probably_fabricated", "clearly_fabricated") or len(bad) >= 2:
        verdict = "review"
    elif m.get("crustdata_verdict") in ("clearly_genuine", "probably_genuine") or (m["any_email_verified"] and not bad):
        verdict = "trusted"
    else:
        verdict = "unconfirmed" if bad or not m["picture"] else "ok"
    return verdict, checks


# ------------------------------------------------------------------ ingest

def signal_id(m):
    return hashlib.sha1(f"{m['person_id']}|{m['to']['company']}|{m['to']['start']}".encode()).hexdigest()[:16]


def ingest(profiles, source):
    """Profiles -> signals. Returns the signals that are new."""
    c = store.conn()
    new = []
    for p in profiles:
        m = parse_move(p)
        if not m or not m["person_id"]:
            continue
        ts = now()
        c.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)",
                  (m["person_id"], ts, m["connections"], m["followers"], m["to"]["title"], m["to"]["company"]))
        kind = classify(m)
        sc, why = score(m, kind)
        verdict, checks = trust(m)
        if verdict == "review":
            sc = sc // 2  # an unconfirmed profile must not top the feed
        sid = signal_id(m)
        vals = {"from_co": m["from"]["company"], "to_co": m["to"]["company"], "title": m["to"]["title"]}
        actions = [{"buyer": b, "action": a.format(**vals)} for b, a in PLAYBOOK[kind]]
        data = {**m, "kind": kind, "kind_label": KIND_LABEL[kind], "score": sc, "score_why": why,
                "trust": verdict, "trust_checks": checks, "actions": actions, "source": source}
        row = c.execute("SELECT id, data FROM signals WHERE id=?", (sid,)).fetchone()
        if row:
            old = json.loads(row["data"])
            data["crustdata_verdict"] = old.get("crustdata_verdict")
            if data["crustdata_verdict"]:
                data["trust"], data["trust_checks"] = trust({**m, "crustdata_verdict": data["crustdata_verdict"]})
            c.execute("UPDATE signals SET last_seen=?, score=?, trust=?, data=? WHERE id=?",
                      (ts, sc, data["trust"], json.dumps(data, default=str), sid))
            continue
        c.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (sid, m["person_id"], m["name"], kind, sc, verdict, ts, ts, source, json.dumps(data, default=str)))
        new.append(data)
        store.event("signal", f"{KIND_LABEL[kind]}: {m['name']} — ex-{m['from']['company']} {m['from']['title']} → "
                    f"{m['to']['title']} @ {m['to']['company']} (score {sc})", {"id": sid, "score": sc})
    return new


# ------------------------------------------------------------------ live sources

def scan(since_days=60, country="India", pages=2, limit=25):
    """Live Person Search for recent moves. Never cached: each scan asks Crustdata now."""
    cd = Crustdata()
    if not cd.live:
        raise CrustdataError("CRUSTDATA_API_KEY not set")
    body = {"filters": filters(since_days, country), "fields": FIELDS, "limit": limit,
            "sorts": [{"field": "experience.employment_details.start_date", "order": "desc"}]}
    profiles, total, cursor = [], None, None
    for _ in range(pages):
        if cursor:
            body["cursor"] = cursor
        res = cd._post("/person/search", body, cache=False)
        profiles += res.get("profiles") or []
        total, cursor = res.get("total_count"), res.get("next_cursor")
        store.usage("/person/search", 0.03 * len(res.get("profiles") or []), f"{len(res.get('profiles') or [])} results")
        if not cursor:
            break
    new = ingest(profiles, "Crustdata Person Search (live scan)")
    store.event("scan", f"Scanned Crustdata: {total} people match the move filter; read {len(profiles)}, "
                f"{len(new)} new signal(s)", {"total": total, "read": len(profiles), "new": len(new)})
    return {"total": total, "read": len(profiles), "new": len(new)}


def create_watch(since_days=60, country="India", webhook_url=None, every_hours=1):
    """Save the move filter as a real Crustdata Discovery Watch. Pull-only unless a
    public webhook URL is given; its first run (a free sample of 5) lands in seconds."""
    cd = Crustdata()
    body = {"filters": filters(since_days, country), "fields": FIELDS,
            "config": {"trigger": {"type": "interval", "every_hours": every_hours}, "max_results_per_run": 25},
            "notifications": [{"type": "webhook", "url": webhook_url}] if webhook_url else []}
    w = cd._post("/watch/person/search", body, cache=False)
    store.conn().execute("INSERT OR REPLACE INTO watches VALUES(?,?,?,?,?,?)",
                         (w["id"], now(), json.dumps(body["filters"]), w.get("status"), None, json.dumps(w)))
    store.event("watch", f"Created Crustdata Discovery Watch #{w['id']} ({w.get('status')}), runs every "
                f"{every_hours}h" + (f", webhook {webhook_url}" if webhook_url else ", pull-only"), {"watch": w["id"]})
    return w


def _records(summary):
    """People delivered in a run, whatever the envelope."""
    out = []
    for n in (summary.get("notifications") or []) if isinstance(summary, dict) else []:
        for item in ((n.get("payload") or {}).get("notifications") or []):
            rec = item.get("record") if isinstance(item, dict) else None
            if isinstance(rec, dict):
                if not rec.get("crustdata_person_id") and str(item.get("uid", "")).rsplit("_", 1)[-1].isdigit():
                    rec = {**rec, "crustdata_person_id": int(str(item["uid"]).rsplit("_", 1)[-1])}
                out.append(rec)
    if out:
        return out
    for path in (("results", "added"), ("records",), ("results",), ("data", "added")):
        cur = summary
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, list):
            return cur
    return []


def poll_watches():
    """Read new runs of every watch we own and turn delivered people into signals."""
    cd = Crustdata()
    c = store.conn()
    out = 0
    for w in c.execute("SELECT id FROM watches").fetchall():
        wid = w["id"]
        try:
            meta = cd.get(f"/watch/person/search/{wid}")
            runs = cd.get(f"/watch/person/{wid}/runs?limit=10")
        except CrustdataError as e:
            store.event("error", f"Watch #{wid}: {e}")
            continue
        c.execute("UPDATE watches SET status=?, last_polled=?, data=? WHERE id=?",
                  (meta.get("status"), now(), json.dumps(meta), wid))
        run_list = runs if isinstance(runs, list) else runs.get("runs") or runs.get("data") or []
        for r in run_list:
            rid = r.get("id") or r.get("run_id")
            if not rid or c.execute("SELECT 1 FROM seen_runs WHERE watch_id=? AND run_id=?", (wid, rid)).fetchone():
                continue
            try:
                summary = cd.get(f"/watch/person/{wid}/runs/{rid}/summary")
            except CrustdataError as e:
                store.event("error", f"Watch #{wid} run {rid}: {e}")
                continue
            recs = _records(summary)
            if str(r.get("status", "")).lower() in ("running", "queued", "pending", "in_progress") and not recs:
                continue  # come back when the run has finished
            c.execute("INSERT OR IGNORE INTO seen_runs VALUES(?,?)", (wid, rid))
            new = ingest(recs, f"Crustdata Watcher #{wid}, run {rid}")
            out += len(new)
            store.event("watch_run", f"Watch #{wid} run {rid} delivered {len(recs)} people → {len(new)} new signal(s)",
                        {"watch": wid, "run": rid})
    return out


def ingest_webhook(payload):
    """A watcher pushed a run to us."""
    md = payload.get("metadata") or {}
    recs = _records(payload)
    new = ingest(recs, f"Crustdata Watcher #{md.get('watch_id')} webhook, run {md.get('run_id')}")
    store.event("watch_run", f"Webhook from watch #{md.get('watch_id')}: {len(recs)} people → {len(new)} new signal(s)")
    return len(new)


def deep_check(signal_id_):
    """Crustdata's own authenticity verdict (Person Enrich `assessment`), on demand."""
    c = store.conn()
    row = c.execute("SELECT data FROM signals WHERE id=?", (signal_id_,)).fetchone()
    if not row:
        return {"ok": False, "message": "no such signal"}
    data = json.loads(row["data"])
    if not data.get("profile_url"):
        return {"ok": False, "message": "no profile URL on record for this person"}
    try:
        res = Crustdata()._post("/person/enrich", {"professional_network_profile_urls": [data["profile_url"]],
                                                   "fields": ["basic_profile.name", "assessment"]})
    except CrustdataError as e:
        if "assessment" in str(e) or "403" in str(e):
            msg = ("This Crustdata key has no grant for the `assessment` field (Crustdata enables it per key). "
                   "The free consistency checks above still apply.")
            store.event("trust", f"Deep check {data['name']}: {msg}")
            return {"ok": False, "message": msg}
        return {"ok": False, "message": str(e)}
    store.usage("/person/enrich", 1, "authenticity")
    match = ((res[0].get("matches") or [{}])[0] if isinstance(res, list) and res else {}) or {}
    a = ((match.get("person_data") or {}).get("assessment") or {}).get("authenticity") or {}
    data["crustdata_verdict"], data["crustdata_tier"] = a.get("verdict"), a.get("tier")
    data["trust"], data["trust_checks"] = trust(data)
    data["trust_checks"].append({"check": "crustdata_authenticity", "ok": None if a.get("verdict") in (None, "cannot_verify")
                                 else a.get("verdict") in ("clearly_genuine", "probably_genuine"),
                                 "detail": f"Crustdata authenticity: {a.get('verdict') or 'no verdict'}"
                                           + (f" (tier {a.get('tier')} of 0–9, 0 = most trusted)" if a.get("tier") is not None else "")})
    c.execute("UPDATE signals SET trust=?, data=? WHERE id=?", (data["trust"], json.dumps(data, default=str), signal_id_))
    store.event("trust", f"Deep check {data['name']}: Crustdata says {a.get('verdict') or 'no verdict'}")
    return {"ok": True, "verdict": a.get("verdict"), "tier": a.get("tier")}


# ------------------------------------------------------------------ aggregates

def gravity(signals):
    """Talent Gravity: which startups pull talent out of big companies (a leading
    indicator of a raise), and which big companies are leaking it."""
    to, frm = {}, {}
    for s in signals:
        if s["trust"] == "review":
            continue  # unconfirmed profiles do not move an index
        w = 3 if SENIOR.search(s["from"]["title"] or "") else 2 if MID.search(s["from"]["title"] or "") else 1
        t = to.setdefault(s["to"]["company"], {"company": s["to"]["company"], "people": 0, "weight": 0,
                                               "from": set(), "headcount": s["to"]["headcount"]})
        t["people"] += 1
        t["weight"] += w
        t["from"].add(s["from"]["company"])
        f = frm.setdefault(s["from"]["company"], {"company": s["from"]["company"], "people": 0, "senior": 0,
                                                  "to": set()})
        f["people"] += 1
        f["senior"] += 1 if w == 3 else 0
        f["to"].add(s["to"]["company"])
    fix = lambda d, k: [{**v, k: sorted(v[k])} for v in d.values()]  # noqa: E731
    return (sorted(fix(to, "from"), key=lambda x: (-x["weight"], -x["people"]))[:10],
            sorted(fix(frm, "to"), key=lambda x: (-x["people"], -x["senior"]))[:10])


def feed(limit=200):
    c = store.conn()
    sigs = [json.loads(r["data"]) | {"id": r["id"], "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
            for r in c.execute("SELECT * FROM signals ORDER BY first_seen DESC, score DESC LIMIT ?", (limit,))]
    to, frm = gravity(sigs)
    events = [dict(r) for r in c.execute("SELECT id, ts, kind, message FROM events ORDER BY id DESC LIMIT 60")]
    watches = [dict(r) | {"data": json.loads(r["data"] or "{}")} for r in c.execute("SELECT * FROM watches")]
    used = c.execute("SELECT endpoint, ROUND(SUM(credits),2) credits, COUNT(*) calls FROM usage GROUP BY endpoint").fetchall()
    return {"signals": sigs, "gravity": to, "exodus": frm, "events": events, "watches": watches,
            "usage": [dict(u) for u in used]}
