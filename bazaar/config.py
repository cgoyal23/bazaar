"""Configuration and the procurement policy.

Every limit that decides whether money moves lives here, in code. No prompt can
change these values, and no agent reads them from a model's output.
"""
import datetime
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Procurement policy (enforced in code by Risk Officer, Contract and Payments).
POLICY = {
    "max_advance_pct": 30,              # never pay more than 30% before delivery
    "auto_pay_limit_paise": 5_00_000_00,  # payments above INR 5,00,000 need a human
    "min_company_age_years": 2,
    "min_verification_score": 0.75,
    "delivery_buffer_days": 3,          # delivered at least 3 days before the deadline
    "units_per_worker_month": 120,      # capacity model for cut-and-sew leather goods
    "production_share_of_headcount": 0.7,
    "capacity_claim_tolerance": 3.0,    # a claim above 3x the evidence is "unsupported"
    "qc_days": 2,
}


def today() -> datetime.date:
    value = os.environ.get("BAZAAR_TODAY")
    return datetime.date.fromisoformat(value) if value else datetime.date.today()


def pace() -> float:
    """Seconds to pause after each timeline event, so a live demo is watchable."""
    try:
        return float(os.environ.get("BAZAAR_PACE", "0"))
    except ValueError:
        return 0.0


def settle_seconds() -> float:
    """How long the payment waits for 'bank settlement' — the window to kill the worker."""
    try:
        return float(os.environ.get("BAZAAR_SETTLE_SECONDS", "0"))
    except ValueError:
        return 0.0


def crash_at() -> str:
    return os.environ.get("BAZAAR_CRASH_AT", "")


def buyer_name() -> str:
    return os.environ.get("BAZAAR_BUYER", "Demo Buyer Pvt Ltd")


def fmt_inr(paise: int, symbol: str = "INR ") -> str:
    """Format paise as Indian-grouped rupees: 114500000 -> 'INR 11,45,000.00'."""
    negative = paise < 0
    paise = abs(int(paise))
    rupees, p = divmod(paise, 100)
    s = str(rupees)
    if len(s) > 3:
        last3, rest = s[-3:], s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        s = ",".join(groups) + "," + last3
    return f"{'-' if negative else ''}{symbol}{s}.{p:02d}"


def parse_inr(text: str) -> int:
    """'11,45,000.00' -> paise."""
    return int(round(float(text.replace(",", "").strip()) * 100))
