"""Global Bookings redesign: reservation records vs monthly totals, honest occupancy visuals, a reconciling band and activity list, filters,
drawers, states. Scratch database only.

Run: .venv/bin/python tests/test_bookings_redesign.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-bookings-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("DASH_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

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


def prop(pid, name, fee=None, active=1, start=None):
    c.execute("INSERT INTO properties (id, code, name, address, type, active, management_fee_pct, is_managed, start_date) VALUES (?,?,?,?,'flat',?,?,?,?)",
              (pid, pid.upper(), name, name, active, fee, 1 if fee else 0, start))
    completeness.seed_defaults(c, pid)


def tx(pid, date, amount, direction, category, desc="row", source="manual", batch=None, ref=None):
    c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source,import_batch_id,source_ref) VALUES (?,?,?,?,?,?,0,?,?,?)",
              (pid, date, desc, amount, direction, category, source, batch, ref))


def stay(pid, ci, co, platform, gross, net):
    c.execute("INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source) "
              "VALUES (?,?,?,?,?,?,?,0,?,'confirmed','booking_statement')", (pid, platform, f"R-{pid}-{ci}", ci, co, gross, gross - net, net))


def aggregate(pid, ym, nights, batch=None, ref=None):
    c.execute("INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source, import_batch_id, source_ref) "
              "VALUES (?, 'excel', 'monthly-aggregate', ?, ?, 0, 0, 0, 0, 'confirmed', 'workbook', ?, ?)", (pid, f"{ym}-01", f"{ym}-{1 + nights:02d}", batch, ref))


prop("res1", "Flat 1, Reserved House, London, N1 1AA")
prop("mon1", "Flat 2, Monthly House, London, N2 2AA")
prop("mg1", "Flat 3, Managed House, London, N3 3AA", fee=15.0)
prop("none1", "Flat 4, Nobooking House, London, N4 4AA")
prop("empty1", "Flat 5, Empty House, London, N5 5AA")
prop("later1", "Flat 6, Later House, London, N6 6AA", start="2027-06-01")
prop("zero1", "Flat 7, Zero House, London, N7 7AA")
prop("inc1", "Flat 8, Income House, London, N8 8AA")
prop("rev1", "Flat 9, Review House, London, N9 9AA")
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
# a real month for each flavour of evidence (2026-09, 30 days)
stay("res1", "2026-09-02", "2026-09-05", "airbnb", 330, 300)          # 3 nights
stay("res1", "2026-09-10", "2026-09-14", "booking_com", 520, 480)     # 4 nights
aggregate("mon1", "2026-09", 20, batch=7, ref="Days Booked 2026-09")
tx("mon1", "2026-09-01", 1500, "income", "booking_income", "11-12 BDC", "workbook", 7, "MON!AC10")
tx("mon1", "2026-09-01", 900, "income", "booking_income", "13-20 direct", "workbook", 7, "MON!AC11")
aggregate("mg1", "2026-09", 12, batch=7, ref="Days Booked 2026-09")
tx("mg1", "2026-09-01", 6000, "income", "booking_income", "guests", "workbook", 7, "MG!AC10")
tx("mg1", "2026-09-01", 900, "expense", "management_fee", "fee", "workbook", 7, "MG!AC19")
tx("none1", "2026-09-03", 80, "expense", "cleaning")                                    # recorded, but no booking evidence at all
tx("zero1", "2026-09-04", 40, "expense", "cleaning")
aggregate("zero1", "2026-09", 0, batch=7, ref="Days Booked 2026-09")                    # a recorded month with 0 booked nights: a real zero
tx("inc1", "2026-09-01", 700, "income", "booking_income", "income only", "workbook", 7, "INC!AC10")      # income with zero booked nights
aggregate("rev1", "2026-09", 10, batch=7, ref="Days Booked 2026-09")
tx("rev1", "2026-09-01", 400, "income", "booking_income", "x", "workbook", 7, "REV!AC10")
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(7, 'Business_Accounts_2026.xlsx', 'abcdef0123456789', '2026-10-06 11:00', '2026-10-12 11:05:00', 'applied', '2026-09', 'workbook', ?, ?, '{}', '{}')",
          (json.dumps(["mon1", "mg1", "zero1", "inc1", "rev1"]), json.dumps({
              "rev1": [{"metric": "Income without booked nights", "workbook": None, "imported": 400.0, "diff": None, "status": "REVIEW"}],
              "mon1": [{"metric": "Income", "workbook": 2400.0, "imported": 2400.0, "diff": 0.0, "status": "PASS"}]})))
c.commit()

from app import create_app  # noqa: E402
import services.kpis as kpis  # noqa: E402

S0, E0 = kpis.month_bounds(2026, 9)


def get(path):
    return create_app().test_client().get(path)


def text(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


Q = "from=2026-09-01&to=2026-09-01&compare=previous_period"
h = get(f"/bookings?{Q}").data.decode()
rows = {m.group(1): m.group(0) for m in re.finditer(r'<tr class="bk-row[^"]*"[^>]*?property/([a-z0-9-]+)/drawer.*?</tr>', h.replace("/bookings/property/", "property/"), re.S)}

print("page structure")
check("one band, one open list on a soft surface, no KPI cards, no 'Upcoming 30 days' / channel-mix cards",
      h.count('class="bk-band"') == 1 and 'class="pr-surface bk-surface"' in h and 'class="tiles' not in h and "Upcoming 30 days" not in h and "Channel mix" not in h)
check("the header band shows the title and the period in the context bar; Portfolio and Activity are two views", 'class="bar-title">Bookings</h1>' in h and ">Portfolio<" in h and ">Activity<" in h)
check("Gross Booking Revenue is labelled as booking revenue, not the company's, with its definition on hover", "booking revenue, not the company" in text(h) and "Total guest booking value" in h)

print("the band reconciles with the figures")
ps = [r for r in rows if r not in ("later1",)]
booked, avail, gross = kpis.booked_nights(c, None, S0, E0), kpis.available_nights(c, None, S0, E0), kpis.accommodation_revenue(c, None, S0, E0)
band = text(h.split('class="bk-band"')[1].split("</section>")[0])
check("Booked nights, occupancy and Gross Booking Revenue in the band equal the portfolio KPIs",
      f"Booked nights {booked} of {avail:.0f} available" in band.replace(".0 ", " ") or f"Booked nights {booked} of {int(avail)} available" in band, (band[:200], booked, avail))
check("…occupancy and GBR", f"Occupancy {round(kpis.occupancy(c, None, S0, E0) * 100)}%" in band and f"£{gross:,.0f}" in band, (band[:260], gross))

print("data type is visible and honest")
check("reservation records are labelled as such and their evidence is counted", "Reservation records" in rows["res1"] and "2 reservations" in rows["res1"])
check("a day strip is painted ONLY from reservation records: 7 booked days of 30 (3 + 4)", len(re.findall(r'<i class="b"></i>', rows["res1"])) == 7 and rows["res1"].count("<i") >= 30 and "mrail" not in rows["res1"])
check("a monthly total is labelled Monthly total with its batch, and shows a dashed proportional rail, never invented days", "Monthly total" in rows["mon1"] and "Batch #7" in rows["mon1"] and 'class="mrail"' in rows["mon1"] and 'class="dstrip"' not in rows["mon1"])
check("…the rail is the real occupancy (20 of 30 nights = 67%)", "67%" in rows["mon1"] and "20" in rows["mon1"] and "/ 30" in text(rows["mon1"]).replace("  ", " "))
check("recorded data but no booking evidence says 'No booking data' and keeps the measured zeros (0 / 30 nights, 0%, £0), as everywhere else", "No booking data" in rows["none1"] and "0 / 30" in text(rows["none1"]) and "0%" in rows["none1"] and "£0" in rows["none1"])
check("NO DATA != REAL ZERO: an empty property shows a dash and 'No data'; a recorded month with 0 nights shows 0 and 0%", "—" in rows["empty1"] and "No data" in rows["empty1"] and "0%" in rows["zero1"] and re.search(r'>0</a>', rows["zero1"]) is not None)
check("not started: 'Not active in this period', no figures", "Not active in this period" in rows["later1"] and "mlink" not in rows["later1"])
check("deterministic flags only: income without booked nights, and REVIEW on a booking-related check (linked to the import)",
      "Income without booked nights" in rows["inc1"] and "REVIEW" in rows["rev1"] and 'href="/imports/7#prop-rev1"' in rows["rev1"])
check("a healthy property has no flag", "ev-flag" not in rows["mon1"] and "ev-flag" not in rows["res1"])

print("activity")
a = get(f"/bookings?{Q}&view=activity").data.decode()
arows = re.findall(r'<tr class="bk-row clickable ak-(\w+)".*?</tr>', a, re.S)
check("Activity lists reservations, monthly totals and income lines as different things", {"reservation", "monthly", "income"} <= set(arows), set(arows))
res_rows = [m.group(0) for m in re.finditer(r'<tr class="bk-row clickable ak-reservation".*?</tr>', a, re.S)]
check("a reservation shows stay dates, channel as dot + word, nights and amount; it never shows a guest (none is stored)",
      len(res_rows) == 2 and "2 Sep → 5 Sep" in res_rows[-1] and "Airbnb" in "".join(res_rows) and "Booking.com" in "".join(res_rows) and "Guest" not in text(a), res_rows[:1])
mon_rows = [m.group(0) for m in re.finditer(r'<tr class="bk-row clickable ak-monthly".*?</tr>', a, re.S)]
check("a monthly total reads 'September booking total', has no stay dates and no channel", any("September booking total" in r and "→" not in r and "chan-" not in r for r in mon_rows))
tot = re.search(r'class="recs-total">.*?</tr>', a, re.S).group(0)
tn = int(re.findall(r'<td class="num">(\d+)</td>', tot)[0]); ta = float(re.findall(r'£([\d,]+\.\d{2})', tot)[0].replace(",", ""))
check("the Activity totals row equals the band: nights = Booked nights and amount = Gross Booking Revenue", tn == booked and abs(ta - gross) < 0.51, (tn, booked, ta, gross))
ch = get(f"/bookings?{Q}&view=activity&channel=airbnb").data.decode()
check("the channel filter keeps only reservation records of that channel, with a removable chip", len(re.findall(r'ak-reservation', ch)) == 1 and 'aria-label="Remove filter Airbnb"' in ch)
check("Data type filter: Monthly total keeps monthly rows only", set(re.findall(r'ak-(\w+)"', get(f"/bookings?{Q}&view=activity&dtype=monthly").data.decode())) == {"monthly"})

print("filters and sorting")
pids = lambda html: re.findall(r"property/([a-z0-9-]+)/drawer", html.replace("/bookings/property/", "property/"))
check("model filter: managed only", set(pids(get(f"/bookings?{Q}&model=managed").data.decode())) == {"mg1"})
check("data type filter: reservation records only", set(pids(get(f"/bookings?{Q}&dtype=reservations").data.decode())) == {"res1"})
check("search by name", set(pids(get(f"/bookings?{Q}&q=income").data.decode())) == {"inc1"})
byn = pids(get(f"/bookings?{Q}&sort=nights").data.decode())
check("sorted by booked nights, highest first (monthly 20, managed 12, rev 10, res 7)", byn.index("mon1") < byn.index("mg1") < byn.index("rev1") < byn.index("res1"), byn)
check("the property selector (context bar) narrows the list", set(pids(get(f"/bookings?{Q}&property=mg1").data.decode())) == {"mg1"})
check("a 'monthly totals only' note is shown when no property has reservation records, and not otherwise",
      "Monthly booking totals are available for this period, but no individual reservation records were imported." not in text(h)
      and "Monthly booking totals are available" in text(get(f"/bookings?from=2026-09-01&to=2026-09-01&compare=none&property=mon1").data.decode()))

print("drawers (read only)")
rid = c.execute("SELECT id FROM bookings WHERE reservation_id LIKE 'R-res1-2026-09-02'").fetchone()[0]
aid = c.execute("SELECT id FROM bookings WHERE property_id='mon1'").fetchone()[0]
rd, ad = get(f"/bookings/{rid}/drawer").data.decode(), get(f"/bookings/{aid}/drawer").data.decode()
check("a reservation drawer says Reservation record and shows the real fields", "Reservation record" in rd and "Check-in" in rd and "2026-09-02" in rd and "Nights" in rd and "<form" not in rd)
check("a monthly-total drawer says so plainly and shows batch, source cell and applied date", "This is a monthly total, not an individual reservation." in ad and "#7" in ad and "Days Booked 2026-09" in ad and "<form" not in ad)
pd = get(f"/bookings/property/mon1/drawer?{Q}").data.decode()
check("the property drawer shows nights, occupancy, GBR, the data type and source, and links to the property's Bookings tab",
      "20 / 30 nights" in pd and "67%" in pd and "Monthly total" in pd and "not individual reservations" in pd and 'href="/imports/7#prop-mon1"' in pd and "/properties/mon1/bookings?" in pd and "<form" not in pd, pd[:300])
check("every row has a named button to open its evidence", all('aria-label="Booking evidence for' in r for r in rows.values()) and len(re.findall(r'aria-label="Open record:', a)) >= 5)

print("states")
nd = get("/bookings?from=2027-01-01&to=2027-01-01&compare=none")
ndt = text(nd.data.decode())
check("with truly no data it says so and offers the monthly import; no 'upload a statement'", "No data imported for this period." in ndt and "Import monthly workbook" in ndt and "Upload a statement" not in ndt)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
sys.exit(1 if FAILS else 0)
