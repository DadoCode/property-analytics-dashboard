import datetime
import json
import mimetypes
from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

import db
import services.ingest as ingest
import services.reconcile as rc
from services import runtime
from services.audit import record
from services.common import CATEGORIES, get_properties, get_property
from services.common import MONTH_NAMES, short_name
import services.review as review_helpers
from services.documents import find_duplicate_reservation, save_upload

bp = Blueprint("documents", __name__)

DOC_TYPE_LABELS = ingest.DOC_TYPE_NAMES
INLINE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _period_text(doc):
    """'Feb 2026' / 'Jan–Mar 2026' for a document, or None when undetected."""
    period = ingest.detection_of(doc).get("period")
    if period:
        return ingest.period_label(period)
    if doc["detected_year"] and doc["detected_month"]:
        return f"{MONTH_NAMES[doc['detected_month']][:3]} {doc['detected_year']}"
    return None


@bp.route("/documents/<int:doc_id>/drawer")
def drawer(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return "<p class='note'>Document not found.</p>", 404
    prop = get_property(conn, doc["property_id"])
    stats = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt, SUM(include) inc FROM document_items WHERE document_id=?", (doc_id,)).fetchone()
    v = _doc_views(conn, doc_id)[0]
    return render_template(
        "partials/document_drawer.html", doc=doc, prop=prop, stats=stats, state=v["state"], state_label=v["state_label"], glyph=v["glyph"],
        type_label=DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"] or "Document"),
        period=_period_full(doc), warnings=ingest.detection_of(doc).get("warnings", []),
    )


@bp.route("/documents/<int:doc_id>/file")
def file(doc_id):
    doc = db.get_conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        abort(404)
    path = runtime.resolve_stored(doc["stored_path"])
    if not path:
        return "The original file is no longer on disk (it may have been moved or deleted).", 404
    # Only PDFs and plain images are shown in the browser (the review page needs that). Anything else is a download, so an uploaded
    # HTML/SVG page can never run inside the app's own origin. nosniff stops the browser second-guessing the type.
    mime = mimetypes.guess_type(doc["filename"] or path.name)[0] or "application/octet-stream"
    inline = mime == "application/pdf" or mime in INLINE_IMAGE_TYPES
    resp = send_file(path, mimetype=mime if inline else "application/octet-stream", as_attachment=not inline,
                     download_name=doc["filename"] or path.name, conditional=True)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ---- the evidence list ---------------------------------------------------
# A document is in exactly one of three review states, decided only by checks the app already makes
# (see _flags). Nothing here is inferred from an upload date, and nothing here touches the ledger.
STATE_RANK = {"review": 0, "unreviewed": 1, "reviewed": 2}
STATE_LABEL = {"review": "Needs review", "unreviewed": "Unreviewed", "reviewed": "Reviewed"}
STATE_GLYPH = {"review": "\u25cf", "unreviewed": "\u25cb", "reviewed": "\u2713"}
# older links and bookmarks used the stored-status filter keys
_STATUS_ALIASES = {"confirmed": "reviewed", "complete": "reviewed", "failed": "review", "uploaded": "unreviewed", "processing": "unreviewed"}
SOURCE_LABEL = {"spreadsheet": "Spreadsheet", "ai": "AI extraction", "none": "Not read"}
_SORTS = {"review": "Review first", "uploaded_desc": "Newest upload", "uploaded_asc": "Oldest upload",
          "period_desc": "Period, newest", "period_asc": "Period, oldest"}
_GROUPS = {"status": "Status", "property": "Property", "type": "Type", "period": "Period"}
_UNMATCHED_CODES = {"property_missing", "property_ambiguous", "po_unmatched"}
_DUP_CODES = {"duplicate_file", "duplicate_content", "duplicate_rows"}
_LOW = 0.7        # services.documents.LOW_CONFIDENCE


def fdate(iso):
    """'2026-09-05' -> '5 Sep 2026'. Anything that is not a clean ISO date is shown exactly as read."""
    if not iso:
        return None
    try:
        d = datetime.date.fromisoformat(str(iso)[:10]) if len(str(iso)) >= 10 else None
    except ValueError:
        d = None
    return f"{d.day} {ingest.MONTH_ABBR[d.month]} {d.year}" if d else str(iso)


def _period_full(doc):
    """'September 2026', 'Jan\u2013Mar 2026', or None when the period was never confirmed (never guessed from the upload date)."""
    period = ingest.detection_of(doc).get("period")
    if period and period.get("from"):
        a, b = period["from"], period.get("to")
        if not b or b == a:
            y, m = map(int, a.split("-"))
            return f"{MONTH_NAMES[m]} {y}"
        return ingest.period_label(period)
    if doc["detected_year"] and doc["detected_month"]:
        return f"{MONTH_NAMES[doc['detected_month']]} {doc['detected_year']}"
    return None


def _flags(doc, det, agg, linked):
    """Why an unreviewed document needs a person, in plain words. Only existing checks: extraction failed, property not matched, period not
    confirmed, amount missing or uncertain, a conflict the upload already warned about, a possible duplicate."""
    codes = {w.get("code") for w in det.get("warnings", []) if w.get("level") == "warn"}
    reasons = []
    if doc["status"] == "failed":
        reasons.append("Couldn't read automatically")
    if not linked:
        reasons.append("Property not linked")
    elif codes & _UNMATCHED_CODES:
        reasons.append("Property uncertain")
    if not (det.get("period") or (doc["detected_year"] and doc["detected_month"])):
        reasons.append("Period not confirmed")
    elif "period_mismatch" in codes:
        reasons.append("Period conflicts with the lines")
    if doc["duplicate_of_document"] or codes & _DUP_CODES:
        reasons.append("Possible duplicate")
    if agg and agg["missing"]:
        reasons.append("Amount missing")
    elif agg and agg["low"] or "partial_extraction" in codes:
        reasons.append("Amount uncertain")
    if "income_guess" in codes:
        reasons.append("Category uncertain")
    known = _UNMATCHED_CODES | _DUP_CODES | {"period_mismatch", "period_missing", "partial_extraction", "income_guess"}
    if codes - known:
        reasons.append("Flagged on upload")
    return reasons


def _doc_views(conn, only_id=None):
    """One lightweight view per document: everything the list and the queue need, computed from stored fields in a single pass."""
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    aggs = {r["document_id"]: r for r in conn.execute(
        """SELECT document_id, COUNT(*) n, COALESCE(SUM(include),0) n_inc,
                  SUM(CASE WHEN include=1 AND property_id IS NULL THEN 1 ELSE 0 END) unlinked,
                  SUM(CASE WHEN include=1 AND ((item_kind='reservation' AND net_revenue IS NULL) OR (item_kind!='reservation' AND (amount IS NULL OR amount<=0))) THEN 1 ELSE 0 END) missing,
                  SUM(CASE WHEN include=1 AND confidence IS NOT NULL AND confidence<? AND reviewed=0 THEN 1 ELSE 0 END) low,
                  SUM(CASE WHEN include=1 THEN ABS(CASE WHEN item_kind='reservation' THEN COALESCE(net_revenue,0) ELSE COALESCE(amount,0) END) ELSE 0 END) total,
                  COUNT(DISTINCT property_id) n_props, MAX(property_id) one_prop,
                  COUNT(DISTINCT NULLIF(vendor,'')) n_vendors, MAX(NULLIF(vendor,'')) one_vendor
           FROM document_items GROUP BY document_id""", (_LOW,))}
    sql = "SELECT * FROM documents" + (" WHERE id=?" if only_id else "")
    out = []
    for d in conn.execute(sql, (only_id,) if only_id else ()):
        det, agg = ingest.detection_of(d), aggs.get(d["id"])
        pid = d["property_id"]
        if pid:
            prop_text, linked = names.get(pid, pid), True
        else:
            # no single property chosen at upload: say what the lines themselves matched, and whether every included line was matched
            linked = bool(agg and agg["n_inc"] and not agg["unlinked"])
            n_props = agg["n_props"] if agg else 0
            prop_text = None if not n_props else (names.get(agg["one_prop"], agg["one_prop"]) if n_props == 1 else f"Multiple properties ({n_props})")
        reviewed = d["status"] == "confirmed"
        reasons = [] if reviewed else _flags(d, det, agg, linked)
        state = "reviewed" if reviewed else ("review" if reasons else "unreviewed")
        method = det.get("method") or ""
        source = "spreadsheet" if method.startswith("spreadsheet") else ("ai" if method.startswith("AI") else "none")
        total = det.get("total") if det.get("total") is not None else (agg["total"] if agg else None)
        out.append({
            "id": d["id"], "filename": d["filename"], "doc_type": d["doc_type"], "type_label": DOC_TYPE_LABELS.get(d["doc_type"], d["doc_type"] or "Document"),
            "property_id": pid, "property_text": prop_text, "linked": linked,
            "period": _period_full(d), "period_key": (d["detected_year"], d["detected_month"]) if d["detected_year"] and d["detected_month"] else None,
            "state": state, "state_label": STATE_LABEL[state], "glyph": STATE_GLYPH[state], "reasons": reasons,
            "amount": round(total, 2) if (agg and agg["n_inc"] and total) else None,
            "supplier": agg["one_vendor"] if (agg and agg["n_vendors"] == 1) else None, "n_lines": agg["n"] if agg else 0,
            "source": source, "source_label": SOURCE_LABEL[source], "uploaded_at": d["uploaded_at"], "uploaded": fdate(d["uploaded_at"][:10]),
            "stored_path": d["stored_path"], "duplicate": bool(d["duplicate_of_document"]) or any(r == "Possible duplicate" for r in reasons),
        })
    return out


def _review_order(views):
    """Review first (needs review, unreviewed, reviewed), newest first inside each."""
    ordered = sorted(views, key=lambda v: (v["uploaded_at"], v["id"]), reverse=True)
    return sorted(ordered, key=lambda v: STATE_RANK[v["state"]])


def _last_import(conn):
    """The most recently applied workbook import (kept for callers that still link to it)."""
    try:
        row = conn.execute("SELECT id, period, applied_at FROM import_batches WHERE status='applied' AND kind='workbook' AND period IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:                       # a demo database that predates the import tables
        return None
    if not row:
        return None
    from routes.imports import month_label
    return {"id": row["id"], "period_label": month_label(row["period"]), "applied_at": row["applied_at"]}


@bp.route("/documents")
def index():
    conn = db.get_conn()
    a = request.args
    prefill_property = a.get("property") or ""
    prefill_type = a.get("type") if a.get("type") in DOC_TYPE_LABELS else ""
    f = {"d_property": a.get("d_property") or prefill_property, "d_type": a.get("d_type") or "", "d_period": a.get("d_period") or "",
         "d_source": a.get("d_source") if a.get("d_source") in SOURCE_LABEL else "", "d_q": (a.get("d_q") or "").strip()}
    f_status = _STATUS_ALIASES.get(a.get("d_status") or "", a.get("d_status") or "")
    f_status = f_status if f_status in STATE_RANK else ""
    sort = a.get("d_sort") if a.get("d_sort") in _SORTS else "review"
    group = a.get("d_group") if "d_group" in a else ("status" if sort == "review" else "")
    group = group if group in _GROUPS else ""

    everything = _doc_views(conn)
    names = {p["id"]: p["name"] for p in get_properties(conn)}

    def keep(v, skip=None):
        if skip != "d_property" and f["d_property"] and v["property_id"] != f["d_property"]:
            return False
        if skip != "d_type" and f["d_type"] and v["doc_type"] != f["d_type"]:
            return False
        if skip != "d_period" and f["d_period"] and not (v["period_key"] and "%d-%02d" % v["period_key"] == f["d_period"]):
            return False
        if skip != "d_source" and f["d_source"] and v["source"] != f["d_source"]:
            return False
        if skip != "d_q" and f["d_q"] and f["d_q"].lower() not in v["filename"].lower():
            return False
        return True

    scoped = [v for v in everything if keep(v)]
    counts = {k: sum(1 for v in scoped if v["state"] == k) for k in STATE_RANK}
    rows = [v for v in scoped if not f_status or v["state"] == f_status]
    total_all = len(everything)

    if sort == "review":
        rows = _review_order(rows)
    elif sort.startswith("uploaded"):
        rows = sorted(rows, key=lambda v: (v["uploaded_at"], v["id"]), reverse=sort.endswith("desc"))
    else:       # documents with no confirmed period always sink to the bottom
        have = sorted([v for v in rows if v["period_key"]], key=lambda v: (v["period_key"], v["uploaded_at"]), reverse=sort.endswith("desc"))
        rows = have + sorted([v for v in rows if not v["period_key"]], key=lambda v: v["uploaded_at"], reverse=True)

    overflow = max(0, len(rows) - 200)
    rows = rows[:200]
    for v in rows:
        v["file_ok"] = runtime.resolve_stored(v["stored_path"]) is not None

    groups = None
    if group:
        def label_of(v):
            return {"status": v["state_label"], "property": v["property_text"] or "Property not linked", "type": v["type_label"],
                    "period": v["period"] or "Period not confirmed"}[group]
        order, seen = [], {}
        for v in rows:
            k = label_of(v)
            if k not in seen:
                seen[k] = []
                order.append(k)
            seen[k].append(v)
        if group == "property":
            order.sort(key=lambda k: (k == "Property not linked", k.lower()))
        elif group == "type":
            order.sort(key=str.lower)
        groups = [{"label": k, "n": len(seen[k]), "rows": seen[k]} for k in order]

    def U(**changes):
        base = {k: v for k, v in {**f, "d_status": f_status, "d_sort": sort if sort != "review" else "", "d_group": a.get("d_group") if "d_group" in a else None}.items() if v}
        for k, v in changes.items():
            if v is None or v == "":
                base.pop(k, None)
            else:
                base[k] = v
        return url_for("documents.index", **base) if base else url_for("documents.index")

    # options for each menu come from what exists, never from a fixed list
    periods = sorted({v["period_key"] for v in everything if v["period_key"]}, reverse=True)
    menus = []
    prop_opts = sorted({(v["property_id"], v["property_text"]) for v in everything if v["property_id"]}, key=lambda t: t[1].lower())
    menus.append(("Property", "Filter by property", f"Property: {short_name(names.get(f['d_property'], f['d_property']))}" if f["d_property"] else None,
                  [("All properties", U(d_property=None), not f["d_property"])] + [(short_name(n), U(d_property=i), f["d_property"] == i) for i, n in prop_opts]))
    if periods:
        menus.append(("Period", "Filter by period", f"Period: {MONTH_NAMES[int(f['d_period'][5:])]} {f['d_period'][:4]}" if f["d_period"] and len(f["d_period"]) == 7 and f["d_period"][5:].isdigit() else None,
                      [("Any period", U(d_period=None), not f["d_period"])] + [(f"{MONTH_NAMES[m]} {y}", U(d_period="%d-%02d" % (y, m)), f["d_period"] == "%d-%02d" % (y, m)) for y, m in periods]))
    types_present = [k for k in DOC_TYPE_LABELS if any(v["doc_type"] == k for v in everything)]
    menus.append(("Type", "Filter by document type", f"Type: {DOC_TYPE_LABELS[f['d_type']]}" if f["d_type"] in DOC_TYPE_LABELS else None,
                  [("All types", U(d_type=None), not f["d_type"])] + [(DOC_TYPE_LABELS[k], U(d_type=k), f["d_type"] == k) for k in types_present]))
    menus.append(("Status", "Filter by review state", f"Status: {STATE_LABEL[f_status]}" if f_status else None,
                  [("All", U(d_status=None), not f_status)] + [(f"{STATE_LABEL[k]} ({counts[k]})", U(d_status=k), f_status == k) for k in STATE_RANK]))
    sources_present = [k for k in SOURCE_LABEL if any(v["source"] == k for v in everything)]
    if len(sources_present) > 1 or f["d_source"]:
        menus.append(("Source", "Filter by how the file was read", f"Source: {SOURCE_LABEL[f['d_source']]}" if f["d_source"] else None,
                      [("Any source", U(d_source=None), not f["d_source"])] + [(SOURCE_LABEL[k], U(d_source=k), f["d_source"] == k) for k in sources_present]))
    group_menu = ("Group", "Group the list", f"Group: {_GROUPS[group]}" if group else None,
                  [("No grouping", U(d_group="none"), not group)] + [(lbl, U(d_group=k), group == k) for k, lbl in _GROUPS.items()])
    sort_menu = ("Sort", "Sort the list", f"Sort: {_SORTS[sort]}", [(lbl, U(d_sort=k if k != "review" else None, d_group=None), sort == k) for k, lbl in _SORTS.items()])

    chips = []
    for key, label in (("d_property", f"Property: {short_name(names.get(f['d_property'], f['d_property']))}"), ("d_type", f"Type: {DOC_TYPE_LABELS.get(f['d_type'], f['d_type'])}"),
                       ("d_period", f"Period: {f['d_period']}"), ("d_source", f"Source: {SOURCE_LABEL.get(f['d_source'], '')}"), ("d_q", f"\u201c{f['d_q']}\u201d")):
        if f[key]:
            if key == "d_period" and len(f["d_period"]) == 7 and f["d_period"][5:].isdigit():
                label = f"Period: {MONTH_NAMES[int(f['d_period'][5:])]} {f['d_period'][:4]}"
            chips.append({"label": label, "href": U(**{key: None})})
    if f_status:
        chips.append({"label": f"Status: {STATE_LABEL[f_status]}", "href": U(d_status=None)})
    filtered = bool(chips)

    return render_template(
        "documents.html", active="documents", all_properties=get_properties(conn), active_property=None, recon_needed=rc.needed_count(conn), recon_total=len(rc.candidates(conn)),
        rows=rows, groups=groups, counts=counts, scoped_total=len(scoped), total=total_all, overflow=overflow, doc_types=DOC_TYPE_LABELS,
        menus=menus, group_menu=group_menu, sort_menu=sort_menu, chips=chips, filtered=filtered, U=U, f=f, f_status=f_status, sort=sort,
        extraction_available=extraction_available(), prefill_property=prefill_property, prefill_type=prefill_type,
        prefill_property_name=short_name(names.get(prefill_property, prefill_property)) if prefill_property else None,
    )


def extraction_available():
    import services.extraction as extraction
    return extraction.available()


@bp.route("/documents/upload", methods=["POST"])
def upload():
    conn = db.get_conn()
    files = request.files.getlist("document")
    files = [f for f in files if f and f.filename]
    if not files:
        flash("Choose at least one file before uploading.", "warning")
        return redirect(url_for("documents.index"))
    doc_type = request.form.get("doc_type", "other")
    property_id = request.form.get("property_id") or None
    last_doc_id = None
    for file in files:
        last_doc_id = save_upload(conn, file, doc_type, property_id, flash)
    if len(files) == 1 and last_doc_id:
        return redirect(url_for("documents.review", doc_id=last_doc_id))
    return redirect(url_for("documents.index"))


@bp.route("/property/<property_id>/upload", methods=["POST"])
def upload_property(property_id):
    conn = db.get_conn()
    file = request.files.get("document")
    if not file or not file.filename:
        flash("Choose a file before uploading.", "warning")
        return redirect(url_for("properties.detail", property_id=property_id))
    doc_id = save_upload(conn, file, request.form.get("doc_type", "other"), property_id, flash)
    if not doc_id:
        return redirect(url_for("properties.documents_tab", property_id=property_id))
    return redirect(url_for("documents.review", doc_id=doc_id))


def _size_text(n):
    if not n:
        return None
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(n / 1024, 1):.0f} KB"


def _summary_strip(doc, prop, items, detection, names):
    """Everything a reviewer needs to judge the parse before confirming:
    what the file was taken to be, which property/period was chosen and
    how, what was read, and how sure the parser was."""
    res = bool(items and items[0]["item_kind"] == "reservation")
    amount_of = (lambda i: i["net_revenue"]) if res else (lambda i: i["amount"])
    vals = [(i, abs(amount_of(i) or 0)) for i in items]
    extracted_total = detection.get("total")
    if extracted_total is None:
        extracted_total = round(sum(v for _, v in vals), 2)
    included_total = round(sum(v for i, v in vals if i["include"]), 2)
    income = round(sum(v for i, v in vals if not res and i["direction"] == "income"), 2)
    expense = round(sum(v for i, v in vals if not res and i["direction"] != "income"), 2)
    prop_info = detection.get("property") or {}
    confs = [i["confidence"] for i in items if i["confidence"] is not None]
    low = sum(1 for c in confs if c < 0.7)
    if prop:
        prop_how = prop_info.get("source") or "selected"
    elif prop_info.get("candidates"):
        prop_how = "unsure: " + " or ".join(c["name"] for c in prop_info["candidates"][:2])
    else:
        prop_how = "not detected"
    multi_props = len({i["property_id"] for i in items if i["property_id"]})
    return {
        "filename": doc["filename"], "size": _size_text(doc["file_size"]), "uploaded": doc["uploaded_at"],
        "doc_type": DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"] or "Document"),
        "property": prop["name"] if prop else ("Multiple properties (%d)" % multi_props if multi_props > 1 else None),
        "property_how": prop_how if not (multi_props > 1 and not prop) else "matched line by line",
        "period": _period_text(doc), "period_how": (detection.get("period") or {}).get("source"),
        "rows": len(items), "included": sum(1 for i in items if i["include"]), "noun": "reservation" if res else "line",
        "extracted_total": extracted_total, "included_total": included_total, "income": income, "expense": expense,
        "edited_total": abs(included_total - extracted_total) > 0.004 and doc["status"] != "confirmed",
        "confidence": round(sum(confs) / len(confs) * 100) if confs else None, "low_confidence": low,
        "method": detection.get("method"),
    }


@bp.route("/documents/<int:doc_id>/review")
def review(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("We couldn't find that document. It may have been removed; check the Documents list.", "error")
        return redirect(url_for("overview.index"))
    prop = get_property(conn, doc["property_id"])
    items = conn.execute(
        "SELECT *, raw_description AS description FROM document_items WHERE document_id=? ORDER BY line_index", (doc_id,)
    ).fetchall()
    today = datetime.date.today()
    year = doc["detected_year"] or today.year
    month = doc["detected_month"] or today.month
    res_mode = bool(items and items[0]["item_kind"] == "reservation") or (not items and doc["doc_type"] == "booking_statement")
    confirmed = doc["status"] == "confirmed"

    views = []
    for it in items:
        orig = review_helpers.original_of(it)
        row = it
        dup = None          # a document never posts to the ledger, so there is no earlier row to decide against
        include = bool(it["include"])
        if dup:  # a duplicate replaces the earlier upload (or is left out if it matches the Excel history) unless you choose otherwise
            include = (it["dup_decision"] or dup["default"]) != "exclude"
        overlap = ingest.excel_overlap(conn, it["property_id"], it["check_in"]) if (not confirmed and not dup and it["item_kind"] == "reservation") else 0.0
        views.append({
            "row": row, "dup": dup, "include": include, "changed": review_helpers.changed_fields(it), "excel_overlap": overlap,
            "orig": {"note": orig.get("_note"), "status": orig.get("status"), "status_note": orig.get("_status_note"), "po": orig.get("po"), "po_note": orig.get("_po_note"), "order_id": orig.get("order_id"),
                     "vendor": orig.get("vendor"), "description": orig.get("description"), "amount": orig.get("amount"),
                     "category": orig.get("category"), "date": orig.get("date"),
                     "check_in": orig.get("check_in"), "check_out": orig.get("check_out"), "net": orig.get("net"),
                     "gross": orig.get("gross"), "fees": orig.get("fees")},
        })

    summary = None
    created = {"transactions": [], "bookings": []}
    if confirmed:
        created["transactions"] = conn.execute(
            """SELECT t.id, t.date, t.vendor, t.description, t.amount, t.category, p.name AS property_name
               FROM transactions t JOIN properties p ON p.id=t.property_id WHERE t.document_id=? ORDER BY t.date, t.id""", (doc_id,)).fetchall()
        created["bookings"] = conn.execute(
            """SELECT b.id, b.check_in, b.check_out, b.platform, b.net_revenue, p.name AS property_name
               FROM bookings b JOIN properties p ON p.id=b.property_id WHERE b.document_id=? ORDER BY b.check_in, b.id""", (doc_id,)).fetchall()
        added = len(created["transactions"]) + len(created["bookings"])
        month_src = [(v["row"]["check_in"] if v["row"]["item_kind"] == "reservation" else v["row"]["date"]) for v in views if v["row"]["include"]]
        month_src = sorted(m[:7] for m in month_src if m and len(m) >= 7)
        summary = {"month": month_src[0] if month_src else None, "added": added, "excluded": sum(1 for v in views if not v["row"]["include"]),
                   "corrected": sum(1 for v in views if v["changed"]),
                   "noun": "reservation" if res_mode else "transaction"}

    focus_id = request.args.get("item", type=int)
    focus = next((v["row"] for v in views if v["row"]["id"] == focus_id), None) if focus_id else None
    fname = (doc["filename"] or "").lower()
    is_pdf = fname.endswith(".pdf")
    is_image = fname.endswith((".png", ".jpg", ".jpeg", ".webp"))
    stored_file = runtime.resolve_stored(doc["stored_path"])
    preview = review_helpers.file_preview(stored_file) if (stored_file and not (is_pdf or is_image)) else None

    detection = ingest.detection_of(doc)
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    # reservation lines whose listing matches none of your properties yet: one decision per listing, remembered for next time
    unmatched = []
    if res_mode and not confirmed:
        unmatched = [{"listing": r["raw_description"], "n": r["n"]} for r in conn.execute(
            """SELECT raw_description, COUNT(*) n FROM document_items WHERE document_id=? AND item_kind='reservation'
                 AND property_id IS NULL AND raw_description IS NOT NULL AND raw_description != ''
               GROUP BY raw_description ORDER BY n DESC, raw_description""", (doc_id,))]
    overlap_lines = [v for v in views if v["excel_overlap"]]
    overlap = {"lines": len(overlap_lines), "ticked": sum(1 for v in overlap_lines if v["include"])}
    events = ingest.events_for(conn, doc_id)
    kpi_changes = next((e["detail"].get("kpi_changes") for e in reversed(events)
                        if e["event"] == "confirmed" and e["detail"]), None)
    dup_doc = conn.execute("SELECT id, filename, status, uploaded_at FROM documents WHERE id=?", (doc["duplicate_of_document"],)).fetchone() \
        if doc["duplicate_of_document"] else None
    all_views = _doc_views(conn)
    view = next(v for v in all_views if v["id"] == doc_id)
    order = [v["id"] for v in _review_order(all_views)]
    pos = order.index(doc_id)
    queue = {"pos": pos + 1, "n": len(order), "prev": order[pos - 1] if pos else None, "next": order[pos + 1] if pos + 1 < len(order) else None}

    # read-only facts: exactly what the extraction stored, "\u2014" where it stored nothing, never tidied
    lines = []
    for v in views:
        it = v["row"]
        res = it["item_kind"] == "reservation"
        ref = " \u00b7 ".join(x for x in ([f"Order {v['orig']['order_id']}"] if v["orig"]["order_id"] else []) + ([f"PO {v['orig']['po']}"] if v["orig"]["po"] else [])) or None
        lines.append({"id": it["id"], "res": res, "title": (it["description"] or it["platform"] or "Reservation") if res else (it["vendor"] or it["description"]),
                      "desc": None if res else (it["description"] if it["vendor"] and it["description"] and it["description"] != it["vendor"] else None),
                      "date": fdate(it["check_in"] if res else it["date"]), "to": fdate(it["check_out"]) if res else None,
                      "amount": it["net_revenue"] if res else it["amount"], "included": bool(it["include"]),
                      "category": None if (res or not it["category"]) else it["category"].replace("_", " ").title(),
                      "platform": it["platform"] if res else None, "reference": it["reservation_id"] if res else ref,
                      "page": it["source_page"], "row": it["source_row"]})

    # linked evidence: only records that already exist. Nothing here creates one.
    linked, seen_tx = [], set()
    for t in created["transactions"]:
        seen_tx.add(t["id"])
        linked.append({"rel": "Supports", "kind": "tx", "id": t["id"], "text": f"{t['vendor'] or t['description'] or 'Transaction'} \u00b7 \u00a3{t['amount']:,.2f}",
                       "sub": f"{t['property_name']} \u00b7 {t['category'].replace('_', ' ').title()}"})
    for b in created["bookings"]:
        linked.append({"rel": "Supports", "kind": "bk", "id": b["id"], "text": f"{b['platform'] or 'Reservation'} \u00b7 \u00a3{b['net_revenue']:,.2f}",
                       "sub": f"{b['property_name']} \u00b7 {fdate(b['check_in'])}"})
    for v in views:
        tid = v["row"]["duplicate_of"]
        if tid and v["row"]["item_kind"] != "reservation" and tid not in seen_tx:
            t = conn.execute("SELECT t.*, p.name pname FROM transactions t JOIN properties p ON p.id=t.property_id WHERE t.id=?", (tid,)).fetchone()
            if t:
                seen_tx.add(tid)
                linked.append({"rel": "Matches", "kind": "tx", "id": t["id"], "text": f"{t['vendor'] or t['description'] or 'Transaction'} \u00b7 \u00a3{t['amount']:,.2f}",
                               "sub": f"{t['pname']} \u00b7 {t['category'].replace('_', ' ').title()}"})
    return render_template(
        "review_document.html", active="documents", all_properties=get_properties(conn),
        active_property=doc["property_id"], prop=prop, doc=doc, items=views, view=view, queue=queue, lines=lines, linked=linked,
        flats=get_properties(conn, include_overhead=not res_mode), year=year, month=month, unmatched=unmatched,
        categories=CATEGORIES, is_pdf=is_pdf, is_image=is_image, res_mode=res_mode, confirmed=confirmed,
        summary=summary, preview=preview, focus=focus, doc_type_label=DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"] or "Document"),
        period_label=_period_full(doc), status_label=ingest.status_display(doc["status"])[0],
        strip=_summary_strip(doc, prop, items, detection, names), warnings=detection.get("warnings", []),
        events=events, kpi_changes=kpi_changes, created=created, dup_doc=dup_doc, overlap=overlap, fdate=fdate,
        extraction_available=extraction_available(),
        needs_ai=not fname.endswith((".csv", ".tsv", ".xls", ".xlsx", ".xlsm")),
    )


def _num(text):
    try:
        return abs(float(text))
    except (TypeError, ValueError):
        return None


def _names(conn):
    return {p["id"]: p["name"] for p in get_properties(conn)}


def _log_edits(conn, doc_id):
    """Record what the reviewer changed relative to what the parser read --
    once per distinct set of edits, not on every blocked confirm attempt."""
    detail = []
    for it in conn.execute("SELECT * FROM document_items WHERE document_id=? ORDER BY line_index", (doc_id,)):
        changes = review_helpers.field_changes(it)
        if changes:
            detail.append({"line": it["vendor"] or it["raw_description"] or f"line {it['line_index'] + 1}", "changes": changes})
    if not detail:
        return
    last = conn.execute("SELECT detail FROM document_events WHERE document_id=? AND event='edited' ORDER BY id DESC LIMIT 1", (doc_id,)).fetchone()
    if last and json.loads(last["detail"] or "null") == json.loads(json.dumps(detail)):
        return
    n = sum(len(d["changes"]) for d in detail)
    ingest.log_event(conn, doc_id, "edited", f"{n} value{'s' if n != 1 else ''} changed on {len(detail)} line{'s' if len(detail) != 1 else ''}", detail)


def _log_kept_as_evidence(conn, doc_id, n_lines):
    ingest.log_event(conn, doc_id, "confirmed", f"Kept as supporting evidence ({n_lines} extracted line{'s' if n_lines != 1 else ''} retained). Nothing was added to the ledger.",
                     {"added": 0, "noun": "evidence", "evidence_only": True})


def _save_review(conn, doc_id):
    """Keep the reviewer's corrections to the extracted lines. They stay on the document as evidence; nothing is posted anywhere."""
    f = request.form
    ids = f.getlist("item_id")
    included = set(f.getlist("include"))

    def col(name, i):
        values = f.getlist(name)
        return values[i] if i < len(values) else None

    for i, item_id in enumerate(ids):
        item = conn.execute("SELECT * FROM document_items WHERE id=? AND document_id=?", (item_id, doc_id)).fetchone()
        if not item:
            continue
        include = 1 if item_id in included else 0
        pid = col("property_id", i) or None
        if item["item_kind"] == "reservation":
            gross, fees, net = _num(col("gross", i)), _num(col("fees", i)) or 0, _num(col("net", i))
            if net is None and gross is not None:
                net = max(gross - fees, 0)
            conn.execute("""UPDATE document_items SET property_id=?, platform=?, reservation_id=?, check_in=?, check_out=?, gross_revenue=?,
                              platform_fees=?, net_revenue=?, amount=?, include=?, reviewed=1 WHERE id=?""",
                         (pid, (col("platform", i) or "").strip() or None, (col("reservation_id", i) or "").strip() or None, col("check_in", i), col("check_out", i),
                          gross, fees, net, net, include, item_id))
        else:
            category, amount = col("category", i), _num(col("amount", i))
            vendor, desc, date = col("vendor", i) or "", col("description", i) or "", col("date", i)
            capex = 1 if col("type", i) == "capex" else 0
            conn.execute("""UPDATE document_items SET property_id=?, vendor=?, raw_description=?, amount=?, category=?, capex=?, date=?,
                              direction=?, include=?, reviewed=1, final_value=? WHERE id=?""",
                         (pid, vendor, desc, amount, category, capex, date, "income" if category == "booking_income" else "expense", include,
                          json.dumps({"property_id": pid, "vendor": vendor, "description": desc, "amount": amount, "category": category, "capex": capex,
                                      "date": date, "include": bool(include)}), item_id))
    _log_edits(conn, doc_id)


@bp.route("/documents/<int:doc_id>/confirm", methods=["POST"])
def confirm(doc_id):
    """Confirming a document keeps it as supporting evidence. It NEVER adds an income, expense or reservation row to the ledger: the monthly
    workbook is the financial source, and a document-posted row would be counted a second time when the workbook arrives. A one-off correction
    goes through the property's Expenses tab as a manual adjustment."""
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("We couldn't find that document. It may have been removed; check the Documents list.", "error")
        return redirect(url_for("overview.index"))
    if doc["status"] == "confirmed":
        flash("This document is already kept as supporting evidence.", "info")
        return redirect(url_for("documents.review", doc_id=doc_id))
    _save_review(conn, doc_id)
    n = conn.execute("SELECT COUNT(*) FROM document_items WHERE document_id=?", (doc_id,)).fetchone()[0]
    _log_kept_as_evidence(conn, doc_id, n)
    conn.execute("UPDATE documents SET status='confirmed', reviewed=1 WHERE id=?", (doc_id,))
    conn.commit()
    flash("\u2713 Marked reviewed. Kept as supporting evidence; nothing was added to your figures: the monthly workbook is the source. "
          "For a one-off correction use the property's Expenses tab.", "success")
    return redirect(url_for("documents.review", doc_id=doc_id))


@bp.route("/documents/<int:doc_id>/undo", methods=["POST"])
def undo(doc_id):
    """Reverses a confirmed import: removes the transactions/reservations it
    created and puts the document back to 'needs review' with its extracted
    lines intact, so it can be corrected and confirmed again."""
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc or doc["status"] != "confirmed":
        flash("Only a confirmed document can be undone. This one hasn't been confirmed yet.", "info")
        return redirect(url_for("documents.index"))
    # confirm() points document_items.duplicate_of at the transaction each line became
    conn.execute("UPDATE document_items SET duplicate_of=NULL WHERE document_id=? AND duplicate_of IN (SELECT id FROM transactions WHERE document_id=?)", (doc_id, doc_id))
    conn.execute("UPDATE document_items SET reviewed=0 WHERE document_id=?", (doc_id,))
    n_tx = conn.execute("DELETE FROM transactions WHERE document_id=? AND source='upload'", (doc_id,)).rowcount
    n_bk = conn.execute("DELETE FROM bookings WHERE document_id=? AND source='upload'", (doc_id,)).rowcount
    conn.execute("UPDATE documents SET status='extracted', reviewed=0 WHERE id=?", (doc_id,))
    record(conn, "document", doc_id, "delete", field="import", old_value=f"{n_tx} transactions, {n_bk} reservations")
    ingest.log_event(conn, doc_id, "undone", f"Import undone: removed {n_tx} transaction{'s' if n_tx != 1 else ''} and {n_bk} reservation{'s' if n_bk != 1 else ''}",
                     {"transactions": n_tx, "reservations": n_bk})
    conn.commit()
    flash(f"\u2713 Import undone: removed {n_tx} transaction{'s' if n_tx != 1 else ''} and {n_bk} reservation{'s' if n_bk != 1 else ''}. "
          f"Your Excel history was never touched.", "success")
    return redirect(url_for("documents.review", doc_id=doc_id))


@bp.route("/documents/<int:doc_id>/reject", methods=["POST"])
def reject(doc_id):
    """Throw away an unconfirmed draft: its extracted lines and the stored
    file. Nothing was ever in the ledger, so no figure moves. A confirmed
    import must be undone first."""
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("That document is already gone.", "info")
        return redirect(url_for("documents.index"))
    if doc["status"] == "confirmed":
        flash("This import is confirmed and its records are in your figures. Undo the import first, then you can delete the draft.", "warning")
        return redirect(url_for("documents.review", doc_id=doc_id))
    n_items = conn.execute("DELETE FROM document_items WHERE document_id=?", (doc_id,)).rowcount
    conn.execute("DELETE FROM document_events WHERE document_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    record(conn, "document", doc_id, "delete", field="draft", old_value=f"{doc['filename']} ({n_items} extracted lines)")
    conn.commit()
    try:
        stored = runtime.resolve_stored(doc["stored_path"])      # only ever a file inside the uploads folder
        if stored:
            stored.unlink()
    except OSError:
        pass  # the draft is gone from the app either way; a stray file on disk is harmless
    flash(f"\u2713 Deleted the draft \u201c{doc['filename']}\u201d. Nothing was added to your figures.", "success")
    return redirect(url_for("documents.index"))


@bp.route("/documents/<int:doc_id>/map-listing", methods=["POST"])
def map_listing(doc_id):
    """One decision for every reservation line carrying the same listing name:
    which of your properties it is, or that it isn't one of yours (left out).
    Remembered, so the next statement is matched automatically."""
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    listing = (request.form.get("listing") or "").strip()
    choice = request.form.get("property_id") or ""
    if not doc or doc["status"] == "confirmed" or not listing or not choice:
        flash("Choose a property for that listing first.", "warning")
        return redirect(url_for("documents.review", doc_id=doc_id))
    if choice == "__ignore__":
        n = conn.execute("UPDATE document_items SET include=0 WHERE document_id=? AND item_kind='reservation' AND raw_description=? AND property_id IS NULL",
                         (doc_id, listing)).rowcount
        ingest.alias_remember(conn, listing, None, ignore=True)
        flash(f"\u2713 Left out {n} reservation{'s' if n != 1 else ''} for \u201c{listing}\u201d. It will be skipped on future statements too.", "success")
    else:
        if not get_property(conn, choice):
            flash("That property doesn't exist.", "error")
            return redirect(url_for("documents.review", doc_id=doc_id))
        n = 0
        for it in conn.execute("SELECT id, check_in, check_out, reservation_id FROM document_items WHERE document_id=? AND item_kind='reservation' AND raw_description=? AND property_id IS NULL", (doc_id, listing)).fetchall():
            dup_id = find_duplicate_reservation(conn, choice, {"reservation_id": it["reservation_id"], "check_in": it["check_in"], "check_out": it["check_out"]})
            tick = (ingest.duplicate_default(ingest.source_of(conn, "bookings", dup_id)) == "replace") if dup_id else True
            conn.execute("UPDATE document_items SET property_id=?, include=? WHERE id=?", (choice, 1 if tick else 0, it["id"]))
            n += 1
        ingest.alias_remember(conn, listing, choice)
        flash(f"\u2713 Assigned {n} reservation{'s' if n != 1 else ''} for \u201c{listing}\u201d to {get_property(conn, choice)['name']}. Remembered for next time.", "success")
    ingest.log_event(conn, doc_id, "edited", f"Listing \u201c{listing}\u201d -> {'not tracked' if choice == '__ignore__' else get_property(conn, choice)['name']} ({n} line{'s' if n != 1 else ''})",
                     {"listing": listing, "choice": choice, "lines": n})
    conn.commit()
    return redirect(url_for("documents.review", doc_id=doc_id))
