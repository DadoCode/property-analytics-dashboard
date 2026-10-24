import re

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

import db
import services.kpis as kpis
import services.reporting as reporting
from services.context import request_context
from services.common import MONTH_NAMES, get_properties, get_property, short_name

bp = Blueprint("reports", __name__)

YM = re.compile(r"^\d{4}-\d{2}$")
DEFAULT_TYPE = "portfolio_monthly"


def _build(conn, args):
    """Turns query params into a report dict, or (None, message) when the
    params don't describe a valid report."""
    rtype = args.get("type", "")
    cy, cm = kpis.current_period(conn)
    month = args.get("month") or f"{cy}-{cm:02d}"
    property_id = args.get("property") or None

    if rtype == "portfolio_monthly":
        if not YM.match(month):
            return None, "Pick a month for this report."
        y, m = map(int, month.split("-"))
        return reporting.build_portfolio_monthly(conn, y, m), None
    if rtype == "property_monthly":
        if not YM.match(month):
            return None, "Pick a month for this report."
        if not property_id or not get_property(conn, property_id):
            return None, "Choose a property to see its statement."
        y, m = map(int, month.split("-"))
        return reporting.build_property_monthly(conn, property_id, y, m), None
    if rtype == "annual_portfolio":
        year = args.get("year") or str(cy)
        if not year.isdigit() or len(year) != 4:
            return None, "Enter a four-digit year."
        return reporting.build_annual_portfolio(conn, int(year)), None
    if rtype == "expense":
        f, t = args.get("from") or f"{cy}-01", args.get("to") or f"{cy}-{cm:02d}"
        if not (YM.match(f) and YM.match(t)) or f > t:
            return None, "Pick a valid from/to month range (from must not be after to)."
        if property_id and not get_property(conn, property_id):
            return None, "That property doesn't exist."
        return reporting.build_expense(conn, f, t, property_id), None
    return None, "Choose a report type."


def _page(conn, args):
    """The one Reports page: choose a report, set its period and property, see it as it will be exported, then export it.
    Reports are built on demand from the recorded data; nothing is stored, so there is no history to list."""
    ctx = request_context(conn)
    rtype = args.get("type") if args.get("type") in reporting.REPORT_TYPES else DEFAULT_TYPE
    values = {"month": args.get("month") or ctx["to_input"], "year": args.get("year") or str(ctx["end_year"]),
              "from": args.get("from") or ctx["from_input"], "to": args.get("to") or ctx["to_input"],
              "property": args.get("property") if "property" in args else (ctx["property_id"] or "")}
    report, message = _build(conn, {**values, "type": rtype})
    flats = get_properties(conn, include_overhead=False)
    export_params = {"type": rtype, **{k: v for k, v in values.items() if v and k in {"property_monthly": ("month", "property"), "portfolio_monthly": ("month",),
                                                                                        "annual_portfolio": ("year",), "expense": ("from", "to", "property")}[rtype]}}
    type_links = [{"key": k, "label": label, "href": url_for("reports.index", type=k, **{a: b for a, b in values.items() if b}), "on": k == rtype,
                   "contents": reporting.REPORT_CONTENTS[k]} for k, label in reporting.REPORT_TYPES.items()]
    return render_template(
        "reports/index.html", active="reports", all_properties=get_properties(conn), active_property=None, flats=flats,
        rtype=rtype, values=values, report=report, message=message, type_links=type_links, contents=reporting.REPORT_CONTENTS[rtype],
        hero=reporting.hero_display(report) if report else [],
        sections=[{**s, "display_rows": reporting.rows_for_display(s)} for s in report["sections"]] if report else [],
        export_params=export_params, month_label=lambda ym: f"{MONTH_NAMES[int(ym[5:7])]} {ym[:4]}" if YM.match(ym or "") else ym,
        short_name=short_name,
    )


@bp.route("/reports")
def index():
    return _page(db.get_conn(), request.args)


@bp.route("/reports/view")
def view():
    """Kept so older links still work: the preview is now the Reports page itself."""
    return _page(db.get_conn(), request.args)


@bp.route("/reports/export")
def export():
    conn = db.get_conn()
    report, error = _build(conn, request.args)
    if error:
        flash(error, "error")
        return redirect(url_for("reports.index"))
    fmt = request.args.get("format", "csv")
    name = report["filename"]
    if fmt == "xlsx":
        return Response(reporting.to_xlsx(report),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={name}.xlsx"})
    if fmt == "pdf":
        return Response(reporting.to_pdf(report), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={name}.pdf"})
    return Response(reporting.to_csv(report), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})
