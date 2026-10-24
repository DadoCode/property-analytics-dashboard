"""Global Expenses redesign: a ledger whose band, groups and rows all reconcile; filters, grouping, source treatment, reversals, capex, the
inspector drawer and the read-only rule for workbook rows. Scratch database only.

Run: .venv/bin/python tests/test_expenses_redesign.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-expenses-test-"))
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


def prop(pid, name, fee=None):
    c.execute("INSERT INTO properties (id, code, name, address, type, active, management_fee_pct, is_managed) VALUES (?,?,?,?,'flat',1,?,?)",
              (pid, pid.upper(), name, name, fee, 1 if fee else 0))
    completeness.seed_defaults(c, pid)


def tx(pid, date, amount, category, vendor="", desc="", capex=0, source="manual", batch=None, ref=None):
    cur = c.execute("INSERT INTO transactions (property_id,date,vendor,description,amount,direction,category,capex,source,import_batch_id,source_ref) VALUES (?,?,?,?,?,'expense',?,?,?,?,?)",
                    (pid, date, vendor, desc, amount, category, capex, source, batch, ref))
    return cur.lastrowid


prop("op1", "Flat 1, Operated House, London, N1 1AA")
prop("mg1", "Flat 2, Managed House, London, N2 2AA", fee=15.0)
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(7, 'Business_Accounts_2026.xlsx', 'abcdef0123456789', '2026-10-06 11:00', '2026-10-12 11:05:00', 'applied', '2026-09', 'workbook', ?, '{}', '{}', '{}')", (json.dumps(["op1", "mg1"]),))
wb_clean = tx("op1", "2026-09-05", 300, "cleaning", "Keys (cleaner)", "weekly clean", source="workbook", batch=7, ref="OP1!AC20")
tx("op1", "2026-09-08", 100, "purchase", "Amazon", "lamp", capex=1)                                 # manual, capex
tx("op1", "2026-09-09", -50, "cleaning", "Keys (cleaner)", "refund", source="excel_import")           # a reversal / credit, earlier Excel
tx("mg1", "2026-09-03", 2500, "cleaning", "Owner cleaner", "", source="workbook", batch=7, ref="MG1!AC20")
tx("mg1", "2026-09-01", 1500, "management_fee", "Fee", "management fee", source="workbook", batch=7, ref="MG1!AC19")     # company income: never an expense here
tx("general-overheads", "2026-09-10", 400, "software", "Notion", "", source="workbook", batch=7, ref="MAIN!S16")
tx("general-overheads", "2026-09-11", 900, "salary", "", "Priya salary", source="workbook", batch=7, ref="MAIN!S20")
tx("op1", "2026-08-02", 70, "cleaning", "Keys (cleaner)", "", source="manual")
c.commit()

from app import create_app  # noqa: E402
from routes.expenses import _costs  # noqa: E402
import services.kpis as kpis  # noqa: E402

S0, E0 = kpis.month_bounds(2026, 9)


def get(path):
    return create_app().test_client().get(path)


def text(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


Q = "from=2026-09-01&to=2026-09-01&compare=none"
h = get(f"/expenses?{Q}").data.decode()
money = lambda v: float(v.replace(",", ""))
band = text(h.split('class="bk-band ex-band"')[1].split("</section>")[0])

print("the band reconciles")
prop_total, bus_total = _costs(c, S0, E0, scope="property"), _costs(c, S0, E0, scope="business")
check("Total Recorded Costs, Property Costs and Business Costs equal the existing cost functions (property 2850 = 300+100-50+2500; business 1300)",
      prop_total == 2850.0 and bus_total == 1300.0 and f"Total Recorded Costs £{prop_total + bus_total:,.0f}" in band and f"Property Costs £{prop_total:,.0f}" in band and f"Business Costs £{bus_total:,.0f}" in band, band)
check("the management-fee transfer is excluded from the band and from the ledger", "Fee" not in re.findall(r'class="ex-vendor">(.*?)</span>', h) and "£4,150" in band and "£5,650" not in band, band)
rows = re.findall(r'<tr class="ex-row.*?</tr>', h, re.S)
amts = [money(a) * (-1 if s else 1) for s, a in re.findall(r'class="num ex-amt">(−?)£([\d,]+\.\d{2})', h)]
check("Transactions = the rows listed (6), and the ledger total row equals the band and the sum of the rows", len(rows) == 6 and f"Transactions {len(rows)}" in band and abs(sum(amts) - (prop_total + bus_total)) < 0.005, (len(rows), amts))
check("Opex and Capex are a quiet secondary line (Capex £100)", "Opex £4,050 · Capex £100" in band)

print("quick views and filters")
cap = get(f"/expenses?{Q}&t_type=capex").data.decode()
check("the Capex view lists only capex rows, and the band follows it", len(re.findall(r'class="ex-row', cap)) == 1 and "Matching costs £100" in text(cap.split('class="bk-band ex-band"')[1].split("</section>")[0]))
check("quick views are four text links: All, Property Costs, Business Costs, Capex (no tab system)", [t for t in re.findall(r'<a href="[^"]*" class="(?:on)?" aria-current="[a-z]+">(.*?)</a>', h)][:4] == ["All", "Property Costs", "Business Costs", "Capex"])
pv = get(f"/expenses?{Q}&scope=property").data.decode()
check("Property Costs view: the ledger excludes Business rows and the headline is Property Costs £2,850", "Notion" not in pv and "Priya" not in pv and "Property Costs £2,850" in text(pv.split('class="bk-band ex-band"')[1].split("</section>")[0]))
fc = get(f"/expenses?{Q}&t_category=cleaning").data.decode()
fb = text(fc.split('class="bk-band ex-band"')[1].split("</section>")[0])
famts = [money(a) * (-1 if s else 1) for s, a in re.findall(r'class="num ex-amt">(−?)£([\d,]+\.\d{2})', fc)]
check("a filter's band equals its rows: Category Cleaning = 300 - 50 + 2500 = 2750 over 3 rows", abs(sum(famts) - 2750) < 0.005 and "Matching costs £2,750" in fb and "Transactions 3" in fb, (famts, fb))
check("an active filter is a readable removable token, with Reset; no filters shows nothing", 'aria-label="Remove filter Category: Cleaning"' in fc and 'class="chip-reset"' in fc and 'class="pr-chips"' not in h)
sq = get(f"/expenses?{Q}&t_q=lamp").data.decode()
check("search covers vendor and description (lamp is a description)", len(re.findall(r'class="ex-row', sq)) == 1 and "Amazon" in sq)
check("search filters as you type (no Search button, no select row)", 'id="ex-search"' in h and 'type="submit"' not in h.split('id="ex-search"')[1].split("</form>")[0] and "<select" not in h.split('id="ledger"')[1].split("</table>")[0] and "fetch(url)" in h)

print("grouping")
def groups(q):
    html = get(f"/expenses?{Q}&{q}").data.decode()
    out = []
    for gm in re.finditer(r'<tbody>\s*<tr class="grp">.*?</tbody>', html, re.S):
        blk = gm.group(0)
        name = re.search(r'class="g-name">(.*?)</span>', blk).group(1)
        n, amt = re.search(r'class="g-meta">(\d+) transactions? · £([\d,]+\.\d{2})', blk).groups()
        ra = [money(a) * (-1 if s else 1) for s, a in re.findall(r'class="num ex-amt">(−?)£([\d,]+\.\d{2})', blk)]
        out.append((name, int(n), money(amt), len(ra), round(sum(ra), 2)))
    return out
for key in ("property", "category", "vendor", "scope"):
    g = groups(f"t_group={key}")
    check(f"group by {key}: every group's count and subtotal equal its rows, and the groups add up to the ledger", bool(g) and all(n == nr and abs(a - sr) < 0.005 for _nm, n, a, nr, sr in g) and abs(sum(a for _n, _c, a, _r, _s in g) - 4150) < 0.005 and sum(n for _x, n, *_ in g) == 6, g)
gs = {n: (cnt, a) for n, cnt, a, _r, _s in groups("t_group=scope")}
check("grouping by scope separates Property costs (£2,850) from Business costs (£1,300)", gs.get("Property costs") == (4, 2850.0) and gs.get("Business costs") == (2, 1300.0), gs)
check("grouping by vendor shows 'No vendor' as its own group (the salary row has none)", any(n == "No vendor" and cnt == 1 for n, cnt, *_ in groups("t_group=vendor")))

print("rows: source, scope, capex, reversal")
check("each row reads vendor, then property and scope, then category, source and amount", re.search(r'ex-vendor">Keys \(cleaner\).*?ex-prop.*?ex-scope">Property cost.*?ex-cat">Cleaning.*?ex-src.*?class="num ex-amt"', h, re.S) is not None)
check("business rows say Business / Business cost; property rows name the property and say Property cost", "Business</span><span class=\"ex-scope\">Business cost" in h.replace("\n", "") and 'ex-scope">Property cost' in h)
check("source is quiet text: workbook rows say 'Monthly workbook · Batch #7' linked to that property's block; earlier Excel and manual entries say so",
      'href="/imports/7#prop-op1"' in h and "Monthly workbook · Batch #7" in h and "Earlier Excel import" in h and "Manual entry" in h)
check("a reversal is shown as −£50.00 with a quiet 'Reversal / credit' label, not as an error, and it reduces the total", "−£50.00" in h and "Reversal / credit" in h and "neg" not in re.search(r'<tr class="ex-row.*?−£50\.00.*?</tr>', h, re.S).group(0))
check("capex is identifiable as quiet text, not a chip", re.search(r'ex-cat">Purchase<span class="ex-capex">Capex</span>', h) is not None)
check("no multi-select, no checkboxes, no bulk bar (there is no safe bulk action)", 'type="checkbox"' not in h.split('id="ledger"')[1] and "bulk" not in h.lower())
check("no chart, no summary cards, no 'Top vendors' / property tables on the page", "<canvas" not in h and 'class="tiles' not in h and "Top vendors" not in h)

print("the drawer (an inspector)")
d = get(f"/expenses/transactions/{wb_clean}").data.decode()
dt = text(d)
check("the amount leads; vendor, description and date follow", d.index("£300.00") < d.index("Keys (cleaner)") < d.index("weekly clean") < d.index("2026-09-05"))
check("classification: scope, property, category, opex", "Scope Property cost" in dt and "Property Operated House" in dt.replace("Flat 1, ", "").replace(", London, N1 1AA", "") and "Category Cleaning" in dt and "Classification Opex" in dt, dt[:420])
check("source: workbook, batch, cell and applied date, then 'Controlled by workbook import' as the control state",
      all(k in dt for k in ("Batch", "#7", "OP1!AC20", "Applied")) and "Controlled by workbook import".upper() in dt.upper() and d.index("Classification") < d.index("Controlled by workbook import") < d.index("Imported from"))
check("history only shows real events: the workbook import", "Imported from the monthly workbook" in dt)
check("a workbook row is read-only: no edit form, no delete, and the correction route is stated", "Save changes" not in d and "Delete transaction" not in d and "can't be edited or deleted" in d and 'class="ro-note"' in d)
man = c.execute("SELECT id FROM transactions WHERE vendor='Amazon'").fetchone()[0]
md = get(f"/expenses/transactions/{man}").data.decode()
check("a manual entry says it was entered by hand and keeps edit and delete, tucked into a disclosure", "entered by hand" in md.lower() and "Save changes" in md and "Delete transaction" in md and '<details class="ws-add dr-edit">' in md)
rev = c.execute("SELECT id FROM transactions WHERE description='refund'").fetchone()[0]
check("a reversal says so in the drawer too", "Reversal / credit" in get(f"/expenses/transactions/{rev}").data.decode())
fee_id = c.execute("SELECT id FROM transactions WHERE category='management_fee'").fetchone()[0]
check("a management-fee row (not in the ledger) still opens, labelled as company income", "Management fee · company income" in get(f"/expenses/transactions/{fee_id}").data.decode())
before = c.execute("SELECT amount, category FROM transactions WHERE id=?", (wb_clean,)).fetchone()
cl = create_app().test_client()
cl.post(f"/expenses/transactions/{wb_clean}/edit", data={"property_id": "op1", "vendor": "x", "description": "x", "amount": "1", "category": "other"})
cl.post(f"/expenses/transactions/{wb_clean}/delete")
check("the server still refuses to edit or delete a workbook row even if the form is forged", tuple(db.get_conn().execute("SELECT amount, category FROM transactions WHERE id=?", (wb_clean,)).fetchone()) == tuple(before))

print("one property, no data, accessibility")
one = get(f"/expenses?{Q}&property=op1").data.decode()
check("with one property selected there is no Property/Business split, just that property's total (£350 = 300+100-50)", "Total costs £350" in text(one.split('class="bk-band ex-band"')[1].split("</section>")[0]) and "Business Costs" not in text(one.split('class="bk-band ex-band"')[1].split("</section>")[0]))
nd = text(get("/expenses?from=2027-01-01&to=2027-01-01&compare=none").data.decode())
check("a period with nothing recorded shows dashes and says so once, with the import action", nd.count("No data imported for this period.") == 1 and "Import monthly workbook" in nd)
check("every menu has a name, the view switch is a labelled group, and the search is labelled", all(a in h for a in ('aria-label="Filter by category"', 'aria-label="Group the ledger"', 'aria-label="Sort the ledger"', 'role="group" aria-label="View"', 'aria-label="Search vendor or description"')))

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
sys.exit(1 if FAILS else 0)
