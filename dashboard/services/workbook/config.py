"""What the importer is allowed to read, and what each sheet means.

Nothing here is inferred at import time. A sheet is read only if it is named
below (or is the Main Page / Expense Breakdown sheet for the workbook's year);
everything else is ignored, and a credential-type sheet is never opened at all.
"""
import re

# How a management-fee row may be prefixed in a workbook ("<prefix>manage ..."): lower-case, trailing space. Demo: none.
MANAGEMENT_ROW_PREFIXES = ()
# First names that mark an owner-pay (salary) row on the shared business sheet. Demo: a generic word.
OWNER_PAY_FIRST_NAMES = ("owner",)


def _prefix_group():
    return "(?:" + "|".join(re.escape(p) for p in MANAGEMENT_ROW_PREFIXES) + ")?" if MANAGEMENT_ROW_PREFIXES else ""


# ---------------------------------------------------------------- never opened
# A sheet whose name contains any of these is excluded BY NAME: not parsed,
# not logged, not stored, not displayed. (The reader never opens it.)
BLOCKED_SHEET_MARKERS = ("login", "password", "credential", "provider")
BLOCKED_SHEET_NAMES = ("Credentials",)


def is_blocked(sheet_name):
    n = sheet_name.replace("&amp;", "&").strip().lower()
    return n in {b.lower() for b in BLOCKED_SHEET_NAMES} or any(m in n for m in BLOCKED_SHEET_MARKERS)


# code -> (canonical property id, canonical name). Demo values; real ones come from the private overlay below.
PROPERTY_SHEETS = {'RV1': ('flat-riverside-1', '12 Riverside Court'), 'RV2': ('flat-riverside-2', '14 Riverside Court'), 'MPL': ('flat-maple-house', '5 Maple House'), 'CNL': ('flat-canal-view', '22 Canal View'), 'KGX': ('flat-kings-cross', '8 Kings Cross Apartments')}

MAIN_SHEET_BASE = "Main Page"            # -> "Main Page26": business costs + management-fee control totals
BREAKDOWN_SHEET_BASE = "Expense Breakdown"   # -> "Expense Breakdown26": item detail behind the sheets' "Purchases"

IGNORED_SHEETS = {'2025 vs 2026': 'comparison sheet -- would duplicate transactions', 'copy of main page': 'archive copy of the Main Page -- would duplicate transactions'}

# Breakdown sections are titled "<code> Expenses Breakdown"; the code must be a property code above.
BREAKDOWN_SECTION_RE = re.compile(r"^\s*(\S+)\s+Expenses Breakdown", re.I)

MAIN_ECHO_LABELS = {'canal view': 'CNL'}
MAIN_SUBTOTAL_LABEL = "general"          # the sum of the itemised rows above it -- never imported as well

MAIN_FEE_LABELS = {'riverside 1': 'RV1', 'riverside 2': 'RV2', 'maple': 'MPL'}

MAIN_ONLY_PROPERTIES = {}

# ------------------------------------------------------------------ categories
# Label -> dashboard category, using the vocabulary the Excel history already uses.
_CATEGORY_RULES = [
    # a management fee row: "Mngmt Fee (15%)", "Management Fee (15%)", but also a bare "Management 15%" (a sheet with no word "fee")
    (r"m(?:a)?n?g?e?m?n?t.*fee|mngm.*fee|management fee|fee.*\(\d+%\)|^\s*@PREFIX@manage(?:ment|r)\b|^\s*mngm", "management_fee"),
    (r"cleaning|\bcleaner\b|\bclean(?:s)?\b", "cleaning"),
    (r"council tax", "council_tax"),
    (r"\b(wifi|wi-fi|broadband|electricity|electric|water|gas|heating)\b", "utilities"),
    (r"\brent\b", "rent"),
    (r"deposit", "deposit"),
    (r"furniture|\btv\b|sofa|mattress", "furniture"),
    (r"plumb|repair|maintenance|boiler", "maintenance"),
    (r"purchases?|amazon|temu", "purchase"),
]
_BUSINESS_RULES = [
    (r"g-?suite|pricelabs|claude|godaddy|domain|website|lodgify|estate ?pilot|\bico\b", "software"),
    (r"\bads?\b|advert|marketing|biz cards|logos|followers", "marketing"),
    (r"insurance", "insurance"),
    (r"companies house|confirmation statement|incorp|accountant", "accounting"),
]


def categorise(label, business=False):
    text = (label or "").strip().lower()
    for pattern, category in (_BUSINESS_RULES if business else []) + _CATEGORY_RULES:
        if business and category == "management_fee":
            continue                    # a management fee is a property-sheet concept; never hide a business cost behind it
        if re.search(pattern.replace("@PREFIX@", _prefix_group()), text):      # the prefixes may come from the private overlay, loaded after this module body
            return category
    return "other"


PROPERTY_ALIASES = {'RV1': ['RV1', '12 Riverside Court'], 'RV2': ['RV2', '14 Riverside Court'], 'MPL': ['MPL', '5 Maple House'], 'CNL': ['CNL', '22 Canal View'], 'KGX': ['KGX', '8 Kings Cross Apartments']}

TOLERANCE = 0.01          # money, in pounds
OCCUPANCY_TOLERANCE = 0.005

CANONICAL_NAMES = {'flat-riverside-1': ('12 Riverside Court', 'high', 'demo data', None), 'flat-riverside-2': ('14 Riverside Court', 'high', 'demo data', None), 'flat-maple-house': ('5 Maple House', 'high', 'demo data', None), 'flat-canal-view': ('22 Canal View', 'high', 'demo data', None), 'flat-kings-cross': ('8 Kings Cross Apartments', 'high', 'demo data', None)}

NEW_PROPERTY_DEFAULTS = {}

PRE_START_DUPLICATE_CLEANUP = set()

START_DATES = {}

CONFIRMED_DISTINCT = []

MODEL_DECISIONS = {}

STATUS_DECISIONS = {}


# ------------------------------------------------------- optional private overlay
def _load_private_overlay():
    """The real property codes, names and decisions are private and are NOT in this repository. If a private_config.py exists
    (DASH_PRIVATE_CONFIG, else next to the database), every UPPER_CASE name it defines replaces the demo value above. Without it the
    importer simply runs against the fictional demo properties."""
    import importlib.util
    import os
    from pathlib import Path
    path = os.environ.get("DASH_PRIVATE_CONFIG")
    if not path:
        db_path = os.environ.get("DASHBOARD_DB_PATH")
        path = (Path(db_path).parent if db_path else Path(__file__).resolve().parents[3] / "data") / "private_config.py"
    path = Path(path)
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("un_private_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in dir(mod):
        if name.isupper():
            globals()[name] = getattr(mod, name)
    return str(path)


PRIVATE_OVERLAY = _load_private_overlay()
