"""Crustdata client: retries on 429/5xx/timeouts, never retries 401/402/403,
and caches every response in SQLite so a resumed run spends no credits twice.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from . import db

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

    def _post(self, path: str, body: dict, retries: int = 3, timeout: int = 30):
        cache_key = db.sha256(path + db.dumps(body))
        row = db.conn().execute("SELECT response FROM api_cache WHERE key=?", (cache_key,)).fetchone()
        if row:
            return json.loads(row["response"])
        last = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                BASE + path, data=json.dumps(body).encode(), method="POST",
                headers={"authorization": f"Bearer {self.key}", "content-type": "application/json",
                         "x-api-version": "2025-11-01", "user-agent": "bazaar-procurement/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read())
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
                    raise CrustdataError(f"{path}: HTTP {e.code}") from e
            except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError) as e:
                last = type(e).__name__
                wait = 2 ** attempt
            if attempt < retries:
                self.on_event("retry", f"Crustdata {path} failed ({last}); retry {attempt + 1}/{retries} in {wait:.0f}s")
                time.sleep(wait)
        raise CrustdataError(f"{path}: gave up after {retries} retries ({last})")

    def search_companies(self, query: str, country: str = "IND", limit: int = 6):
        body = {
            "search": {"query": query, "mode": "hybrid"},
            "filters": {"field": "locations.country", "type": "=", "value": country},
            "fields": ["basic_info.name", "basic_info.primary_domain", "basic_info.description",
                       "basic_info.year_founded", "headcount.total", "locations.headquarters"],
            "limit": limit,
        }
        return self._post("/company/search", body).get("companies", []) or []

    def enrich_domain(self, domain: str):
        data = self._post("/company/enrich", {"domains": [domain],
                                              "fields": ["basic_info", "headcount", "locations"]})
        if isinstance(data, list) and data:
            matches = data[0].get("matches") or []
            if matches:
                best = max(matches, key=lambda m: m.get("confidence_score") or 0)
                return best.get("company_data")
        return None

    def web_search(self, query: str):
        data = self._post("/web/search/live", {"query": query, "sources": ["web", "news"]})
        return data.get("results", []) if isinstance(data, dict) else []
