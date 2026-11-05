"""A mock bank and the buyer's ledger.

The bank behaves like a real payments API that honours idempotency keys: the
same key always returns the same transfer and never moves money twice. A reused
key with a different amount or account is refused outright.

The ledger posts one entry per bank transfer (UNIQUE transfer_id), so a crash
between "bank accepted" and "ledger posted" heals on resume with no double entry.
"""
import uuid

from . import db
from .sandbox import BANK_DIRECTORY


class IdempotencyConflict(Exception):
    pass


def find_transfer(idempotency_key: str):
    row = db.conn().execute("SELECT * FROM bank_transfers WHERE idempotency_key=?",
                            (idempotency_key,)).fetchone()
    return dict(row) if row else None


def transfer(idempotency_key: str, amount_paise: int, account: str, ifsc: str,
             beneficiary: str, reference: str):
    """Returns (transfer, created). created=False means the bank recognised the key."""
    if amount_paise <= 0:
        raise ValueError("amount must be positive")
    with db.tx() as c:
        row = c.execute("SELECT * FROM bank_transfers WHERE idempotency_key=?",
                        (idempotency_key,)).fetchone()
        if row:
            existing = dict(row)
            if existing["amount_paise"] != amount_paise or existing["account"] != account:
                raise IdempotencyConflict(
                    f"key {idempotency_key} already used for {existing['amount_paise']} to {existing['account']}")
            return existing, False
        tid = "TX-" + uuid.uuid4().hex[:8].upper()
        c.execute("INSERT INTO bank_transfers VALUES(?,?,?,?,?,?,?,?)",
                  (tid, idempotency_key, amount_paise, account, ifsc, beneficiary, reference, db.now()))
    return find_transfer(idempotency_key), True


def penny_drop(account: str, ifsc: str):
    """Account-holder name the bank returns for a 1-rupee verification transfer (sandbox)."""
    return BANK_DIRECTORY.get((account, ifsc))


def post_ledger(run_id: str, transfer_row: dict, po_number: str, memo: str):
    """Double-entry: Dr Advances to suppliers / Cr Bank. Returns (entry, posted_now)."""
    c = db.conn()
    cur = c.execute(
        "INSERT OR IGNORE INTO ledger(run_id,transfer_id,po_number,debit,credit,amount_paise,memo,ts) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (run_id, transfer_row["id"], po_number, "Advances to suppliers", "Bank — current account",
         transfer_row["amount_paise"], memo, db.now()))
    entry = dict(c.execute("SELECT * FROM ledger WHERE transfer_id=?", (transfer_row["id"],)).fetchone())
    return entry, cur.rowcount == 1


def ledger_for_po(po_number: str):
    return [dict(r) for r in db.conn().execute(
        "SELECT * FROM ledger WHERE po_number=? ORDER BY id", (po_number,))]


def transfers_for_reference(reference: str):
    return [dict(r) for r in db.conn().execute(
        "SELECT * FROM bank_transfers WHERE reference=? ORDER BY created_at", (reference,))]
