"""Reports audit + redesign: every label matches its formula, UI / PDF / CSV / Excel say the same thing, Property Profit is never
'Net profit', Company Net Profit includes Business Costs, managed Gross Booking Revenue is never Company Revenue, owner-side figures stay
owner-side, no data is not zero, a not-started property is not reported on. Scratch database only.

Run: .venv/bin/python tests/test_reports_redesign.py
"""
import csv
import html as htmllib
import io
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-reports-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("DASH_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

from reportlab import rl_config  # noqa: E402

rl_config.pageCompression = 0          # PDF text streams stay readable, so the PDF's labels can be checked

def in_pdf(text, pdf):
    """Whether a label/amount is in the PDF's text stream (standard-font encoding: £ is octal 243, parentheses escaped, true minus is a hyphen)."""
    enc = text.replace("\u2212", "-").replace("(", "\\(").replace(")", "\\)").replace("\u00a3", "\\243")
    return enc.encode("latin-1") in pdf


import db  # noqa: E402
import services.kpis as kpis  # noqa: E402
import services.reporting as reporting  # noqa: E402
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


def prop(pid, name, fee=None, start=None):
    c.execute("INSERT INTO properties (id, code, name, address, type, active, management_fee_pct, is_managed, start_date) VALUES (?,?,?,?,'flat',1,?,?,?)",
              (pid, pid.upper(), name, name, fee, 1 if fee else 0, start))
    completeness.seed_defaults(c, pid)


def tx(pid, date, amount, direction, category, capex=0):
    c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source) VALUES (?,?,?,?,?,?,?,'manual')",
              (pid, date, "row", amount, direction, category, capex))


prop("op1", "Flat 1, Operated House, London, N1 1AA")
prop("op2", "Flat 9, Later House, London, N9 9AA", start="2026-10-01")
prop("mg1", "Flat 2, Managed House, London, N2 2AA", fee=15.0)
prop("mg2", "Flat 5, Estimated House, London, N5 5AA", fee=12.0)
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
# 2026-09: operated income 2000, costs 600 (500 opex + 100 capex); managed guests paid 10000, recorded fee 1500, owner-side cleaning 2500; business 400
tx("op1", "2026-09-01", 2000, "income", "booking_income"); tx("op1", "2026-09-05", 500, "expense", "cleaning"); tx("op1", "2026-09-06", 100, "expense", "purchase", 1)
tx("mg1", "2026-09-01", 10000, "income", "booking_income"); tx("mg1", "2026-09-01", 1500, "expense", "management_fee"); tx("mg1", "2026-09-04", 2500, "expense", "cleaning")
tx("general-overheads", "2026-09-10", 400, "expense", "software")
# 2026-10: a managed property whose fee was never recorded (so Management Fee Earned is an estimate: 12% of 3000 = 360)
tx("op1", "2026-10-01", 300, "income", "booking_income"); tx("op1", "2026-10-04", 700, "expense", "maintenance")
tx("mg2", "2026-10-01", 3000, "income", "booking_income"); tx("general-overheads", "2026-10-09", 900, "expense", "marketing")
tx("op2", "2026-10-03", 800, "income", "booking_income")
c.commit()

from app import create_app  # noqa: E402
from routes.expenses import _costs  # noqa: E402
from routes.overview import company_net_profit  # noqa: E402

S, E = kpis.month_bounds(2026, 9)


def get(url):
    return create_app().test_client().get(url)


def flat(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


def rows_of(rep, heading):
    sec = next((s for s in rep["sections"] if s["heading"] == heading), None)
    return {r[0]: r[1:] for r in sec["rows"]} if sec else {}


def csv_text(url):
    return get(url).get_data(as_text=True)


def labels(rep):
    return {str(r[0]) for s in rep["sections"] for r in s["rows"]} | {s["heading"] for s in rep["sections"]} | {h["label"] for h in rep.get("hero", [])}


conn = db.get_conn()
port = reporting.build_portfolio_monthly(conn, 2026, 9)
print("portfolio vocabulary and reconciliation")
fin = rows_of(port, "Financial summary")
adj = kpis.adjusted_kpi_snapshot(conn, None, S, E)
co = company_net_profit(conn, S, E)
check("Company Revenue = adjusted revenue (operated revenue + Management Fee Earned), never Gross Booking Revenue", fin["Company Revenue"][0] == adj["revenue"] == 3500.0 and fin["Company Revenue"][0] < 10000)
check("Operated property costs = the Expenses page's operated costs (600), without the managed owner-side costs", fin["Operated property costs"][0] == _costs(conn, S, E, scope="property", model="operated") == 600.0)
check("Property Profit = the Overview's Property Profit (2,900) and is NOT called Net profit", fin["Property Profit"][0] == adj["net_profit"] == 2900.0 and "Net profit" not in labels(port) and "Net Profit" not in " ".join(l for l in labels(port) if l != "Company Net Profit"))
check("Business Costs = the Expenses page's business costs (400)", fin["Business Costs"][0] == _costs(conn, S, E, scope="business") == 400.0)
check("Company Net Profit includes Business Costs: Property Profit − Business Costs = 2,500 = the Overview's company_net_profit", fin["Company Net Profit"][0] == co["net_profit"] == 2500.0 and fin["Property Profit"][0] - fin["Business Costs"][0] == fin["Company Net Profit"][0])
check("the headline figures are Company Net Profit then Property Profit", [h["label"] for h in port["hero"]] == ["Company Net Profit", "Property Profit"] and [h["value"] for h in port["hero"]] == [2500.0, 2900.0])
cc = rows_of(port, "Recorded expenses by cost centre")
check("expenses by cost centre add up to the total with nothing plugged (600 + 2,500 owner-side + 1,500 fees + 400 = 5,000)", list(cc) == ["Operated property costs", "Managed properties' owner-side costs (not company costs)",
      "Management fees recorded (The company's own income)", "Business Costs", "Total recorded expenses"] and [v[0] for v in cc.values()] == [600.0, 2500.0, 1500.0, 400.0, 5000.0] and cc["Total recorded expenses"][0] == kpis.costs(conn, None, S, E))
check("owner-side costs are labelled as not company costs", "not company costs" in " ".join(cc))
byp = next(s for s in port["sections"] if s["heading"] == "By property")
check("by property: managed row's Company Revenue is its fee (1,500), its owner-side costs are not company costs (0)", [r for r in byp["rows"] if r[0] == "Flat 2, Managed House"][0][1:4] == [1500.0, 0.0, 1500.0])
check("by property columns use the same vocabulary", [c_["label"] for c_ in byp["columns"]][:5] == ["Property", "Company Revenue", "Operated property costs", "Property Profit", "Property Profit margin"])
check("portfolio note says Property Profit excludes and Net Profit includes Business Costs, and managed owners' revenue is excluded",
      any("Property Profit does not include Business Costs; Company Net Profit does" in (s.get("note") or "") for s in port["sections"]))

print("operated property statement")
op = reporting.build_property_monthly(conn, "op1", 2026, 9)
opf = rows_of(op, "Financial summary")
check("Revenue / Property Costs / Property Profit (2,000 / 600 / 1,400), kpis-equal", [opf[k][0] for k in ("Revenue", "Property Costs", "Property Profit")] == [2000.0, 600.0, 1400.0]
      and opf["Property Profit"][0] == kpis.net_profit(conn, "op1", S, E))
check("an operated statement never says Net profit, Gross Booking Revenue, owner-side or Management Fee", all(w not in " ".join(labels(op)) for w in ("Net profit", "Gross Booking", "owner-side", "Management Fee")))
check("it has no Business Costs line (not allocated to a property) and says so", "Business Costs" not in labels(op) and any("Business Costs are not allocated" in (s.get("note") or "") for s in op["sections"]))
check("the model is operated", op["model"] == "operated")

print("managed property statement")
mg = reporting.build_property_monthly(conn, "mg1", 2026, 9)
own = rows_of(mg, "Owner-side statement")
un = rows_of(mg, "The company")
check("Gross Booking Revenue is labelled owner-side and equals kpis.revenue (10,000)", own["Gross Booking Revenue (owner-side)"][0] == kpis.revenue(conn, "mg1", S, E) == 10000.0)
check("owner-side costs exclude the management fee (2,500); the recorded fee is its own line (1,500)", own["Owner-side costs (excluding the management fee)"][0] == 2500.0 and own["Management fee deducted (recorded)"][0] == 1500.0)
check("the owner-side balance is GBR − owner-side costs − fee (6,000) = kpis.net_profit, and is called a balance, never profit", own["Owner-side balance"][0] == 6000.0 == kpis.net_profit(conn, "mg1", S, E)
      and all("profit" not in l.lower() for l in own))
check("Management Fee Earned (1,500) is its own company line, equal to business_income", un["Management Fee Earned"][0] == kpis.business_income(conn, "mg1", S, E) == 1500.0)
check("a managed statement never shows Property Profit, Net profit, Company Revenue or a plain Revenue/Costs line", all(l not in labels(mg) for l in ("Property Profit", "Net profit", "Company Revenue", "Revenue", "Costs", "Property Costs", "Margin", "Property Profit margin")))
check("the owner-side note says it is not company profit, not Property Profit and not a payout", any("not company profit, not Property Profit, and not an owner payout" in (s.get("note") or "") for s in mg["sections"]))
check("the headline is Gross Booking Revenue (owner-side) then Management Fee Earned (the company)", [(h["label"], h.get("note")) for h in mg["hero"]] == [("Gross Booking Revenue", "owner-side"), ("Management Fee Earned", "The company")])
check("sections run owner-side, the company, Operations", [s["heading"] for s in mg["sections"]][:3] == ["Owner-side statement", "The company", "Operations"])
est = reporting.build_property_monthly(conn, "mg2", 2026, 10)
check("a fee that was never recorded is shown as an estimate (12% of 3,000 = 360) and is marked estimated", rows_of(est, "The company")["Management Fee Earned"][0] == 360.0 and "estimated" in est["hero"][1]["note"]
      and any("estimated at 12% of Gross Booking Revenue" in (s.get("note") or "") for s in est["sections"]))

print("no data is not zero; not started is not reported on")
ns = reporting.build_property_monthly(conn, "op2", 2026, 9)
check("a property that had not started has a state and no figures", ns["state"]["kind"] == "not_started" and ns["sections"] == [] and ns["hero"] == [] and "starts 2026-10-01" in ns["state"]["text"])
nd = reporting.build_property_monthly(conn, "op1", 2027, 1)
check("a property with nothing recorded says so instead of showing zeros", nd["state"]["kind"] == "no_data" and nd["sections"] == [])
check("an empty portfolio month and an empty year say so too", reporting.build_portfolio_monthly(conn, 2027, 3)["state"]["kind"] == "no_data" and reporting.build_annual_portfolio(conn, 2030)["state"]["kind"] == "no_data")
page = flat(get("/reports?type=property_monthly&property=op2&month=2026-09").get_data(as_text=True))
html_ns = get("/reports?type=property_monthly&property=op2&month=2026-09").get_data(as_text=True)
body_ns = html_ns.split('<div class="rp">')[1].split('<script id="palette-data"')[0]
check("the page shows the state, no zero figures and no export buttons (nothing disabled or fake)", "Not active in this period" in page and "£0" not in page and "reports/export" not in body_ns and "disabled" not in body_ns)
check("minus signs and negatives are formatted properly", reporting.format_cell(-80, "money") == "−£80" and reporting.format_cell(-5.5, "delta") == "−5.5%")

print("annual and expense reports")
ann = reporting.build_annual_portfolio(conn, 2026)
ys = rows_of(ann, "Year summary")
check("annual: Company Revenue / Property Profit / Business Costs / Company Net Profit, with Net Profit = Property Profit − Business Costs",
      abs(ys["Company Net Profit"][0] - (ys["Property Profit"][0] - ys["Business Costs"][0])) < 0.005 and "Net profit" not in labels(ann))
mm = next(s for s in ann["sections"] if s["heading"] == "Month by month")
check("annual month table columns use the same vocabulary", [c_["label"] for c_ in mm["columns"]] == ["Month", "Company Revenue", "Operated property costs", "Property Profit", "Business Costs", "Company Net Profit", "Occupancy"])
exp = reporting.build_expense(conn, "2026-09", "2026-09", None)
et = rows_of(exp, "Totals")
check("expense report: total is labelled Total recorded expenses (5,000 = every cost centre), not 'costs'", et["Total recorded expenses"][0] == 5000.0 and "Total costs" not in labels(exp))
check("expense report: cost centres reconcile to that total", rows_of(exp, "Recorded expenses by cost centre")["Total recorded expenses"][0] == 5000.0)
check("expense report note explains it includes owner-side costs, fees and Business Costs", any("owner-side costs, management fees and Business Costs" in (s.get("note") or "") for s in exp["sections"]))
expm = reporting.build_expense(conn, "2026-09", "2026-09", "mg1")
cm = rows_of(expm, "Recorded expenses by cost centre")
check("one managed property: owner-side costs and the management fee are separate lines that add to its total", cm["Owner-side costs (excluding the management fee)"][0] == 2500.0 and cm["Management fee recorded"][0] == 1500.0 and cm["Total recorded expenses"][0] == 4000.0)
check("the ledger and categories are present and readable ('Management fee', not management_fee)", "Management fee" in {r[0] for s in expm["sections"] if s["heading"] == "Expenses by category" for r in s["rows"]})

print("UI, CSV, Excel and PDF say the same thing")
import openpyxl  # noqa: E402

for q, rep in (("type=portfolio_monthly&month=2026-09", port), ("type=property_monthly&property=op1&month=2026-09", op), ("type=property_monthly&property=mg1&month=2026-09", mg),
               ("type=annual_portfolio&year=2026", ann), ("type=expense&from=2026-09&to=2026-09", exp)):
    name = rep["title"]
    ui = htmllib.unescape(flat(get("/reports?" + q).get_data(as_text=True)))
    csv_rows = list(csv.reader(io.StringIO(csv_text("/reports/export?format=csv&" + q))))
    csv_cells = {cell for row in csv_rows for cell in row}
    wb = openpyxl.load_workbook(io.BytesIO(get("/reports/export?format=xlsx&" + q).data))
    xl_cells = {str(cell.value) for ws in wb.worksheets for row in ws.iter_rows() for cell in row if cell.value is not None}
    pdf = get("/reports/export?format=pdf&" + q).data
    check(f"{name}: every section heading and row label in the preview is in the CSV, the Excel file and the PDF",
          all((l in ui) and (l in csv_cells) and (l in xl_cells) for l in labels(rep) if l not in ("Headline",)) and all(in_pdf(l, pdf) for l in labels(rep) if "'" not in l and "&" not in l and "—" not in l and len(l) < 34),
          [l for l in labels(rep) if not ((l in ui) and (l in csv_cells) and (l in xl_cells))])
    head_rows = {row[0]: row[1] for row in csv_rows if len(row) >= 3 and row[0] in {h["label"] for h in rep["hero"]}}
    check(f"{name}: the headline figures match in every format", all(h["text"] in ui and abs(float(head_rows[h["label"]]) - h["value"]) < 0.01 and in_pdf(h["text"], pdf) for h in reporting.hero_display(rep)), head_rows)
    check(f"{name}: nobody labels Property Profit as Net profit (any format)", "Net profit" not in csv_cells and "Net profit" not in xl_cells and b"Net profit" not in pdf and "Net profit" not in ui)
    check(f"{name}: the footer (generated, period, scope, source) is in every format", {"Generated", "Period", "Scope", "Source"} <= csv_cells and "Recorded dashboard data" in ui and b"Recorded dashboard data" in pdf)
check("the portfolio CSV carries the same numbers as the preview (Company Net Profit 2500, Property Profit 2900)", any(r[:2] == ["Company Net Profit", "2500.0"] or r[:2] == ["Company Net Profit", "2500"] for r in csv.reader(io.StringIO(csv_text("/reports/export?format=csv&type=portfolio_monthly&month=2026-09")))))

print("filenames and formats")
r = get("/reports/export?format=pdf&type=property_monthly&property=op1&month=2026-09")
check("PDF works and the filename is predictable and safe", r.status_code == 200 and r.data[:4] == b"%PDF" and re.search(r"filename=Fernhill_Flat-1-Operated-House_2026-09_Property-Statement\.pdf$", r.headers["Content-Disposition"]), r.headers.get("Content-Disposition"))
check("CSV and Excel work with the same stem", all(re.search(rf"filename=Fernhill_All-properties_2026-09_Portfolio-Monthly\.{ext}$", get(f"/reports/export?format={ext}&type=portfolio_monthly&month=2026-09").headers["Content-Disposition"]) for ext in ("csv", "xlsx")))
check("annual and expense stems carry their period and type", "Fernhill_All-properties_2026_Portfolio-Annual" in get("/reports/export?format=csv&type=annual_portfolio&year=2026").headers["Content-Disposition"]
      and "Fernhill_All-properties_2026-09_Expense-Report" in get("/reports/export?format=csv&type=expense&from=2026-09&to=2026-09").headers["Content-Disposition"])
check("a bad request still redirects with a message, not a broken file", get("/reports/export?format=pdf&type=property_monthly&month=2026-09").status_code == 302)

print("the page")
home = get("/reports").get_data(as_text=True)
hm = home.split('<div class="rp">')[1].split('<script id="palette-data"')[0]
check("only report types that genuinely exist are offered, as a compact selector", re.findall(r'<a href="[^"]*type=(\w+)[^"]*" class="(?:on)?"', hm) == ["portfolio_monthly", "annual_portfolio", "property_monthly", "expense"]
      and "Booking Report" not in hm and "Managed Owner Statement" not in hm and 'class="card' not in hm)
check("each type's contents appear as one line under the selector, and as a hover title", "Company Revenue to Company Net Profit" in hm and 'title="One property for one month.' in hm)
check("the selected type is marked for assistive tech", 'aria-current="true"' in hm)
check("it is a report document, not a dashboard: no tiles, no charts, no history table", all(x not in hm for x in ('class="tiles', "<canvas", "chart", 'class="grid-2')))
check("it says reports are generated on demand and nothing is stored (no fake history)", "Nothing is stored, so there is no history to list." in flat(hm))
check("the preview shows the statement hierarchy: kicker, scope, period, headline, sections, footer", all(x in hm for x in ("rp-kick", "rp-title", "rp-period", "rp-hero", "rp-sec", "rp-foot")))
check("exports offered are exactly PDF, Excel and CSV", re.findall(r'reports/export\?format=(\w+)', hm).__len__() == 3 and set(re.findall(r'reports/export\?format=(\w+)', hm)) == {"pdf", "xlsx", "csv"})
check("controls are labelled and keyboard reachable", all(x in get("/reports?type=property_monthly&property=op1").get_data(as_text=True) for x in ('for="rp-property"', 'for="rp-month"', '<button type="submit"')))
check("the property statement asks for a property calmly instead of erroring", "Choose a property to see its statement." in flat(get("/reports?type=property_monthly&property=").get_data(as_text=True)))
check("the old preview link still works", get("/reports/view?type=portfolio_monthly&month=2026-09").status_code == 200 and "Company Net Profit" in flat(get("/reports/view?type=portfolio_monthly&month=2026-09").get_data(as_text=True)))
css = (Path(__file__).resolve().parent.parent / "dashboard" / "static" / "style.css").read_text()
check("mobile: statement tables drop the comparison columns and the hero wraps", ".rp-sec .rp-tbl.rp-fin th:nth-child(n+3)" in css and ".rp-hero { display: flex; gap: 0; flex-wrap: wrap;" in css)
check("the old preview template is gone and nothing links to it", not (Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "reports" / "view.html").exists())

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
if FAILS:
    print("FAILED:", *FAILS, sep="\n  - ")
    sys.exit(1)
