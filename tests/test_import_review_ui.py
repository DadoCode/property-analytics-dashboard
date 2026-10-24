"""Import review redesign. The applied page and the history run through the real routes (scratch database). The PREVIEW page needs a staged workbook,
which cannot be built here (the workbook fixture is unavailable and none is fabricated), so its template is rendered with a hand-made plan of the same shape
as services/workbook/plan.py produces: this checks the presentation (triage order, band, REVIEW / PASS / NO CONTROL treatment, change rows, the already-up-to-date
state, the form fields Apply depends on), not the planner. Scratch database only.

Run: .venv/bin/python tests/test_import_review_ui.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-import-ui-test-"))
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


prop("op1", "Flat 1, Operated House, London, N1 1AA")
prop("mg1", "Flat 2, Managed House, London, N2 2AA", fee=15.0)
prop("nc1", "Flat 3, Nocontrol House, London, N3 3AA")
rate = {"metric": "Management fee rate", "workbook": 0.12, "imported": 0.10, "diff": 0.02, "status": "REVIEW"}
checks = {"op1": [{"metric": "Income", "workbook": 1500.0, "imported": 1500.0, "diff": 0.0, "status": "PASS"}],
          "mg1": [{"metric": "Income", "workbook": 2000.0, "imported": 2000.0, "diff": 0.0, "status": "PASS"}, rate],
          "nc1": [{"metric": "Operating profit (control)", "workbook": None, "imported": 0, "diff": None, "status": "NO CONTROL"}]}
tot = lambda r: {"revenue": r, "property_costs": 0.0, "management_fee": 100.0, "profit": 100.0, "days": 20, "occupancy": 0.5, "total_expenses": 0.0}
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals, rows_added, rows_changed, rows_removed) VALUES "
          "(7, 'Business_Accounts_Tracker_2026_Sept_v2.xlsx', 'abcdef0123456789', '2026-10-06 10:00', '2026-10-12 10:05:00', 'applied', '2026-09', 'workbook', ?, ?, ?, ?, 2, 1, 0)",
          (json.dumps(["op1", "mg1", "nc1"]), json.dumps(checks), json.dumps({"op1": tot(1000.0), "mg1": tot(1800.0), "nc1": tot(500.0)}), json.dumps({"op1": tot(1500.0), "mg1": tot(2000.0), "nc1": tot(500.0)})))
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(8, 'Business_Accounts_Tracker_2026_Aug.xlsx', 'abcdef0123456780', '2026-10-01 10:00', 'staged', '2026-08', 'workbook', '[]', '{}', '{}', '{}')")
c.commit()

from app import create_app  # noqa: E402

app = create_app()


def get(path):
    return app.test_client().get(path)


def text(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


print("applied import: an audit record")
ap = get("/imports/7").data.decode()
apt = text(ap)
check("a quiet three-step flow shows 'Applied' as the current step", re.search(r'<li class="on" aria-current="step"><span class="n"[^>]*>3</span>Applied</li>', ap) is not None)
check("the header says Applied, the batch, and the month (not 'Import preview with another button')", "Applied · Batch #7" in apt and "<h1>September 2026</h1>" in ap and "Import preview" not in apt)
check("the band says what was done: 3 areas updated, 3 row changes", re.search(r"3 areas were updated · 3 row changes", apt) is not None, apt[:300])
check("the band's rail counts REVIEW 1, NO CONTROL 1, PASS 1 (areas, from the stored import-time results)", all(k in apt for k in ("REVIEW 1", "NO CONTROL 1", "PASS 1")))
check("REVIEW comes first: the REVIEW area is before the NO CONTROL and PASS areas, in both the comparison and the reconciliation",
      ap.index('id="prop-mg1"') < ap.index('id="prop-nc1"') < ap.index('id="prop-op1"') and ap.index("Managed House") < ap.index("Nocontrol House") < ap.index("Operated House"))
check("the import-time result is still the stored one: the REVIEW check reads 12% recorded against 10% configured, +2 pp", re.search(r"Management fee rate.*?12%.*?10%.*?\+2 pp", apt) is not None and 'class="rc rc-review">REVIEW' in ap)
check("PASS is collapsed with a quiet count; NO CONTROL is neutral and not a failure", re.search(r'<details class="sub"><summary><span class="rc-pass-text">✓ \d+ checks? passed', ap) is not None and "not a failure" in apt and "rc rc-nc" in ap)
check("Undo is secondary: after the whole record, and still available", ap.index("Undo import") > ap.index("Reconciliation at import time") and 'undo-block' in ap)
c.execute("UPDATE properties SET management_fee_pct=12.0 WHERE id='mg1'"); c.commit()
ap2 = text(get("/imports/7").data.decode())
check("if the configuration changes later it is a separate labelled note; the historical REVIEW does not change", "Current configuration" in ap2 and "12% · ✓ now matches the recorded rate" in ap2 and "the result at import time is unchanged" in ap2 and "REVIEW 1" in ap2)
c.execute("UPDATE properties SET management_fee_pct=15.0 WHERE id='mg1'"); c.commit()

print("import history: an audit log")
h = get("/imports").data.decode()
ht = text(h)
check("the history is a flat list with Month / Workbook / Applied / Batch / Changes / State, no card grid", all(f">{x}" in h or f"{x}</a>" in h for x in ("Month", "Workbook", "Applied", "Batch", "Changes", "State")) and 'class="imp-hist-table"' in h)
check("a row reads September 2026 · the workbook · 12 Oct 2026-style applied date · #7 · '3 changes' · ✓ Applied", "September 2026" in ht and "2026-10-12" in ht and "#7 · Workbook" in ht and re.search(r"\d+ changes?", ht) is not None and "✓ Applied" in ht)
check("a preview that was never applied is 'Not imported yet', not a failure", "Not imported yet" in ht)
check("the flow shows Upload as the current step; the standing sentence and sort/filter still work", re.search(r'<li class="on" aria-current="step"><span class="n"[^>]*>1</span>Upload</li>', h) is not None and "If a workbook-controlled figure is wrong, correct the workbook and re-import the month." in ht
      and get("/imports?status=applied&sort=period&dir=asc").status_code == 200 and get("/imports?sort=bogus&status=bogus").status_code == 200)

print("preview: rendered from a plan of the real shape")
from flask import render_template  # noqa: E402


def counts(n=0, ch=0, rm=0, same=0):
    return {"NEW": n, "CHANGED": ch, "REMOVED": rm, "UNCHANGED": same}


def fig(**kw):
    base = {"revenue": 1000.0, "property_costs": 100.0, "management_fee": 100.0, "days": 20, "occupancy": 0.5, "profit": 900.0, "model": "operated"}
    base.update(kw)
    return base


def item(pid, name, status, cnt, cur, aft, checks_, flagged=False, **kw):
    it = {"code": pid.upper(), "property_id": pid, "name": name, "sheet": pid.upper() + "26", "reasons": [], "rows": [], "checks": checks_, "counts": cnt,
          "current": cur, "after": aft, "days": 20, "bookings": {}, "new_property": None, "status": status, "flagged": flagged,
          "change_count": cnt["NEW"] + cnt["CHANGED"] + cnt["REMOVED"], "workbook": aft or {}, "notes": []}
    it.update(kw)
    return it


PASS_C = [{"metric": "Income", "workbook": 1000.0, "imported": 1000.0, "diff": 0.0, "status": "PASS", "fmt": "money", "note": ""}]
NC_C = [{"metric": "Operating profit (control)", "workbook": None, "imported": 0, "diff": None, "status": "NO CONTROL", "fmt": "money", "note": ""}]
REV_C = PASS_C + [{"metric": "Management fee rate", "workbook": 0.12, "imported": 0.15, "diff": -0.03, "status": "REVIEW", "fmt": "pct", "note": ""}]
rows_changed = [{"status": "CHANGED", "old": {"amount": 1250.0, "description": "fee", "vendor": "", "category": "management_fee", "direction": "expense", "capex": 0},
                 "new": {"amount": 1296.0, "description": "fee", "vendor": "", "category": "management_fee", "direction": "expense", "capex": 0, "source_ref": "TBR26!AC19"}}]
review_it = item("mg1", "Managed House", "review", counts(0, 1, 0, 9), fig(model="managed", management_fee=1250.0, profit=1250.0), fig(model="managed", management_fee=1296.0, profit=1296.0), REV_C, True, rows=rows_changed)
change_it = item("op1", "Operated House", "ok", counts(0, 1, 0, 4), fig(property_costs=100.0, profit=900.0), fig(property_costs=150.0, profit=850.0), PASS_C)
BLANK_C = [{"metric": "Income", "workbook": None, "imported": 830.54, "diff": None, "status": "NO CONTROL", "fmt": "money", "note": "the workbook's own total for this is blank"},
           {"metric": "Opex", "workbook": None, "imported": -53.44, "diff": None, "status": "NO CONTROL", "fmt": "money", "note": "the workbook's own total for this is blank"},
           {"metric": "Total costs", "workbook": None, "imported": -53.44, "diff": None, "status": "NO CONTROL", "fmt": "money", "note": "the workbook's own total for this is blank"}]
many_it = item("dr1", "Dalton House", "review", counts(0, 1, 0, 3), fig(model="managed", management_fee=100.0), fig(model="managed", management_fee=102.0), BLANK_C + [REV_C[1]], True)
same_it = item("nc1", "Nocontrol House", "unchanged", counts(0, 0, 0, 6), fig(), fig(), NC_C)
idle_it = {"code": "LT", "property_id": "later", "name": "Later House", "sheet": "LT26", "status": "not_active", "reasons": ["NOT ACTIVE: Later House joined the portfolio on 2027-06-01."], "rows": [], "checks": [], "counts": {}, "current": None, "after": None, "new_property": None}
business = {"property_id": "general-overheads", "name": "Business costs (Main Page)", "status": "unchanged", "reasons": [], "rows": [], "checks": PASS_C, "counts": counts(0, 0, 0, 12), "suspects": [],
            "change_count": 0, "current": {"business_costs": 500.0}, "after": {"business_costs": 500.0}, "flagged": False, "days": None, "bookings": {}}
report = {"global_errors": [], "year": 2026, "used": ["MG26", "OP26"], "ignored": [{"sheet": "X", "reason": "not a property"}], "blocked": ["Logins"], "required": [], "unmapped": [], "missing_properties": [], "can_import": True}
batch = {"id": 9, "filename": "Business_Accounts_Tracker_2026_Sept_v2.xlsx", "uploaded_at": "2026-10-12 11:00", "file_hash": "abcdef0123456789"}


def render(plan_items, business_item, last=None):
    plan = {"ym": "2026-09", "year": 2026, "properties": plan_items, "business": business_item, "global_errors": [], "notes": [], "excluded": [], "distinct_refs": None, "distinct_keys": []}
    with app.test_request_context("/imports/9"):
        return render_template("import_batch.html", report=report, overview=[{"ym": "2026-09", "changed_rows": 2}], ym="2026-09", plan=plan, month_issues=[], elsewhere=0,
                               fingerprint="fp123", last_imported=last or {}, batch=batch, month_label=lambda ym: "September 2026", business_id="general-overheads",
                               all_properties=[], active_property=None, active="documents")


pv = render([change_it, idle_it, same_it, review_it], business)
pt = text(pv)
check("the step flow shows Preview as current; the page is headed with the month and the file", re.search(r'<li class="on" aria-current="step"><span class="n"[^>]*>2</span>Preview</li>', pv) is not None and "<h1>September 2026</h1>" in pv and "Business_Accounts_Tracker_2026_Sept_v2.xlsx" in pt)
check("the heading and the existing sentence are kept ('Import September 2026 only', 'excluded by name')", "Import September 2026 only" in pt and "excluded by name" in pt)
check("one band: '0 new · 2 changed · 0 removed · 31 unchanged' (the sum of the areas) and a verdict line", re.search(r"<b>0</b> new.*?<b>2</b> changed.*?<b>0</b> removed.*?<b>31</b> unchanged", pv, re.S) is not None and "attention before you apply" in pt)
check("the rail is a filter with REVIEW 1, NO CONTROL 1, PASS 2 (Operated House and the business costs) and named buttons", all(k in pt for k in ("REVIEW 1", "NO CONTROL 1", "PASS 2")) and 'aria-label="REVIEW: 1 of 4' in pv and pv.count('class="rl rl-imp-') == 3)
check("triage order: Needs attention (REVIEW) first, then Will change, then the quiet group", pv.index("Needs attention") < pv.index("Will change") < pv.index("already up to date or out of scope") and pv.index("Managed House") < pv.index("Operated House"))
rb = pv[pv.index('id="p-mg1"'):pv.index('id="p-op1"')]
check("a REVIEW area is open by default and states ONE exact reason: workbook 12% vs configured 15%, the fixed explanation and the certain next step",
      "<details class=\"imp-body\" open>" in rb and "Workbook 12% · Configured 15%" in text(rb) and "The recorded fee rate differs from the configured rate." in rb and "Correct the workbook or update the property configuration before re-importing." in rb)
check("a flagged area is not pre-ticked; a clean one with changes is", 'name="include" value="mg1" ' in pv and 'name="include" value="mg1" checked' not in pv and 'name="include" value="op1" checked' in pv)
check("the change is a direction, not four columns: £1,250.00 → £1,296.00 with +£46.00, and the exact comparison on hover/focus",
      re.search(r'class="was">£1,250\.00</span><span class="arr" aria-hidden="true">→</span><span class="now">£1,296\.00</span>', rb) is not None and "+£46.00" in text(rb) and all(k in rb for k in ("<b>Current</b>", "<b>Workbook</b>", "<b>After</b>", "<b>Change</b>")))
check("only the figures that differ appear as change rows: for a managed area just the fee (its 'profit' is the same number, so it is not repeated), and not the unchanged revenue, days or occupancy", len(re.findall(r'class="chg-row"', rb)) == 1 and "Management fee earned" in rb, len(re.findall(r'class="chg-row"', rb)))
cb = pv[pv.index('id="p-op1"'):pv.index('id="p-mg1"')] if pv.index('id="p-op1"') < pv.index('id="p-mg1"') else pv[pv.index('id="p-op1"'):]
check("a PASS area keeps its checks collapsed ('All figures, checks and rows' is closed) and shows PASS quietly", '<details class="imp-more">' in cb and 'class="st st-pass"' in cb)
check("the full reconciliation table is still one click away, with its original heading", "Reconciliation (workbook vs what would be imported)" in pv)
nb = pv[pv.index('id="p-nc1"'):]
nblock = nb.split('id="p-')[1]
check("NO CONTROL is neutral: its own glyph and 'not a failure' wording, no REVIEW styling, and never in 'Needs attention'", 'class="st st-nc"' in nblock and "not a failure" in nblock and "st-review" not in nblock.split("</details>")[0] and 'data-state="nocontrol" id="p-nc1"' in pv and pv.index('id="p-nc1"') > pv.index("Will change"))
lb = pv.split('id="p-later"')[1].split('id="p-')[0]
check("an area that is not active says 'Not active in this period', never missing, no data or review", "Not active in this period" in lb and "REVIEW" not in lb and "No data" not in lb and "missing" not in lb.lower())
check("the single primary action is 'Apply for September'; Cancel is secondary; the acknowledgement appears because something is flagged",
      "Apply for September" in pv and 'form="imp-cancel"' in pv and 'name="ack"' in pv and pv.count(">Apply for") == 1)
check("the form fields Apply depends on are intact (month, fingerprint, exclude_form, include)", all(k in pv for k in ('name="month" value="2026-09"', 'name="fingerprint" value="fp123"', 'name="exclude_form" value="1"')))
check("the legend defining PASS, REVIEW and NO CONTROL is still on the page", all(s in pt for s in ("Figures reconcile with the available control.", "Something does not reconcile or needs checking.", "No comparison/control value was provided, so the check cannot be evaluated. This is not a failure.")))

mb = render([many_it], business)
mt = text(mb)
check("several figures that only lack a workbook total are ONE block (3 figures, one cause), not a stack of REVIEW boxes; a real failed check stays its own block",
      mb.count('class="imp-flag"') == 2 and "3 figures with money imported but no workbook total to confirm them" in mt and "Income £830.54 · Opex −£53.44 · Total costs −£53.44" in mt and "Workbook 12% · Configured 15%" in mt)
sp = render([change_it, idle_it, item("nr1", "Quiet House", "no_activity", counts(), None, None, PASS_C)], business)
check("an area with no activity or not active shows no PASS / NO CONTROL state (nothing was imported or compared), and is not counted in the rail", 'data-state="" id="p-nr1"' in sp and 'data-state="" id="p-later"' in sp and "st-pass" not in sp.split('id="p-nr1"')[1].split('id="p-')[0])

calm = render([same_it, idle_it], business, last={"op1": {"batch": 23, "date": "2026-10-12"}})
ct = text(calm)
check("nothing would change: one calm line 'Already up to date — this import would make no changes.' with when it was last imported; no apply button, no review UI",
      "Already up to date — this import would make no changes." in ct and "Last imported 2026-10-12" in ct and "import #23" in ct and "Apply for" not in ct and 'class="imp-prop"' not in calm.split('imp-quiet')[0])
still = item("dr2", "Dalton House", "review", counts(0, 0, 0, 12), fig(model="managed"), fig(model="managed"), BLANK_C + [REV_C[1]], True)
cs_ = render([still, idle_it], business, last={"dr2": {"batch": 15, "date": "2026-10-06"}})
cst = text(cs_)
check("re-importing the same workbook when a REVIEW already stands in it is still 'Already up to date' (nothing would be written): no apply button, the flagged area is shown as 'Still flagged', and the band says so",
      "Already up to date — this import would make no changes." in cst and "Apply for" not in cst and "Still flagged" in cst and "Needs attention" not in cst and "1 area still flagged in the workbook, no change would be made" in cst
      and 'name="ack"' not in cs_)
check("…and the band says 'Already up to date'", "Already up to date" in text(calm.split('class="imp-band"')[1].split("</section>")[0]))

css = (Path(__file__).resolve().parent.parent / "dashboard" / "static" / "style.css").read_text()
check("mobile: change rows stack vertically (current, workbook, after, change) instead of four columns, and the history becomes records", "@media (max-width: 700px)" in css and ".chg-row { grid-template-columns: 1fr auto; }" in css and ".imp-hist-table tr { display: grid;" in css)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
sys.exit(1 if FAILS else 0)
