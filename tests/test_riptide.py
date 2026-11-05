"""Riptide, offline, on synthetic Crustdata-shaped profiles (no real person's data).
Run with:  python3 -m unittest discover -s tests -v"""
import datetime
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["BAZAAR_TODAY"] = "2026-11-06"
os.environ["BAZAAR_NO_DOTENV"] = "1"
os.environ.pop("CRUSTDATA_API_KEY", None)

from riptide import engine, store  # noqa: E402


def role(name, title, start, end=None, headcount=0, company_id=0, verified=False, industries=None, default=False):
    return {"name": name, "title": title, "start_date": start + "T00:00:00",
            "end_date": end + "T00:00:00" if end else None, "company_headcount_latest": headcount,
            "crustdata_company_id": company_id, "business_email_verified": verified,
            "company_industries": industries or [], "employment_type": "Full-time", "is_default": default}


def person(pid, name, current, past, connections=800, followers=900, picture=True):
    return {"crustdata_person_id": pid,
            "basic_profile": {"name": name, "headline": "", "location": {"full_location": "Bengaluru, India"},
                              "profile_picture_permalink": "https://x.example/p.jpg" if picture else None},
            "experience": {"employment_details": {"current": current, "past": past}},
            "professional_network": {"connections": connections, "followers": followers},
            "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.com/in/{pid}"}}}


FOUNDER = person(1, "Asha Founder",
                 [role("Stealth", "Co-founder", "2026-10-20", default=True)],
                 [role("Google", "Director, Engineering", "2018-01-01", "2026-09-30", 180000, 11, verified=True)])
HIRE = person(2, "Ravi Hire",
              [role("Tiny AI", "VP Engineering", "2026-09-01", headcount=14, company_id=77, default=True)],
              [role("Flipkart", "Senior Engineering Manager", "2019-05-01", "2026-08-15", 30000, 12)])
INTERN = person(3, "Ira Intern",
                [role("Small Co", "Engineer", "2026-10-01", headcount=8, default=True)],
                [role("Microsoft", "Software Engineering Intern", "2025-05-01", "2025-08-01", 220000, 13)])
PROFESSOR = person(4, "Prof Move",
                   [role("Tiny Lab", "Researcher", "2026-10-01", headcount=5, default=True)],
                   [role("Big University", "Professor", "2010-01-01", "2026-09-01", 9000, 14,
                         industries=["Higher Education"])])
FAKE = person(5, "Too Good",
              [role("Nova Labs", "Chief Executive Officer", "2026-10-25", headcount=2, default=True)],
              [role("Google", "Vice President", "2015-01-01", "2026-10-01", 0, 0)],  # employer not linked
              connections=40, followers=9000, picture=False)


class Fresh(unittest.TestCase):
    def setUp(self):
        os.environ["RIPTIDE_DB"] = os.path.join(tempfile.mkdtemp(), "r.db")


class Moves(Fresh):
    def test_classifies_the_move(self):
        self.assertEqual(engine.classify(engine.parse_move(FOUNDER)), "stealth_founder")
        self.assertEqual(engine.classify(engine.parse_move(HIRE)), "senior_startup_hire")

    def test_not_every_profile_is_a_move(self):
        self.assertIsNone(engine.parse_move(INTERN))     # an internship is not a big-company stint
        self.assertIsNone(engine.parse_move(PROFESSOR))  # a university is not a big company

    def test_score_is_explained(self):
        m = engine.parse_move(FOUNDER)
        total, parts = engine.score(m, "stealth_founder")
        self.assertEqual(total, min(100, sum(p["points"] for p in parts)))
        self.assertGreaterEqual(total, 80)  # director, 8 years at Google, stealth, 17 days ago


class Trust(Fresh):
    def test_genuine_and_suspicious_profiles(self):
        verdict, _ = engine.trust(engine.parse_move(FOUNDER), history=[])
        self.assertEqual(verdict, "trusted")
        verdict, checks = engine.trust(engine.parse_move(FAKE), history=[])
        self.assertEqual(verdict, "review")
        failed = {c["check"] for c in checks if c["ok"] is False}
        self.assertTrue({"employer_linked", "network_fits_claim", "audience_shape", "photo"} <= failed)

    def test_growth_spike_between_snapshots(self):
        hist = [{"ts": "2026-11-01T00:00:00", "connections": 400, "followers": 500},
                {"ts": "2026-11-03T00:00:00", "connections": 410, "followers": 4200}]
        _, checks = engine.trust(engine.parse_move(HIRE), history=hist)
        g = next(c for c in checks if c["check"] == "growth_velocity")
        self.assertFalse(g["ok"])
        self.assertIn("+3,700 followers", g["detail"])


class Ingest(Fresh):
    def test_dedupes_and_ranks_and_keeps_fakes_out_of_gravity(self):
        new = engine.ingest([FOUNDER, HIRE, INTERN, FAKE], "test")
        self.assertEqual({s["name"] for s in new}, {"Asha Founder", "Ravi Hire", "Too Good"})
        self.assertEqual(engine.ingest([FOUNDER, HIRE], "test again"), [])  # one move, one signal
        fake = next(s for s in new if s["name"] == "Too Good")
        self.assertEqual(fake["trust"], "review")
        f = engine.feed()
        self.assertNotIn("Nova Labs", [g["company"] for g in f["gravity"]])  # flagged profiles move no index
        snaps = store.conn().execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"]
        self.assertGreaterEqual(snaps, 3)

    def test_watcher_payloads(self):
        summary = {"notifications": [{"payload": {"notifications": [
            {"uid": "indb_discovery_person_2", "record": {k: v for k, v in HIRE.items() if k != "crustdata_person_id"}}]}}]}
        self.assertEqual(engine._records(summary)[0]["crustdata_person_id"], 2)
        webhook = {"metadata": {"watch_id": 9, "run_id": 1}, "results": {"added": [FOUNDER]}}
        self.assertEqual(engine.ingest_webhook(webhook), 1)
        self.assertIn("Watcher #9", engine.feed()["signals"][0]["source"])

    def test_filter_is_valid_crustdata_grammar(self):
        f = engine.filters(60, "India")
        self.assertEqual(f["op"], "and")
        since = next(c for c in f["conditions"] if c["type"] == "=>")["value"]
        self.assertEqual(since, (datetime.date(2026, 11, 6) - datetime.timedelta(days=60)).isoformat())


if __name__ == "__main__":
    unittest.main()
