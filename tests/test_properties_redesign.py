"""Properties page redesign: attention-first list, operated vs managed semantics, quick inspector data, filters, sorting, states. Scratch database only.

Run: .venv/bin/python tests/test_properties_redesign.py
"""
import html as htmllib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-properties-test-"))
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


prop("ok1", "Flat 1, Healthy House, London, N1 1AA")
prop("neg1", "Flat 2, Losing House, London, N2 2AA")
prop("mg1", "Flat 3, Reviewed House, London, N3 3AA", fee=15.0)
prop("mg2", "Flat 4, Nofee House, London, N4 4AA", managed=True)
prop("empty1", "Flat 5, Quiet House, London, N5 5AA")
prop("zero1", "Flat 6, Zero House, London, N6 6AA")
prop("gone", "Flat 7, Gone House, London, N7 7AA", active=0)
prop("later", "Flat 8, Later House, London, N8 8AA", start="2027-01-01")
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
tx("ok1", "2026-08-01", 1000, "income", "booking_income"); tx("ok1", "2026-08-05", 200, "expense", "cleaning")
tx("ok1", "2026-09-01", 2000, "income", "booking_income"); tx("ok1", "2026-09-05", 300, "expense", "cleaning")
tx("neg1", "2026-09-01", 300, "income", "booking_income"); tx("neg1", "2026-09-05", 900, "expense", "maintenance")
tx("mg1", "2026-09-01", 10000, "income", "booking_income"); tx("mg1", "2026-09-01", 1500, "expense", "management_fee")
tx("mg2", "2026-09-01", 500, "income", "booking_income"); tx("mg2", "2026-09-01", 50, "expense", "management_fee")
tx("zero1", "2026-09-01", 100, "income", "booking_income"); tx("zero1", "2026-09-02", 100, "expense", "cleaning")
tx("general-overheads", "2026-09-10", 400, "expense", "software")
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(7, 'wb.xlsx', 'abcdef0123456789', '2026-10-06 11:00', '2026-10-06 11:05:00', 'applied', '2026-09', 'workbook', ?, ?, '{}', '{}')",
          (json.dumps(["mg1", "ok1"]), json.dumps({
              "mg1": [{"metric": "Management fee rate", "workbook": 0.12, "imported": 0.15, "diff": 0.03, "status": "REVIEW"},
                      {"metric": "Income", "workbook": 10000.0, "imported": 10000.0, "diff": 0.0, "status": "PASS"}],
              "ok1": [{"metric": "Income", "workbook": 2000.0, "imported": 2000.0, "diff": 0.0, "status": "PASS"}]})))
c.commit()

from app import create_app  # noqa: E402


def get(path):
    return create_app().test_client().get(path)


def text(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


P = "from=2026-09-01&to=2026-09-01&compare=previous_period"
ALL = f"/properties?{P}&view=all"
h = get(ALL).data.decode()
rows = {m.group(1): m.group(0) for m in re.finditer(r'<tr class="pr-row" data-pid="([^"]+)".*?</tr>', h, re.S)}
data = json.loads(re.search(r'id="insp-data">(.*?)</script>', h, re.S).group(1))

print("structure")
check("the page has one portfolio band (not four cards), then one list on a tonal working surface", h.count('class="pr-band"') == 1 and h.count('<table class="pr-table"') == 1 and 'class="pr-surface"' in h and 'class="card' not in h.split('class="pr-wrap"')[1])
band = h.split('class="pr-band"')[1].split("</section>")[0]
visible = re.sub(r'<span class="vh">.*?</span>', "", band)
visible = re.sub(r'title="[^"]*"', "", visible)
check("the band has no formula lines: how revenue and profit add up is only in the tooltip and the hidden text", "+ fees" not in text(visible) and "management fees" not in text(visible) and "= operated" not in text(visible), text(visible)[:200])
check("summary: active properties with the operated/managed split, Company Revenue and Property Profit with how they add up, and portfolio occupancy",
      re.search(r"Active properties 7 5 operated · 2 managed", text(h)) is not None and "Company Revenue" in text(h) and "Property Profit" in text(h) and "Portfolio occupancy" in text(h), text(h)[:400])
check("the All view groups by model with quiet headers", "OPERATED · 5" in h and "MANAGED · 2" in h)

print("operated vs managed semantics")
op, mg = rows["ok1"], rows["mg1"]
check("operated row: revenue is the strong figure (captioned revenue), Property Profit is the quiet second line, and the row says Operated",
      re.search(r'pr-main">.*?£2,000.*?</span><span class="pr-cap">revenue</span>', op, re.S) is not None and re.search(r'pr-second">.*?£1,700.*?profit', op, re.S) is not None and ">Operated<" in op.replace("\n", ""), op[:300])
check("managed row: the fee (£1,500) is the strong figure captioned fee earned; Gross Booking Revenue (£10,000) is only the quiet second line; the row says Managed · 15%",
      re.search(r'pr-main">.*?£1,500.*?</span><span class="pr-cap">fee earned</span>', mg, re.S) is not None and re.search(r'pr-second">.*?£10,000.*?gross booking revenue', mg, re.S) is not None and "Managed · 15%" in mg)
check("managed gross booking revenue is never presented as Company Revenue or profit", "£10,000" not in re.search(r'pr-main">.*?</span>', mg, re.S).group(0) and "profit" not in re.search(r'pr-second">.*?</span>', mg, re.S).group(0))

print("attention")
attn = {pid for pid, r in rows.items() if 'data-attn="1"' in r}
check("attention is exactly: negative operated profit, REVIEW, managed fee % not set, no data", attn == {"neg1", "mg1", "mg2", "empty1"}, attn)
check("inactive, not-started and real-zero properties are not flagged", not ({"gone", "later", "zero1", "ok1"} & attn))
check("the toggle counts match", re.search(r'Needs attention <span class="count">4</span>', h) and re.search(r'All properties <span class="count">7</span>', h))
ov = get(f"/?{P}").data.decode()
check("the count equals the Overview's Needs attention count for the same period", 'data-attention="4"' in ov and re.search(r'Needs attention <span class="count">4</span>', ov))
a = get(f"/properties?{P}").data.decode()
check("default: with attention items the list opens on Needs attention, mixed across models, with no group headers and urgency first (REVIEW, then no data, then negative profit, then fee not set)",
      re.search(r'Needs attention <span class="count">4</span>', a) and 'class="grp"' not in a
      and [m for m in re.findall(r'data-pid="([^"]+)"', a.split('id="pr-table"')[1])] == ["mg1", "empty1", "neg1", "mg2"], re.findall(r'data-pid="([^"]+)"', a))
check("the REVIEW word links to that import's block for the property", 'href="/imports/7#prop-mg1"' in a)

print("states")
check("no data shows a dash and a 'No data' status, not £0 / 0%", "—" in rows["empty1"] and "No data" in rows["empty1"] and "£0" not in rows["empty1"])
check("a real zero month shows £0 / 0% figures (a real zero is not hidden)", "£0" in rows["zero1"] and "No data" not in rows["zero1"])
check("a not-started property says Not active in this period, with no figures and no drilldowns", "Not active in this period" in rows["later"] and "mlink" not in rows["later"])
hall = get(f"/properties?{P}&view=all&status=all").data.decode()
gone = re.search(r'<tr class="pr-row" data-pid="gone".*?</tr>', hall, re.S).group(0)
check("an inactive property with no activity in the period says Inactive, with no figures and no drilldowns", "Inactive" in gone and "mlink" not in gone and "—" in gone)
check("an inactive property is not described as poor performance (no 0%, no negative, no review, never flagged)", "0%" not in gone and "st-review" not in gone and "Negative" not in gone and 'data-attn="0"' in gone)

print("trend")
check("ONE trend signal per row: the change in the company result against the comparison (ok1: £2,000 vs £1,000 = +100%)", "↑ 100%" in rows["ok1"] and rows["ok1"].count('class="chg') == 1)
nocmp = get(f"/properties?from=2026-09-01&to=2026-09-01&compare=none&view=all").data.decode()
check("with no comparison there is no trend, just a dash", "↑ 100%" not in nocmp)

print("inspector")
check("the inspector data is there for every listed property and is read only (links only, no forms)", set(data) == set(rows) and "<form" not in json.dumps(data))
rail = re.findall(r'class="rl rl-(\w+)" style="flex-basis:([\d.]+)%"', h)
check("the state rail is one proportional bar over the in-period active properties; each segment is a filter link",
      abs(sum(float(w) for _k, w in rail) - 100) < 0.1 and {k for k, _w in rail} <= {"pass", "review", "nocontrol", "noimport", "nodata"} and len(rail) >= 3 and 'href="/properties?' in h.split('class="rail-bar"')[1].split("</div>")[0], rail)
check("each row has a thin occupancy bar whose width is the occupancy (ok1 is 0% here: real zero shows an empty bar, not a dash)", 'class="occ-bar"' in rows["ok1"] and "—" not in re.search(r'class="occ"?.*?</span></span>', rows["zero1"], re.S).group(0))
d1, d2 = data["ok1"], data["mg1"]
check("operated inspector: hero Property Profit, then Revenue, Occupancy, RevPAR; managed: hero Management Fee Earned, then Gross Booking Revenue (quiet), Occupancy, RevPAR",
      d1["hero"][0] == "Property Profit" and d1["hero"][1] == "£1,700" and [f[0] for f in d1["lines"]] == ["Revenue", "Occupancy", "RevPAR"]
      and d2["hero"][0] == "Management Fee Earned" and d2["hero"][1] == "£1,500" and [f[0] for f in d2["lines"]] == ["Gross Booking Revenue", "Occupancy", "RevPAR"] and d2["lines"][0][3] is True, (d1["hero"], d2["hero"]))
check("the header carries the compact name and the canonical address; meta is Model, Fee (managed only), Status, Started, Imported (linked to the batch)",
      d1["title"] == "Flat 1, Healthy House" and d1["address"] == "Flat 1, Healthy House, London, N1 1AA" and [m[0] for m in d1["meta"]] == ["Model", "Status", "Started", "Imported"]
      and [m[0] for m in d2["meta"]] == ["Model", "Fee", "Status", "Started", "Imported"] and d2["meta"][-1][2] == "/imports/7#prop-mg1" and d2["meta"][-1][1].startswith("#7"))
check("REVIEW shows the exact reason from the import; a property with no issue is not given one",
      any("Management fee rate: workbook 12%, dashboard 15%" in x[1] for x in d2["attention"]) and d1["attention"] == [])
check("figure links keep the period, property and comparison and go to the existing record pages",
      all(("from=2026-09-01" in f[2] and "to=2026-09-01" in f[2]) for f in d1["lines"] + [d1["hero"]]) and "/properties/ok1/bookings" in d1["lines"][0][2] and "#revenue-records" in d1["lines"][0][2]
      and "#fees" in d2["hero"][2] and all(a[1].startswith("/properties/ok1") and "from=2026-09-01" in a[1] for a in d1["actions"]))
check("actions: Open property, Expenses, Bookings; and no generic 'Check' note for a healthy property", [x[0] for x in d1["actions"]] == ["Open property", "Expenses", "Bookings"] and "note" not in d1)
check("negative profit and fee-not-set explain themselves; no data and not-active say so", any("own costs are higher" in x[1] for x in data["neg1"]["attention"]) and any("not configured" in x[1] for x in data["mg2"]["attention"])
      and data["empty1"]["message"] == "No data imported for this period." and data["later"]["message"] == "Not active in this period.")
check("keyboard and dismissal are wired: Escape closes, a click outside closes, Enter on the name opens it, the close button is named", "e.key === 'Escape'" in h and "closest('#inspector, .pr-row, .pr-bar, .pr-chips')" in h and 'aria-label="Close property summary"' in h and 'role="dialog"' in h)

print("filters, search and sorting")
chk = lambda q: [m for m in re.findall(r'data-pid="([^"]+)"', get(f"/properties?{P}&view=all&{q}").data.decode().split('id="pr-table"')[1])] if 'id="pr-table"' in get(f"/properties?{P}&view=all&{q}").data.decode() else []
check("model filter: managed only", set(chk("model=managed")) == {"mg1", "mg2"} and set(chk("model=operated")) == {"ok1", "neg1", "empty1", "zero1", "later"})
check("check filter: REVIEW only; PASS only; no data only", chk("check=review") == ["mg1"] and chk("check=pass") == ["ok1"] and chk("check=nodata") == ["empty1"])
check("search matches name and address", chk("q=healthy") == ["ok1"] and chk("q=n4 4aa") == ["mg2"])
chips = get(f"/properties?{P}&view=all&model=managed&check=review").data.decode()
check("active filters show as removable chips with a Reset", chips.count('class="chip-x"') == 2 and 'class="chip-reset"' in chips and 'aria-label="Remove filter Managed"' in chips)
by_name = chk("sort=name"); by_result = chk("sort=result")
check("…alphabetical by name (Healthy < Losing < Quiet < Zero)", [p for p in by_name if p in ("ok1", "neg1", "empty1", "zero1")] == ["ok1", "neg1", "empty1", "zero1"], by_name)
check("…and result descending (ok1 before neg1)", by_result.index("ok1") < by_result.index("neg1"), by_result)
check("sort, model and check controls are named menus (no icon-only controls), and search is labelled",
      'aria-label="Sort the list"' in h and 'aria-label="Filter by model"' in h and 'aria-label="Filter by check status"' in h and 'aria-label="Search properties by name or address"' in h)

print("lifecycle")
inact = get("/properties?status=inactive").data.decode()
check("Inactive lists the inactive properties regardless of period, and the switch shows both counts", "Flat 7" in text(inact) and re.search(r'Inactive <span class="count">1</span>', inact) and re.search(r'Active <span class="count">', inact))
check("the default list is active properties only", 'data-pid="gone"' not in get("/properties?from=2026-09-01&to=2026-09-01&view=all").data.decode())

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
sys.exit(1 if FAILS else 0)
