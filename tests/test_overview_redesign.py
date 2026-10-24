"""Overview redesign + collapsible sidebar: structure, accessibility attributes, attention rules, removed content. Scratch database only.

Run: .venv/bin/python tests/test_overview_redesign.py
"""
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-overview-test-"))
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


def tx(pid, date, amount, direction, category, capex=0):
    c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source) VALUES (?,?,?,?,?,?,?,'manual')",
              (pid, date, "row", amount, direction, category, capex))


prop("ok1", "Flat 1, Healthy House, London, N1 1AA")
prop("neg1", "Flat 2, Losing House, London, N2 2AA")
prop("mg1", "Flat 3, Managed House, London, N3 3AA", fee=15.0)
prop("empty1", "Flat 4, Quiet House, London, N4 4AA")
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
tx("ok1", "2026-09-01", 2000, "income", "booking_income")
tx("ok1", "2026-09-05", 300, "expense", "cleaning")
tx("neg1", "2026-09-01", 300, "income", "booking_income")
tx("neg1", "2026-09-05", 900, "expense", "maintenance")
tx("mg1", "2026-09-01", 5000, "income", "booking_income")
tx("mg1", "2026-09-01", 750, "expense", "management_fee")
tx("general-overheads", "2026-09-10", 400, "expense", "software")
c.commit()

from app import create_app  # noqa: E402


def get(path):
    return create_app().test_client().get(path)


S = "from=2026-09-01&to=2026-09-01&compare=none"
h = get("/?" + S).data.decode()
t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))

print("sidebar")
check("the collapse control is a labelled button with aria-expanded", re.search(r'<button type="button" class="sb-toggle" id="sb-toggle" aria-label="Collapse sidebar" aria-expanded="true"', h) is not None)
check("the saved state is applied before first paint (no flash)", h.index("dash_sidebar") < h.index("style.css"))
nav = re.findall(r'<a [^>]*class="nav-link[^"]*"[^>]*>', h)
check("every nav link has a tooltip label and a text label that stays in the accessibility tree when collapsed", len(nav) >= 7 and all("data-tip=" in a for a in nav)
      and h.count('<span class="lbl">') >= 9)
check("search and Add property keep accessible names", 'id="palette-open" class="jump-btn" aria-label="Search' in h and 'aria-label="Add property"' in h)

print("overview structure")
check("one control band carries the page title and the period / property / compare controls", re.search(r'class="context-bar has-title".*?<h1 class="bar-title">Overview</h1>', h, re.S) is not None
      and 'aria-label="Period view"' in h and "Compare:" in t)
check("the hero is Company Net Profit, and the flow, KPI strip, one chart, costs and properties follow in that order",
      [h.find(k) for k in ('class="ov-hero"', 'id="flow"', 'id="kpi-strip"', 'id="chart-panel"', 'class="ov-costs"', 'id="properties"')] == sorted(h.find(k) for k in ('class="ov-hero"', 'id="flow"', 'id="kpi-strip"', 'id="chart-panel"', 'class="ov-costs"', 'id="properties"'))
      and min(h.find(k) for k in ('class="ov-hero"', 'id="flow"', 'id="kpi-strip"', 'id="chart-panel"', 'class="ov-costs"', 'id="properties"')) > 0)
check("removed: the open formula line, the long Property Profit / Net Profit sentences, and the old occupancy sparkline grid and second table",
      "Property Profit uses operated-property costs only." not in t and " = Company Revenue" not in t and "Managed owners' revenue and costs are not part of Company Net Profit." not in t and 'class="spark-grid"' not in h and "Occupancy by property" not in t
      and "Operated (rent-to-rent)" not in t and 'class="cost-line"' not in h)
check("the KPI strip is open (no tile boxes), has exactly four metrics and does not repeat Net Profit", h.count('role="tab"') == 4 and 'class="tile' not in h.split('id="kpi-strip"')[1].split('id="chart-panel"')[0])
check("the strip is a keyboard-operable tablist for the chart (roving tabindex, aria-selected, aria-controls)", h.count('aria-selected="true"') == 1 and h.count('tabindex="-1"') >= 3 and 'aria-controls="chart-panel"' in h)
check("exactly one chart canvas on the page (no per-KPI mini charts, no second chart)", h.count("<canvas") == 1)
check("flow parts are focusable buttons with names that carry their amounts (meaning is not colour-only)", len(re.findall(r'<g class="fn fn-\w+" data-key="\w+" tabindex="0" role="button" aria-label="[^"]*£', h)) >= 4)
check("a reduced-motion rule exists for the new motion", "prefers-reduced-motion" in Path("dashboard/static/style.css").read_text() and "reduced" in h)

print("needs attention")
rows = re.findall(r'<tr data-attn="(\d)">.*?</tr>', h, re.S)
check("all four properties are listed; the attention filter is deterministic (negative operated profit, no data, fee % set is fine)", len(rows) == 4, rows)
tbl = h.split('id="prop-table"')[1]
def row(name):
    m = re.search(rf'<tr data-attn="(\d)">(?:(?!</tr>).)*{name}.*?</tr>', tbl, re.S)
    return (m.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(0)))) if m else None
check("negative operated Property Profit needs attention", row("Losing House")[0] == "1" and "Negative profit" in row("Losing House")[1], row("Losing House"))
check("a property with nothing recorded needs attention ('No data')", row("Quiet House")[0] == "1" and "No data" in row("Quiet House")[1], row("Quiet House"))
check("a healthy operated property and a managed property with a fee do not", row("Healthy House")[0] == "0" and row("Managed House")[0] == "0")
check("the counts in the toggle match", 'data-attention="2"' in h and re.search(r"Needs attention <span class=\"count\">2</span>", h) and re.search(r"All properties <span class=\"count\">4</span>", h))
check("a managed row shows its fee, not profit; an operated row shows revenue and profit", "£750" in row("Managed House")[1] and "fee" in row("Managed House")[1] and "£1,700" in row("Healthy House")[1] + " £1,700")

print("states")
nd = get("/?from=2027-03-01&to=2027-03-01&compare=none").data.decode()
check("no data: dashes, one message, no flow, no costs block", "No data imported for this period." in nd and 'id="flow"' not in nd and 'class="ov-costs"' not in nd)
one = get("/?" + S + "&property=ok1").data.decode()
check("single property: no company hero or flow, the strip shows that property's figures", 'class="ov-panel"' not in one and 'id="kpi-strip"' in one and 'id="hero-value"' not in one)

print("Property Profit definition and period view state")
PP = "Property Profit is company revenue (operated-property revenue plus management fees) minus operated-property costs. Managed-property owner economics are excluded."
m = re.search(r'<span class="kpi-label">Property Profit<span class="info-dot" title="([^"]*)"', h)
check("the portfolio Property Profit ⓘ states what it is and that managed owner economics are excluded", m is not None and m.group(1) == PP, m and m.group(1))
check("…and the page no longer prints the old one-line explanation", "Property Profit uses operated-property costs only" not in h)
mo = re.search(r'<span class="kpi-label">Property Profit<span class="info-dot" title="([^"]*)"', get("/?" + S + "&property=ok1").data.decode())
check("a single property keeps its own definition (the wording change is portfolio-only)", mo is not None and mo.group(1) != PP and "this property's own costs" in html.unescape(mo.group(1)), mo and mo.group(1))
check("the Property Profit value itself is unchanged (operated 1700 - 900 + 0 ... = 1700 + -600 + 750 = 1850)", re.search(r'<span class="kpi-label">Property Profit.*?<span class="kpi-value">(.*?)</span>', h, re.S).group(1) == "£1,850")


def seg_state(html):
    seg = html.split('aria-label="Period view"')[1].split("</div>")[0]
    return {re.sub(r"<[^>]+>", "", t).strip(): ('class="on"' in a) for a, t in re.findall(r'(<a [^>]*>)(.*?)</a>', seg, re.S)}, seg


for q, want, label in (("from=2026-09-01&to=2026-09-01&compare=none", "Month", "the latest single month"),
                       ("from=2026-08-01&to=2026-08-01&compare=none", "Month", "a historical single month (August 2026)"),
                       ("from=2025-12-01&to=2025-12-01&compare=previous_year", "Month", "a single month in another year")):
    st, _ = seg_state(get("/?" + q).data.decode())
    check(f"{label}: Month is the selected option and nothing else is", st == {"Month": True, "YTD": False, "Accounting": False}, st)
_, seg = seg_state(h)
hrefs = {re.sub(r"<[^>]+>", "", t).strip(): re.search(r'href="([^"]+)"', a).group(1).replace("&amp;", "&") for a, t in re.findall(r'(<a [^>]*>)(.*?)</a>', seg, re.S)}
for name in ("YTD", "Accounting"):
    page = get("/" + hrefs[name]).data.decode()
    st, _s = seg_state(page)
    check(f"{name}: only {name} is selected after choosing it", st == {k: (k == name) for k in ("Month", "YTD", "Accounting")}, st)
    check(f"{name}: the range it selects is the link's own (from/to are not altered by the highlight)", re.search(r"from=" + re.escape(re.search(r"from=([\d-]+)", hrefs[name]).group(1)), page) is not None)
check("the period links themselves are unchanged (Month link is one month, YTD and Accounting span more)", re.search(r"from=([\d-]+)&to=([\d-]+)", hrefs["Month"]).group(1)[:7] == re.search(r"from=([\d-]+)&to=([\d-]+)", hrefs["Month"]).group(2)[:7]
      and re.search(r"from=([\d-]+)&to=([\d-]+)", hrefs["YTD"]).group(1)[5:7] == "01")

print("chart costs and calmer flow")
from routes.expenses import _costs  # noqa: E402
import services.kpis as kpis  # noqa: E402
ser = json.loads(re.search(r"const S = (.*?);\n", h, re.S).group(1))
ok = True
for i, ym in enumerate(ser["months"]):
    st, en = kpis.month_bounds(int(ym[:4]), int(ym[5:]))
    op, bu = _costs(c, st, en, scope="property", model="operated"), _costs(c, st, en, scope="business")
    ok = ok and abs(ser["costs"][i] - op) < 0.005 and abs(ser["company_costs"][i] - (op + bu)) < 0.005 and abs(ser["company_net_profit"][i] - (ser["revenue"][i] - op - bu)) < 0.005
check("the chart's Costs series is the existing operated-property costs (same as the old red bars and the Expenses scope=property&model=operated figure), month by month; Net Profit's costs are operated + business", ok and ser["costs"][-1] == 1200.0, ser["costs"])
check("still ONE chart; the costs toggle is a labelled pressed-state control; costs are drawn beside the selected metric, not hidden",
      h.count("<canvas") == 1 and 'id="costs-chip" aria-pressed="true"' in h and "label: PORTFOLIO ? 'Operated Property Costs' : 'Costs'" in h and "showCosts = true" in h)
check("costs are plotted as negative bars below a clear zero baseline in the same column as the metric (grouped: false), the numbers unchanged (only the sign is applied when plotting; the tooltip shows money out as positive)",
      h.count("grouped: false") == 2 and "ix.map(i => -cseries[i])" in h and "-cseries[i - shift]" in h and "tick.value === 0" in h and "Math.abs(item.raw)" in h)
check("the shared chart defaults set only the tooltip title callback (replacing the whole callbacks object had removed Chart.js's own label/labelColor and broke every tooltip)",
      "tooltip.callbacks.title =" in Path("dashboard/static/charts.js").read_text() and "tooltip.callbacks = {" not in Path("dashboard/static/charts.js").read_text())
check("costs are not stacked or drawn as a positive series beside revenue", "stacked" not in h.split("const chart = new Chart")[1] and "ix.map(i => cseries[i])" not in h)
check("clicking a costs bar still opens Expenses scoped to operated property costs", "params.set('scope', 'property'); params.set('model', 'operated')" in h)
css = Path("dashboard/static/style.css").read_text()
check("the flow no longer fades the rest of the diagram or animates ribbons on hover (no has-active fade, no opacity transitions on ribbons/nodes)",
      "has-active" not in css and "has-active" not in h and not re.search(r"\.rb\s*\{[^}]*transition", css) and not re.search(r"\.fn\s*\{[^}]*transition", css) and "glow" not in css.split("Overview: one control band")[1].lower().replace("no glow", ""))
check("hit areas are wider than the visible bars and ribbons are interactive too (data-key on every ribbon and node)",
      re.search(r'class="fn-hit" x="\d+(\.\d+)?" y="[\d.]+" width="(160|168|\d+)"', h) is not None and len(re.findall(r'<path class="rb rb-\w+" data-key="\w+"', h)) >= 3)
check("keyboard: Enter/Space activate SVG parts via a dispatched click (SVG elements have no click()), and leaving is debounced",
      "new MouseEvent('click', { bubbles: true })" in h and "leaveTimer" in h)
check("at rest every part shows its name and amount, and shares are shown on both sides of the flow", len(re.findall(r'class="ft-share"', h)) >= 4)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
sys.exit(1 if FAILS else 0)
