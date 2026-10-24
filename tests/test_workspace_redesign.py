"""Property workspace redesign: the shared shell (identity, economics strip, tabs, facts), model-specific semantics, no repeated metrics,
NO CONTROL vs no import, and the Overview / Performance tabs. Scratch database only.

Run: .venv/bin/python tests/test_workspace_redesign.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-workspace-test-"))
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


def prop(pid, name, fee=None, managed=None, active=1, start=None):
    c.execute("INSERT INTO properties (id, code, name, address, type, active, management_fee_pct, is_managed, start_date) VALUES (?,?,?,?,'flat',?,?,?,?)",
              (pid, pid.upper(), name, name, active, fee, 1 if (fee or managed) else 0, start))
    completeness.seed_defaults(c, pid)


def tx(pid, date, amount, direction, category):
    c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source) VALUES (?,?,?,?,?,?,0,'manual')",
              (pid, date, "row", amount, direction, category))


prop("op", "Flat 1, Operated House, London, N1 1AA", start="2026-03-01")
prop("mg", "Flat 2, Managed House, London, N2 2AA", fee=15.0)
prop("nc", "Flat 3, Nocontrol House, London, N3 3AA")
prop("noimp", "Flat 4, Noimport House, London, N4 4AA")
prop("empty", "Flat 5, Empty House, London, N5 5AA")
prop("later", "Flat 6, Later House, London, N6 6AA", start="2027-06-01")
prop("gone", "Flat 7, Gone House, London, N7 7AA", active=0)
prop("zero", "Flat 8, Zero House, London, N8 8AA")
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
for pid in ("op", "nc", "noimp"):
    tx(pid, "2026-08-01", 1000, "income", "booking_income"); tx(pid, "2026-08-04", 200, "expense", "cleaning")
    tx(pid, "2026-09-01", 2000, "income", "booking_income"); tx(pid, "2026-09-05", 300, "expense", "cleaning"); tx(pid, "2026-09-06", 100, "expense", "utilities")
tx("mg", "2026-09-01", 10000, "income", "booking_income"); tx("mg", "2026-09-01", 1000, "expense", "management_fee")
tx("zero", "2026-09-03", 50, "expense", "cleaning")
tx("general-overheads", "2026-09-10", 400, "expense", "software")
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(7, 'wb.xlsx', 'abcdef0123456789', '2026-10-06 11:00', '2026-10-12 11:05:00', 'applied', '2026-09', 'workbook', ?, ?, '{}', '{}')",
          (json.dumps(["op", "mg", "nc"]), json.dumps({
              "op": [{"metric": "Income", "workbook": 2000.0, "imported": 2000.0, "diff": 0.0, "status": "PASS"}],
              "mg": [{"metric": "Management fee rate", "workbook": 0.10, "imported": 0.15, "diff": 0.05, "status": "REVIEW"}],
              "nc": [{"metric": "Income", "workbook": None, "imported": 0, "diff": None, "status": "NO CONTROL"}]})))
c.commit()

from app import create_app  # noqa: E402
import services.provenance as prov  # noqa: E402


def get(path):
    return create_app().test_client().get(path)


def text(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


Q = "from=2026-09-01&to=2026-09-01&compare=previous_period"
cells = lambda h: re.findall(r'<div class="wc wc-(\w+)">(.*?)</div>\s*(?=<|\Z)', h, re.S)

print("control state (REVIEW / PASS / NO CONTROL / no import)")
cs = lambda pid: prov.control_state(c, pid, "2026-09", "2026-09")
check("an import whose checks agree is PASS; a failed check is REVIEW", cs("op") and cs("op")[0] == "pass" and cs("mg") and cs("mg")[0] == "review")
check("imported but with no control value to compare is NO CONTROL", cs("nc") and cs("nc")[0] == "nocontrol")
check("NO import covering the property is None: it is never inferred as NO CONTROL", cs("noimp") is None and cs("empty") is None)

print("shared shell: identity, strip, tabs, facts")
h = get(f"/properties/op?{Q}").data.decode()
check("identity once: one h1, the compact name, the address and 'Operated · Active'", h.count('class="ws-title"') == 1 and 'class="ws-meta">Operated<span class="sep">·</span><span>Active</span>' in h and 'class="ws-addr"' in h)
check("the top band is a breadcrumb back to Properties with the period and compare controls", re.search(r'<nav class="bar-crumb" aria-label="Breadcrumb"><a href="/properties[^"]*">Properties</a>.*?aria-current="page"', h, re.S) is not None and "Compare:" in text(h))
vals = re.findall(r'class="wc-value"[^>]*>(.*?)</(?:a|span)>', h)
check("operated strip: Company Revenue £2,000 − Property Costs £400 = Property Profit £1,600, then Occupancy and RevPAR", vals[:3] == ["£2,000", "£400", "£1,600"] and len(vals) == 5 and vals[3].endswith("%"), vals)
check("the strip reads as an equation with − and = connectors and no explanatory sentence", 'class="ws-op" aria-hidden="true">−</span>' in h and 'class="ws-op" aria-hidden="true">=</span>' in h and "Revenue £" not in text(h.split('class="ws-strip')[1].split("</section>")[1]))
check("each strip figure is a drilldown that keeps the period (revenue records, expenses, booked nights)", re.search(r'class="wc-value" href="/properties/op/bookings\?[^"]*from=2026-09-01[^"]*#revenue-records"', h) is not None
      and re.search(r'class="wc-value" href="/properties/op/expenses\?[^"]*from=2026-09-01', h) is not None)
check("tabs keep the period, compare and property in every link", all("from=2026-09-01" in href for href in re.findall(r'<a href="(/properties/op[^"]*)" class="tab', h)) and len(re.findall(r'class="tab', h)) >= 5)
check("facts: Model, Status, Started (Mar 2026), Last imported (12 Oct 2026 · Batch #7, linked), workbook check PASS linked to its block",
      "Operated" in h.split('id="ws-facts"')[1] and "Mar 2026" in h and re.search(r'<a href="/imports/7#prop-op">12 Oct 2026 · Batch #7</a>', h) is not None and 'class="st st-pass" href="/imports/7#prop-op"' in h)
check("the facts are a disclosure that is open on wide screens and closed on narrow ones", '<details class="ws-facts" id="ws-facts" open>' in h and "<summary>Property details</summary>" in h)
check("no performance metric is repeated in the facts panel", not re.search(r"£|Occupancy|RevPAR", h.split('id="ws-facts"')[1].split("</details>")[0]))

print("managed strip")
m = get(f"/properties/mg?{Q}").data.decode()
mv = re.findall(r'class="wc-value"[^>]*>(.*?)</(?:a|span)>', m)
check("managed strip: Gross Booking Revenue £10,000 (quiet) → Management Fee Earned £1,000 (strong) → Occupancy → RevPAR", mv[:2] == ["£10,000", "£1,000"] and len(mv) == 4, mv)
check("the guests' money is styled quiet and captioned 'guests paid'; the fee carries the configured %", 'wc wc-gross' in m and "guests paid" in m and re.search(r'wc-fee.*?15% fee', re.sub(r"\s+", " ", m), re.S) is not None)
check("no Owner Share is shown or invented (existing data does not support a trustworthy one), and no 'Company Revenue' / 'Property Profit' for a managed property",
      "Owner Share" not in m and "Owner share" not in m and "Company Revenue" not in text(m.split('class="ws-strip')[1].split("</section>")[0]) and "Property Profit" not in text(m.split('class="ws-strip')[1].split("</section>")[0]))
check("hover on Gross Booking Revenue says most of it belongs to the owner", "mostly belongs to its owner" in m)

print("no repeated metrics, attention and semantics")
check("the Overview tab does not repeat the headline figures as tiles or a second KPI grid", 'class="tiles' not in h.split('class="ws-body"')[1] and 'class="tile' not in h.split('class="ws-body"')[1])
check("Overview: one performance chart, a property-costs breakdown that adds up to the Expenses total, then recent records", h.count("<canvas") == 1 and "Property costs" in h and "Recent records" in h)
cb = re.findall(r'<span class="cl">(.*?)</span><span class="cv">£([\d,]+)</span>', h)
check("the costs breakdown categories sum to the property's costs (£400)", sum(int(v.replace(",", "")) for _l, v in cb) == 400 and ("Cleaning", "300") in [(l, v) for l, v in cb], cb)
check("REVIEW: one compact attention line with the exact reason and a link to the import (managed fee rate)", re.search(r'class="ws-attn ws-attn-review".*?REVIEW.*?Management fee rate: workbook 10%, dashboard 15%', m, re.S) is not None and "Open the import" in m)
ncp = get(f"/properties/nc?{Q}").data.decode()
check("NO CONTROL is shown quietly (information, not a warning) when imported with nothing to compare", 'class="ws-attn ws-attn-nocontrol"' in ncp and "ws-attn-review" not in ncp and 'class="st st-nc"' in ncp)
nip = get(f"/properties/noimp?{Q}").data.decode()
check("a property with data but no import says 'No import for this period', not NO CONTROL, and shows no attention line", "No import for this period" in nip and "NO CONTROL" not in nip and "ws-attn" not in nip.split('class="ws-body"')[1])
check("a managed property's Overview shows the fee reconciliation (configured vs recorded) instead of a cost breakdown", "Management fee" in m and "Configured" in m and "Recorded" in m and "10.0%" in m and "Property costs" not in m.split('class="ws-body"')[1])

print("states")
no = get(f"/properties/empty?{Q}").data.decode()
check("no data: the strip says so once with the import action, no figures, no comparison", text(no).count("No data imported for this period.") == 1 and 'class="wc-value"' not in no and "Import monthly workbook" in no)
lt = get(f"/properties/later?{Q}").data.decode()
check("not started: says it had not joined the portfolio; no figures, no 'no data' claim", "had not joined the portfolio" in lt and 'class="wc-value"' not in lt and "No data imported for this period." not in text(lt))
gn = get(f"/properties/gone?{Q}").data.decode()
check("inactive with nothing recorded: says it is no longer active, history kept", "no longer active" in gn and 'class="wc-value"' not in gn)
zv = re.findall(r'class="wc-value"[^>]*>(.*?)</(?:a|span)>', get(f"/properties/zero?{Q}").data.decode())
check("the real-zero rule: a property-month with a recorded cost but £0 revenue keeps its strip (£0 revenue, £50 costs, −£50 profit)", zv[:3] == ["£0", "£50", "−£50"] and "0%" in zv, zv)
oh = get(f"/properties/general-overheads?{Q}").data.decode()
check("the business cost centre has no economics strip and no property facts", 'class="ws-strip' not in oh and "Started" not in oh.split('id="ws-facts"')[1])

print("Performance tab")
pf = get(f"/properties/op/performance?{Q}").data.decode()
check("Performance: ONE chart with a metric switcher (Revenue, Property Profit, Occupancy, ADR, RevPAR), compare and a range control; no KPI tile row",
      pf.count("<canvas") == 1 and all(f'data-m="{k}"' in pf for k in ("revenue", "net_profit", "occupancy", "adr", "revpar")) and 'id="pf-range"' in pf and 'class="tiles' not in pf.split('class="ws-body"')[1])
pm = get(f"/properties/mg/performance?{Q}").data.decode()
check("managed Performance offers Gross Booking Revenue and Management Fee Earned, never Revenue / Property Profit", 'data-m="gross"' in pm and 'data-m="fee"' in pm and 'data-m="revenue"' not in pm and 'data-m="net_profit"' not in pm)
check("the switcher is a labelled tablist with selected state; the chart has a text alternative", 'role="tablist" aria-label="Metric"' in pf and 'aria-selected="true"' in pf and 'role="img" aria-label=' in pf)

print("other tabs")
bk = get(f"/properties/op/bookings?{Q}").data.decode()
check("Bookings keeps its evidence sections and anchors and shows a quiet one-line summary instead of a tile row", all(k in bk for k in ('id="revenue-records"', 'id="booked-nights"', 'class="ws-sum"')) and 'class="tiles"' not in bk)
ex = get(f"/properties/op/expenses?{Q}").data.decode()
check("Expenses: the ledger comes first and 'Add an expense' is a disclosure", ex.index("Expenses") < ex.index("Add an expense") and '<details class="ws-add">' in ex)
dc = get(f"/properties/op/documents?{Q}").data.decode()
check("Documents says evidence only", "supporting evidence" in text(dc))
st = get(f"/properties/op/settings?{Q}").data.decode()
check("Settings stays configuration only (no performance strip is repeated as tiles)", 'class="tiles' not in st.split('class="ws-body"')[1])

print("facts reflect current configuration")
cl = create_app().test_client()
cl.post("/properties/mg/ownership", data={"kind": "managed", "fee": "12"})
m2 = get(f"/properties/mg?{Q}").data.decode()
check("changing the fee % updates the facts, the strip caption and the configured fee on the Overview", re.search(r"<dt>Fee</dt><dd>12%</dd>", m2) is not None and "12% fee" in m2 and re.search(r"Configured</dt><dd>12%", m2) is not None)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
sys.exit(1 if FAILS else 0)
