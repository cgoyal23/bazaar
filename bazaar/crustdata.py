"""Crustdata client: retries on 429/5xx/timeouts, never retries 401/402/403,
and caches every response in SQLite so a resumed run spends no credits twice.
"""
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request

from . import config, db

BASE = os.environ.get("CRUSTDATA_BASE", "https://api.crustdata.com")


class CrustdataError(Exception):
    pass


def dig(obj, path, default=None):
    """Read 'a.b.c' from nested dicts, or from a flat dotted key."""
    if not isinstance(obj, dict):
        return default
    if path in obj:
        return obj[path]
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class Crustdata:
    def __init__(self, on_event=None, key=None):
        self.key = key if key is not None else os.environ.get("CRUSTDATA_API_KEY", "")
        self.on_event = on_event or (lambda *a, **k: None)

    @property
    def live(self) -> bool:
        return bool(self.key)

    def _post(self, path: str, body: dict, retries: int = 3, timeout: int = 30, cache: bool = True, method="POST"):
        """cache=False for live reads (a watcher run, a fresh scan) that must not replay an old answer."""
        cache_key = db.sha256(method + path + db.dumps(body))
        if cache:
            row = db.conn().execute("SELECT response FROM api_cache WHERE key=?", (cache_key,)).fetchone()
            if row:
                return json.loads(row["response"])
        last = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                BASE + path, data=None if method == "GET" else json.dumps(body).encode(), method=method,
                headers={"authorization": f"Bearer {self.key}", "content-type": "application/json",
                         "x-api-version": "2025-11-01", "user-agent": "bazaar-procurement/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=config.ssl_context()) as r:
                    data = json.loads(r.read())
                if cache:
                    db.conn().execute("INSERT OR REPLACE INTO api_cache VALUES(?,?,?,?)",
                                      (cache_key, path, json.dumps(data), db.now()))
                return data
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code in (401, 402, 403):
                    raise CrustdataError(f"{path}: HTTP {e.code} — not retryable "
                                         "(check key, credits or endpoint access)") from e
                if e.code == 429 or e.code >= 500:
                    wait = float(e.headers.get("x-ratelimit-reset") or 2 ** attempt)
                    wait = min(max(wait, 1.0), 20.0)
                else:
                    try:
                        detail = e.read().decode(errors="replace")[:400]
                    except Exception:
                        detail = ""
                    raise CrustdataError(f"{path}: HTTP {e.code} {detail}") from e
            except urllib.error.URLError as e:
                if isinstance(e.reason, ssl.SSLCertVerificationError):
                    raise CrustdataError(f"{path}: TLS certificate check failed — no CA bundle found "
                                         "(pip install certifi, or run 'Install Certificates.command')") from e
                last = f"URLError: {e.reason}"
                wait = 2 ** attempt
            except (socket.timeout, TimeoutError, ValueError) as e:
                last = type(e).__name__
                wait = 2 ** attempt
            if attempt < retries:
                self.on_event("retry", f"Crustdata {path} failed ({last}); retry {attempt + 1}/{retries} in {wait:.0f}s")
                time.sleep(wait)
        raise CrustdataError(f"{path}: gave up after {retries} retries ({last})")

    def get(self, path: str):
        return self._post(path, {}, cache=False, method="GET")

    def search_companies(self, query: str, country: str = "IND", limit: int = 10):
        body = {
            "search": {"query": query, "mode": "hybrid"},
            "filters": {"field": "locations.country", "type": "=", "value": country},
            "fields": ["basic_info.name", "basic_info.primary_domain", "basic_info.description",
                       "basic_info.year_founded", "headcount.total", "locations.headquarters"],
            "limit": limit,
        }
        return self._post("/company/search", body).get("companies", []) or []

    def enrich_domain(self, domain: str, fields=("basic_info", "headcount", "locations")):
        """Best record for a domain. Crustdata can rank a different company first
        (e.g. one that once linked to the domain), so a record that does not list
        the domain among its own domains is returned with _domain_match=False."""
        data = self._post("/company/enrich", {"domains": [domain], "fields": list(fields)})
        if not (isinstance(data, list) and data):
            return None
        matches = data[0].get("matches") or []
        if not matches:
            return None
        want = domain.lower().removeprefix("www.")

        def owns(m):
            info = (m.get("company_data") or {}).get("basic_info") or {}
            doms = [str(d).lower().removeprefix("www.") for d in (info.get("all_domains") or [])]
            if info.get("primary_domain"):
                doms.append(str(info["primary_domain"]).lower().removeprefix("www."))
            return want in doms

        owned = [m for m in matches if owns(m)]
        best = max(owned or matches, key=lambda m: m.get("confidence_score") or 0)
        rec = dict(best.get("company_data") or {})
        rec["_domain_match"] = bool(owned)
        rec["_confidence"] = best.get("confidence_score")
        rec["_alternatives"] = len(matches)
        return rec

    def enrich_id(self, company_id, fields=("basic_info", "headcount", "locations")):
        """Enrich by Crustdata company id: an exact key, so no ownership check is needed."""
        data = self._post("/company/enrich", {"crustdata_company_ids": [int(company_id)], "fields": list(fields)})
        if isinstance(data, list) and data and data[0].get("matches"):
            rec = dict(data[0]["matches"][0].get("company_data") or {})
            rec["_domain_match"] = True
            return rec
        return None

    def identify(self, name=None, domain=None):
        """Free. Returns [(confidence, company_data)] for a name or a domain."""
        body = {"names": [name]} if name else {"domains": [domain]}
        data = self._post("/company/identify", body)
        out = []
        if isinstance(data, list):
            for item in data:
                for m in item.get("matches") or []:
                    out.append((m.get("confidence_score") or 0, m.get("company_data") or {}))
        return out

    def web_search(self, query: str, sources=("web", "news"), location="IN"):
        data = self._post("/web/search/live", {"query": query, "sources": list(sources), "location": location})
        return data.get("results", []) if isinstance(data, dict) else []
