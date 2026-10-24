import calendar as cal
import datetime
import json
import re

from flask import Blueprint, redirect, render_template, request, url_for

import db
import services.ingest as ingest
import services.kpis as kpis
import services.sources as src
from services.common import METRIC_INFO, gbp0, pct0, MONTH_ABBR, MONTH_NAMES, channel_key, get_properties, is_managed, pct_delta, short_name
from services.context import compare_bounds, link_params, range_params, request_context, workspace_params
import services.drilldown as drill
from services.provenance import month_label, review_status, workbook_source
from services.completeness import has_real_data, not_active, period_has_data

bp = Blueprint("bookings", __name__)


def _has_real_bookings(conn):
    """Whether ANY property has a real day-level booking (not an Excel
    monthly-aggregate row) -- same signal as properties._ws()'s
    has_calendar_data, used here to hide the portfolio-wide Calendar tab
    the same way, for the same reason."""
    return bool(conn.execute(
        "SELECT 1 FROM bookings WHERE status='confirmed' AND reservation_id != 'monthly-aggregate' LIMIT 1").fetchone())


@bp.route("/occupancy")
def legacy_occupancy():
    return redirect(url_for("bookings.performance"), code=301)


SORTS = {"nights": "Booked nights", "occupancy": "Occupancy", "gbr": "Gross Booking Revenue", "adr": "ADR", "name": "Property name"}
DTYPES = {"reservations": "Reservation records", "monthly": "Monthly total", "none": "No booking data"}
_BOOKING_CHECK = re.compile(r"income|booked|nights|days|occupancy|revenue", re.I)


def _day_strip(res_rows, start, end):
    """The days of a single month that a reservation record actually covers (clipped to the month), or None."""
    s, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    booked = set()
    for r in res_rows:
        ci, co = max(datetime.date.fromisoformat(r["check_in"]), s), min(datetime.date.fromisoformat(r["check_out"]), e)
        d = ci
        while d < co:
            booked.add((d - s).days)
            d += datetime.timedelta(days=1)
    return booked


def _property_rows(conn, ctx, start, end):
    """One row per property for the period: what the booking evidence says, and what KIND of evidence it is (reservation records, a monthly total,
    or none). Every number is an existing KPI / evidence figure (services/drilldown.nights_evidence); nothing is recalculated here."""
    from routes.properties import _review_reasons
    s, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    single = (ctx["start_year"], ctx["start_month"]) == (ctx["end_year"], ctx["end_month"])
    lo, hi = start[:7], (e - datetime.timedelta(days=1)).strftime("%Y-%m")
    rows = []
    for p in get_properties(conn, include_overhead=False, period=(start, end)):
        pid = p["id"]
        idle = bool(not_active(conn, pid, start, end)) and not (not p["active"] and kpis._has_activity(conn, pid, s, e))
        no_data = not has_real_data(conn, pid, start, end)
        row = {"id": pid, "name": p["name"], "model": "Managed" if is_managed(p) else "Operated", "idle": idle, "no_data": no_data, "inactive": not p["active"],
               "dtype": "none", "flags": [], "strip": None, "source": None, "booked": 0, "available": 0, "occupancy": 0.0, "gross": 0.0, "adr": 0.0, "n_res": 0}
        if not idle:
            row["available"] = kpis.available_nights(conn, pid, start, end)        # the portfolio occupancy denominator counts every property expected to have data
        if not idle and not no_data:
            ev = drill.nights_evidence(conn, p, start, end)
            res, agg = [r for r in ev["rows"] if not r["aggregate"]], [r for r in ev["rows"] if r["aggregate"]]
            dtype = "mixed" if (res and agg) else "reservations" if res else "monthly" if agg else "none"
            if agg:
                c = agg[0]["src"]
                row["source"] = ({"text": f"Monthly total · Batch #{c['batch_id']}", "href": f"/imports/{c['batch_id']}#{c['anchor']}"} if c["kind"] == "workbook"
                                 else {"text": "Monthly total · earlier Excel import", "href": None} if c["kind"] == "legacy" else {"text": "Monthly total · entered by hand", "href": None})
            if res:
                row["source"] = ({"text": f"{len(res)} reservation{'s' if len(res) != 1 else ''}" + (" + monthly total" if agg else ""), "href": None})
            row.update(dtype=dtype, booked=ev["kpi_booked"], available=ev["available"], occupancy=ev["occupancy"], gross=ev["gross"], adr=ev["adr"], n_res=len(res))
            if single and dtype == "reservations":
                row["strip"] = sorted(_day_strip(res, start, end))
            if dtype == "none" and ev["gross"] == 0:
                row["flags"].append("No booking data")
            if ev["gross"] > 0 and ev["kpi_booked"] == 0:
                row["flags"].append("Income without booked nights")
            rv = review_status(conn, pid, lo, hi)
            if rv and rv[0] == "REVIEW" and any(_BOOKING_CHECK.search(t) for t in _review_reasons(conn, pid, rv[1])):
                row["flags"].append("Review")
                row["review_href"] = f"/imports/{rv[1]}#prop-{pid}"
        rows.append(row)
    return rows, (e - s).days, single


def _activity(conn, ctx, props, start, end):
    """Records behind the booking figures, newest first: reservation records (stay dates, channel, amount), monthly totals (Days Booked, never a stay)
    and the workbook's booking-income lines. The amounts add up to Gross Booking Revenue; the nights to Booked nights."""
    out = []
    for p in props:
        pid, name = p["id"], p["name"]
        ev = drill.nights_evidence(conn, p, start, end)
        rr = drill.revenue_records(conn, p, start, end)
        monthly_amount = {i["id"]: i for i in rr["income"] if i.get("drawer") == "booking"}
        for r in ev["rows"]:
            full = conn.execute("SELECT platform FROM bookings WHERE id=?", (r["id"],)).fetchone()
            if r["aggregate"]:
                inc = monthly_amount.get(r["id"])
                out.append({"type": "monthly", "date": r["check_in"], "pid": pid, "property": name, "nights": r["nights"], "amount": inc["amount"] if inc else None,
                            "src": r["src"], "channel": None, "url": url_for("bookings.booking_drawer", booking_id=r["id"]),
                            "label": f"{MONTH_NAMES[int(r['check_in'][5:7])]} booking total"})
            else:
                out.append({"type": "reservation", "date": r["check_in"], "check_out": r["check_out"], "pid": pid, "property": name, "nights": r["nights"], "amount": r["net"],
                            "src": r["src"], "channel": full["platform"] if full else None, "url": url_for("bookings.booking_drawer", booking_id=r["id"]),
                            "label": f"{r['check_in'][8:10].lstrip('0')} {MONTH_ABBR[int(r['check_in'][5:7])]} → {r['check_out'][8:10].lstrip('0')} {MONTH_ABBR[int(r['check_out'][5:7])]}"})
        for i in rr["income"]:
            if i["booking"] and not i.get("drawer"):
                out.append({"type": "income", "date": i["date"], "pid": pid, "property": name, "nights": None, "amount": i["amount"], "src": i["src"], "channel": None,
                            "url": url_for("expenses.transaction_drawer", tx_id=i["id"]), "label": i["text"] or "Booking income"})
    out.sort(key=lambda r: (r["date"], r["property"]), reverse=True)
    return out


@bp.route("/bookings")
def index():
    conn = db.get_conn()
    ctx = request_context(conn)
    pid = ctx["property_id"]
    all_props = get_properties(conn)
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    a = request.args
    q = (a.get("q") or "").strip().lower()
    model = a.get("model") if a.get("model") in ("operated", "managed") else None
    dtype = a.get("dtype") if a.get("dtype") in DTYPES else None
    channel = a.get("channel") or None
    sort = a.get("sort") if a.get("sort") in SORTS else "nights"
    view = a.get("view") if a.get("view") in ("portfolio", "activity") else "portfolio"
    mtd = ctx["partial"] and ctx["choice"] == "this_month"

    rows_all, ndays, single = _property_rows(conn, ctx, start, end)
    if pid:
        rows_all = [r for r in rows_all if r["id"] == pid]
    if model:
        rows_all = [r for r in rows_all if r["model"].lower() == model]
    counted = [r for r in rows_all if not r["idle"] and not r["no_data"]]
    # The band is the sum of the rows below it (every property expected to have data in the period), so the two always reconcile with the portfolio KPIs.
    booked, available, gross = sum(r["booked"] for r in counted), sum(r["available"] for r in rows_all if not r["idle"]), sum(r["gross"] for r in counted)
    prev = compare_bounds(ctx)
    prev_nights = prev_gross = None
    if prev and not pid and not model:
        prev_nights, prev_gross = kpis.booked_nights(conn, None, *prev), kpis.accommodation_revenue(conn, None, *prev)
    no_data = not period_has_data(conn, pid, start, end)
    summary = {"booked": booked, "available": available, "occupancy": (booked / available) if available else 0.0, "gross": gross,
               "adr": (gross / booked) if booked else 0.0, "no_data": no_data,
               "d_nights": None if mtd or prev_nights is None else pct_delta(booked, prev_nights, min_base=5),
               "d_gross": None if mtd or prev_gross is None else pct_delta(gross, prev_gross, min_base=100)}
    # the data-type rail: how the portfolio's booking evidence is made up (properties expected to have data)
    kinds = {"reservations": 0, "monthly": 0, "none": 0}
    for r in counted:
        kinds["reservations" if r["dtype"] in ("reservations", "mixed") else r["dtype"]] += 1
    n_rail = sum(kinds.values()) or 1
    rail = [{"key": k, "label": DTYPES[k], "count": v, "pct": round(v / n_rail * 100, 2)} for k, v in kinds.items() if v]

    rows = rows_all
    if q:
        rows = [r for r in rows if q in r["name"].lower()]
    if dtype:
        rows = [r for r in rows if (("reservations" if r["dtype"] in ("reservations", "mixed") else r["dtype"]) == dtype) and not r["idle"] and not r["no_data"]]
    keyf = {"nights": lambda r: -r["booked"], "occupancy": lambda r: -r["occupancy"], "gbr": lambda r: -r["gross"], "adr": lambda r: -r["adr"], "name": lambda r: r["name"].lower()}[sort]
    shown = sorted([r for r in rows if not r["idle"] and not r["no_data"]], key=keyf) + sorted([r for r in rows if r["no_data"] and not r["idle"]], key=lambda r: r["name"].lower()) \
        + sorted([r for r in rows if r["idle"]], key=lambda r: r["name"].lower())

    activity, act_total, channels = [], None, []
    if view == "activity":
        wanted = {r["id"] for r in rows if not r["idle"] and not r["no_data"]}
        props = [p for p in get_properties(conn, include_overhead=False, period=(start, end)) if p["id"] in wanted]
        recs = _activity(conn, ctx, props, start, end)
        channels = sorted({channel_key(r["channel"]) for r in recs if r["type"] == "reservation" and r["channel"]})
        if dtype:
            recs = [r for r in recs if (dtype == "reservations" and r["type"] == "reservation") or (dtype == "monthly" and r["type"] == "monthly") or (dtype == "none" and False)]
        if channel:
            recs = [r for r in recs if r["type"] == "reservation" and channel_key(r["channel"]) == channel]
        act_total = {"n": len(recs), "nights": sum(r["nights"] or 0 for r in recs), "amount": sum(r["amount"] or 0 for r in recs)}
        activity = recs[:250]
        act_total["more"] = max(0, len(recs) - len(activity))
    base_args = {"q": q or None, "model": model, "dtype": dtype, "channel": channel, "sort": sort if sort != "nights" else None, "view": view if view != "portfolio" else None}

    def U(**changes):
        merged = {**base_args, **changes}
        return url_for("bookings.index", **link_params(True, **{k: v for k, v in merged.items() if v not in (None, "")}))

    n_active = sum(1 for r in rows_all if not r["idle"])
    return render_template(
        "bookings/overview.html", active="bookings", active_bookings_tab="activity" if view == "activity" else "portfolio",
        all_properties=all_props, active_property=None, context_bar=True, ctx=ctx, bar_title="Bookings",
        bar_sub=f"{ctx['display']} · {n_active} {'property' if n_active == 1 else 'properties'}",
        summary=summary, rail=rail, rows=shown, view=view, q=q, model=model, dtype=dtype, channel=channel, channels=channels, sort=sort, sorts=SORTS, dtypes=DTYPES, U=U,
        activity=activity, act_total=act_total, month_label=month_label, ndays=ndays, single=single, compare_label=ctx["compare_display"] or "",
        has_calendar_data=_has_real_bookings(conn), no_data=no_data, any_reservations=any(r["dtype"] in ("reservations", "mixed") for r in counted),
        totals_only=bool(not no_data and (booked or gross) and not any(r["dtype"] in ("reservations", "mixed") for r in counted)),
    )


@bp.route("/bookings/property/<property_id>/drawer")
def property_drawer(property_id):
    """A property's booking evidence for the selected period, as a read-only summary (same figures as its row)."""
    conn = db.get_conn()
    ctx = request_context(conn)
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    p = conn.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone()
    if not p:
        return "<p class='note'>Property not found.</p>", 404
    rows, _n, _s = _property_rows(conn, ctx, start, end)
    row = next((r for r in rows if r["id"] == property_id), None)
    if not row:
        return "<p class='note'>Nothing recorded for this property in this period.</p>", 404
    ev = drill.nights_evidence(conn, p, start, end) if not row["idle"] and not row["no_data"] else None
    ws = lambda anchor="": url_for("properties.bookings", property_id=property_id, **workspace_params(ctx)) + (f"#{anchor}" if anchor else "")
    return render_template("partials/booking_property_drawer.html", r=row, ev=ev, name=p["name"], ctx=ctx, ws=ws)


@bp.route("/bookings/<int:booking_id>/drawer")
def booking_drawer(booking_id):
    conn = db.get_conn()
    b = conn.execute(
        """SELECT b.*, p.name AS property_name,
                  CAST(julianday(b.check_out) - julianday(b.check_in) AS INTEGER) AS nights
           FROM bookings b JOIN properties p ON p.id = b.property_id WHERE b.id=?""", (booking_id,)).fetchone()
    if not b:
        return "<p class='note'>Booking not found.</p>", 404
    line = None
    if b["document_id"]:
        # the extracted line this reservation came from: same confirmation code, else same stay dates
        line = (conn.execute("SELECT * FROM document_items WHERE document_id=? AND reservation_id=? AND reservation_id != '' LIMIT 1",
                             (b["document_id"], b["reservation_id"])).fetchone()
                or conn.execute("SELECT * FROM document_items WHERE document_id=? AND check_in=? AND check_out=? LIMIT 1",
                                (b["document_id"], b["check_in"], b["check_out"])).fetchone())
    return render_template("partials/booking_drawer.html", b=b, prov=ingest.provenance(conn, b["document_id"], line), wb=workbook_source(conn, b))


@bp.route("/bookings/day/<day>")
def day_drawer(day):
    conn = db.get_conn()
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        return "<p class='note'>Unknown date.</p>", 404
    rows = conn.execute(
        """SELECT b.*, p.name AS property_name,
                  CAST(julianday(b.check_out) - julianday(b.check_in) AS INTEGER) AS nights
           FROM bookings b JOIN properties p ON p.id = b.property_id
           WHERE b.status='confirmed' AND b.reservation_id != 'monthly-aggregate' AND b.check_in <= ? AND b.check_out > ?
           ORDER BY p.name""", (day, day)).fetchall()
    d = datetime.date.fromisoformat(day)
    total = len(get_properties(conn, include_overhead=False, period=(day, (d + datetime.timedelta(days=1)).isoformat())))
    return render_template("partials/day_drawer.html", rows=rows, total=total, label=f"{d.day} {MONTH_NAMES[d.month]} {d.year}")


def calendar_data(conn, ctx, pid):
    """The month grid + upcoming reservations for a given context, scoped
    to one property (pid set) or the whole portfolio (pid=None) -- shared
    by the portfolio Bookings > Calendar tab and a property workspace's
    own Calendar tab, so the same query logic backs both."""
    year, month = ctx["end_year"], ctx["end_month"]
    start, end = kpis.month_bounds(year, month)
    total_flats = 1 if pid else (len(get_properties(conn, include_overhead=False, period=(start, end))) or 1)
    scope, sparams = ("AND b.property_id=?", (pid,)) if pid else ("", ())

    overlapping = conn.execute(
        f"""SELECT b.check_in, b.check_out, b.platform FROM bookings b WHERE b.status='confirmed' AND b.reservation_id != 'monthly-aggregate'
             AND b.check_in<? AND b.check_out>? {scope}""",
        (end, start, *sparams),
    ).fetchall()
    spans = [(datetime.date.fromisoformat(r["check_in"]), datetime.date.fromisoformat(r["check_out"]), channel_key(r["platform"])) for r in overlapping]

    days_in_month = cal.monthrange(year, month)[1]
    first_weekday = datetime.date(year, month, 1).weekday()  # Monday=0
    day_cells = [None] * first_weekday
    for d in range(1, days_in_month + 1):
        day = datetime.date(year, month, d)
        on_day = [ch for ci, co, ch in spans if ci <= day < co]
        occupied = len(on_day)
        channels = [k for k in ("airbnb", "booking", "direct", "vrbo", "other") if k in on_day]
        day_cells.append({"day": d, "iso": day.isoformat(), "occupied": occupied, "total": total_flats, "channels": channels,
                           "pct": round(occupied / total_flats * 100)})

    py, pm = kpis.prior_month(year, month)
    ny, nm = kpis.add_months(year, month, 1)
    # a month the workbook carried as monthly totals has booked nights but no night-by-night records: say so instead of drawing every day as empty
    month_nights = kpis.booked_nights(conn, pid, start, end)
    totals_only = (not spans) and month_nights > 0

    upcoming_rows = conn.execute(
        f"""SELECT b.*, p.name AS property_name,
                  CAST(julianday(b.check_out) - julianday(b.check_in) AS INTEGER) AS nights
           FROM bookings b JOIN properties p ON p.id = b.property_id
           WHERE b.status='confirmed' AND b.reservation_id != 'monthly-aggregate' AND b.check_in >= ? {scope}
           ORDER BY b.check_in ASC LIMIT 30""",
        (datetime.date.today().isoformat(), *sparams),
    ).fetchall()

    return {
        "month_label": f"{MONTH_NAMES[month]} {year}", "day_cells": day_cells,
        "py": py, "pm": pm, "ny": ny, "nm": nm,
        "weekday_labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "upcoming_rows": upcoming_rows, "totals_only": totals_only, "month_nights": month_nights,
    }


@bp.route("/bookings/calendar")
def calendar_tab(property_id=None):
    conn = db.get_conn()
    legacy = request.args.get("month", "")
    if re.match(r"^\d{4}-\d{2}$", legacy):  # older ?month=YYYY-MM links
        return redirect(url_for("bookings.calendar_tab", **{"from": legacy + "-01", "to": legacy + "-01"}))
    ctx = request_context(conn)
    pid = ctx["property_id"]
    all_props = get_properties(conn)
    viewing = next((p for p in all_props if p["id"] == pid), None) if pid else None
    data = calendar_data(conn, ctx, pid)

    # month arrows move the shared context, so every other page follows
    def month_href(y, m):
        return url_for("bookings.calendar_tab", **range_params(ctx, **{"from": f"{y}-{m:02d}-01", "to": f"{y}-{m:02d}-01"}))

    return render_template(
        "bookings/calendar.html", active="bookings", active_bookings_tab="calendar", bar_title="Bookings", bar_sub=ctx["display"],
        all_properties=all_props, active_property=None, context_bar=True, ctx=ctx, viewing=viewing, hide_compare=True,
        prev_href=month_href(data["py"], data["pm"]), next_href=month_href(data["ny"], data["nm"]),
        has_calendar_data=_has_real_bookings(conn),
        **{k: v for k, v in data.items() if k not in ("py", "pm", "ny", "nm")},
    )


@bp.route("/bookings/performance")
def performance(property_id=None):
    conn = db.get_conn()
    ctx = request_context(conn)
    # This page has no property selector (hide_property=True below) and
    # always shows every flat -- a stray "property" left over from
    # browsing elsewhere shouldn't make "Reset to latest" appear here.
    ctx["is_latest"] = ctx["period_is_latest"] and ctx["compare"] == "previous_period"
    flats = get_properties(conn, include_overhead=False)
    year, month = ctx["end_year"], ctx["end_month"]
    py, pm = kpis.prior_month(year, month)
    mtd = ctx["partial"] and ctx["choice"] == "this_month"
    # Anchored at the selected period and clipped to trailing 12 months --
    # otherwise a barely-started current month (or years of history) would
    # either fake a cliff at the end of the trend line or make it
    # unreadable. See the matching note in routes/overview.py.
    anchor_ym = f"{year}-{month:02d}"
    months = [m for m in kpis.months_with_data(conn, None) if m <= anchor_ym][-12:]

    series_by_property = {}
    rows, idle_rows = [], []
    period_start, period_end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    pstart, pend = kpis.range_bounds(*kpis.prior_period(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    for p in flats:
        own_months = set(kpis.months_with_data(conn, p["id"]))
        values = []
        for ym in months:
            if ym not in own_months:
                values.append(None)
                continue
            y, m = map(int, ym.split("-"))
            s, e = kpis.month_bounds(y, m)
            values.append(round(kpis.occupancy(conn, p["id"], s, e) * 100, 1))
        series_by_property[p["name"]] = values

        # A property that is inactive, or had not started, and recorded nothing in this period has no occupancy to rank: showing it as 0%
        # would read as measured poor performance. It is listed as "—" (never plotted) with the reason; a property that genuinely had
        # activity in the period stays in, whatever its status today.
        # An active property with nothing recorded at all for the period is likewise "—" ("No data imported"), not a measured 0%.
        if not_active(conn, p["id"], period_start, period_end) and not (not p["active"] and kpis._has_activity(conn, p["id"], datetime.date.fromisoformat(period_start), datetime.date.fromisoformat(period_end))):
            idle_rows.append({"id": p["id"], "name": p["name"], "why": "Inactive" if not p["active"] else "Not active in this period"})
            continue
        if not has_real_data(conn, p["id"], period_start, period_end):
            idle_rows.append({"id": p["id"], "name": p["name"], "why": "No data imported"})
            continue
        # The whole selected period (not just its last month), so the rows add up to the portfolio occupancy tile that links here.
        cstart, cend = period_start, period_end
        cur_occ = kpis.occupancy(conn, p["id"], cstart, cend)
        prev_occ = kpis.occupancy(conn, p["id"], pstart, pend)
        rows.append({
            "id": p["id"], "name": p["name"], "occupancy": cur_occ,
            "days_booked": kpis.booked_nights(conn, p["id"], cstart, cend),
            "available": kpis.available_nights(conn, p["id"], cstart, cend),
            "gross": kpis.accommodation_revenue(conn, p["id"], cstart, cend),
            "revpar": kpis.revpar(conn, p["id"], cstart, cend),
            "delta": None if mtd else (pct_delta(cur_occ, prev_occ) if pstart[:7] in own_months else None),
            "adr": kpis.adr(conn, p["id"], cstart, cend),
        })
    rows.sort(key=lambda r: r["occupancy"], reverse=True)
    # The portfolio line the Overview Occupancy / RevPAR tiles show, from the same KPI functions: the rows above add up to it.
    totals = {"days_booked": kpis.booked_nights(conn, None, period_start, period_end), "available": kpis.available_nights(conn, None, period_start, period_end),
              "occupancy": kpis.occupancy(conn, None, period_start, period_end), "gross": kpis.accommodation_revenue(conn, None, period_start, period_end),
              "adr": kpis.adr(conn, None, period_start, period_end), "revpar": kpis.revpar(conn, None, period_start, period_end)}

    portfolio_months = set(kpis.months_with_data(conn, None))
    portfolio_series = []
    for ym in months:
        if ym not in portfolio_months:
            portfolio_series.append(None)
            continue
        y, m = map(int, ym.split("-"))
        s, e = kpis.month_bounds(y, m)
        portfolio_series.append(round(kpis.occupancy(conn, None, s, e) * 100, 1))

    heatmap_months = months[-12:]
    # history stays: any property with a recorded month in the window keeps its row (blank where it recorded nothing)
    heatmap_rows = [{"id": p["id"], "name": p["name"], "cells": series_by_property[p["name"]][-12:]} for p in flats
                    if any(v is not None for v in series_by_property[p["name"]][-12:])]
    heatmap_rows.sort(key=lambda h: next((r["occupancy"] for r in rows if r["id"] == h["id"]), -1), reverse=True)

    return render_template(
        "bookings/performance.html", active="bookings", active_bookings_tab="performance", bar_title="Bookings", bar_sub=ctx["display"],
        all_properties=get_properties(conn), active_property=None, context_bar=True, ctx=ctx, hide_property=True,
        current_month=ctx["display"], rows=rows, idle_rows=idle_rows, totals=totals,
        rows_json=json.dumps([{**r, "short": short_name(r["name"])} for r in rows]),
        heatmap_months=[MONTH_ABBR[int(ym.split('-')[1])] + " " + ym.split('-')[0][2:] for ym in heatmap_months],
        heatmap_rows=heatmap_rows,
        months_json=json.dumps(months), portfolio_json=json.dumps(portfolio_series),
        has_calendar_data=_has_real_bookings(conn),
    )
