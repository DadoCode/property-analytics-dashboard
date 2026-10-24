import json
import re
from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import services.ingest as ingest
import services.kpis as kpis
from services.audit import actor as audit_actor, record, record_edits
from services.common import CATEGORIES, MANAGED_SQL, MONTH_ABBR, MONTH_NAMES, get_properties, get_property, is_managed, pct_delta, short_name
from services.completeness import period_has_data
from services.context import compare_bounds, range_params, request_context
from services.provenance import is_workbook_row, workbook_source
from services.vendors import get_or_create_vendor

bp = Blueprint("expenses", __name__)

# ----------------------------------------------------------------------
# Phase 3 classification rule (see the audit note below): a transaction
# is a PROPERTY cost or a BUSINESS cost purely by which cost centre it's
# actually posted to (properties.type = 'flat' vs 'overhead') -- never
# guessed from its category. There's exactly one overhead cost centre in
# the schema today (id='general-overheads') and property_id is NOT NULL
# on every transaction, so "type='overhead'" is the complete, reliable
# signal -- no property_id IS NULL case exists to handle separately.
# ----------------------------------------------------------------------
SCOPES = ("all", "property", "business")
MODELS = ("operated", "managed")        # narrows Property Costs to operated (rent-to-rent) or managed properties


def _scope(args):
    s = args.get("scope", "all")
    return s if s in SCOPES else "all"


def _model_sql(model):
    """SQL fragment (on `properties p`) narrowing to operated or managed flats; '' when no model is chosen."""
    return f"p.type='flat' AND {'NOT ' if model == 'operated' else ''}{MANAGED_SQL}" if model in MODELS else ""


def _costs(conn, start, end, scope="all", property_id=None, capex=None, model=None):
    """Sum of real expense transactions for a scope, joined to
    properties.type so Property/Business is decided by cost-centre, not
    category. category='management_fee' is always excluded here: that
    transaction sits on a flat's own book with direction='expense'
    (money leaving the FLAT's account), which is correct for the flat's
    own P&L -- but it's the fee the company itself receives, already
    counted as Company Revenue / Management Fee Earned elsewhere.
    Counting it again here as Expenses spend would double it: once as
    income, once as a cost. Corrected after a real regression -- a first
    version of this Phase 3 pass counted it as a Property Cost, which
    put money the company earns on the same page as money it spent (see
    tests/test_expenses_management_fee.py)."""
    clauses = ["t.direction='expense'", "t.category != 'management_fee'", "t.date>=?", "t.date<?"]
    params = [start, end]
    if property_id:
        clauses.append("t.property_id=?")
        params.append(property_id)
    elif scope == "property":
        clauses.append("p.type='flat'")
        if model in MODELS:
            clauses.append(_model_sql(model))
    elif scope == "business":
        clauses.append("p.type='overhead'")
    if capex is not None:
        clauses.append(f"t.capex={1 if capex else 0}")
    where = " AND ".join(clauses)
    row = conn.execute(
        f"SELECT COALESCE(SUM(t.amount),0) FROM transactions t JOIN properties p ON p.id=t.property_id WHERE {where}",
        params).fetchone()
    return row[0]


def _ledger_where(ctx, start, end, args, model=None):
    """WHERE fragments for the ledger: the shared context (period,
    property) plus the ledger's own narrowing filters. A chart click
    sets t_month, which replaces the period with that single month.
    t_scope narrows to one specific property or 'business' within
    whatever the page's top-level segment already selected -- distinct
    from the global property switcher (ctx['property_id']), which the
    route only lets apply when it's set to a single specific property
    (see index()); when the global switcher is "All properties" this is
    the only property-level narrowing available."""
    month = args.get("t_month") or ""
    if month and re.match(r"^\d{4}-\d{2}$", month):
        y, m = map(int, month.split("-"))
        start, end = kpis.month_bounds(y, m)
    # category != 'management_fee' here too, for the same reason as
    # _costs() -- the ledger's own header total ("N · £X") has to match
    # the summary tiles above it, so the exclusion has to be identical
    # everywhere this page shows a total, not just in the tiles. The fee
    # transactions aren't deleted -- they're still fully visible via
    # Management Fee Earned's own drilldown and the transaction drawer,
    # just not listed as if they were company spend on this page.
    clauses = ["t.direction='expense'", "t.category != 'management_fee'", "t.date>=?", "t.date<?"]
    params = [start, end]
    if ctx["property_id"]:
        clauses.append("t.property_id=?"); params.append(ctx["property_id"])
    scope = _scope(args)
    if not ctx["property_id"]:
        if scope == "property":
            clauses.append("p.type='flat'")
            if model in MODELS:
                clauses.append(_model_sql(model))
        elif scope == "business":
            clauses.append("p.type='overhead'")
    t_scope = args.get("t_scope") or ""
    if t_scope == "business":
        clauses.append("p.type='overhead'")
    elif t_scope:
        clauses.append("t.property_id=?"); params.append(t_scope)
    if args.get("t_category"):
        clauses.append("t.category=?"); params.append(args["t_category"])
    if args.get("t_type") == "opex":
        clauses.append("t.capex=0")
    elif args.get("t_type") == "capex":
        clauses.append("t.capex=1")
    if args.get("t_vendor"):
        clauses.append("t.vendor_id=?"); params.append(int(args["t_vendor"]))
    if args.get("t_source"):
        clauses.append("t.source=?"); params.append(args["t_source"])
    if (args.get("t_batch") or "").isdigit():                      # rows written by one workbook import
        clauses.append("t.import_batch_id=?"); params.append(int(args["t_batch"]))
    q = (args.get("t_q") or "").strip()
    if q:
        clauses.append("(t.vendor LIKE ? OR t.description LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    return clauses, params, month, scope, t_scope


def _back(default_endpoint="expenses.index"):
    """Where to land after an edit/delete: the page the drawer was opened on, with every filter it carried (period,
    property, scope, category, vendor, search, sort...). Same-host referrers on this app's own pages only."""
    from urllib.parse import urlparse
    ref = request.referrer
    if ref:
        u = urlparse(ref)
        if u.netloc in ("", request.host) and (u.path.startswith("/expenses") or u.path.startswith("/properties/")):
            return redirect(u.path + (f"?{u.query}" if u.query else "") + (f"#{u.fragment}" if u.fragment else ""))
    return redirect(url_for(default_endpoint))


@bp.route("/expenses")
def index():
    conn = db.get_conn()
    ctx = request_context(conn)
    flats = get_properties(conn, include_overhead=False)
    pid = ctx["property_id"]
    viewing = next((p for p in get_properties(conn) if p["id"] == pid), None) if pid else None
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    cmp_bounds = compare_bounds(ctx)
    mtd = ctx["partial"] and ctx["choice"] == "this_month"
    d = lambda cur, prev, base=100: None if mtd else (pct_delta(cur, prev, min_base=base) if prev is not None else None)

    # A specific cost centre is already selected via the global context
    # bar -- the Property/Business split doesn't apply to one property,
    # so this page falls back to a single-scope view (same shape as
    # before Phase 3, just without a standalone Opex/Capex tile pair).
    scope = None if pid else _scope(request.args)
    # model=operated|managed narrows Property Costs to one kind of property, so a figure like Overview's "Operated Property
    # Costs" can be reproduced exactly. It is a narrowing of the property scope and is ignored for one property or Business Costs.
    model = request.args.get("model") if request.args.get("model") in MODELS else None
    if pid or request.args.get("scope") == "business":
        model = None
    elif model:
        scope = "property"

    # ---- the ledger: the shared context + its own filters. Everything on the page (the band, the groups, the rows) comes from this one WHERE. ----
    clauses, params, f_month, f_scope, f_t_scope = _ledger_where(ctx, start, end, request.args, model=model)
    where = " AND ".join(clauses)
    t_sort = request.args.get("t_sort") if request.args.get("t_sort") in ("date", "amount") else "date"
    t_dir = request.args.get("t_dir") if request.args.get("t_dir") in ("asc", "desc") else "desc"
    order = f"t.{t_sort} {t_dir.upper()}, t.id {t_dir.upper()}"
    t_group = request.args.get("t_group") if request.args.get("t_group") in GROUPS else ""
    f = {k: request.args.get(k) or "" for k in ("t_category", "t_type", "t_vendor", "t_q", "t_scope", "t_source", "t_batch")}
    f["t_month"] = f_month
    ledger_filtered = any(f.values())

    # the band: aggregates over exactly the rows the ledger lists
    agg = {"n": 0, "amt": 0.0, "property": 0.0, "business": 0.0, "opex": 0.0, "capex": 0.0}
    for r in conn.execute(f"SELECT p.type pt, t.capex cx, COUNT(*) n, COALESCE(SUM(t.amount),0) amt FROM transactions t JOIN properties p ON p.id=t.property_id WHERE {where} GROUP BY p.type, t.capex", params):
        agg["n"] += r["n"]; agg["amt"] += r["amt"]
        agg["business" if r["pt"] == "overhead" else "property"] += r["amt"]
        agg["capex" if r["cx"] else "opex"] += r["amt"]
    ledger_total = {"n": agg["n"], "amt": agg["amt"]}

    def prev_of(**kw):
        return _costs(conn, *cmp_bounds, **kw) if cmp_bounds else None

    if pid:
        hero_label = "Total costs"
    else:
        hero_label = {"all": "Total Recorded Costs", "property": "Property Costs", "business": "Business Costs"}[scope]
        if model:
            hero_label = f"{model.capitalize()} Property Costs"
    hero_delta = None
    if not ledger_filtered and cmp_bounds:
        hero_delta = d(agg["amt"], prev_of(property_id=pid) if pid else prev_of(scope=scope, model=model))
    if ledger_filtered:
        hero_label = "Matching costs"
    band = {"label": hero_label, "value": agg["amt"], "n": agg["n"], "opex": agg["opex"], "capex": agg["capex"], "delta": hero_delta,
            "split": (not pid) and scope == "all", "property": agg["property"], "business": agg["business"]}
    no_data = not period_has_data(conn, pid, start, end)

    # ---- rows, optionally grouped (a group's subtotal is over ALL its matching rows; the rows shown are capped per group) ----
    gexpr = {"property": "CASE WHEN p.type='overhead' THEN 'Business costs' ELSE p.name END", "category": "t.category",
             "vendor": "COALESCE(NULLIF(t.vendor,''),'No vendor')", "scope": "CASE WHEN p.type='overhead' THEN 'Business costs' ELSE 'Property costs' END"}.get(t_group)
    LIMIT = 2000 if t_group else 200
    ledger = conn.execute(
        f"""SELECT t.*, p.name AS property_name, p.type AS property_type{(', ' + gexpr + ' AS gkey') if gexpr else ''} FROM transactions t
            JOIN properties p ON p.id = t.property_id WHERE {where} ORDER BY {order} LIMIT {LIMIT}""", params).fetchall()
    rows = [_row_view(t) for t in ledger]
    chips = []
    base = range_params(ctx)
    if model:
        base["model"] = model

    def U(**changes):
        """The ledger URL with one thing changed: period, property and comparison carried; None removes a filter."""
        cur = {**range_params(ctx), **({"scope": scope} if scope else {}), **({"model": model} if model else {}), **{k: v for k, v in f.items() if v},
               **({"t_group": t_group} if t_group else {}), **({"t_sort": t_sort, "t_dir": t_dir} if (t_sort, t_dir) != ("date", "desc") else {})}
        cur.update(changes)
        return url_for("expenses.index", **{k: v for k, v in cur.items() if v not in (None, "")})

    groups = []
    if gexpr:
        totals = {r["g"]: (r["n"], r["amt"]) for r in conn.execute(
            f"SELECT {gexpr} AS g, COUNT(*) n, COALESCE(SUM(t.amount),0) amt FROM transactions t JOIN properties p ON p.id=t.property_id WHERE {where} GROUP BY g", params)}
        per_group = {}
        for row, t in zip(rows, ledger):
            lst = per_group.setdefault(t["gkey"], [])
            if len(lst) < 40:
                lst.append(row)
        for g, (n, amt) in sorted(totals.items(), key=lambda kv: -kv[1][1]):
            label = g.replace("_", " ").title() if t_group == "category" else short_name(g) if t_group == "property" else g
            first = (per_group.get(g) or [None])[0]
            ghref = (U(t_scope="business", t_group=None) if g == "Business costs" and t_group in ("property",) else
                     U(t_scope=first["property_id"], t_group=None) if t_group == "property" and first else
                     U(t_category=g, t_group=None) if t_group == "category" else
                     U(scope="business" if g == "Business costs" else "property", t_group=None) if t_group == "scope" else
                     U(t_q=g, t_group=None) if t_group == "vendor" and g != "No vendor" else None)
            groups.append({"key": g, "label": "Historical Adjustment" if g == "reconciliation" else label, "n": n, "amt": amt, "rows": per_group.get(g, []),
                           "more": n - len(per_group.get(g, [])), "href": ghref})

    def chip(label, **drop):
        chips.append({"label": label, "href": U(**drop)})
    if model:
        chips.append({"label": f"Model: {model.capitalize()}", "href": url_for("expenses.index", **{**range_params(ctx), "scope": "property", **{k: v for k, v in f.items() if v}})})
    if f_month:
        y, m = map(int, f_month.split("-")); chip(f"Month: {MONTH_NAMES[m]} {y}", t_month=None)
    if f["t_scope"]:
        sname = "Business" if f["t_scope"] == "business" else next((p["name"] for p in flats if p["id"] == f["t_scope"]), f["t_scope"])
        chip(f"Property: {sname}" if f["t_scope"] != "business" else "Scope: Business", t_scope=None)
    if f["t_type"]:
        chip(f"Type: {f['t_type'].capitalize()}", t_type=None)
    if f["t_category"]:
        chip(f"Category: {f['t_category'].replace('_', ' ').title()}", t_category=None)
    if f["t_vendor"]:
        vname = conn.execute("SELECT name FROM vendors WHERE id=?", (f["t_vendor"],)).fetchone()
        chip(f"Vendor: {vname['name'] if vname else 'Vendor'}", t_vendor=None)
    if f["t_source"]:
        chip(f"Source: {f['t_source'].replace('_', ' ').title()}", t_source=None)
    if f["t_batch"]:
        chip(f"Import #{f['t_batch']}", t_batch=None)
    if f["t_q"]:
        chip(f'Search: "{f["t_q"]}"', t_q=None)

    sources = [r["source"] for r in conn.execute("SELECT DISTINCT source FROM transactions WHERE source IS NOT NULL ORDER BY source")]
    all_vendors = conn.execute("SELECT id, name FROM vendors ORDER BY name").fetchall()
    sorts = [("Newest first", "date", "desc"), ("Oldest first", "date", "asc"), ("Largest first", "amount", "desc"), ("Smallest first", "amount", "asc")]
    sort_label = next(l for l, c, dr in sorts if (c, dr) == (t_sort, t_dir))
    quick = [("All", U(scope="all", t_type=None, model=None), scope == "all" and not f["t_type"] and not model and not pid),
             ("Property Costs", U(scope="property", t_type=None, model=None), scope == "property" and not model and not f["t_type"]),
             ("Business Costs", U(scope="business", t_type=None, model=None, t_scope=None), scope == "business" and not f["t_type"]),
             ("Capex", U(t_type="capex"), f["t_type"] == "capex")] if not pid else [("Capex", U(t_type="capex"), f["t_type"] == "capex"), ("All", U(t_type=None), not f["t_type"])]

    menus = []
    if not pid:
        prop_name = ("Business" if f["t_scope"] == "business" else short_name(next((p["name"] for p in flats if p["id"] == f["t_scope"]), f["t_scope"]))) if f["t_scope"] else None
        menus.append(("Property", "Filter by property", f"Property: {prop_name}" if prop_name else None,
                      [("All properties", U(t_scope=None), not f["t_scope"]), ("Business", U(t_scope="business"), f["t_scope"] == "business")]
                      + [(short_name(p["name"]), U(t_scope=p["id"]), f["t_scope"] == p["id"]) for p in flats]))
        if scope != "business":
            menus.append(("Model", "Filter by property model", f"Model: {model.capitalize()}" if model else None,
                          [("Any model", U(model=None), not model), ("Operated", U(scope="property", model="operated"), model == "operated"), ("Managed", U(scope="property", model="managed"), model == "managed")]))
    menus.append(("Category", "Filter by category", f"Category: {f['t_category'].replace('_', ' ').title()}" if f["t_category"] else None,
                  [("Any category", U(t_category=None), not f["t_category"])] + [(c.replace("_", " ").title(), U(t_category=c), f["t_category"] == c) for c in CATEGORIES if c != "management_fee"]))
    menus.append(("Vendor", "Filter by vendor", None if not f["t_vendor"] else "Vendor: " + next((v["name"] for v in all_vendors if str(v["id"]) == f["t_vendor"]), "selected"),
                  [("Any vendor", U(t_vendor=None), not f["t_vendor"])] + [(v["name"], U(t_vendor=v["id"]), f["t_vendor"] == str(v["id"])) for v in all_vendors]))
    menus.append(("Type", "Filter by opex or capex", f"Type: {f['t_type'].capitalize()}" if f["t_type"] else None,
                  [("Opex and capex", U(t_type=None), not f["t_type"]), ("Opex only", U(t_type="opex"), f["t_type"] == "opex"), ("Capex only", U(t_type="capex"), f["t_type"] == "capex")]))
    if len(sources) > 1:
        menus.append(("Source", "Filter by source", f"Source: {f['t_source'].replace('_', ' ').title()}" if f["t_source"] else None,
                      [("Any source", U(t_source=None), not f["t_source"])] + [(x.replace("_", " ").title(), U(t_source=x), f["t_source"] == x) for x in sources]))
    group_menu = ("Group", "Group the ledger", f"Group: {GROUPS[t_group]}" if t_group else None,
                  [("No grouping", U(t_group=None), not t_group)] + [(label, U(t_group=k), t_group == k) for k, label in GROUPS.items()])
    sort_menu = ("Sort", "Sort the ledger", f"Sort: {sort_label}", [(l, U(t_sort=c, t_dir=dr), (c, dr) == (t_sort, t_dir)) for l, c, dr in sorts])

    return render_template(
        "expenses.html", active="expenses", all_properties=get_properties(conn), active_property=None,
        context_bar=True, ctx=ctx, viewing=viewing, scope=scope, flats=flats, bar_title="Expenses",
        bar_sub=(viewing["name"] if viewing else ctx["display"]),
        band=band, menus=menus, group_menu=group_menu, sort_menu=sort_menu, no_data=no_data, ledger=rows, ledger_total=ledger_total, groups=groups, t_group=t_group, groups_def=GROUPS,
        f=f, chips=chips, t_sort=t_sort, t_dir=t_dir, sorts=sorts, sort_label=sort_label, U=U, quick=quick, sources=sources, model=model,
        all_vendors=all_vendors, categories=CATEGORIES, compare_label=ctx["compare_display"] or "", pid=pid,
        base_params=base, truncated=(ledger_total["n"] > len(ledger) and not t_group),
    )


GROUPS = {"property": "Property", "category": "Category", "vendor": "Vendor", "scope": "Scope"}


def _row_view(t):
    """One ledger row as the page shows it: what it is, where it sits, and where it came from. Presentation only."""
    keys = t.keys()
    if t["category"] == "reconciliation":
        category = "Historical adjustment"
    else:
        category = t["category"].replace("_", " ").title()
    if t["import_batch_id"] if "import_batch_id" in keys else False:
        source = {"text": f"Monthly workbook · Batch #{t['import_batch_id']}", "href": f"/imports/{t['import_batch_id']}#prop-{t['property_id']}",
                  "title": f"Written by workbook import #{t['import_batch_id']}" + (f" · {t['source_ref']}" if "source_ref" in keys and t["source_ref"] else "")}
    elif t["source"] == "excel_import":
        source = {"text": "Earlier Excel import", "href": None, "title": ""}
    elif t["source"] == "manual":
        source = {"text": "Manual entry", "href": None, "title": ""}
    else:
        source = {"text": (t["source"] or "").replace("_", " ").title(), "href": None, "title": ""}
    business = t["property_type"] == "overhead"
    y, m, dd = t["date"].split("-")
    return {"id": t["id"], "date": t["date"], "date_label": f"{int(dd)} {MONTH_ABBR[int(m)]} {y}", "vendor": t["vendor"] or "", "description": t["description"] or "", "amount": t["amount"], "capex": bool(t["capex"]),
            "property_id": t["property_id"], "property_name": "Business" if business else t["property_name"], "business": business,
            "scope_text": "Business cost" if business else "Property cost", "category": category, "source": source, "reversal": t["amount"] < 0}


@bp.route("/expenses/transactions/<int:tx_id>")
def transaction_drawer(tx_id):
    conn = db.get_conn()
    tx = conn.execute(
        """SELECT t.*, p.name AS property_name, p.type AS property_type FROM transactions t
           JOIN properties p ON p.id = t.property_id WHERE t.id=?""",
        (tx_id,),
    ).fetchone()
    if not tx:
        return "<div class='card'>Transaction not found.</div>", 404
    history = conn.execute(
        "SELECT * FROM audit_log WHERE entity_type='transaction' AND entity_id=? ORDER BY timestamp DESC LIMIT 10",
        (tx_id,),
    ).fetchall()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (tx["document_id"],)).fetchone() if tx["document_id"] else None
    line = None
    if doc:
        line = conn.execute("SELECT * FROM document_items WHERE document_id=? AND duplicate_of=? LIMIT 1", (doc["id"], tx_id)).fetchone() \
            or conn.execute("SELECT * FROM document_items WHERE document_id=? AND ABS(amount-?)<0.01 AND include=1 LIMIT 1", (doc["id"], tx["amount"])).fetchone()
    return render_template(
        "partials/transaction_drawer.html", tx=tx, history=history, doc=doc, line=line,
        prov=ingest.provenance(conn, tx["document_id"], line), wb=workbook_source(conn, tx), workbook_row=is_workbook_row(tx),
        flats=get_properties(conn, include_overhead=True), categories=CATEGORIES,
    )


@bp.route("/expenses/transactions/<int:tx_id>/edit", methods=["POST"])
def edit_transaction(tx_id):
    conn = db.get_conn()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return "<div class='card'>Transaction not found.</div>", 404
    if tx["category"] == "reconciliation":
        flash("Historical adjustments can't be edited: they keep the historical totals matching the original Excel accounts.", "warning")
        return _back()
    if is_workbook_row(tx):
        flash("That row was written by a monthly workbook import, so it can't be edited one row at a time. Correct the workbook and re-import it, or undo the import.", "warning")
        return _back()

    new_property_id = request.form.get("property_id", tx["property_id"])
    if not get_property(conn, new_property_id):
        flash("That property doesn't exist, so nothing was changed. Choose one from the list.", "error")
        return _back()

    vendor_name = request.form.get("vendor", "").strip()
    vendor_id = get_or_create_vendor(conn, vendor_name)
    new_values = {
        "property_id": new_property_id,
        "vendor": vendor_name,
        "description": request.form.get("description", tx["description"] or ""),
        "amount": abs(float(request.form.get("amount") or tx["amount"])),
        "category": request.form.get("category", tx["category"]),
        "capex": 1 if request.form.get("capex") == "on" else 0,
    }
    changed = record_edits(conn, "transaction", tx_id, tx, new_values)
    if changed:
        conn.execute(
            """UPDATE transactions SET property_id=?, vendor=?, vendor_id=?, description=?,
                 amount=?, category=?, capex=?, edited_at=datetime('now'), edited_by=? WHERE id=?""",
            (new_values["property_id"], vendor_name, vendor_id, new_values["description"],
             new_values["amount"], new_values["category"], new_values["capex"], audit_actor(), tx_id),
        )
        conn.commit()
        flash("✓ Transaction updated.", "success")
    return _back()


@bp.route("/expenses/transactions/<int:tx_id>/delete", methods=["POST"])
def delete_transaction(tx_id):
    conn = db.get_conn()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return _back()
    if tx["category"] == "reconciliation":
        flash("Historical adjustments can't be deleted: they keep the historical totals matching the original Excel accounts.", "warning")
        return _back()
    if is_workbook_row(tx):
        flash("That row was written by a monthly workbook import, so it can't be deleted one row at a time. Correct the workbook and re-import it, or undo the import.", "warning")
        return _back()
    record(conn, "transaction", tx_id, "delete", old_value=f"{tx['vendor']} £{tx['amount']}")
    conn.execute("UPDATE document_items SET duplicate_of=NULL WHERE duplicate_of=?", (tx_id,))
    conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    conn.commit()
    flash("✓ Transaction deleted.", "success")
    return _back()
