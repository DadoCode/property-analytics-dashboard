"""Documents / evidence review redesign: an open, review-first evidence list and a split review workspace.
Documents are evidence only: nothing here may create an accounting entry, infer a period, or invent an extracted fact.
Scratch database and uploads folder only (extraction key forced off, so nothing reaches the network).

Run: .venv/bin/python tests/test_documents_redesign.py
"""
import io
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-documents-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("DASH_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
import services.extraction as extraction  # noqa: E402

extraction._load_key = lambda: None

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


db.ensure_schema()
c = db.get_conn()
for pid, name in [("larkfield", "Flat 602, Larkfield Wharf Building, 21 Pemberton Street, London, SE1 1AA"), ("nn4", "Flat 3, 48 Mill Road, NN4 3SX")]:
    c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES (?,?,?,?, 'flat', 1)", (pid, pid.upper(), name, name))
c.execute("INSERT INTO properties (id, code, name, address, type) VALUES ('general-overheads','GEN','General','', 'overhead')")
c.commit()
c.close()

from werkzeug.datastructures import MultiDict  # noqa: E402

from app import create_app  # noqa: E402

app = create_app()


def get(url):
    return app.test_client().get(url)


def page(url):
    return get(url).get_data(as_text=True)


def q(sql, *args):
    conn = db.get_conn()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def upload(name, data, doc_type="other", property_id=""):
    if isinstance(data, str):
        data = data.encode()
    app.test_client().post("/documents/upload", data={"document": (io.BytesIO(data), name), "doc_type": doc_type, "property_id": property_id},
                           content_type="multipart/form-data")
    return q("SELECT * FROM documents ORDER BY id DESC LIMIT 1")[0]


def ledger():
    return (q("SELECT COUNT(*) n FROM transactions")[0]["n"], q("SELECT COUNT(*) n FROM bookings")[0]["n"])


def names_in(html):
    return re.findall(r'<span class="dc-name" title="[^"]*">([^<]+)</span>', html)


GAS = "Date,Vendor,Description,Amount\n2026-09-14,British Gas,Gas and electricity,184.20\n"
CLEAN = "Date,Vendor,Description,Amount\n2026-09-02,CleanCo,Turnover clean,45.00\n2026-09-09,CleanCo,Deep clean,80.50\n"
AMZ = "Date,Vendor,Description,Amount\n2026-09-05,Amazon,Towels,34.99\n"

print("empty state")
empty = page("/documents")
check("no documents: a calm empty state, no rail, no list", "No documents yet" in empty and 'class="dc-rail"' not in empty and 'class="dc-row' not in empty)
check("the header is compact and labelled evidence only", "<h1>Documents</h1>" in empty and "Evidence only" in empty
      and "Accounting values are controlled by the" in empty and 'href="/imports"' in empty)

ledger_before = ledger()
d_gas = upload("British Gas Sep.csv", GAS, "utility_bill", "larkfield")          # clean: nothing flagged -> Unreviewed
d_clean = upload("Cleaning Sep.csv", CLEAN, "cleaning_invoice", "nn4")        # clean, two lines -> becomes Reviewed
d_amz = upload("Amazon orders.csv", AMZ, "amazon_order", "")                  # no property chosen, none matched -> Needs review
d_scan = upload("scan0042.png", b"\x89PNG\r\n\x1a\n" + b"0" * 60, "other", "")  # cannot be read: no period, no property
d_twin = upload("British Gas Sep copy.csv", GAS, "utility_bill", "larkfield")    # same kind, property, period, total -> possible duplicate
d_pdf = upload("statement.pdf", b"%PDF-1.4 fake", "bank_statement", "nn4")    # unreadable PDF, property chosen

check("uploading six documents created no transaction and no booking", ledger() == ledger_before == (0, 0))

print("review state is derived only from existing checks")
html = page("/documents")
main = html[html.index('<div class="dc">'):html.index('<script id="palette-data"')]   # the page itself, without the shell
check("a clean, fully matched document is Unreviewed (not flagged)", re.search(r"British Gas Sep\.csv.*?Unreviewed", html, re.S) is not None)
check("a document with no matched property is Needs review and says 'Property not linked'", "Property not linked" in html and re.search(r"Amazon orders\.csv.*?Property not linked.*?Needs review", html, re.S) is not None)
check("a possible duplicate says so (existing detection) and is Needs review", re.search(r"British Gas Sep copy\.csv.*?Possible duplicate", html, re.S) is not None)
check("an unreadable file says it could not be read", re.search(r"scan0042\.png.*?Couldn&#39;t read automatically", html, re.S) is not None)
check("states are a glyph and a word, never a pill", "dc-state" in html and 'class="pill' not in html)
check("the three states each carry a distinct glyph", all(g in html for g in ("●", "○")))

print("review-first ordering")
order = names_in(html)
flagged = {"Amazon orders.csv", "scan0042.png", "statement.pdf", "British Gas Sep copy.csv"}
check("flagged documents come first, the unflagged follow", set(order[:4]) == flagged and set(order[4:]) == {"British Gas Sep.csv", "Cleaning Sep.csv"}, order)
sections = re.findall(r'<h2 class="dc-gh"><span class="g-name">([^<]+)</span><span class="g-meta">(\d+)</span>', html)
check("group headers show a name and a count only, in review-first order", sections == [("Needs review", "4"), ("Unreviewed", "2")], sections)
check("inside a group the newest upload comes first", order[:4] == ["statement.pdf", "British Gas Sep copy.csv", "scan0042.png", "Amazon orders.csv"], order)

print("period and property are never invented")
scan = q("SELECT * FROM documents WHERE id=?", d_scan["id"])[0]
check("an unreadable scan has no stored period or property", scan["detected_year"] is None and scan["property_id"] is None)
row_scan = re.search(r'<a class="dc-row[^>]*href="/documents/%d/review">(.*?)</a>' % d_scan["id"], html, re.S).group(1)
check("an unknown period reads 'Period not confirmed', never the upload month", "Period not confirmed" in row_scan and "October 2026" not in row_scan and "Property not linked" in row_scan)
row_gas = re.search(r'<a class="dc-row[^>]*href="/documents/%d/review">(.*?)</a>' % d_gas["id"], html, re.S).group(1)
check("a detected period is written in full ('September 2026')", "September 2026" in row_gas)
check("the extracted amount and supplier appear quietly on the row", "£184.20" in row_gas and "British Gas" in row_gas)
check("every row says the file is kept as evidence", html.count("Kept as evidence") >= 6)
check("viewing the list leaves property and period untouched", q("SELECT property_id FROM documents WHERE id=?", d_amz["id"])[0]["property_id"] is None
      and q("SELECT detected_year FROM documents WHERE id=?", d_scan["id"])[0]["detected_year"] is None)

print("no clutter")
check("no dropzone, no workbook card, no summary tiles, no tab strip", all(x not in main for x in ("dropzone", "Import monthly workbook", 'class="tiles', 'class="tabs', "Source documents")))
check("upload is 'Upload evidence', never 'Import document'", "Upload evidence" in html and "Import document" not in html)
check("no confidence or health scores on the list", "Confidence" not in main and "health" not in main.lower())
check("no document action offers to import or post", all(x not in html for x in ("Confirm and import", "Approve import", "Post to ledger", "Add to ledger")))
check("one thin rail with a state key replaces the summary", html.count('class="rail-bar dc-bar"') == 1 and "dc-key" in html)
check("the key counts match the list", re.search(r"<b>4</b> Needs review", html) and re.search(r"<b>2</b> Unreviewed", html) and re.search(r"<b>0</b> Reviewed", html))

print("filters, tokens and menus")
for key, url in (("status", "/documents?d_status=review"), ("property", "/documents?d_property=larkfield"), ("type", "/documents?d_type=utility_bill"),
                 ("period", "/documents?d_period=2026-09"), ("search", "/documents?d_q=copy"), ("source", "/documents?d_source=spreadsheet")):
    h = page(url)
    check(f"filter by {key} narrows the list and shows a removable token", 'class="chip-x"' in h and "Remove filter" in h and len(names_in(h)) < len(names_in(html)), (url, names_in(h)))
check("status filter keeps only that state", set(names_in(page("/documents?d_status=unreviewed"))) == {"British Gas Sep.csv", "Cleaning Sep.csv"})
check("an older failed-status link now means needs review", "scan0042.png" in page("/documents?d_status=failed") and "British Gas Sep.csv" not in names_in(page("/documents?d_status=failed")))
check("the property filter menu lists real properties only", "Larkfield Wharf Building" in page("/documents") and "Property: " in page("/documents?d_property=larkfield"))
check("menus are labelled for assistive tech", all(a in html for a in ('aria-label="Filter by property"', 'aria-label="Filter by review state"', 'aria-label="Sort the list"', 'aria-label="Group the list"', 'aria-label="Search file name"')))
check("an explicit sort drops the status grouping", "dc-gh" not in page("/documents?d_sort=uploaded_asc") and names_in(page("/documents?d_sort=uploaded_asc"))[0] == "British Gas Sep.csv")
check("group by property shows property names and counts", "g-name" in page("/documents?d_group=property") and "Property not linked" in page("/documents?d_group=property"))
check("an empty filtered result says nothing matches, with a way back", "No documents match" in page("/documents?d_q=zzzz"))

print("upload")
check("upload is a quiet panel posting to the existing route", 'action="/documents/upload"' in html and 'class="upload-form dc-form"' in html and 'type="file" name="document" multiple required' in html)
check("it asks only for the file, an optional property and a type", html.count("<select") >= 2 and "Detect property" in html)
check("the AI-key note stays hidden until a file needs it", 'id="ai-note" hidden' in html)
pre = page("/documents?property=larkfield&type=utility_bill")
check("a deep link opens the panel with property and type already set", re.search(r'<details class="dc-upload" id="upload" open', pre) and 'value="larkfield" selected' in pre and "already set" in pre)

print("review workspace")
rv = page(f"/documents/{d_gas['id']}/review")
check("the five inspector sections are present", all(f'id="s-{k}"' in rv for k in ("doc", "facts", "link", "rev", "src")))
check("the source viewer and inspector share a split", 'class="rv-split"' in rv and 'class="dr-insp"' in rv and 'class="rv-viewer"' in rv)
check("document facts: type, property, period, uploaded", all(x in rv for x in ("Utility bill", "September 2026", "Larkfield Wharf Building")))
check("extracted facts are label-over-value and read-only", all(f"<dt>{x}</dt>" in rv for x in ("Supplier", "Date", "Amount", "Reference", "Category")) and "14 Sep 2026" in rv and "£184.20" in rv)
check("a fact the extraction never stored is '—', not guessed", re.search(r"<dt>Reference</dt><dd><span class=\"fx-none\">—</span>", rv) is not None)
has_conf = q("SELECT COUNT(*) n FROM document_items WHERE document_id=? AND confidence IS NOT NULL", d_gas["id"])[0]["n"] > 0
check("confidence is shown only where one was stored", ("Confidence" in rv) == has_conf)
check("no linked financial record is a neutral line", "No linked financial record." in rv)
check("the review section shows the state and one 'Mark reviewed' action", "Mark reviewed" in rv and rv.count("Mark reviewed") == 1 and "Unreviewed" in rv)
check("the only note under it is that nothing reaches the figures", "Kept as evidence. Nothing is added to your figures." in rv)
check("the file is reachable from the page", f'/documents/{d_gas["id"]}/file' in rv and get(f"/documents/{d_gas['id']}/file").status_code == 200)
check("corrections are tucked behind one quiet disclosure", "Correct extracted values" in rv and 'id="review-form"' in rv and 'form="review-form"' in rv)
check("viewer controls are labelled and the page has a back link", "← Documents" in rv.replace("&larr;", "←") and all(a in page(f"/documents/{q('SELECT id FROM documents WHERE filename=?', 'statement.pdf')[0]['id']}/review") for a in ('aria-label="Next page"', 'aria-label="Zoom in"')))
check("the queue control moves through the list", re.search(r"\d of 6", rv) and 'aria-label="Next document"' in rv + page(f"/documents/{d_pdf['id']}/review") + page(f"/documents/{d_scan['id']}/review"))

multi = page(f"/documents/{d_clean['id']}/review")
check("a multi-line document shows its total and a read-only line list", "Extracted total" in multi and "£125.50" in multi and 'class="fx-lines"' in multi and "Turnover clean" in multi)

none = page(f"/documents/{d_scan['id']}/review")
check("nothing extracted: the plain evidence line is shown", "No structured facts extracted. The file is still retained as evidence." in none)
check("unknown property and period are shown as unknown, with reasons", "Property not linked" in none and "Period not confirmed" in none and "Needs review" in none)

dup = page(f"/documents/{d_twin['id']}/review")
check("a duplicate links to the earlier upload and nothing is merged or removed", f"/documents/{d_gas['id']}/review" in dup and "Possible duplicate" in dup
      and q("SELECT COUNT(*) n FROM documents WHERE filename LIKE 'British Gas Sep%'")[0]["n"] == 2)

unmatched = page(f"/documents/{d_amz['id']}/review")
check("the property linking workflow stays: line selects, Property not linked", "Property not linked" in unmatched and 'name="property_id"' in unmatched)

print("linked evidence points at existing records")
conn = db.get_conn()
conn.execute("INSERT INTO transactions (property_id, date, vendor, description, amount, direction, category, source, document_id) VALUES ('larkfield','2026-09-14','British Gas','Gas and electricity',184.20,'expense','utilities','excel_import',NULL)")
tx_id = conn.execute("SELECT MAX(id) FROM transactions").fetchone()[0]
line = conn.execute("SELECT id FROM document_items WHERE document_id=? LIMIT 1", (d_gas["id"],)).fetchone()[0]
conn.execute("UPDATE document_items SET duplicate_of=? WHERE id=?", (tx_id, line))
conn.commit()
conn.close()
before = ledger()
lk = page(f"/documents/{d_gas['id']}/review")
check("a matching recorded cost is linked, through the existing transaction drawer", "Matches" in lk and f"/expenses/transactions/{tx_id}" in lk and "British Gas · £184.20" in lk)
check("the link names the property and category", "Utilities" in lk and "Larkfield Wharf" in lk)
check("viewing linked evidence creates no second transaction", ledger() == before)
check("the existing transaction drawer opens from that link", get(f"/expenses/transactions/{tx_id}").status_code == 200)

print("marking reviewed is the only write, and it never touches the ledger")
ledger_now = ledger()
cl = app.test_client()
items = q("SELECT * FROM document_items WHERE document_id=?", d_clean["id"])
form = []
for it in items:
    form += [("item_id", it["id"]), ("include", it["id"]), ("property_id", "nn4"), ("vendor", it["vendor"]), ("description", it["raw_description"] or ""), ("category", it["category"] or "other"),
             ("type", "opex"), ("date", it["date"]), ("amount", it["amount"])]
r = cl.post(f"/documents/{d_clean['id']}/confirm", data=MultiDict(form), follow_redirects=True)
t = r.get_data(as_text=True)
check("mark reviewed keeps it as evidence and says nothing was added", "Marked reviewed" in t and "Nothing was added to your figures" in t)
check("the document is stored as reviewed with its lines retained", q("SELECT status, reviewed FROM documents WHERE id=?", d_clean["id"])[0]["status"] == "confirmed"
      and q("SELECT reviewed FROM documents WHERE id=?", d_clean["id"])[0]["reviewed"] == 1 and q("SELECT COUNT(*) n FROM document_items WHERE document_id=?", d_clean["id"])[0]["n"] == 2)
check("no transaction or booking was created", ledger() == ledger_now and q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", d_clean["id"])[0]["n"] == 0)
after = page("/documents")
check("the older confirmed-status link now means reviewed", names_in(page("/documents?d_status=confirmed")) == ["Cleaning Sep.csv"] and names_in(page("/documents?d_status=complete")) == ["Cleaning Sep.csv"])
check("it moves to the Reviewed group and recedes", re.search(r"Cleaning Sep\.csv.*?Reviewed", after, re.S) and names_in(after)[-1] == "Cleaning Sep.csv" and "dc-reviewed" in after)
rev = page(f"/documents/{d_clean['id']}/review")
check("the reviewed page shows ✓ Reviewed, the evidence line and a way to reopen", "Reviewed" in rev and "Kept as supporting evidence" in rev and "Reopen for review" in rev and "Mark reviewed" not in rev)
check("a reviewed document offers no correction form", 'id="review-form"' not in rev)

print("legacy rows an earlier version posted stay visible and linked")
conn = db.get_conn()
conn.execute("INSERT INTO transactions (property_id, date, vendor, description, amount, direction, category, source, document_id) VALUES ('nn4','2026-09-02','CleanCo','Turnover clean',45,'expense','cleaning','upload',?)", (d_clean["id"],))
legacy_id = conn.execute("SELECT MAX(id) FROM transactions").fetchone()[0]
conn.commit()
conn.close()
leg = page(f"/documents/{d_clean['id']}/review")
check("an earlier-version record is listed as 'Supports' with a drawer link", "Supports" in leg and f"/expenses/transactions/{legacy_id}" in leg and "an earlier version of the app posted these" in leg and "Undo this import" in leg)
conn = db.get_conn()
conn.execute("DELETE FROM transactions WHERE id=?", (legacy_id,))
conn.commit()
conn.close()

print("drawer, accessibility and responsive hooks")
dr = get(f"/documents/{d_gas['id']}/drawer").get_data(as_text=True)
check("the old document drawer speaks the same language", "Evidence only" in dr and "Unreviewed" in dr and "Property not linked" not in dr)
check("rows are real links with the whole row as the target", len(re.findall(r'<a class="dc-row', html)) == 6 and all(re.search(r'<a class="dc-row[^>]*href="/documents/\d+/review"', html) for _ in [0]))
check("cells carry visually-hidden labels for screen readers", 'class="vh">Property: ' in html and 'class="vh">Period: ' in html and 'class="vh">State: ' in html)
check("the list header row is hidden from assistive tech (rows are self-describing)", 'class="dc-cols" aria-hidden="true"' in html)
check("keyboard: menus are details/summary, focusable and escapable; chips are links", "<summary" in html and 'class="chip-x"' in page("/documents?d_status=review"))
css = open(Path(__file__).resolve().parent.parent / "dashboard" / "static" / "style.css").read()
check("mobile: rows collapse to a compact stack and the split collapses to one column", "@media (max-width: 760px)" in css and "grid-template-areas: \"file file file\" \"meta meta meta\" \"per amt state\"" in css
      and ".rv-split { grid-template-columns: minmax(0, 1fr); gap: var(--s4); }" in css)
check("mobile: the source preview comes before the inspector in the markup", rv.index('class="rv-viewer"') < rv.index('class="dr-insp"'))
pdoc = page("/properties/nn4/documents")
check("the property Documents tab uses the same state words", "Confirmed" not in pdoc and "Reviewed" in pdoc and "Needs review" in pdoc)

print("demo mode keeps uploads off")
os.environ["DASH_DEMO_MODE"] = "1"
demo = create_app().test_client()
dd = demo.get("/documents").get_data(as_text=True)
check("the demo shows the list and says uploads are off", "Uploads are off in this demo" in dd and "Upload evidence" in dd)
os.environ.pop("DASH_DEMO_MODE", None)

check("only the explicit test rows exist in the ledger", q("SELECT COUNT(*) n FROM transactions WHERE source='upload'")[0]["n"] == 0)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
if FAILS:
    print("FAILED:", *FAILS, sep="\n  - ")
    sys.exit(1)
