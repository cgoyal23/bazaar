"""Configuration and the procurement policy.

Every limit that decides whether money moves lives here, in code. No prompt can
change these values, and no agent reads them from a model's output.
"""
import datetime
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path=os.path.join(ROOT, ".env")):
    """Read KEY=VALUE lines from .env (gitignored). Real environment variables win."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                os.environ.setdefault(k, v.strip().strip("'\""))
    except OSError:
        pass


if not os.environ.get("BAZAAR_NO_DOTENV"):
    _load_dotenv()

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


_SSL = None


def ssl_context():
    """HTTPS context that also works on python.org macOS builds, which ship without
    root certificates: fall back to certifi, then to the system bundle."""
    global _SSL
    if _SSL is None:
        import ssl
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs():
            cafile = None
            try:
                import certifi
                cafile = certifi.where()
            except ImportError:
                for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
                    if os.path.exists(path):
                        cafile = path
                        break
            if cafile:
                ctx = ssl.create_default_context(cafile=cafile)
        _SSL = ctx
    return _SSL


# Buyer profile: who is buying and the defaults a procurement team works to.
BUYER = {
    "name": os.environ.get("BAZAAR_BUYER", "Demo Buyer Pvt Ltd"),
    "ship_to_city": os.environ.get("BAZAAR_SHIP_TO", "Bengaluru"),
    "default_lead_days": int(os.environ.get("BAZAAR_LEAD_DAYS", "30")),
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
    return BUYER["name"]


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
