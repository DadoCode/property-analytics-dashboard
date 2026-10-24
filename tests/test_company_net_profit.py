"""Company Net Profit (company level): Company Revenue - Operated Property Costs - Business Costs. Scratch database only.

Run: .venv/bin/python tests/test_company_net_profit.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-netprofit-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("DASH_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
import services.kpis as kpis  # noqa: E402
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


def tx(pid, date, amount, direction, category, capex=0, desc="row"):
    c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source) VALUES (?,?,?,?,?,?,?,'manual')",
              (pid, date, desc, amount, direction, category, capex))


prop("op1", "Flat 1, Operated House, London, N1 1AA")
prop("mg1", "Flat 2, Managed House, London, N2 2AA", fee=15.0)
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")

# 2026-09: operated income 2000, operated costs 600 (500 opex + 100 capex); managed guests paid 10000, fee 1500, owner-side cleaning 2500; business 400
tx("op1", "2026-09-01", 2000, "income", "booking_income")
tx("op1", "2026-09-05", 500, "expense", "cleaning")
tx("op1", "2026-09-06", 100, "expense", "purchase", capex=1)
tx("mg1", "2026-09-01", 10000, "income", "booking_income")
tx("mg1", "2026-09-01", 1500, "expense", "management_fee")
tx("mg1", "2026-09-03", 2500, "expense", "cleaning")
tx("general-overheads", "2026-09-10", 400, "expense", "software")
# 2026-10: a loss month: operated 300 income, 700 costs, business 900; managed fee 200 on 1333 gross
tx("op1", "2026-10-01", 300, "income", "booking_income")
tx("op1", "2026-10-04", 700, "expense", "maintenance")
tx("mg1", "2026-10-01", 1333, "income", "booking_income")
tx("mg1", "2026-10-01", 200, "expense", "management_fee")
tx("general-overheads", "2026-10-09", 900, "expense", "marketing")
# 2026-11: real zero: operated income 100 and cost 100, nothing else
tx("op1", "2026-11-01", 100, "income", "booking_income")
tx("op1", "2026-11-02", 100, "expense", "cleaning")
c.commit()

from app import create_app  # noqa: E402
from routes.overview import company_net_profit  # noqa: E402
from routes.expenses import _costs  # noqa: E402
import routes.targets as targets  # noqa: E402


def text(resp):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", resp.data.decode(), flags=re.S)))


def page(q):
    return create_app().test_client().get("/?" + q)          # a fresh client: no remembered property from an earlier request


def hero_value(resp):
    """The Company Net Profit hero figure (portfolio scope only)."""
    m = re.search(r'id="hero-value">(.*?)</div>', resp.data.decode(), re.S)
    return m.group(1).strip() if m else None


def tile_value(resp, label):
    """A figure of the open KPI strip (Company Revenue, Property Profit, Occupancy, RevPAR); 'Company Net Profit' is the hero."""
    if label == "Company Net Profit":
        return hero_value(resp)
    m = re.search(rf'<span class="kpi-label">{label}.*?<span class="kpi-value">(.*?)</span>', resp.data.decode(), re.S)
    return m.group(1).strip() if m else None


def flow_of(resp):
    """The money-flow data the page draws from (the same figures as the hero and the breakdown)."""
    m = re.search(r"const FLOW = (.*?);\n", resp.data.decode(), re.S)
    return json.loads(m.group(1)) if m else None


S, E = kpis.month_bounds(2026, 9)
n = company_net_profit(c, S, E)
print("the numbers")
check("A: operated revenue counts in full (2000) and B: the managed property adds only its management fee (1500): Company Revenue 3500", n["revenue"] == 3500.0, n)
check("C: managed Gross Booking Revenue (10000) does not inflate it", n["revenue"] < 10000 and kpis.revenue(c, "mg1", S, E) == 10000.0)
check("E: operated property costs are subtracted (600, capex included)", n["operated_costs"] == 600.0, n)
check("D: the managed property's own costs (2500 cleaning + the fee row) are not subtracted", n["operated_costs"] == 600.0 and _costs(c, S, E, scope="property", model="managed") == 2500.0)
check("F: business costs are subtracted (400)", n["business_costs"] == 400.0)
check("G: Company Revenue - Operated Property Costs - Business Costs = Company Net Profit exactly (3500 - 600 - 400 = 2500)",
      n["net_profit"] == 2500.0 and n["revenue"] - n["operated_costs"] - n["business_costs"] == n["net_profit"])
adj = kpis.adjusted_kpi_snapshot(c, None, S, E)
check("L: Property Profit (the existing KPI) is unchanged: 2900 = operated profit 1400 + fee 1500, and Net Profit = Property Profit - Business Costs",
      adj["net_profit"] == 2900.0 and adj["revenue"] == 3500.0 and n["net_profit"] == adj["net_profit"] - n["business_costs"], adj)
view = targets._property_view(c, {"id": "mg1", "name": "x"}, None, None, "2026-09", "2026-09")
check("M: the raw Targets calculation is untouched (managed: guest revenue minus ALL recorded costs = 6000) -- the page no longer shows that profit for a managed property, see test_targets_redesign",
      view["metrics"]["profit"]["actual"] == 6000.0 and view["metrics"]["revenue"]["actual"] == 10000.0, view["metrics"]["profit"])

print("the Overview")
r = page("from=2026-09-01&to=2026-09-01&compare=none")
t = text(r)
h = r.data.decode()
check("the Overview hero shows Company Net Profit with the formula result", hero_value(r) == "£2,500" and "Company Net Profit" in t, hero_value(r))
check("Net Profit is not repeated in the KPI strip, which keeps Company Revenue £3,500, Property Profit £2,900 (existing meanings and values)",
      tile_value(r, "Company Revenue") == "£3,500" and tile_value(r, "Property Profit") == "£2,900" and h.count('class="kpi-label">Company Net Profit') == 0)
bd = re.search(r'id="net-profit-breakdown"[^>]*>(.*?)</div>\s*<button', h, re.S)
bt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", bd.group(1))) if bd else ""
check("G: the calculation (behind 'How this is calculated', closed by default) states Revenue - Operated Property Costs - Business Costs = Net Profit with the same figures",
      bd is not None and 'id="net-profit-breakdown" hidden' in h and re.search(r"Company Revenue £3,500 Operated Property Costs − £600 Business Costs − £400 Net Profit £2,500", bt) is not None, bt[:260])
check("the formula is not an open line on the page: it is only in the closed disclosure", "Company Net Profit £2,500 = " not in t and "Company Revenue minus operated property costs and recorded business costs." in h)
check("drilldown: revenue -> the properties behind it, operated costs -> Expenses (property scope, operated), business costs -> Expenses (business scope)",
      re.search(r'<a href="/properties[^"]*">Company Revenue</a>', bd.group(1) if bd else "") is not None
      and re.search(r'href="/expenses\?[^"]*scope=property[^"]*model=operated[^"]*"', h) is not None and re.search(r'href="/expenses\?[^"]*scope=business', h) is not None)
check("the short definition is in the info control and the calculation says managed owners' revenue and costs are not included",
      'title="Company Revenue minus operated property costs and recorded business costs."' in h and "Managed owners' revenue and costs are not included." in bt + " " + re.sub(r"<[^>]+>", " ", h) and "Based on recorded dashboard data" in h)
check("the cost breakdown shows Operated Property Costs £600 and Business Costs £400, each a link, with managed costs £2,500 kept separate and labelled not part of Net Profit",
      "Operated Property Costs £600" in t and "Business Costs £400" in t and "Managed property costs £2,500" in t and "not part of Net Profit" in t
      and re.search(r'class="cost-item" aria-label="Operated Property Costs £600', h) is not None and re.search(r'class="cost-item" aria-label="Business Costs £400', h) is not None)
f = flow_of(r)
check("the money flow carries the same figures: revenue 3500 = operated 2000 + fees 1500; costs 600 and 400; net 2500; the managed owner's 10000 is nowhere in it",
      f is not None and (f["revenue"], f["ops_rev"], f["fees"], f["operated"], f["business"], f["net"]) == (3500.0, 2000.0, 1500.0, 600.0, 400.0, 2500.0)
      and "10000" not in json.dumps(f) and "10,000" not in json.dumps(f), f and {k: f[k] for k in ("revenue", "ops_rev", "fees", "operated", "business", "net")})
nodes = {n["key"]: n for n in f["nodes"]}
check("the flow's bands are proportional to the amounts (operated 600 vs business 400 -> heights 3:2) and the Net Profit band is there when positive",
      abs(nodes["operated"]["h"] / nodes["business"]["h"] - 1.5) < 0.05 and "net" in nodes and "shortfall" not in nodes)
check("hovering a part reveals its detail: operated costs show the amount, share of revenue and the Expenses link",
      f["detail"]["operated"]["value"] == "£600" and "17% of Company Revenue" in f["detail"]["operated"]["text"] and f["detail"]["operated"]["links"][0][1].find("model=operated") > 0)

print("sign, zero, no data, period")
r = page("from=2026-10-01&to=2026-10-01&compare=none")
check("H: a negative result is signed, not only coloured: 300 + 200 fee - 700 - 900 = −£1,100", tile_value(r, "Company Net Profit") == "−£1,100", tile_value(r, "Company Net Profit"))
bt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.search(r'id="net-profit-breakdown"[^>]*>(.*?)</div>\s*<button', r.data.decode(), re.S).group(1)))
check("H: …and the calculation shows the minus sign too, and the flow shows the loss as a shortfall (costs 1600 against revenue 500)", "Net Profit −£1,100" in bt
      and flow_of(r)["loss"] and {n["key"]: n for n in flow_of(r)["nodes"]}["shortfall"]["v"] == 1100.0 and "net" not in {n["key"] for n in flow_of(r)["nodes"]})
r = page("from=2026-11-01&to=2026-11-01&compare=none")
check("I: a month with recorded data whose result is exactly zero shows £0 (and the breakdown is shown)", tile_value(r, "Company Net Profit") == "£0" and 'id="net-profit-breakdown"' in r.data.decode(), tile_value(r, "Company Net Profit"))
r = page("from=2027-03-01&to=2027-03-01&compare=previous_period")
check("J: a period with nothing recorded shows — and 'No data imported for this period.', with no breakdown and no comparison", tile_value(r, "Company Net Profit") == "—"
      and "No data imported for this period." in text(r) and 'id="net-profit-breakdown"' not in r.data.decode() and 'id="flow"' not in r.data.decode() and 'class="delta' not in r.data.decode().split('class="ov-hero"')[1].split("</section>")[0])
check("K: the period filter is respected: Sep 2,500, Oct −1,100, Sep+Oct together 1,400", tile_value(page("from=2026-09-01&to=2026-09-01&compare=none"), "Company Net Profit") == "£2,500"
      and tile_value(page("from=2026-10-01&to=2026-10-01&compare=none"), "Company Net Profit") == "−£1,100"
      and tile_value(page("from=2026-09-01&to=2026-10-01&compare=none"), "Company Net Profit") == "£1,400")
r = page("from=2026-10-01&to=2026-10-01&compare=previous_period")
check("a comparison is shown against the prior period (Sep 2,500 -> Oct −1,100) and is not hidden", "vs September" in text(r) and "Company Net Profit" in text(r))

print("single-property scope")
for pid in ("op1", "mg1"):
    r = page(f"from=2026-09-01&to=2026-09-01&property={pid}&compare=none")
    check(f"a company KPI is never shown as {pid}'s: no Company Net Profit tile or breakdown at property scope", tile_value(r, "Company Net Profit") is None and 'id="net-profit-breakdown"' not in r.data.decode())
check("the property's own Property Profit is still shown there (and the dark hero/flow is not drawn for one property)", tile_value(page("from=2026-09-01&to=2026-09-01&property=op1&compare=none"), "Property Profit") == "£1,400"
      and 'class="ov-panel"' not in page("from=2026-09-01&to=2026-09-01&property=op1&compare=none").data.decode())
check("the monthly series for the 'Show monthly trend' chart is supplied, one value per month up to the selected month (Sep, Oct, Nov)", '"company_net_profit": [2500.0, -1100.0, 0.0]' in page("from=2026-11-01&to=2026-11-01&compare=none").data.decode())

print("reports use the same definitions")
rep = text(create_app().test_client().get("/reports?type=portfolio_monthly&month=2026-09"))
check("the portfolio report calls the 2,900 figure Property Profit (it excludes Business Costs) and shows Company Net Profit 2,500 beside it; nothing is called 'Net profit'",
      "Property Profit £2,900" in rep and "Company Net Profit £2,500" in rep and "Net profit" not in rep, rep[rep.find("Financial summary"):][:300])

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
sys.exit(1 if FAILS else 0)
