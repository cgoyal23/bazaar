"""Run with:  python3 -m unittest discover -s tests -v"""
import datetime
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["BAZAAR_TODAY"] = "2026-11-06"
os.environ["BAZAAR_PACE"] = "0"
os.environ["BAZAAR_SETTLE_SECONDS"] = "0"
os.environ["BAZAAR_NO_DOTENV"] = "1"  # tests stay offline even if .env holds keys
os.environ.pop("CRUSTDATA_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

from bazaar import bank, db, orchestrator  # noqa: E402
from bazaar.agents import payments, planner  # noqa: E402

EXAMPLE = "We need 5,000 leather laptop sleeves in Bengaluru by 15 December, under ₹12 lakh."
TODAY = datetime.date(2026, 11, 6)


class Fresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["BAZAAR_DB"] = os.path.join(self.tmp, "t.db")

    def run_purchase(self, request=EXAMPLE):
        run_id = db.create_run(request, orchestrator.mode())
        status = orchestrator.run(run_id)
        return run_id, status

    def worker(self, run_id, **env):
        return subprocess.call([sys.executable, "-m", "bazaar.worker", run_id], cwd=ROOT,
                               env=dict(os.environ, **env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class PlannerTests(unittest.TestCase):
    def check(self, text, qty=5000, city="Bengaluru", deadline="2026-12-15", budget=1200000.0, per_unit=False):
        f, _ = planner.parse(text, TODAY)
        self.assertEqual(f["quantity"], qty)
        self.assertEqual(f["city"], city)
        self.assertEqual(f["deadline"].isoformat(), deadline)
        self.assertEqual(f["budget"]["rupees"], budget)
        self.assertEqual(f["budget"]["per_unit"], per_unit)
        self.assertIn("leather", f["item"])

    def test_example(self):
        self.check(EXAMPLE)

    def test_same_case_other_wordings(self):
        self.check("pls arrange 5k leather sleeves for laptops, deliver bangalore before Dec 15th, budget 12L max")
        self.check("Pls arrange 5000 pcs of leather laptop sleeves. budget: Rs 12,00,000. Bangalore. 15/12")
        self.check("5000 leather laptop sleeves to Bengaluru by 2026-12-15 at max ₹240 per unit",
                   budget=240.0, per_unit=True)

    def test_crore(self):
        f, _ = planner.parse("10,000 leather wallets to Mumbai by 1 Jan under ₹1.2 crore", TODAY)
        self.assertEqual(f["budget"]["rupees"], 12000000.0)
        self.assertEqual(f["deadline"].isoformat(), "2027-01-01")  # next occurrence

    def test_missing_fields_ask_a_human(self):
        os.environ["BAZAAR_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
        run_id = db.create_run("We need leather laptop sleeves for the office, quickly", {})
        self.assertEqual(orchestrator.run(run_id), "needs_input")
        exc = db.Store(run_id).exceptions()
        self.assertEqual(exc[0]["kind"], "needs_input")


class EndToEnd(Fresh):
    def test_full_purchase(self):
        run_id, status = self.run_purchase()
        self.assertEqual(status, "completed")
        s = db.Store(run_id)
        verify = s.step_done("verify")["results"]
        v = {r["supplier"]: r for r in verify}
        self.assertEqual(v["Zestora Leather Hub"]["verdict"], "REJECT")          # fake, cheapest
        self.assertEqual(v["Vantrix Leather Manufacturing"]["verdict"], "REJECT")  # capacity bluff
        self.assertTrue(any(c["check"] == "capacity" and c["ok"] is False
                            for c in v["Vantrix Leather Manufacturing"]["checks"]))
        self.assertEqual(len(verify), 5)  # 6 bids, Sarvaka's two names merged into one record
        risk = {r["supplier"]: r["decision"] for r in s.step_done("risk_review")["reviews"]}
        self.assertEqual(risk["Ojasvi Hidecraft LLP"], "BLOCK")  # 40% advance
        c = s.step_done("contract")
        self.assertEqual(c["voided"][0]["supplier"], "Ambrel Leatherworks Pvt Ltd")  # late price change
        self.assertEqual(c["supplier"], "Sarvaka Leather Co.")
        self.assertEqual(c["terms"]["total_paise"], 229 * 5000 * 100)
        po = c["po_number"]
        self.assertEqual(len(bank.transfers_for_reference(po)), 1)
        self.assertEqual(len(bank.ledger_for_po(po)), 1)
        self.assertEqual(bank.ledger_for_po(po)[0]["amount_paise"], 229 * 5000 * 100 * 30 // 100)
        held = s.step_done("inbox_watch")["processed"]
        self.assertEqual(held[0]["status"], "held")  # bank-change fraud
        audit = s.step_done("audit")
        self.assertEqual(audit["flags"], [])
        self.assertTrue(db.verify_chain(run_id)[0])

    def test_rerun_is_a_noop(self):
        run_id, _ = self.run_purchase()
        orchestrator.run(run_id)  # everything already done
        po = db.Store(run_id).step_done("contract")["po_number"]
        self.assertEqual(len(bank.transfers_for_reference(po)), 1)

    def test_crash_after_bank_transfer_pays_once(self):
        run_id = db.create_run(EXAMPLE, {})
        code = self.worker(run_id, BAZAAR_CRASH_AT="after_bank_transfer")
        self.assertEqual(code, 137)
        transfers = db.conn().execute("SELECT * FROM bank_transfers").fetchall()
        self.assertEqual(len(transfers), 1)
        self.assertEqual(db.conn().execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"], 0)
        self.assertEqual(self.worker(run_id), 0)
        self.assertEqual(db.get_run(run_id)["status"], "completed")
        self.assertEqual(db.conn().execute("SELECT COUNT(*) n FROM bank_transfers").fetchone()["n"], 1)
        self.assertEqual(db.conn().execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"], 1)
        # no message was sent twice on resume
        dup = db.conn().execute("SELECT COUNT(*) n FROM outbox GROUP BY key HAVING n>1").fetchall()
        self.assertEqual(dup, [])

    def test_approving_fraud_cannot_double_pay(self):
        run_id, _ = self.run_purchase()
        exc = [e for e in db.Store(run_id).exceptions() if e["kind"] == "bank_change"][0]
        out = payments.resolve_exception(exc["id"], "approve")
        self.assertFalse(out["ok"])
        self.assertIn("already paid", out["message"])
        self.assertEqual(db.conn().execute("SELECT COUNT(*) n FROM bank_transfers").fetchone()["n"], 1)

    def test_tampering_breaks_the_chain(self):
        run_id, _ = self.run_purchase()
        db.conn().execute("UPDATE events SET message='nothing to see' WHERE id=(SELECT MIN(id)+5 FROM events)")
        self.assertFalse(db.verify_chain(run_id)[0])


class UnseenRequests(Fresh):
    def test_bigger_budget_does_not_raise_the_price(self):
        _, s1 = self.run_purchase()
        tight = db.Store(db.list_runs(1)[0]["id"]).step_done("negotiate")["deals"]
        rid, _ = self.run_purchase(EXAMPLE.replace("12 lakh", "20 lakh"))
        loose = db.Store(rid).step_done("negotiate")["deals"]
        price = lambda deals: {d["supplier"]: d["price"]["unit_price"] for d in deals}
        self.assertEqual(price(tight), price(loose))
        self.assertEqual(db.Store(rid).step_done("contract")["terms"]["unit_price_paise"], 229 * 100)

    def test_item_outside_supplier_line_goes_to_a_human(self):
        rid, status = self.run_purchase("Buy 300 cotton tote bags for Pune office by 1 Dec under Rs 90,000")
        self.assertEqual(status, "needs_human")
        s = db.Store(rid)
        self.assertEqual(s.step_done("plan")["item"], "cotton tote bag")
        self.assertEqual(s.exceptions()[0]["kind"], "no_supplier")
        self.assertEqual(db.conn().execute("SELECT COUNT(*) n FROM bank_transfers").fetchone()["n"], 0)

    def test_nobody_verified_says_so(self):
        rid, status = self.run_purchase(EXAMPLE.replace("5,000", "50,000").replace("12 lakh", "1.2 crore"))
        self.assertEqual(status, "needs_human")
        self.assertEqual(db.Store(rid).exceptions()[0]["kind"], "no_verified_supplier")


class RealCompanyChecks(Fresh):
    """verify_real on Crustdata-shaped responses, offline."""
    def test_verdicts_cite_crustdata(self):
        from bazaar import crustdata
        from bazaar.agents import verifier
        records = {
            "big.in": {"basic_info": {"status": "active", "year_founded": 2001},
                       "headcount": {"total": 300, "timeseries": [{"date": "2026-04-01", "employee_count": 280}]},
                       "locations": {"headquarters": "Unit 4, Jajmau, Kanpur, Uttar Pradesh, India"}},
            "tiny.in": {"basic_info": {"status": "active"}, "headcount": {"total": 3}},
            "shrinking.in": {"basic_info": {"status": "active"},
                             "headcount": {"total": 100, "timeseries": [{"date": "2026-04-01", "employee_count": 400}]}},
            "blank.in": {"basic_info": {"status": "active"}},
        }
        orig = crustdata.Crustdata.enrich_domain
        crustdata.Crustdata.enrich_domain = lambda self, d: records.get(d)
        try:
            run_id = db.create_run(EXAMPLE, {})
            store = db.Store(run_id)
            spec = {"quantity": 5000, "delivery_city": "Bengaluru", "latest_acceptable_delivery": "2026-12-12"}
            out = verifier.verify_real(store, [{"name": d, "domain": d} for d in [*records, "none.in"]], spec, limit=5)
        finally:
            crustdata.Crustdata.enrich_domain = orig
        v = {r["domain"]: r["verdict"] for r in out}
        self.assertEqual(v, {"big.in": "ACTIVE_CAN_SUPPLY", "tiny.in": "ACTIVE_TOO_SMALL",
                             "shrinking.in": "NOT_SUITABLE", "blank.in": "UNVERIFIABLE", "none.in": "NO_RECORD"})
        self.assertEqual(out[0]["headquarters"], "Uttar Pradesh, India")
        self.assertTrue(all(f["source"].startswith(("Crustdata", "Verifier")) for f in store.facts()))


class BankTests(Fresh):
    def test_idempotency(self):
        t1, c1 = bank.transfer("K1", 100, "60300055667788", "SBXB0000721", "x", "PO-1")
        t2, c2 = bank.transfer("K1", 100, "60300055667788", "SBXB0000721", "x", "PO-1")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(t1["id"], t2["id"])
        with self.assertRaises(bank.IdempotencyConflict):
            bank.transfer("K1", 999, "99887766554433", "SBXB0000666", "y", "PO-1")


class BoundaryTests(unittest.TestCase):
    def test_buyer_agents_cannot_read_hidden_instructions(self):
        for f in (ROOT / "bazaar" / "agents").glob("*.py"):
            src = f.read_text()
            for forbidden in ("_hidden", "_policy", "_spec", "SUPPLIERS", "FRAUDSTER", "reveal("):
                self.assertNotIn(forbidden, src, f"{f.name} touches supplier internals ({forbidden})")


if __name__ == "__main__":
    unittest.main()
