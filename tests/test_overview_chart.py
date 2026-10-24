"""Overview chart system, end to end: every selectable metric (Company Revenue, Property Profit, Company Net Profit, Occupancy, RevPAR)
gets a real, non-empty series; the selector builds a sane chart config for each one; formatting, cost series, compare series and the 0-100% occupancy
axis are right; switching A -> B -> A and toggling compare/costs/range never leaves an empty or stale config.

It runs the page's REAL inline script and the REAL static/charts.js under Node, with a recording stand-in for Chart.js and a small DOM stub, and drives
the real selector click handlers. It also guards the regression behind the blank charts: charts.js must change Chart.js's default animation settings
IN PLACE (replacing the object drops Chart.js's own `type`/`fn` keys, colour animations then have no interpolator, throw "this._fn is not a function"
and leave the chart stuck mid-frame, blank on every metric switch). Scratch database only.

Run: .venv/bin/python tests/test_overview_chart.py     (needs Node; without it the suite exits 2 and reports NOT RUN)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-ovchart-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("DASH_DEMO_MODE", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))

NODE = shutil.which("node")
if not NODE:
    print("FAIL: Node is not installed -- the Overview chart suite NOT RUN.", file=sys.stderr)
    sys.exit(2)

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


def tx(pid, date, amount, direction, category):
    c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source) VALUES (?,?,?,?,?,?,0,'manual')",
              (pid, date, "row", amount, direction, category))


def stay(pid, ci, co, net):
    c.execute("INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, net_revenue, status, source) VALUES (?,?,?,?,?,?,0,?,'confirmed','manual')",
              (pid, "airbnb", f"{pid}-{ci}", ci, co, net, net))


prop("op1", "Flat 1, Operated House, London, N1 1AA")
prop("mg1", "Flat 2, Managed House, London, N2 2AA", fee=15.0)
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
# six months, with a loss month (October) so negative values are drawn; real bookings so occupancy/RevPAR/ADR are not all zero
for ym, inc, cost, mg_inc, biz, nights in (("2026-06", 1800, 500, 8000, 300, 10), ("2026-07", 2000, 600, 9000, 350, 14), ("2026-08", 2400, 700, 9500, 400, 18),
                                            ("2026-09", 2000, 600, 10000, 400, 15), ("2026-10", 300, 700, 1333, 900, 4), ("2026-11", 2200, 650, 9800, 420, 20)):
    tx("op1", f"{ym}-02", inc, "income", "booking_income")
    tx("op1", f"{ym}-05", cost, "expense", "cleaning")
    tx("mg1", f"{ym}-02", mg_inc, "income", "booking_income")
    tx("mg1", f"{ym}-02", round(mg_inc * 0.15, 2), "expense", "management_fee")
    tx("mg1", f"{ym}-06", 800, "expense", "cleaning")
    tx("general-overheads", f"{ym}-10", biz, "expense", "software")
    y, m = map(int, ym.split("-"))
    stay("op1", f"{ym}-01", f"{y}-{m:02d}-{1 + nights:02d}", inc)
    stay("mg1", f"{ym}-01", f"{y}-{m:02d}-{1 + max(nights - 2, 1):02d}", mg_inc)
c.commit()

from app import create_app  # noqa: E402
from routes.overview import company_net_profit  # noqa: E402

CHARTS_JS = (ROOT / "dashboard" / "static" / "charts.js").read_text()
METRICS = ("revenue", "net_profit", "company_net_profit", "occupancy", "revpar")
MONEY = {"revenue", "net_profit", "company_net_profit", "revpar"}

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
// ---- a recording stand-in for Chart.js: it keeps the config the page builds, and every update() snapshots it
function deep() { return new Proxy(function () {}, { get: (t, k) => (k in t ? t[k] : (t[k] = deep())), set: (t, k, v) => { t[k] = v; return true; }, apply: () => undefined }); }
class FakeChart {
  constructor(canvas, cfg) { this.canvas = canvas; this.data = cfg.data; this.options = cfg.options; this.updates = 0; FakeChart.last = this; }
  update() { this.updates++; }
}
FakeChart.defaults = deep();
// Chart.js's real default animation object carries these keys; a correct charts.js changes duration/easing IN PLACE and leaves the rest
FakeChart.defaults.animation = { delay: undefined, duration: 1000, easing: 'easeOutQuart', fn: undefined, from: undefined, loop: undefined, to: undefined, type: undefined };
FakeChart.defaults.scales = { linear: { ticks: {}, border: {} }, category: { grid: {}, border: {} } };
FakeChart.defaults.plugins = { legend: { labels: {} }, tooltip: { callbacks: {} } };
FakeChart.defaults.elements = { point: {}, line: {}, bar: {} };
FakeChart.defaults.datasets = { bar: {} };
FakeChart.defaults.font = {};
const handlers = new Map();
class El {
  constructor(id, dataset) { this.id = id; this.dataset = dataset || {}; this.hidden = false; this.style = {}; this.textContent = ''; this.innerHTML = ''; this.href = ''; this.handlers = {};
    this.classList = { toggle() {}, add() {}, remove() {} }; this.attrs = {}; }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
  setAttribute(k, v) { this.attrs[k] = v; } getAttribute(k) { return this.attrs[k]; }
  click() { (this.handlers.click || []).forEach(f => f({ preventDefault() {}, target: this })); }
  scrollIntoView() {} focus() {} closest() { return null; } querySelectorAll() { return []; }
  charAt(i) { return this.href.charAt(i); }
}
const els = {};
const getEl = (id) => (els[id] = els[id] || new El(id));
const strip = input.strip.map(m => new El('kpi-' + m, { metric: m }));
const hero = input.hero.map(m => new El('hero-' + m, { metric: m }));
const rangeBtns = ['6', '12', 'ytd'].map(r => new El('range-' + r, { range: r }));
const doc = {
  getElementById: (id) => (['flow', 'flow-readout', 'how-btn', 'net-profit-breakdown', 'prop-table'].includes(id) ? null : getEl(id)),
  querySelectorAll: (sel) => (sel === '#kpi-strip .kpi' ? strip : sel === '[data-metric="company_net_profit"]' ? hero : sel === '#range-seg button' ? rangeBtns : []),
  addEventListener() {}, body: new El('body'),
};
const ctx = vm.createContext({ Chart: FakeChart, document: doc, window: { matchMedia: () => ({ matches: false }) }, location: { pathname: '/', href: '' }, URLSearchParams, console, setTimeout, Math, JSON, Object, Number, Array, String, Date, Intl });
vm.runInContext(input.charts_js, ctx, { filename: 'charts.js' });
const animationAfter = Object.assign({}, FakeChart.defaults.animation); const animationKeys = Object.keys(FakeChart.defaults.animation);
vm.runInContext(input.page_js, ctx, { filename: 'overview-inline.js' });
const chart = FakeChart.last;
const snap = () => JSON.parse(JSON.stringify({ labels: chart.data.labels, ds: chart.data.datasets.map(d => ({ label: d.label, data: d.data, n: d.data.length })) }));
const evalIn = (code) => vm.runInContext(code, ctx);
const out = { animationAfter, animationKeys, initialMetric: evalIn('metric'), S_keys: Object.keys(evalIn('S')), compare: evalIn('COMPARE'), results: {}, sequence: [], toggles: {} };
const pick = (m) => (strip.find(b => b.dataset.metric === m) || hero.find(b => b.dataset.metric === m));
function describe(m) {
  const s = snap(), y = chart.options.scales.y, ticks = y.ticks.callback;
  const label = chart.options.plugins.tooltip.callbacks.label;
  const v0 = s.ds[0].data, last = v0.length - 1;
  return {
    title: getEl('chart-title').textContent, labels: s.labels, series: v0, costs: s.ds[1].data, costLabel: s.ds[1].label, cmp: s.ds[2].data, cmpCosts: s.ds[3].data,
    chipHidden: getEl('costs-chip').hidden, yMin: y.min, yMax: y.max, tick50: ticks(50), tickNeg: ticks(-5000),
    tipValue: label({ dataset: { label: s.ds[0].label }, datasetIndex: 0, raw: v0[last] }),
    tipCost: s.ds[1].data.length ? label({ dataset: { label: s.ds[1].label }, datasetIndex: 1, raw: s.ds[1].data[last] }) : null,
    key: getEl('chart-key').innerHTML, open: getEl('chart-open').href, updates: chart.updates,
  };
}
for (const m of input.order) { pick(m).click(); out.results[m] = describe(m); }
// switching A -> B -> A must give back exactly the first config (no stale dataset)
for (const m of ['revenue', 'occupancy', 'revenue', 'revpar', 'net_profit', 'company_net_profit', 'occupancy']) { pick(m).click(); out.sequence.push([m, JSON.stringify(describe(m).series)]); }
// costs chip off / on, and the range buttons
pick('revenue').click();
getEl('costs-chip').click(); out.toggles.costsOff = snap().ds[1].data.length; getEl('costs-chip').click(); out.toggles.costsOn = snap().ds[1].data.length;
for (const r of rangeBtns) { r.click(); out.toggles['range_' + r.dataset.range] = snap().labels.length; }
console.log(JSON.stringify(out));
"""


def run_page(url):
    """Renders the Overview, then runs its own inline script + charts.js under Node and returns what the selector produced for every metric."""
    html = create_app().test_client().get(url).get_data(as_text=True)
    scripts = [m for m in re.findall(r"<script>(.*?)</script>", html, flags=re.S) if "const INFO" in m]
    assert len(scripts) == 1, f"expected exactly one Overview chart script, found {len(scripts)}"
    strip = re.findall(r'<button[^>]*class="kpi[^"]*"[^>]*data-metric="(\w+)"', html)
    hero = ["company_net_profit"] if 'data-metric="company_net_profit"' in html.split('id="kpi-strip"')[0] else []
    payload = TMP / "payload.json"
    payload.write_text(json.dumps({"charts_js": CHARTS_JS, "page_js": scripts[0], "strip": strip, "hero": hero, "order": [m for m in METRICS if m in strip + hero]}))
    harness = TMP / "harness.js"
    harness.write_text(HARNESS)
    proc = subprocess.run([NODE, str(harness), str(payload)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-1500:])
    return (json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else None), html, strip, hero


def finite(a):
    return [v for v in a if isinstance(v, (int, float)) and v == v]


Q = "from=2026-11-01&to=2026-11-01&property=all"
out, html, strip, hero = run_page(f"/?{Q}&compare=previous_period")
check("the Overview chart script runs under the real charts.js without throwing", out is not None)
if out is None:
    print("cannot continue"); sys.exit(1)

print("the metric selector exposes every metric")
check("the KPI strip offers Company Revenue, Property Profit, Occupancy and RevPAR, and Company Net Profit is selectable from the hero", strip == ["revenue", "net_profit", "occupancy", "revpar"] and hero == ["company_net_profit"], (strip, hero))
check("the page data has a series for every metric plus both cost series", {"months", "revenue", "costs", "net_profit", "occupancy", "revpar", "company_net_profit", "company_costs"} <= set(out["S_keys"]), out["S_keys"])

S = json.loads(re.search(r"const S = (\{.*?\});", html, flags=re.S).group(1))
conn = db.get_conn()
months = S["months"]
check("the page has six monthly points ending November 2026", months == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"], months)
for k in ("revenue", "costs", "net_profit", "occupancy", "revpar", "company_net_profit", "company_costs"):
    check(f"data series '{k}': one value per month, none null/NaN, not all zero", len(S[k]) == len(months) and len(finite(S[k])) == len(months) and any(abs(v) > 0 for v in S[k]), S[k])

print("each series equals the established KPI function for that month (no formula was touched)")
y0, m0 = 2026, 11
s0, e0 = kpis.month_bounds(y0, m0)
adj = kpis.adjusted_kpi_snapshot(conn, None, s0, e0)
co = company_net_profit(conn, s0, e0)
check("November Company Revenue / Property Profit / Occupancy / RevPAR match adjusted_kpi_snapshot", abs(S["revenue"][-1] - adj["revenue"]) < 0.01 and abs(S["net_profit"][-1] - adj["net_profit"]) < 0.01
      and abs(S["occupancy"][-1] - adj["occupancy"] * 100) < 0.1 and abs(S["revpar"][-1] - adj["revpar"]) < 0.01, (S["revenue"][-1], adj["revenue"]))
check("November Company Net Profit = company_net_profit (Revenue − Operated Property Costs − Business Costs)", abs(S["company_net_profit"][-1] - co["net_profit"]) < 0.01
      and abs(co["net_profit"] - (co["revenue"] - co["operated_costs"] - co["business_costs"])) < 0.01, (S["company_net_profit"][-1], co))
check("the loss month (October) is negative in Company Net Profit, so negative bars are exercised", S["company_net_profit"][4] < 0, S["company_net_profit"][4])
check("occupancy is a real, varying percentage", len({round(v) for v in S["occupancy"]}) > 3 and all(0 < v <= 100 for v in S["occupancy"]), S["occupancy"])
check("a managed property's guest revenue is not in Company Revenue (the chart's revenue is the adjusted figure)", S["revenue"][-1] < 9800 + 2200 and abs(S["revenue"][-1] - adj["revenue"]) < 0.01)

print("the selector builds a correct chart config for every metric")
R = out["results"]
for m in METRICS:
    r = R[m]
    check(f"{m}: the chart gets {len(months)} labels and a non-empty main series (no null/NaN)", r["labels"] == months and len(finite(r["series"])) == len(months) and len(r["series"]) == len(months), r["series"])
    check(f"{m}: first and last plotted values are the data's own first and last", r["series"][0] == S[m][0] and r["series"][-1] == S[m][-1])
    check(f"{m}: the title and the Open-records link change with the selection", r["title"] == {"revenue": "Company Revenue", "net_profit": "Property Profit", "company_net_profit": "Company Net Profit", "occupancy": "Occupancy", "revpar": "RevPAR"}[m] and r["open"].startswith("/"), (r["title"], r["open"]))
    check(f"{m}: the chart updated (not left at its empty first state)", r["updates"] >= 1)
for m in MONEY:
    r = R[m]
    check(f"{m}: £ formatting on the axis and in the tooltip", r["tick50"] == "£50" and r["tickNeg"].startswith("−£") and r["tipValue"].split(": ")[1].lstrip("−").startswith("£") and "%" not in r["tipValue"], (r["tick50"], r["tickNeg"], r["tipValue"]))
check("occupancy: percentage formatting on the axis and in the tooltip, never £", R["occupancy"]["tick50"] == "50%" and "£" not in R["occupancy"]["tipValue"] and R["occupancy"]["tipValue"].endswith("%"), R["occupancy"])
check("occupancy: the axis runs 0-100%", R["occupancy"]["yMin"] == 0 and R["occupancy"]["yMax"] == 100, (R["occupancy"]["yMin"], R["occupancy"]["yMax"]))
check("the money charts keep automatic axes (negative bars must stay visible)", all(R[m].get("yMin") is None and R[m].get("yMax") is None for m in MONEY), {m: (R[m].get("yMin"), R[m].get("yMax")) for m in MONEY})
check("a negative amount reads with a true minus sign (−£5k), never £-5k", R["company_net_profit"]["tickNeg"] == "−£5k")

print("costs appear only where designed")
for m, label in (("revenue", "Operated Property Costs"), ("net_profit", "Operated Property Costs"), ("company_net_profit", "Operated + Business Costs")):
    r = R[m]
    check(f"{m}: a costs series is plotted below zero, labelled '{label}'", len(r["costs"]) == len(months) and all(v <= 0 for v in r["costs"]) and any(v < 0 for v in r["costs"]) and r["costLabel"] == label and not r["chipHidden"], (r["costLabel"], r["costs"]))
check("revenue/profit costs are the operated-property costs series; net profit's are the operated + business series", [-v for v in R["revenue"]["costs"]] == S["costs"] and [-v for v in R["company_net_profit"]["costs"]] == S["company_costs"])
for m in ("occupancy", "revpar"):
    check(f"{m}: no cost series and the Costs chip is hidden", R[m]["costs"] == [] and R[m]["chipHidden"] is True and R[m]["tipCost"] is None, R[m]["costs"])
check("the Costs chip hides and restores the cost series", out["toggles"]["costsOff"] == 0 and out["toggles"]["costsOn"] == len(months), out["toggles"])

print("compare series")
shift = 1
for m in METRICS:
    r = R[m]
    cmp_ = r["cmp"]
    check(f"{m}: previous-period compare has one point per month, the first is blank (nothing before it) and the rest are the prior month's real values", len(cmp_) == len(months) and cmp_[0] is None and cmp_[1:] == S[m][:-1], cmp_)
for m in ("revenue", "net_profit", "company_net_profit"):
    check(f"{m}: the compare costs series is plotted below zero as well", len(R[m]["cmpCosts"]) == len(months) and all((v is None) or v <= 0 for v in R[m]["cmpCosts"]))
for m in ("occupancy", "revpar"):
    check(f"{m}: no compare-costs series", R[m]["cmpCosts"] == [])
check("the legend names the compare series", "Previous period" in R["revenue"]["key"])

for mode in ("none", "previous_year"):
    o2, _h, _s, _g = run_page(f"/?{Q}&compare={mode}")
    check(f"compare={mode}: the page script still runs", o2 is not None)
    if o2 is None:
        continue
    r2 = o2["results"]
    if mode == "none":
        check("compare off: no compare series, and every metric still has its main series", all(r2[m]["cmp"] == [] and r2[m]["cmpCosts"] == [] and len(finite(r2[m]["series"])) == len(months) for m in METRICS), {m: r2[m]["cmp"] for m in METRICS})
        check("compare off: the legend does not mention a comparison", "Previous" not in r2["revenue"]["key"])
    else:
        check("previous year with under a year of history: the compare series exist but are blank, never invented, and the main series is unaffected", all(len(r2[m]["cmp"]) == len(months) and all(v is None for v in r2[m]["cmp"]) and len(finite(r2[m]["series"])) == len(months) for m in METRICS))

print("switching never leaves an empty or stale chart")
seq = dict()
for m, series in out["sequence"]:
    seq.setdefault(m, []).append(series)
check("A -> B -> A: returning to Revenue / Occupancy gives exactly the first dataset again", len(set(seq["revenue"])) == 1 and len(set(seq["occupancy"])) == 1 and json.loads(seq["revenue"][0]) == S["revenue"] and json.loads(seq["occupancy"][0]) == S["occupancy"])
check("every step of a mixed switching sequence has a full, non-empty series", all(len(finite(json.loads(s))) == len(months) for _m, s in out["sequence"]))
check("the range buttons give 6 / 6 / 12 months here (6M, YTD of 2026 = all six, 12M = all six) without emptying the chart", out["toggles"]["range_6"] == 6 and out["toggles"]["range_ytd"] == 6 and out["toggles"]["range_12"] == 6, out["toggles"])

print("the shared root cause: charts.js must not replace Chart.js's default animation object")
anim = out["animationAfter"]
check("after charts.js runs, the default animation still has Chart.js's own keys (type, fn, from, to, loop, delay)", all(k in out["animationKeys"] for k in ("type", "fn", "from", "to", "loop", "delay")), out["animationKeys"])
check("…and the app's duration/easing are applied to it", anim["duration"] == 260 and anim["easing"] == "easeOutCubic", anim)
bad = []
for path in list((ROOT / "dashboard" / "static").glob("*.js")) + list((ROOT / "dashboard" / "templates").rglob("*.html")):
    if re.search(r"Chart\.defaults\.animation\s*=(?!=)", path.read_text()):
        bad.append(str(path.relative_to(ROOT)))
check("no script or template anywhere assigns a new object to Chart.defaults.animation (it would break every chart page, not only the Overview)", not bad, bad)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
if FAILS:
    print("FAILED:", *FAILS, sep="\n  - ")
    sys.exit(1)
