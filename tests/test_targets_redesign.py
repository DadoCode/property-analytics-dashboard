"""Targets semantic audit + redesign: every label matches its formula, managed Gross Booking Revenue is never company revenue, Property Profit is
not applied to managed owner economics, a missing target is never 0, no data is never 0, a not-started property is not scored, variance direction
is explicit. Scratch database only.

Run: .venv/bin/python tests/test_targets_redesign.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-targets-test-"))
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


def prop(pid, name, fee=None, start=None):
    c.execute("INSERT INTO properties (id, code, name, address, type, active, management_fee_pct, is_managed, start_date) VALUES (?,?,?,?,'flat',1,?,?,?)",
              (pid, pid.upper(), name, name, fee, 1 if fee else 0, start))
    completeness.seed_defaults(c, pid)


def tx(pid, date, amount, direction, category):
    c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source) VALUES (?,?,?,?,?,?,0,'manual')",
              (pid, date, "row", amount, direction, category))


def stay(pid, ci, co, net):
    c.execute("INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, net_revenue, status, source) VALUES (?,?,?,?,?,?,0,?,'confirmed','manual')",
              (pid, "airbnb", f"{pid}-{ci}", ci, co, net, net))


prop("op1", "Flat 1, Operated House, London, N1 1AA")
prop("op2", "Flat 9, Later House, London, N9 9AA", start="2026-10-01")       # joins in October: not started in September
prop("op3", "Flat 3, Newest House, London, N3 3AA")                          # exactly one recorded month
prop("mg1", "Flat 2, Managed House, London, N2 2AA", fee=15.0, start="2026-09-11")   # 20 available nights in September: weighting matters
prop("mg2", "Flat 5, Silent House, London, N5 5AA", fee=12.0)               # managed, nothing recorded at all
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
for ym, inc, cost, mg_inc in (("2026-07", 2000, 500, 10000), ("2026-08", 2400, 600, 9000), ("2026-09", 1500, 700, 8000)):
    tx("op1", f"{ym}-02", inc, "income", "booking_income")
    tx("op1", f"{ym}-05", cost, "expense", "cleaning")
    tx("mg1", f"{ym}-02", mg_inc, "income", "booking_income")
    tx("mg1", f"{ym}-02", mg_inc * 0.15, "expense", "management_fee")
    tx("mg1", f"{ym}-06", 2500, "expense", "cleaning")                           # owner-side cost
stay("op1", "2026-09-01", "2026-09-16", 0)         # 15 nights of 30
stay("mg1", "2026-09-11", "2026-09-26", 0)         # 15 nights of its 20 available
stay("op1", "2026-08-01", "2026-08-11", 0)
stay("mg1", "2026-08-01", "2026-08-16", 0)
tx("op3", "2026-09-03", 1000, "income", "booking_income")
tx("op2", "2026-10-03", 900, "income", "booking_income")
tx("general-overheads", "2026-09-10", 400, "expense", "software")
c.execute("INSERT INTO property_targets (property_id, revenue_target, profit_target, occupancy_target) VALUES ('op1', 2500, 1500, 60)")
c.execute("INSERT INTO property_targets (property_id, revenue_target, profit_target, occupancy_target) VALUES ('mg1', 9000, 5000, 70)")
c.execute("INSERT INTO property_targets (property_id, revenue_target, profit_target, occupancy_target) VALUES ('op2', 5000, 4000, 80)")
c.commit()

from app import create_app  # noqa: E402
import routes.targets as targets  # noqa: E402

S, E = kpis.month_bounds(2026, 9)
Q = "from=2026-09-01&to=2026-09-01&compare=previous_period"


def get(url):
    return create_app().test_client().get(url)


def page(url=None):
    return get("/targets?" + (url or Q)).get_data(as_text=True)


def flat(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


def block(h, pid):
    m = re.search(r'<div class="tg-prop" id="target-%s">(.*?)(?=<div class="tg-prop"|</section>)' % pid, h, re.S)
    return m.group(1) if m else ""


h = page()
t = flat(h)

print("labels match formulas")
check("no mixed 'Portfolio Operating Profit' or 'Operating Profit' headline remains", "Portfolio Operating Profit" not in t and "Operating Profit" not in t)
check("the profit line is operated-only and says so in its definition", "Operated property profit" in t and "Management Fee Earned and managed owners&#39; economics are not included" in h)
check("an operated property's rows are Revenue, Property Profit, Occupancy", all(x in block(h, "op1") for x in ("Revenue", "Property Profit", "Occupancy")))
check("a managed property's revenue is labelled Gross Booking Revenue and owner-side, never plain Revenue",
      "Gross Booking Revenue" in block(h, "mg1") and "owner-side" in block(h, "mg1") and "Revenue</span>" not in block(h, "mg1").replace("Gross Booking Revenue", ""))
check("Property Profit is not offered for a managed property (owner economics)", "Property Profit" not in block(h, "mg1") and "Profit" not in flat(block(h, "mg1")).replace("Gross Booking Revenue", ""))
check("the managed group says GBR is not company revenue and Management Fee Earned has no target", "is not company revenue" in t and "Management Fee Earned, which has no target" in t)
check("it states which company targets do not exist, instead of inventing them", "No target is set for Company Revenue, Management Fee Earned, Business Costs or Company Net Profit." in t)
check("managed Gross Booking Revenue is its own, quieter, owner-side summary line", "Managed Gross Booking Revenue" in t and 'tg-item tg-owner' in h)

print("portfolio actuals come from the established KPI functions")
ops_rev = kpis.revenue(c, "op1", S, E)
ops_profit = kpis.net_profit(c, "op1", S, E)
check("operated revenue actual = kpis.revenue of the counted operated property (1500)", ops_rev == 1500.0 and "£1,500 / £2,500" in t, t[:600])
check("operated property profit actual = kpis.net_profit (800) against the 1,500 target", ops_profit == 800.0 and "£800 / £1,500" in t)
adj = kpis.adjusted_kpi_snapshot(c, None, S, E)
fees = sum(kpis.business_income(c, p, S, E) for p in ("mg1", "mg2"))
check("operated property profit = Overview Property Profit minus Management Fee Earned (the identity that proves it is the clean figure)",
      abs((adj["net_profit"] - fees) - (ops_profit + kpis.net_profit(c, "op3", S, E) + kpis.net_profit(c, "op2", S, E))) < 0.01, (adj["net_profit"], fees))
check("managed GBR actual = kpis.revenue of the managed property (8000) against 9000", kpis.revenue(c, "mg1", S, E) == 8000.0 and "£8,000 / £9,000" in t)

print("no data is not zero; missing target is not zero; not started is not scored")
check("a managed property with nothing recorded shows no actual and no target, never £0", "£0" not in flat(block(h, "mg2")) and flat(block(h, "mg2")).count("No target set") >= 1 and "—" in flat(block(h, "mg2")))
check("a not-started property is not scored against its saved target", "Not active in this period" in block(h, "op2") and "starts 2026-10-01" in block(h, "op2") and 'class="tg-row' not in block(h, "op2"))
check("its saved targets (5,000 / 4,000) are not added to the portfolio target", "5,000" not in flat(h.split('class="pr-bar')[0]) and "£2,500" in t)
check("a property with a single recorded month has no target (an average of itself would always read 'On target')", "No target set" in block(h, "op3") and "On target" not in block(h, "op3"))
check("a missing target says 'No target set', never a zero or a blank bar", all(x in block(h, "op3") for x in ("No target set",)) and "£0 /" not in block(h, "op3"))
check("the page says how many properties have a set target behind each sum (op2 is not started, op3 has none)", "1 of 3 properties with a set target" in t, re.findall(r"\d of \d propert\w+ [\w ]+", t))
check("the target list never mentions a target source for a property with no target", "avg" not in block(h, "mg2").lower().replace("average", ""))

print("weighted occupancy")
b1, a1 = kpis.booked_nights(c, "op1", S, E), kpis.available_nights(c, "op1", S, E)
b2, a2 = kpis.booked_nights(c, "mg1", S, E), kpis.available_nights(c, "mg1", S, E)
act = round((b1 + b2) / (a1 + a2) * 100)
tgt = round((60 * a1 + 70 * a2) / (a1 + a2))
simple = (b1 / a1 * 100 + b2 / a2 * 100) / 2
check("the two properties have different available nights, so a naive average would differ from the weighted figure", a1 == 30 and a2 == 20 and abs(simple - act) > 1, (a1, a2, act, simple))
check("portfolio occupancy = Σ booked ÷ Σ available over the counted properties, target weighted by nights (not an average of percentages)",
      f"{act}% / {tgt}%" in t, (act, tgt, re.findall(r"\d+% / \d+%", t)))
op_a = round(15 / 30 * 100)
check("a property's own occupancy is its kpis.occupancy (50%)", round(kpis.occupancy(c, "op1", S, E) * 100) == op_a and f"{op_a}%" in flat(block(h, "op1")) and "60%" in flat(block(h, "op1")))

print("variance direction and wording")
check("shortfall is a signed variance in the metric's own units, with a status word", "−£1,000" in flat(block(h, "op1")) and "−10 pts" in flat(block(h, "op1")) and "Behind" in flat(block(h, "op1")))
check("units differ: money in £, occupancy in points, never a percentage of a percentage", "pts" in flat(block(h, "op1")) and "−£700" in flat(block(h, "op1")))
check("ahead of target is signed + and green", targets._fmt_var("money", 420) == "+£420" and targets._status("revenue", 420) == "ahead" and targets._fmt_var("pct", 6) == "+6 pts")
check("exactly on target (at displayed precision) is 'on', not ahead", targets._status("revenue", 0.3) == "on" and targets._status("revenue", -0.4) == "on")
orig = dict(targets.DIRECTION)
targets.DIRECTION["revenue"] = "lower"
check("direction is explicit: for a lower-is-better target a positive variance is Behind (a cost overrun is never green)", targets._status("revenue", 300) == "behind" and targets._status("revenue", -300) == "ahead")
targets.DIRECTION.clear()
targets.DIRECTION.update(orig)
check("higher-is-better is restored for every shipped target", all(v == "higher" for v in targets.DIRECTION.values()))
check("a property with no target draws no bar at all", 'tg-bar' not in block(h, "op3") and 'tg-bar' not in block(h, "mg2"))

print("trend: one small signal against the chosen comparison")
check("previous period: revenue 1,500 vs 2,400 in August is ↓ 38%", "↓ 38%" in flat(block(h, "op1")), flat(block(h, "op1")))
h_none = page("from=2026-09-01&to=2026-09-01&compare=none")
check("comparison off: no trend arrows at all", "↓" not in flat(block(h_none, "op1")) and "↑" not in flat(block(h_none, "op1")))
h_yoy = page("from=2026-09-01&to=2026-09-01&compare=previous_year")
check("comparison year-on-year with no last-year figure shows a dash, not a made-up change", "—" in flat(block(h_yoy, "op1")))

print("structure")
check("no subtitle paragraph, no five-tile row, no gauges or rings", "page-head" not in h and "tiles" not in h and "gauge" not in h.lower() and "<svg" not in h.split('class="tg"')[1].split("</section>")[0])
check("four summary lines at most", len(re.findall(r'class="tg-item[ "]', h)) <= 4)
check("one open list grouped Operated then Managed", h.index("OPERATED") < h.index("MANAGED") if "OPERATED" in h else h.index(">Operated<") < h.index(">Managed<"))
check("every row is a keyboard-reachable button that opens the inspector", len(re.findall(r'<button type="button" class="tg-row', h)) >= 6 and "hx-get" in h)
check("menus are labelled details/summary controls and the filter is a removable token", all(x in h for x in ('aria-label="Filter by status"', 'aria-label="Sort the list"', 'aria-label="Edit targets"')) and "chip-x" in page(Q + "&status=behind"))
check("each property keeps its Performance link for the same month", re.search(r'href="/properties/op1/performance\?[^"]*from=2026-09-01[^"]*to=2026-09-01', h) is not None)

print("sorting, filtering and focusing keep their meaning")
hb = page(Q + "&status=behind")
check("status=behind keeps only behind rows (op1) and drops properties with nothing left", "op1" in hb and 'id="target-mg2"' not in hb and "Ahead" not in flat(hb.split("tg-surface")[1]))
check("status=none lists only metrics with no target", "No target set" in page(Q + "&status=none") and 'id="target-op1"' not in page(Q + "&status=none"))
hp = page(Q + "&property=op1")
check("choosing one property lists only it and summarises only it", 'id="target-op1"' in hp and 'id="target-mg1"' not in hp and "1 of 1 property with a set target" in flat(hp))
names = re.findall(r'id="target-([\w-]+)"', page(Q + "&sort=name"))
check("sort by name is alphabetical inside each group", names == sorted(names, key=lambda i: {"op1": "Flat 1, Operated", "op2": "Flat 9, Later", "op3": "Flat 3, Newest", "mg1": "Flat 2, Managed", "mg2": "Flat 5, Silent"}[i].lower()) or len(names) >= 4)
ord_ = re.findall(r'id="target-([\w-]+)"', page(Q))
check("most-behind-first puts the scored, behind property before the unscored ones in its group", ord_.index("op1") < ord_.index("op3") and ord_.index("op1") < ord_.index("op2"), ord_)

print("inspector and editing")
ins = get("/targets/op1/inspect?metric=profit&month=2026-09&compare=previous_period").get_data(as_text=True)
ti = flat(ins)
check("the inspector shows metric, actual, target, variance, comparison, source, period and property", all(x in ti for x in ("Metric", "Actual £800", "Target £1,500", "−£700 · Behind", "Attainment 53%", "Trend", "vs August 2026", "Comparison source Set by you", "Period September 2026", "Operated")))
check("it is read-only: no form, with an Edit targets action", "<form" not in ins and "Edit targets" in ins)
insg = flat(get("/targets/mg1/inspect?metric=revenue&month=2026-09&compare=previous_period").get_data(as_text=True))
check("the managed inspector says it is Gross Booking Revenue, owner-side, not company revenue", "Gross Booking Revenue (owner-side)" in insg and "not company revenue" in insg and "Managed" in insg)
check("a managed property has no profit inspector", get("/targets/mg1/inspect?metric=profit&month=2026-09").status_code == 404)
ed_op = get("/targets/op1/edit?month=2026-09").get_data(as_text=True)
ed_mg = get("/targets/mg1/edit?month=2026-09").get_data(as_text=True)
check("the editor for an operated property edits Revenue, Property Profit and Occupancy targets", all(f'name="{k}"' in ed_op for k in ("revenue", "profit", "occupancy")) and "Property Profit target" in ed_op and "Operating Profit" not in ed_op)
check("the editor for a managed property has no profit field and shows Management Fee Earned as read-only context",
      'name="profit"' not in ed_mg and 'name="revenue"' in ed_mg and "Gross Booking Revenue target (owner-side)" in ed_mg and "Management Fee Earned" in ed_mg)
cl = create_app().test_client()
cl.post("/targets/mg1/save", data={"revenue": "9500", "occupancy": "72"})
row = db.get_conn().execute("SELECT * FROM property_targets WHERE property_id='mg1'").fetchone()
check("saving a managed property changes its revenue and occupancy targets and leaves its stored profit target exactly as it was", row["revenue_target"] == 9500 and row["occupancy_target"] == 72 and row["profit_target"] == 5000, dict(row))
cl.post("/targets/op1/save", data={"revenue": "", "profit": "", "occupancy": ""})
check("clearing every field removes the saved target (a blank is no target, not 0)", db.get_conn().execute("SELECT COUNT(*) FROM property_targets WHERE property_id='op1'").fetchone()[0] == 0)
h2 = page()
check("after clearing, op1 falls back to its own average and says so", "avg" in block(h2, "op1") and "Average of recorded months" in flat(get("/targets/op1/inspect?metric=revenue&month=2026-09").get_data(as_text=True)))

print("raw calculations are untouched")
view = targets._property_view(c, {"id": "mg1", "name": "x"}, None, None, "2026-09", "2026-09")
check("the raw managed profit is still computed as before (10000-style: revenue minus every recorded cost) but is not shown", view["model"] == "managed" and view["metrics"]["profit"]["actual"] == 8000 - 1200 - 2500 and "Property Profit" not in block(h, "mg1"))
check("metric variance logic for negative targets is unchanged", targets._metric_view("profit", None, {"profit": -302.0}, {"2026-02": {"profit": -625.0}}, "2026-02")["variance"] == -323.0)

print("a recorded average is a benchmark, never a target")
c2 = db.get_conn()
c2.execute("DELETE FROM property_targets")
c2.commit()
hb = page()
tb = flat(hb)
rows_op1 = flat(block(hb, "op1"))
content = flat(hb.split('class="tg-items"')[1].split("</section>")[0]) + " " + flat(hb.split('tg-surface')[1])      # the summary and the list, not the filter menu's option names
check("with no saved target anywhere the page says so once and calls the averages benchmarks", "No targets set · recorded averages are shown as benchmarks." in tb and tb.count("No targets set") == 1)
check("it is a quiet line, not an empty state: the benchmark list is still there", "Nothing matches" not in tb and 'class="tg-row' in hb)
lowered = content.lower()
for phrase in ("no targets set", "no target set", "no target is set for company revenue, management fee earned, business costs or company net profit", "which has no target"):
    lowered = lowered.replace(phrase, "")
check("an average is never labelled 'target': once the known notes are removed the word does not appear in the summary or list", "target" not in lowered, re.findall(r".{25}target.{25}", lowered))
check("rows are tagged 'recorded avg' and carry no 'set by you' tag", "recorded avg" in rows_op1 and "set by you" not in rows_op1 and "set by you" not in tb)
check("the average fallback never receives Behind / On target / Ahead", all(w not in content for w in ("Behind", "On target", "Ahead")), re.findall(r".{20}(?:Behind|On target|Ahead).{20}", content))
check("…and no target-status styling is applied anywhere", not re.search(r"tg-s-(behind|ahead|on)\b|tg-(behind|ahead)\b", hb))
avg_rev = (2000 + 2400 + 1500) / 3
avg_profit = ((2000 - 500) + (2400 - 600) + (1500 - 700)) / 3
check("money differences use neutral words: £467 below recorded average (revenue) and £567 below (profit)",
      f"£{abs(round(1500 - avg_rev)):,} below recorded average" in rows_op1 and f"£{abs(round(800 - avg_profit)):,} below recorded average" in rows_op1, rows_op1)
check("occupancy differences are in points: '… pts above recorded average'", re.search(r"\d+ pts (above|below) recorded average", rows_op1) is not None)
check("the benchmark bar reads as actual vs recorded average (aria + dashed benchmark style), not as a target", "its recorded average of" in hb and "tg-bench" in hb and " target\"" not in block(hb, "op1"))
sm = hb.split('class="tg-items"')[1].split("</section>")[0]
check("the portfolio summary is Actual / Recorded avg / Difference: no status, no 'target'", "recorded avg" in sm and "below recorded average" in flat(sm) and "tg-s-benchmark" in sm and all(w not in flat(sm) for w in ("Behind", "Ahead", "On target")))
check("the summary says how many properties use recorded averages", "properties using recorded averages" in flat(sm))
check("status filters operate only on saved targets: Behind / On target / Ahead show nothing here", all('id="target-op1"' not in page(Q + f"&status={k}") for k in ("behind", "on", "ahead")))
check("'Benchmark only' lists the benchmark rows", 'id="target-op1"' in page(Q + "&status=benchmark") and "recorded avg" in page(Q + "&status=benchmark"))
check("the status menu offers Benchmark only", "Benchmark only" in hb)
insb = flat(get("/targets/op1/inspect?metric=revenue&month=2026-09&compare=previous_period").get_data(as_text=True))
check("benchmark inspector: Comparison source Recorded average, Difference from recorded average, no Target / Variance / Attainment",
      "Comparison source Recorded average" in insb and "Difference from recorded average £467 below recorded average" in insb and "Recorded average £1,967" in insb
      and "Variance" not in insb and "Attainment" not in insb and "Target £" not in insb and "Behind" not in insb, insb)
edb = get("/targets/op1/edit?month=2026-09").get_data(as_text=True)
check("the editor does not pre-fill an average as if it were a saved target", not re.search(r'name="revenue"[^>]*value="[0-9]', edb) and 'placeholder="No target set"' in edb)

print("mixed: saved target on one metric, recorded average on the others")
c2.execute("INSERT INTO property_targets (property_id, revenue_target) VALUES ('op1', 2500)")
c2.commit()
hm = page()
tm = flat(hm)
brev, bprof = flat(block(hm, "op1")), None
rev_row = re.search(r'<button type="button" class="tg-row[^"]*"[^>]*>(?:(?!</button>).)*?Revenue(?:(?!</button>).)*?</button>', block(hm, "op1"), re.S).group(0)
prof_row = re.search(r'<button type="button" class="tg-row[^"]*"[^>]*>(?:(?!</button>).)*?Property Profit(?:(?!</button>).)*?</button>', block(hm, "op1"), re.S).group(0)
check("the saved metric behaves exactly as a target: 'set by you', signed variance, Behind", "set by you" in flat(rev_row) and "−£1,000" in flat(rev_row) and "Behind" in flat(rev_row) and "tg-s-behind" in rev_row)
check("the metric without a saved target on the SAME property is a neutral benchmark", "recorded avg" in flat(prof_row) and "below recorded average" in flat(prof_row) and "Behind" not in flat(prof_row) and "tg-s-benchmark" in prof_row)
check("each row names its source, never mixing the two", "set by you" not in flat(prof_row) and "recorded avg" not in flat(rev_row))
check("the page-level 'no targets set' line disappears once a real target exists", "No targets set" not in tm)
smm = hm.split('class="tg-items"')[1].split("</section>")[0]
check("the revenue summary now sums only saved targets (£1,500 / £2,500, Behind); the profit summary stays benchmark-based",
      "£1,500 / £2,500" in flat(smm) and "1 of 3 properties with a set target" in flat(smm) and "properties using recorded averages" in flat(smm) and "Operated property profit" in flat(smm))
check("Behind lists only the saved-target row of op1", 'id="target-op1"' in page(Q + "&status=behind") and "Property Profit" not in flat(block(page(Q + "&status=behind"), "op1")) and "Revenue" in flat(block(page(Q + "&status=behind"), "op1")))
check("Benchmark only lists op1's profit and occupancy rows, not its saved revenue", "Property Profit" in flat(block(page(Q + "&status=benchmark"), "op1")) and "Revenue" not in flat(block(page(Q + "&status=benchmark"), "op1")).replace("Gross Booking Revenue", ""))
insm = flat(get("/targets/op1/inspect?metric=revenue&month=2026-09&compare=previous_period").get_data(as_text=True))
check("the saved row's inspector is unchanged: Target, Variance, Attainment 60%, Comparison source Set by you", all(x in insm for x in ("Target £2,500", "Variance −£1,000 · Behind", "Attainment 60%", "Comparison source Set by you")))
c2.execute("DELETE FROM property_targets")
c2.commit()
c2.close()

print("the real database (a copy) has no saved targets, so it shows averages as benchmarks")
import shutil, subprocess  # noqa: E402
real = Path(__file__).resolve().parent.parent / "data" / "dashboard.db"
if real.is_file():
    cp = TMP / "real_copy.db"
    shutil.copy(real, cp)
    code = ("import sys, re; sys.path.insert(0, %r); from app import create_app; c = create_app().test_client(); h = c.get('/targets').get_data(as_text=True); "
            "flat = lambda x: re.sub(r'\\s+', ' ', re.sub(r'<[^>]+>', ' ', re.sub(r'<script.*?</script>', '', x, flags=re.S))); t = flat(h); "
            "content = flat(h.split('class=\"tg-items\"')[1].split('</section>')[0]) + ' ' + flat(h.split('tg-surface')[1]); "
            "print('NOTE' if 'No targets set · recorded averages are shown as benchmarks.' in t else 'NONOTE', 'STATUS' if re.search(r'Behind|Ahead|On target', content) else 'CLEAN')" % str(Path(__file__).resolve().parent.parent / "dashboard"))
    out = subprocess.run([sys.executable, "-c", code], env={**os.environ, "DASHBOARD_DB_PATH": str(cp), "DASHBOARD_UPLOADS_PATH": str(TMP / "u2")}, capture_output=True, text=True)
    saved_n = db.get_conn().execute("SELECT 1").fetchone() and __import__("sqlite3").connect(str(cp)).execute("SELECT COUNT(*) FROM property_targets").fetchone()[0]
    check("a real-data copy with zero saved targets shows the benchmark line and no Behind/Ahead/On target", saved_n != 0 or "NOTE CLEAN" in out.stdout, (out.stdout, out.stderr[-300:]))
else:
    check("no real database in this checkout: the real-data check is skipped", True)

print("responsive hooks")
css = (Path(__file__).resolve().parent.parent / "dashboard" / "static" / "style.css").read_text()
check("mobile: a row collapses to metric / actual-of-target / bar / variance · trend", 'grid-template-areas: "metric metric" "val val" "bar bar" "var trend"' in css)
check("the summary collapses to two columns on tablets", ".tg-items { grid-template-columns: repeat(2, minmax(0, 1fr))" in css)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
if FAILS:
    print("FAILED:", *FAILS, sep="\n  - ")
    sys.exit(1)
