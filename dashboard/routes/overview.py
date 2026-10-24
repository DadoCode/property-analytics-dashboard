import datetime
import json

from flask import Blueprint, render_template, request, url_for

import db
import services.kpis as kpis
from services.common import METRIC_INFO, gbp0, pct0, get_properties, is_managed, pct_delta
from services.provenance import review_status
from routes.expenses import _costs
from services.completeness import has_real_data, not_active, period_has_data
from services.context import request_context, range_params, workspace_params

bp = Blueprint("overview", __name__)


def company_net_profit(conn, start, end):
    """Company Net Profit for [start, end): Company Revenue - Operated Property Costs - Business Costs.

    Nothing here is a new calculation: each term is the existing function the Overview already shows --
      Company Revenue       kpis.adjusted_revenue(conn, None, ...)            (the "Company Revenue" tile: operated revenue + management fees)
      Operated Property Costs  _costs(scope="property", model="operated")        (the Expenses page / Overview cost line)
      Business Costs           _costs(scope="business")                          (the Expenses page / Overview cost line)
    Managed owners' Gross Booking Revenue and the costs on a managed flat's own book are not terms of this sum."""
    revenue = kpis.adjusted_revenue(conn, None, start, end)
    operated = _costs(conn, start, end, scope="property", model="operated")
    business = _costs(conn, start, end, scope="business")
    return {"revenue": revenue, "operated_costs": operated, "business_costs": business, "net_profit": revenue - operated - business}


def _ym_bounds(start, end):
    """'YYYY-MM' of the first and last month of the half-open [start, end) range."""
    last = datetime.date.fromisoformat(end) - datetime.timedelta(days=1)
    return start[:7], last.strftime("%Y-%m")


def build_flow(revenue, fees, operated, business, hrefs):
    """The money-flow picture as plain geometry plus the text each part reveals. Presentation only: every amount is passed in from the
    existing functions (company_net_profit and the management-fee rows); the picture adds no calculation of its own.

      left    operated revenue + management fees           (what Company Revenue is made of)
      middle  Company Revenue (a hatched foot when costs exceed it: the shortfall)
      right   Operated Property Costs, Business Costs, and Net Profit when it is positive
    Heights are proportional to the amounts. Managed owners' booking revenue and their costs never appear."""
    eps = 0.005
    revenue = max(revenue, 0.0)
    net = revenue - operated - business
    shortfall, profit = max(-net, 0.0), max(net, 0.0)
    total = max(revenue, operated + business)
    if total <= eps:
        return None
    fees = min(max(fees, 0.0), revenue)
    ops_rev = revenue - fees
    pct = lambda v: (f"{round(v / revenue * 100):.0f}%" if revenue > eps else "—")
    W, H, TOP, GAP, NW = 760, 220, 22, 12, 10
    XL, XM, XR = 170, 372, 590
    left = [(k, v) for k, v in (("ops_rev", ops_rev), ("fees", fees)) if v > eps]
    right = [(k, v) for k, v in (("operated", operated), ("business", business), ("net", profit)) if v > eps]
    scale = (H - GAP * (max(len(left), len(right)) - 1)) / total

    def stack(items, x):
        nodes, y = [], TOP
        for k, v in items:
            nodes.append({"key": k, "x": x, "y": round(y, 1), "h": round(v * scale, 1), "v": v})
            y += v * scale + GAP
        return nodes

    def ribbon(x1, y1, x2, y2, h):
        xm = (x1 + x2) / 2
        return (f"M{x1:.1f} {y1:.1f}C{xm:.1f} {y1:.1f} {xm:.1f} {y2:.1f} {x2:.1f} {y2:.1f}"
                f"L{x2:.1f} {y2 + h:.1f}C{xm:.1f} {y2 + h:.1f} {xm:.1f} {y1 + h:.1f} {x1:.1f} {y1 + h:.1f}Z")

    lnodes, rnodes = stack(left, XL), stack(right, XR)
    mid = {"key": "revenue", "x": XM, "y": TOP, "h": round(revenue * scale, 1), "v": revenue}
    foot = {"key": "shortfall", "x": XM, "y": round(TOP + revenue * scale, 1), "h": round(shortfall * scale, 1), "v": shortfall} if shortfall > eps else None
    ribbons, off = [], 0.0
    for n in lnodes:                                   # sources into the revenue node, stacked without gaps
        ribbons.append({"key": n["key"], "cls": "rev", "d": ribbon(n["x"] + NW, n["y"], XM, TOP + off, n["h"])})
        off += n["h"]
    off = 0.0
    for n in rnodes:                                   # revenue node out to the sinks
        ribbons.append({"key": n["key"], "cls": "net" if n["key"] == "net" else "cost", "d": ribbon(XM + NW, TOP + off, XR, n["y"], n["h"])})
        off += n["h"]
    if foot:                                           # the part of the costs the revenue could not cover is drawn as a hatched foot, so the stack still adds up
        pass
    names = {"ops_rev": "Operated revenue", "fees": "Management fees", "revenue": "Company Revenue", "shortfall": "Shortfall",
             "operated": "Operated Property Costs", "business": "Business Costs", "net": "Net Profit"}
    shares = {"ops_rev": pct(ops_rev), "fees": pct(fees), "operated": pct(operated), "business": pct(business), "net": pct(profit)}
    nodes = []
    for col in (lnodes, rnodes):                        # two-line labels sit beside their node; push them apart so small nodes never overlap
        floor = None
        for n in col:
            ly = n["y"] + n["h"] / 2
            n["ly"] = round(ly if floor is None else max(ly, floor + 34), 1)
            floor = n["ly"]
    mid["ly"], mid["lx"] = TOP - 9, XM + NW / 2
    for n in lnodes + [mid] + ([foot] if foot else []) + rnodes:
        nodes.append({**n, "w": NW, "label": names[n["key"]], "value": gbp0(n["v"]), "share": shares.get(n["key"], ""), "v": round(n["v"], 2),
                      "pct_w": round(n["v"] / total * 100, 2)})
    height = max([TOP + H] + [n["ly"] + 22 for n in lnodes + rnodes]) + 6
    loss = net < 0
    detail = {
        "revenue": {"title": "Company Revenue", "value": gbp0(revenue), "text": f"Operated revenue {gbp0(ops_rev)} plus management fees {gbp0(fees)}.", "links": [["View properties", hrefs["revenue"]]]},
        "ops_rev": {"title": "Operated revenue", "value": gbp0(ops_rev), "text": f"{pct(ops_rev)} of Company Revenue, from the properties the company runs itself.", "links": [["View properties", hrefs["revenue"]]]},
        "fees": {"title": "Management fees", "value": gbp0(fees), "text": f"{pct(fees)} of Company Revenue, earned on managed properties.", "links": [["View properties", hrefs["revenue"]]]},
        "operated": {"title": "Operated Property Costs", "value": gbp0(operated), "text": f"{pct(operated)} of Company Revenue.", "links": [["View expenses", hrefs["operated"]]]},
        "business": {"title": "Business Costs", "value": gbp0(business), "text": f"{pct(business)} of Company Revenue.", "links": [["View expenses", hrefs["business"]]]},
        "net": {"title": "Company Net Profit", "value": gbp0(net), "text": f"{gbp0(revenue)} revenue − {gbp0(operated)} operated property costs − {gbp0(business)} business costs.", "links": [["Revenue", hrefs["revenue"]], ["Operated costs", hrefs["operated"]], ["Business costs", hrefs["business"]]]},
        "shortfall": {"title": "Shortfall", "value": gbp0(shortfall), "text": "Costs were higher than Company Revenue by this amount.", "links": [["View expenses", hrefs["operated"]]]},
    }
    return {"w": W, "h": round(height), "nw": NW, "total": round(total, 2), "nodes": nodes, "ribbons": ribbons, "detail": detail, "net": round(net, 2), "loss": loss, "revenue": round(revenue, 2),
            "operated": round(operated, 2), "business": round(business, 2), "fees": round(fees, 2), "ops_rev": round(ops_rev, 2),
            "operated_share": pct(operated), "business_share": pct(business)}


def _range_snapshot(conn, property_id, ctx):
    # adjusted_kpi_snapshot(): Revenue/Net profit/Margin reflect only what
    # this business actually earns (full revenue for owned flats, the fee
    # share for managed ones) -- Occupancy/Booked nights/ADR/RevPAR are
    # unaffected, computed the same way as kpi_snapshot().
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    cur = kpis.adjusted_kpi_snapshot(conn, property_id, start, end)
    pstart, pend = kpis.range_bounds(*kpis.prior_period(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    prev = kpis.adjusted_kpi_snapshot(conn, property_id, pstart, pend)
    lystart, lyend = kpis.range_bounds(*kpis.same_period_last_year(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    last_year = kpis.adjusted_kpi_snapshot(conn, property_id, lystart, lyend)
    return cur, prev, last_year


def kpi_rows(conn, property_id, ctx):
    """Four primary KPIs, visually dominant, plus a quieter secondary strip
    -- replaces the old 7-tile row where every metric got equal weight.
    Shared with routes/properties.py so a property workspace's own
    Overview uses the exact same grouping as the portfolio one."""
    cur, prev, last_year = _range_snapshot(conn, property_id, ctx)
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    avg_stay = kpis.avg_stay(conn, property_id, start, end)
    prev_start, prev_end = kpis.range_bounds(*kpis.prior_period(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    prev_avg_stay = kpis.avg_stay(conn, property_id, prev_start, prev_end)

    # A prior-period/prior-year base below this is too small for a percent
    # swing off it to mean anything -- "+8490%" off a near-zero base is
    # noise, not signal, so it's suppressed rather than shown capped or
    # literal (the tile just omits that delta line instead). Net profit's
    # threshold is much higher than revenue's: a thin-margin month can
    # have a genuinely small profit base (a real month, not "no data"),
    # and revenue growth alone can make profit swing by thousands of
    # percent off it without that being the meaningful story.
    MIN_BASE = {"revenue": 100, "net_profit": 1000, "adr": 20, "revpar": 20,
                "occupancy": 0.05, "margin": 0.05, "booked_nights": 2, "avg_stay": 0.5}

    # This Month vs "prior period"/"prior year" compares a partial month
    # (however many days have happened so far) against a FULL prior month
    # -- that's not a real decline, it's an unfinished month next to a
    # finished one, but it rendered exactly like a real red down-arrow
    # every time. Same condition already used for the "MTD," label prefix
    # above -- suppress the coloured delta rather than show a comparison
    # that isn't actually fair.
    mtd = ctx["partial"] and ctx["choice"] == "this_month"

    # NO DATA is decided by coverage (is anything recorded for this period?), never by a figure happening to equal zero: an unevaluable
    # period reads "—" with no comparison, while a period with rows and a measured zero still reads 0.
    has_data = period_has_data(conn, property_id, start, end)

    def tile(label, key, value_fmt, extra_note=None, cur_val=None, prev_val=None):
        if not has_data:
            return {"key": key, "label": label, "value": "—", "delta": None, "delta_ly": None, "note": None, "info": METRIC_INFO.get(key), "no_data": True}
        cv = cur_val if cur_val is not None else cur[key]
        pv = prev_val if prev_val is not None else prev[key]
        lv = (last_year[key] if key in last_year.keys() else None) if cur_val is None else None
        min_base = MIN_BASE.get(key, 0)
        delta = None if mtd else pct_delta(cv, pv, min_base=min_base)
        delta_ly = None if mtd else (pct_delta(cv, lv, min_base=min_base) if lv is not None else None)
        return {
            "key": key, "label": label, "value": value_fmt(cv),
            "delta": delta,
            "delta_ly": delta_ly,
            "note": extra_note,
            "info": METRIC_INFO.get(key),
        }

    primary = [
        tile("Company Revenue", "revenue", lambda v: gbp0(v)),
        tile("Property Profit", "net_profit", lambda v: gbp0(v)),
        tile("Occupancy", "occupancy", lambda v: pct0(v)),
        tile("RevPAR", "revpar", lambda v: gbp0(v)),
    ]
    secondary = [
        tile("ADR", "adr", lambda v: gbp0(v)),
        tile("Booked nights", "booked_nights", lambda v: f"{v:,.0f}"),
        tile("Profit margin", "margin", lambda v: pct0(v)),
        tile("Avg stay", "avg_stay", lambda v: f"{v:.1f} nights" if v else "—", cur_val=avg_stay, prev_val=prev_avg_stay),
    ]
    return primary, secondary, cur




@bp.route("/")
def index():
    conn = db.get_conn()
    nav_properties = get_properties(conn)
    ctx = request_context(conn)
    viewing = next((p for p in nav_properties if p["id"] == ctx["property_id"]), None) if ctx["property_id"] else None
    primary_tiles, _secondary_tiles, cur = kpi_rows(conn, ctx["property_id"], ctx)

    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])

    # Company Net Profit is a company-level figure: it is shown at portfolio scope only, never as if it belonged to one property.
    company = None
    if not ctx["property_id"]:
        has_data = period_has_data(conn, None, start, end)
        company = company_net_profit(conn, start, end) if has_data else None
        mtd = ctx["partial"] and ctx["choice"] == "this_month"
        delta = delta_ly = None
        if company and not mtd:
            pstart, pend = kpis.range_bounds(*kpis.prior_period(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
            lystart, lyend = kpis.range_bounds(*kpis.same_period_last_year(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
            if period_has_data(conn, None, pstart, pend):
                delta = pct_delta(company["net_profit"], company_net_profit(conn, pstart, pend)["net_profit"], min_base=1000)
            if period_has_data(conn, None, lystart, lyend):
                delta_ly = pct_delta(company["net_profit"], company_net_profit(conn, lystart, lyend)["net_profit"], min_base=1000)
        tile = {"key": "company_net_profit", "label": "Company Net Profit", "value": gbp0(company["net_profit"]) if company else "—",
                "delta": delta, "delta_ly": delta_ly, "note": None, "info": METRIC_INFO.get("company_net_profit")}
        primary_tiles.insert(1, tile)
    flats = get_properties(conn, include_overhead=False, period=(start, end))      # active now + inactive ones that had activity in this period

    # Operated and managed flats run on different economics (services/kpis.py adjusted_revenue/business_income), so each row keeps its
    # own "result" column meaning: operated = Property Profit, managed = the management fee. One list, one table; the model is a column.
    lo, hi = _ym_bounds(start, end)
    props = []
    for p in flats:
        managed = is_managed(p)
        snap = kpis.adjusted_kpi_snapshot(conn, p["id"], start, end) if managed else kpis.kpi_snapshot(conn, p["id"], start, end)
        no_data = not has_real_data(conn, p["id"], start, end)
        inactive = not_active(conn, p["id"], start, end)
        rv = review_status(conn, p["id"], lo, hi)
        reasons = []
        if no_data:
            reasons.append("No data")
        if rv and rv[0] == "REVIEW":
            reasons.append("Review")
        if not managed and not no_data and snap["net_profit"] < 0:
            reasons.append("Negative profit")
        if managed and not p["management_fee_pct"]:
            reasons.append("Fee % not set")
        props.append({
            "id": p["id"], "name": p["name"], "model": "Managed" if managed else "Operated", "managed": managed, "no_data": no_data,
            "revenue": snap["revenue"] if not managed else None, "fee": snap["net_profit"] if managed else None,
            "profit": None if managed else snap["net_profit"], "occupancy": snap["occupancy"], "revpar": snap["revpar"],
            "review": rv[1] if rv and rv[0] == "REVIEW" else None, "inactive": inactive["text"] if inactive else None,
            "reasons": reasons, "attention": bool(reasons) and not inactive,
        })
    props.sort(key=lambda r: (not r["attention"], r["managed"], -((r["revenue"] if not r["managed"] else r["fee"]) or 0)))
    attention_count = sum(1 for r in props if r["attention"])

    # Trailing 12 months by default, regardless of how much history exists
    # -- a 3-year line is unreadable as the main chart. Anchored at ctx's
    # end month, not just "whatever the last entry in monthly_series
    # happens to be" -- a barely-started current month with one stray
    # transaction would otherwise show up as a misleading cliff down to
    # near-zero at the end of the line.
    anchor_ym = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    portfolio_series = [s for s in kpis.adjusted_monthly_series(conn, ctx["property_id"]) if s["ym"] <= anchor_ym]

    # Where each headline figure opens. Every destination shows a breakdown that adds up to the number clicked.
    pid = ctx["property_id"]
    if pid:
        def ws(endpoint, anchor=""):
            return url_for(endpoint, property_id=pid, **workspace_params(ctx)) + (f"#{anchor}" if anchor else "")
        drill = {"revenue": ws("properties.bookings", "revenue-records"), "net_profit": ws("properties.detail"),
                 "occupancy": ws("properties.bookings", "booked-nights"), "revpar": ws("properties.bookings", "booked-nights")}
    else:
        props_url = url_for("properties.index", **range_params(ctx))
        drill = {"revenue": props_url, "net_profit": props_url,
                 "occupancy": url_for("bookings.performance", **range_params(ctx)), "revpar": url_for("bookings.performance", **range_params(ctx))}
    hints = {"revenue": "See which properties and records make up this revenue", "net_profit": "See profit and management fees by property",
             "occupancy": "See booked and available nights by property", "revpar": "See booking revenue and available nights by property",
             "company_net_profit": "See how Company Net Profit is made up"}
    net_breakdown, flow = None, None
    if company:
        drill["company_net_profit"] = "#net-profit-breakdown"
        net_breakdown = {**company, "revenue_href": drill["revenue"],
                         "operated_href": url_for("expenses.index", **range_params(ctx, scope="property", model="operated")) + "#ledger",
                         "business_href": url_for("expenses.index", **range_params(ctx, scope="business")) + "#ledger"}
        fees = sum(r["fee"] or 0 for r in props if r["managed"])
        flow = build_flow(company["revenue"], fees, company["operated_costs"], company["business_costs"],
                          {"revenue": net_breakdown["revenue_href"], "operated": net_breakdown["operated_href"], "business": net_breakdown["business_href"]})

    # Property Costs / Business Costs: the same expense logic the Expenses page uses (routes/expenses._costs), nothing new.
    if pid:
        cost_line = {"property": {"value": _costs(conn, start, end, property_id=pid), "href": url_for("expenses.index", **range_params(ctx)), "title": "This property's recorded costs"}}
    else:
        op, mg = _costs(conn, start, end, scope="property", model="operated"), _costs(conn, start, end, scope="property", model="managed")
        cost_line = {"property": {"value": _costs(conn, start, end, scope="property"), "href": url_for("expenses.index", **range_params(ctx, scope="property")),
                                  "title": f"Operated £{op:,.0f} + managed £{mg:,.0f}", "operated": op, "managed": mg,
                                  "managed_href": url_for("expenses.index", **range_params(ctx, scope="property", model="managed")) + "#ledger"},
                     "business": {"value": _costs(conn, start, end, scope="business"), "href": url_for("expenses.index", **range_params(ctx, scope="business")),
                                  "title": "Company-level costs, not part of Property Profit"}}

    hero = next((t for t in primary_tiles if t["key"] == "company_net_profit"), None)
    strip = [t for t in primary_tiles if t["key"] != "company_net_profit"]
    if not ctx["property_id"]:                       # portfolio wording only: one property keeps its own definition
        for t in strip:
            if t["key"] == "net_profit":
                t["info"] = METRIC_INFO["property_profit_portfolio"]
    # Costs exactly as the chart showed them before the redesign: the adjusted snapshot's "costs" (adjusted revenue minus the company's own
    # income), i.e. operated-property costs. Managed owners' costs and business costs are not in it. Net Profit's chart also shows
    # operated + business costs, the two terms that Net Profit subtracts (same _costs() calls as company_net_profit()).
    month_costs = ([(_costs(conn, *kpis.month_bounds(s["year"], s["month"]), scope="property", model="operated"),
                     _costs(conn, *kpis.month_bounds(s["year"], s["month"]), scope="business")) for s in portfolio_series]
                   if not ctx["property_id"] else [])
    series = {
        "months": [s["ym"] for s in portfolio_series],
        "revenue": [round(s["revenue"], 2) for s in portfolio_series],
        "costs": [round(s["costs"], 2) for s in portfolio_series],
        "net_profit": [round(s["net_profit"], 2) for s in portfolio_series],
        "occupancy": [round(s["occupancy"] * 100, 1) for s in portfolio_series],
        "revpar": [round(s["revpar"], 2) for s in portfolio_series],
        # Company Net Profit per month: the same adjusted revenue the series already holds, less that month's operated and business costs
        "company_net_profit": [round(s["revenue"] - op - bu, 2) for s, (op, bu) in zip(portfolio_series, month_costs)],
        "company_costs": [round(op + bu, 2) for op, bu in month_costs],
    }
    # the period the page shows, as months, so the chart can mark it and place the comparison behind it
    span = [m for m in series["months"] if f"{ctx['start_year']}-{ctx['start_month']:02d}" <= m <= anchor_ym]
    return render_template(
        "index.html", active="overview", bar_title="Overview", bar_period_seg=True, net_breakdown=net_breakdown, flow=flow, flow_json=json.dumps(flow) if flow else "null",
        no_data=not period_has_data(conn, ctx["property_id"], start, end), all_properties=nav_properties, flats_count=len(flats), drill=drill, hints=hints,
        cost_line=cost_line, active_property=ctx["property_id"], viewing=viewing, hero=hero, strip=strip, ctx=ctx, context_bar=True,
        props=props, attention_count=attention_count, ctx_params=range_params(ctx),
        series_json=json.dumps(series), span_json=json.dumps(span), anchor_ym=anchor_ym, compare_mode=ctx.get("compare"),
    )
