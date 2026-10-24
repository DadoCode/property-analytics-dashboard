"""Builds a demo database of realistic but entirely FAKE data (five made-up flats, over a year of bookings and costs, a few sample documents), so the
dashboard can be tried without any real data.

    DASHBOARD_DB_PATH=/tmp/demo/dashboard.db DASHBOARD_UPLOADS_PATH=/tmp/demo/uploads .venv/bin/python scripts/seed_demo_data.py
    DASHBOARD_DB_PATH=/tmp/demo/dashboard.db DASHBOARD_UPLOADS_PATH=/tmp/demo/uploads .venv/bin/flask --app dashboard/app run --port 5051

It DELETES and rebuilds the database at DASHBOARD_DB_PATH, so it refuses to run unless that is set explicitly (it never touches data/dashboard.db by default)."""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import os  # noqa: E402

if not os.environ.get("DASHBOARD_DB_PATH"):
    sys.exit("Refusing to run: set DASHBOARD_DB_PATH to a scratch database file (this script deletes and rebuilds it).")

import db  # noqa: E402
from services import runtime  # noqa: E402

random.seed(7)

DB_PATH = db.DB_PATH
if DB_PATH.exists():
    DB_PATH.unlink()

db.ensure_schema()
conn = db.get_conn()

PROPERTIES = [
    ("flat-riverside-1", "RIV1", "12 Riverside Court", "12 Riverside Court, London SE1", "flat", "2024-03-01"),
    ("flat-riverside-2", "RIV2", "14 Riverside Court", "14 Riverside Court, London SE1", "flat", "2024-03-01"),
    ("flat-maple-house", "MPL1", "5 Maple House", "5 Maple House, London E2", "flat", "2024-06-01"),
    ("flat-canal-view", "CNL1", "22 Canal View", "22 Canal View, London N1", "flat", "2024-09-01"),
    ("flat-kings-cross", "KGX1", "8 Kings Cross Apartments", "8 Kings Cross Apartments, London N1C", "flat", "2025-01-01"),
    ("overhead-shared", "OVH", "Shared Overheads", "Portfolio-wide overheads", "overhead", "2024-03-01"),
]

for pid, code, name, address, ptype, start in PROPERTIES:
    conn.execute(
        """INSERT INTO properties (id, code, name, address, active, type, start_date, created_at)
           VALUES (?,?,?,?,1,?,?,datetime('now'))""",
        (pid, code, name, address, ptype, start),
    )

PLATFORMS = ["airbnb", "booking_com", "direct"]
today = date(2026, 9, 1)


def months_back(n):
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


flats = [p for p in PROPERTIES if p[4] == "flat"]

for pid, code, name, address, ptype, start in flats:
    base_rate = random.uniform(85, 160)
    for (y, m) in months_back(14):
        start_dt = date(y, m, 1)
        if start_dt < date(*map(int, start.split("-")[:2]), 1):
            continue
        days_in_month = (date(y + (m == 12), (m % 12) + 1, 1) - start_dt).days
        n_bookings = random.randint(2, 5)
        day_cursor = 1
        month_occupancy_boost = random.uniform(0.55, 0.9)
        for _ in range(n_bookings):
            if day_cursor >= days_in_month - 1:
                break
            stay = random.randint(2, 7)
            gap = random.randint(0, 3)
            check_in = start_dt + timedelta(days=day_cursor + gap - 1)
            check_out = check_in + timedelta(days=stay)
            if (check_out - start_dt).days > days_in_month:
                check_out = start_dt + timedelta(days=days_in_month)
            nights = max((check_out - check_in).days, 1)
            rate = base_rate * random.uniform(0.85, 1.25) * month_occupancy_boost
            gross = round(rate * nights, 2)
            fees = round(gross * 0.03, 2)
            cleaning = round(random.uniform(45, 85), 2)
            net = round(gross - fees, 2)
            conn.execute(
                """INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out,
                       gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source)
                   VALUES (?,?,?,?,?,?,?,?,?, 'confirmed', 'upload')""",
                (pid, random.choice(PLATFORMS), f"DEMO-{pid[:3].upper()}-{y}{m:02d}-{_}",
                 check_in.isoformat(), check_out.isoformat(), gross, fees, cleaning, net),
            )
            day_cursor += stay + gap

        for cat, lo, hi, capex in [
            ("cleaning", 120, 320, 0), ("utilities", 60, 140, 0),
            ("maintenance", 0, 260, 0), ("management_fee", 80, 220, 0),
        ]:
            if cat == "maintenance" and random.random() < 0.5:
                continue
            amt = round(random.uniform(lo, hi), 2)
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, description, amount,
                       direction, category, capex, source)
                   VALUES (?,?,?,?,?, 'expense', ?, ?, 'demo_seed')""",
                (pid, f"{y}-{m:02d}-05", cat.title().replace("_", " ") + " Co", f"{cat} for {y}-{m:02d}",
                 amt, cat, capex),
            )
        if random.random() < 0.25:
            amt = round(random.uniform(300, 1400), 2)
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, description, amount,
                       direction, category, capex, source)
                   VALUES (?,?,?,?,?, 'expense', 'furniture', 1, 'demo_seed')""",
                (pid, f"{y}-{m:02d}-15", "Furnish Co", "Replacement furniture/appliance", amt),
            )


overhead = PROPERTIES[-1]
for (y, m) in months_back(14):
    for cat, lo, hi in [("insurance", 80, 150), ("software", 40, 90), ("accounting", 100, 250), ("marketing", 0, 200)]:
        if random.random() < 0.15:
            continue
        amt = round(random.uniform(lo, hi), 2)
        conn.execute(
            """INSERT INTO transactions (property_id, date, vendor, description, amount,
                   direction, category, capex, source)
               VALUES (?,?,?,?,?, 'expense', ?, 0, 'demo_seed')""",
            (overhead[0], f"{y}-{m:02d}-03", cat.title() + " Ltd", f"{cat} for {y}-{m:02d}", amt, cat),
        )


import json
DOCS = runtime.uploads_dir() / "demo_docs"       # sample documents live in the uploads folder; the database stores the portable relative path
DOCS.mkdir(parents=True, exist_ok=True)
(DOCS / "riverside_cleaning_aug.csv").write_text(
    "date,vendor,description,amount\n2026-08-04,Sparkle Cleaners,Turnover clean,65.00\n"
    "2026-08-11,Sparkle Cleaners,Turnover clean,65.00\n2026-08-19,Sparkle Cleaners,Deep clean,120.00\n"
    "2026-08-27,Sparkle Cleaners,Turnover clean,65.00\n")
(DOCS / "airbnb_payout_maple_aug.csv").write_text(
    "confirmation,check_in,check_out,gross,fees,net\nHMDEMO1A,2026-08-03,2026-08-07,480.00,14.40,465.60\n"
    "HMDEMO2B,2026-08-12,2026-08-16,520.00,15.60,504.40\nHMDEMO3C,2026-08-21,2026-08-26,610.00,18.30,591.70\n")

def add_doc(pid, fn, dtype, status, conf):
    cur = conn.execute(
        """INSERT INTO documents (property_id, filename, stored_path, doc_type, detected_year, detected_month,
               status, confidence, reviewed, extracted_json) VALUES (?,?,?,?,2026,8,?,?,0,'{}')""",
        (pid, fn, "demo_docs/" + fn, dtype, status, conf))
    return cur.lastrowid

d1 = add_doc("flat-riverside-1", "riverside_cleaning_aug.csv", "cleaning_invoice", "extracted", 0.93)
for i, (dt, desc, amt) in enumerate([("2026-08-04", "Turnover clean", 65), ("2026-08-11", "Turnover clean", 65),
                                     ("2026-08-19", "Deep clean", 120), ("2026-08-27", "Turnover clean", 65)]):
    conn.execute(
        """INSERT INTO document_items (document_id, line_index, raw_description, date, vendor, amount, direction,
               property_id, category, capex, confidence, include, item_kind)
           VALUES (?,?,?,?,?,?, 'expense', 'flat-riverside-1', 'cleaning', 0, ?, 1, 'transaction')""",
        (d1, i, desc, dt, "Sparkle Cleaners", amt, 0.95 if i != 2 else 0.7))

d2 = add_doc("flat-maple-house", "airbnb_payout_maple_aug.csv", "booking_statement", "extracted", 0.9)
for i, (rid, ci, co, g, f, n) in enumerate([("HMDEMO1A", "2026-08-03", "2026-08-07", 480, 14.4, 465.6),
                                            ("HMDEMO2B", "2026-08-12", "2026-08-16", 520, 15.6, 504.4),
                                            ("HMDEMO3C", "2026-08-21", "2026-08-26", 610, 18.3, 591.7)]):
    conn.execute(
        """INSERT INTO document_items (document_id, line_index, raw_description, date, amount, direction, property_id,
               confidence, include, item_kind, check_in, check_out, reservation_id, platform,
               gross_revenue, platform_fees, net_revenue)
           VALUES (?,?,?,?,?, 'income', 'flat-maple-house', 0.92, 1, 'reservation', ?,?,?, 'airbnb', ?,?,?)""",
        (d2, i, "Reservation " + rid, ci, n, ci, co, rid, g, f, n))

d3 = add_doc("flat-canal-view", "canal_view_bank_jul.csv", "bank_statement", "confirmed", 0.97)
conn.execute("UPDATE documents SET reviewed=1 WHERE id=?", (d3,))


# --- sample confirmed documents so Data Health shows Complete / Partial / No data ---
for fname in ("booking_statement.csv", "cleaning_invoice.csv", "bank_statement.csv"):
    (DOCS / fname).write_text("date,description,amount\n2026-09-03,Sample line,100.00\n")
def _sample(pid, dtype, when):
    conn.execute("""INSERT INTO documents (property_id, filename, stored_path, doc_type, status, reviewed, uploaded_at)
                    VALUES (?,?,?,?,'confirmed',1,?)""", (pid, dtype + ".csv", "demo_docs/" + dtype + ".csv", dtype, when))
for when in ("2026-08-10 09:00:00", "2026-09-05 09:00:00"):
    for t in ("booking_statement", "cleaning_invoice", "bank_statement"):
        _sample("flat-riverside-1", t, when)                      # complete both months
for t in ("booking_statement", "cleaning_invoice"):
    _sample("flat-riverside-2", t, "2026-09-06 09:00:00")         # September: bank statement missing
    _sample("flat-maple-house", t, "2026-08-08 09:00:00")         # August: bank statement missing
_sample("flat-canal-view", "booking_statement", "2026-09-07 09:00:00")   # September: two sources missing

conn.commit()
n_bookings = conn.execute("SELECT COUNT(*) AS n FROM bookings").fetchone()["n"]
n_tx = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
print(f"Seeded demo DB at {DB_PATH}: {n_bookings} bookings, {n_tx} transactions, {len(PROPERTIES)} properties.")
