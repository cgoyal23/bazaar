"""Product categories: public procurement knowledge, shared by the buyer agents and
the sandbox world.

For each category: the words that identify it, a benchmark unit price for common
items (what a buyer's price book would hold), a capacity model for the Verifier,
how to search Crustdata for real suppliers, and the spec defaults the Planner assumes.
Benchmarks are indicative sandbox values, shown as such; they are not live quotes.
"""
import re

CATEGORIES = {
    "leather_goods": {
        "label": "leather goods", "word": "Leather",
        "keywords": ["leather", "laptop sleeve", "sleeve", "wallet", "belt", "briefcase", "satchel", "purse",
                     "handbag", "laptop bag", "portfolio", "card holder", "passport holder", "glove"],
        "prices": {"laptop sleeve": 265, "sleeve": 265, "wallet": 320, "belt": 280, "laptop bag": 1400,
                   "briefcase": 1900, "handbag": 1500, "card holder": 180, "passport holder": 220},
        "units_per_worker_month": 120, "lead_factor": 1.0, "kind": "manufacturer",
        "search": "{item} manufacturer exporter", "match_terms": ["leather"],
        "assumed": "Genuine leather outer, padded lining, fits 13–15 inch laptops (Planner default — confirm)",
    },
    "beverages": {
        "label": "beverages", "word": "Beverages",
        "keywords": ["diet coke", "coke", "coca cola", "coca-cola", "pepsi", "cola", "soft drink", "soda",
                     "sprite", "fanta", "thums up", "juice", "energy drink", "red bull", "mineral water",
                     "water bottle", "bisleri", "beverage", "drink"],
        "prices": {"diet coke": 34, "coke": 34, "coca cola": 34, "coca-cola": 34, "pepsi": 33, "cola": 33,
                   "sprite": 33, "fanta": 33, "thums up": 33, "soft drink": 33, "soda": 30, "juice": 30,
                   "energy drink": 98, "red bull": 112, "mineral water": 15, "water bottle": 15, "bisleri": 15},
        "units_per_worker_month": 8000, "lead_factor": 0.2, "kind": "distributor",
        "search": "soft drinks beverages distributor wholesaler",
        "match_terms": ["beverage", "soft drink", "drinks", "bottl", "cola", "fmcg", "distribut", "juice", "water"],
        "assumed": "Sealed 300 ml cans/bottles with at least 4 months shelf life left (Planner default — confirm)",
    },
    "packaged_food": {
        "label": "packaged food", "word": "Foods",
        "keywords": ["biscuit", "cookie", "chips", "namkeen", "snack", "chocolate", "dry fruit", "nuts",
                     "cereal", "noodle", "tea", "coffee", "sugar sachet"],
        "prices": {"biscuit": 30, "cookie": 30, "chips": 20, "namkeen": 40, "snack": 30, "chocolate": 60,
                   "noodle": 15, "tea": 260, "coffee": 420},
        "units_per_worker_month": 6000, "lead_factor": 0.25, "kind": "distributor",
        "search": "packaged food FMCG distributor wholesaler",
        "match_terms": ["food", "fmcg", "snack", "distribut", "biscuit", "confection"],
        "assumed": "Sealed, at least 4 months shelf life left, FSSAI-labelled (Planner default — confirm)",
    },
    "electronics": {
        "label": "IT hardware and electronics", "word": "Technologies",
        "keywords": ["laptop", "notebook computer", "monitor", "keyboard", "mouse", "headphone", "headset",
                     "earphone", "charger", "cable", "tablet", "printer", "phone", "smartphone", "webcam",
                     "router", "pen drive", "ssd", "hard disk", "projector", "power bank", "speaker"],
        "prices": {"laptop": 55000, "monitor": 11500, "keyboard": 900, "mouse": 450, "headphone": 1500,
                   "headset": 1800, "earphone": 600, "charger": 700, "cable": 250, "tablet": 22000,
                   "printer": 14000, "smartphone": 15000, "phone": 15000, "webcam": 2200, "router": 2500,
                   "pen drive": 450, "ssd": 4500, "power bank": 1300, "speaker": 1800},
        "units_per_worker_month": 400, "lead_factor": 0.4, "kind": "distributor",
        "search": "IT hardware computer distributor reseller",
        "match_terms": ["computer", "laptop", "hardware", "electronic", "it ", "technolog", "distribut", "peripheral"],
        "assumed": "New and sealed, manufacturer warranty, GST invoice per unit (Planner default — confirm)",
    },
    "furniture": {
        "label": "furniture", "word": "Furniture",
        "keywords": ["chair", "desk", "table", "cabinet", "sofa", "shelf", "rack", "workstation", "stool",
                     "bench", "almirah", "furniture"],
        "prices": {"office chair": 8500, "chair": 6500, "desk": 9500, "table": 9000, "cabinet": 11000,
                   "sofa": 28000, "shelf": 5000, "rack": 5500, "workstation": 18000, "stool": 1800},
        "units_per_worker_month": 40, "lead_factor": 1.0, "kind": "manufacturer",
        "search": "office furniture manufacturer", "match_terms": ["furniture", "seating", "interior", "wood"],
        "assumed": "Commercial grade, delivered and assembled on site (Planner default — confirm)",
    },
    "stationery": {
        "label": "stationery and paper", "word": "Stationers",
        "keywords": ["notebook", "pen", "pencil", "a4 paper", "paper ream", "ream", "diary", "file",
                     "folder", "marker", "stapler", "envelope", "sticky note", "register"],
        "prices": {"notebook": 60, "pen": 10, "pencil": 6, "a4 paper": 290, "paper ream": 290, "ream": 290,
                   "diary": 180, "file": 40, "folder": 35, "marker": 35, "stapler": 120, "envelope": 5},
        "units_per_worker_month": 3000, "lead_factor": 0.4, "kind": "manufacturer",
        "search": "stationery paper products manufacturer", "match_terms": ["stationery", "paper", "notebook", "office supplies"],
        "assumed": "Standard office grade, branded or equivalent (Planner default — confirm)",
    },
    "apparel": {
        "label": "apparel and textiles", "word": "Textiles",
        "keywords": ["t-shirt", "tshirt", "t shirt", "shirt", "hoodie", "cap", "uniform", "jacket", "tote",
                     "cotton", "jute", "canvas", "towel", "apron", "socks", "polo"],
        "prices": {"t-shirt": 260, "tshirt": 260, "t shirt": 260, "polo": 380, "shirt": 450, "hoodie": 750,
                   "cap": 160, "uniform": 650, "jacket": 1400, "tote": 120, "towel": 220, "apron": 180},
        "units_per_worker_month": 250, "lead_factor": 0.8, "kind": "manufacturer",
        "search": "garment apparel manufacturer exporter",
        "match_terms": ["garment", "apparel", "textile", "clothing", "cotton", "fashion", "knit"],
        "assumed": "Size split per a size sheet to be confirmed; logo print included (Planner default — confirm)",
    },
}

GENERAL = {
    "label": "general goods", "word": "Traders", "keywords": [], "prices": {},
    "units_per_worker_month": 500, "lead_factor": 0.6, "kind": "trader",
    "search": "{item} supplier manufacturer", "match_terms": [],
    "assumed": "As described in the request (Planner default — confirm)",
}

# A material word decides the category before any product word does:
# "leather laptop sleeve" is leather goods, "cotton tote bag" is textiles.
_MATERIALS = [("leather", "leather_goods"), ("cotton", "apparel"), ("jute", "apparel"), ("canvas", "apparel")]
_NOT_LEATHER = {"faux", "vegan", "pu", "synthetic", "rexine"}


def _words(text):
    raw = re.findall(r"[a-z0-9\-]+", (text or "").lower())
    sing = [w[:-1] if w.endswith("s") and len(w) > 3 and not w.endswith("ss") else w for w in raw]
    return " " + " ".join(sing) + " ", set(raw) | set(sing)


def _has(phrase_text, kw):
    return f" {kw} " in phrase_text


def get(key):
    return CATEGORIES.get(key, GENERAL)


def categorize(item: str):
    """item -> {"key", "label", "benchmark_price" (rupees or None), "matched"}"""
    text, words = _words(item)
    key, matched = None, None
    for material, cat in _MATERIALS:
        if material in words and not (cat == "leather_goods" and words & _NOT_LEATHER):
            key, matched = cat, material
            break
    if key is None:
        best = None
        for k, c in CATEGORIES.items():
            for kw in c["keywords"]:
                if _has(text, kw) and (best is None or len(kw) > len(best[1])):
                    best = (k, kw)
        if best:
            key, matched = best
    cat = CATEGORIES.get(key, GENERAL)
    price = None
    for kw in sorted(cat["prices"], key=len, reverse=True):
        if _has(text, kw):
            price = cat["prices"][kw]
            break
    return {"key": key or "general", "label": cat["label"], "benchmark_price": price, "matched": matched}


def search_query(key, item):
    return get(key)["search"].format(item=item)


def matches_category(key, description: str) -> bool:
    terms = get(key)["match_terms"]
    if not terms:
        return True
    d = (description or "").lower()
    return any(t in d for t in terms)
