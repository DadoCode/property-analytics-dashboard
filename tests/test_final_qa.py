"""Final-QA regressions: the issues found in the whole-product pass and fixed, so they stay fixed. Scratch database only.

Run: .venv/bin/python tests/test_final_qa.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-finalqa-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("DASH_DEMO_MODE", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))

import db  # noqa: E402
from services import completeness  # noqa: E402

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


db.ensure_schema()
c = db.get_conn()
for pid, name in (("agg", "Aggregate Flat, London, N1 1AA"), ("empty", "Empty Flat, London, N2 2AA")):
    c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES (?,?,?,?,'flat',1)", (pid, pid.upper(), name, name))
    completeness.seed_defaults(c, pid)
# September 2026 carried as a monthly total (no night-by-night reservation), plus one real reservation in a different month so the Calendar tab exists
c.execute("INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, net_revenue, status, source) VALUES ('agg','other','monthly-aggregate','2026-09-01','2026-09-11',1000,0,1000,'confirmed','excel_import')")
c.execute("INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, net_revenue, status, source) VALUES ('agg','airbnb','R1','2026-07-02','2026-07-05',300,0,300,'confirmed','manual')")
c.commit()

from app import create_app  # noqa: E402

CSS = (ROOT / "dashboard" / "static" / "style.css").read_text()
CHARTS = (ROOT / "dashboard" / "static" / "charts.js").read_text()
UI = (ROOT / "dashboard" / "static" / "ui.js").read_text()


def page(url):
    return create_app().test_client().get(url).get_data(as_text=True)


def flat(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


print("calendar: a month carried as a monthly total is explained, not drawn as empty")
sept = page("/bookings/calendar?from=2026-09-01&to=2026-09-01")
check("the calendar says the month was imported as a monthly total with its booked nights", "was imported as a monthly total (10 booked nights)" in flat(sept) and "no night-by-night calendar" in flat(sept))
check("no day claims '0/N booked' for that month", not re.search(r'cal-occ[^>]*>\s*0/\d', sept) and "cal-agg" in sept)
july = page("/bookings/calendar?from=2026-07-01&to=2026-07-01")
check("a month with real reservations still draws occupied days", "monthly total" not in flat(july) and re.search(r'cal-occ[^>]*>\s*1/2', july) is not None)

print("no data is a dash, not a zero")
p = flat(page("/properties?from=2027-01-01&to=2027-01-01"))
check("Properties band: no data shows dashes for Company Revenue, Property Profit and occupancy", re.search(r"Company Revenue — ", p) and re.search(r"Property Profit — ", p) and re.search(r"Portfolio occupancy — ", p) and "£0" not in p.split("Active properties")[1][:200], p[p.find("Active properties"):][:200])
real = flat(page("/properties?from=2026-09-01&to=2026-09-01"))
check("Properties band: a month with data still shows figures", re.search(r"Company Revenue £1,000", real) is not None, real[real.find("Active properties"):][:200])
b = flat(page("/properties/empty/bookings?from=2027-01-01&to=2027-01-01"))
check("property Bookings: nothing recorded -> dashes for reservations, nights, ADR and Gross Booking Revenue", "Reservations — " in b and "Booked nights — " in b and "ADR — " in b and "Gross Booking Revenue —" in b and "£0" not in b.split("Reservations")[1][:140], b[b.find("Reservations"):][:160])

print("old names are gone")
imp = page("/imports")
check("the Imports back link says Documents, and the palette does too", "&larr; Documents</a>" in imp and "Documents &amp; Monthly" not in imp and '"label": "Documents & Monthly Import"' not in imp)
mac = (ROOT / "dashboard" / "templates" / "_import_macros.html").read_text() + (ROOT / "dashboard" / "templates" / "import_batch.html").read_text()
check("workbook control rows are displayed as the workbook's own result, not as a Net/Operating profit", "Workbook net result (control)" in mac and "Workbook result before capex (control)" in mac)

print("accessibility")
for url, needles in {"/": ('aria-label="From month"', 'aria-label="To month"'), "/bookings/performance": ('role="img" aria-label="Portfolio occupancy by month"', 'aria-label="Occupancy against ADR'),
                     "/properties/agg/expenses": ('aria-label="Amount in pounds"', 'aria-label="Vendor"', 'aria-label="Category"'), "/properties/agg/documents": ('aria-label="Document type"',),
                     "/properties/agg/settings": ('aria-label="Management fee percentage"', 'aria-label="Calendar (iCal) link"'), "/audit/figures": ('aria-label="Property"',)}.items():
    h = page(url)
    check(f"{url}: controls and charts have accessible names", all(n in h for n in needles), [n for n in needles if n not in h])
check("the drawer is a dialog: role, modal, focus moves in on open and returns to the opener on close", all(x in CHARTS for x in ("setAttribute('role', 'dialog')", "aria-modal", "d.focus()", "drawerOpener.focus()")))
check("filter popovers are nudged back inside the viewport", "details.pr-menu" in UI and "marginLeft" in UI)


def lum(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b_ = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b_)


def ratio(a, b_):
    la, lb = sorted((lum(a), lum(b_)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


quiet = re.search(r"--text-3:\s*(#[0-9a-fA-F]{6})", CSS).group(1)
check(f"quiet text ({quiet}) reaches 4.5:1 on the canvas and on the tonal surface", ratio(quiet, "#fcfcfb") >= 4.5 and ratio(quiet, "#f5f6f4") >= 4.5, (ratio(quiet, "#fcfcfb"), ratio(quiet, "#f5f6f4")))

print("layout hooks")
check("workspace Recent records scroll inside instead of clipping; the Bookings table scrolls inside its surface", ".ov2-recent { overflow-x: auto" in CSS and ".bk-surface { overflow-x: auto; }" in CSS)
check("the calendar grid can shrink to a phone and the Targets summary stacks on one column", "repeat(7, minmax(0, 1fr))" in CSS and ".tg-items { grid-template-columns: minmax(0, 1fr); }" in CSS)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
if FAILS:
    print("FAILED:", *FAILS, sep="\n  - ")
    sys.exit(1)
