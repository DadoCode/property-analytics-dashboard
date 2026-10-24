import datetime
import json
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import db
import services.kpis as kpis
from services.context import request_context
from services import completeness
from services.common import METRIC_INFO, MONTH_NAMES, gbp0, get_properties, get_property, is_managed, pct_delta

bp = Blueprint("targets", __name__)

YM = re.compile(r"^\d{4}-\d{2}$")
BASES = [("all", "All recorded months"), ("12", "Last 12 months"), ("6", "Last 6 months"), ("3", "Last 3 months")]
METRICS = ("revenue", "profit", "occupancy")
COLUMN = {"revenue": "revenue_target", "profit": "profit_target", "occupancy": "occupancy_target"}
SORTS = {"behind": "Furthest below first", "ahead": "Furthest above first", "name": "Name"}


def _basis(args):
    b = args.get("basis", "all")
    return b if b in {k for k, _ in BASES} else "all"


def _month(args, cur):
    m = args.get("month", cur)
    return m if YM.match(m or "") and m <= cur else cur


def _label(ym):
    return f"{MONTH_NAMES[int(ym[5:])]} {ym[:4]}"


def _num(raw):
    raw = (raw or "").replace(",", "").replace("£", "").replace("%", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _history(conn, property_id, cur):
    series = [r for r in kpis.monthly_series(conn, property_id) if r["ym"] <= cur and r["revenue"] > 0]
    return {r["ym"]: {"revenue": round(r["revenue"], 2), "profit": round(r["net_profit"], 2), "costs": round(r["costs"], 2),
                      "occupancy": round(r["occupancy"] * 100, 1)} for r in series}


def _metric_view(key, saved_row, avg, hist, month):
    saved_val = saved_row[COLUMN[key]] if saved_row else None
    avg_val = avg[key] if avg else None
    target = saved_val if saved_val is not None else avg_val
    actual = hist.get(month, {}).get(key)
    ly = hist.get(f"{int(month[:4]) - 1}-{month[5:]}", {}).get(key)
    have = actual is not None and bool(target)
    # actual/target*100 only reads as "progress toward target" when the
    # target is positive -- dividing by a negative target flips the
    # direction, so a loss that's worse than a negative profit target can
    # come out over 100% ("achieved"), and a result far better than a
    # negative target can come out negative ("not achieved"). Targets
    # entered by hand are already clamped to >=0 (see _num()), but an
    # unsaved target falls back to a historical average, which can be
    # negative for a property whose recorded months were often
    # unprofitable -- so this does happen with real data, not just
    # hypothetically. variance (a plain subtraction) stays correct for
    # any sign and is what the template now shows whenever the
    # percentage wouldn't be trustworthy.
    pct = round(actual / target * 100) if (have and target > 0) else None
    variance = (actual - target) if have else None
    return {"saved": saved_val is not None, "target": target, "avg": avg_val, "actual": actual,
            "last_year": ly, "pct": pct, "variance": variance,
            "met": (variance >= 0) if variance is not None else None,
            "remaining": (target - actual) if have else None}


def _model_of(conn, property_id):
    """'managed' or 'operated', decided by the same rule as everywhere else (services.common.is_managed)."""
    row = get_property(conn, property_id)
    return "managed" if row and is_managed(row) else "operated"


def _property_view(conn, p, saved_row, months, month, cur):
    hist = _history(conn, p["id"], cur)
    avg = kpis.data_average(conn, p["id"], months)
    return {"id": p["id"], "name": p["name"], "hist": hist, "avg_months": avg["months"] if avg else 0,
            "model": _model_of(conn, p["id"]),
            "metrics": {k: _metric_view(k, saved_row, avg, hist, month) for k in METRICS}}


# ---- what each model may be targeted on -----------------------------------------------------------------
# Operated: the flat's own revenue and Property Profit are the company's. Managed: the booking revenue is mostly the
# owner's, and a profit built from it (minus owner-side costs and the fee) is owner economics, so it is not offered.
# the company's own income from a managed flat is Management Fee Earned, which has no stored target.
MODEL_METRICS = {
    "operated": (("revenue", "Revenue"), ("profit", "Property Profit"), ("occupancy", "Occupancy")),
    "managed": (("revenue", "Gross Booking Revenue"), ("occupancy", "Occupancy")),
}
UNIT = {"revenue": "money", "profit": "money", "occupancy": "pct"}
# every target today is higher-is-better; a cost-type target would be "lower" here and flip the status, never the arithmetic
DIRECTION = {"revenue": "higher", "profit": "higher", "occupancy": "higher"}
STATUS_TEXT = {"behind": "Behind", "on": "On target", "ahead": "Ahead", "benchmark": "Benchmark only", "none": "No target", "nodata": "No data"}
# Only a target the user SAVED is scored (Behind / On target / Ahead). The recorded-average fallback is a benchmark: it gets a neutral
# difference ("£207 below recorded average"), never a status, never the word "target".
TARGET_STATUSES = ("behind", "on", "ahead")


def _fmt(unit, v):
    return gbp0(v) if unit == "money" else f"{round(v)}%"


def _fmt_var(unit, v):
    """Signed variance in the metric's own units: +£420, \u2212\u00a31,201, +6 pts, \u221212 pts."""
    r = round(v)
    if r == 0:
        return "\u00a30" if unit == "money" else "0 pts"
    sign = "+" if r > 0 else "\u2212"
    return f"{sign}\u00a3{abs(r):,.0f}" if unit == "money" else f"{sign}{abs(r)} pts"


def _fmt_diff(unit, v):
    """Neutral difference from the recorded average, as (main, sub): ('£207 below', 'recorded average'). No sign, no judgement."""
    r = round(v)
    if r == 0:
        return "In line with", "recorded average"
    amount = f"\u00a3{abs(r):,.0f}" if unit == "money" else f"{abs(r)} pts"
    return f"{amount} {'above' if r > 0 else 'below'}", "recorded average"


def _status(key, variance):
    """Behind / on target / ahead from the DISPLAYED variance (no invented tolerance), respecting the metric's direction."""
    r = round(variance)
    if r == 0:
        return "on"
    good = r > 0 if DIRECTION[key] == "higher" else r < 0
    return "ahead" if good else "behind"


def _bar(unit, actual, target):
    """Ghost (target) and solid (actual) widths in %, or None when a bar would mislead (no positive target)."""
    if not target or target <= 0 or actual is None:
        return None
    if unit == "pct":
        return {"ghost": min(target, 100), "solid": max(min(actual, 100), 0)}
    scale = max(target, actual if actual > 0 else 0)
    return {"ghost": round(target / scale * 100, 1), "solid": round(max(actual, 0) / scale * 100, 1)}


def _compare_month(month, compare):
    y, m = int(month[:4]), int(month[5:])
    if compare == "previous_period":
        py, pm = kpis.prior_month(y, m)
        return f"{py}-{pm:02d}"
    if compare == "previous_year":
        return f"{y - 1}-{m:02d}"
    return None


def _trend(key, actual, previous):
    """One small direction cue against the chosen comparison month: % change for money, points for occupancy."""
    if actual is None or previous is None:
        return None
    if UNIT[key] == "pct":
        d = round(actual - previous)
        text = f"{abs(d)} pts"
    else:
        d = pct_delta(actual, previous, min_base=1)
        if d is None:
            return None
        d = round(d)
        text = f"{abs(d)}%"
    return {"arrow": "\u2191" if d > 0 else "\u2193" if d < 0 else "\u2192", "text": text if d else "0", "delta": d}


def _row(key, label, view, month, cmp_month, basis_label, scored, model):
    """One property x metric line, every field already worded. A missing target is 'No target set', a missing actual is '\u2014':
    neither is ever turned into 0."""
    m = view["metrics"][key]
    unit = UNIT[key]
    target = m["target"] if m["target"] else None
    if target is not None and not m["saved"] and view["avg_months"] <= 1 and month in view["hist"]:
        # an average of one month IS that month: scoring it against itself would always read "on target", so it is not a target yet
        target = None
    actual = m["actual"] if scored else None
    saved = bool(m["saved"]) and target is not None
    row = {"key": key, "label": label, "unit": unit, "owner_side": key == "revenue" and model == "managed",
           "actual": actual, "target": target if saved else None, "benchmark": target if (target is not None and not saved) else None,
           "saved": saved, "mode": "none" if target is None else ("target" if saved else "benchmark"),
           "status": "none", "variance": None, "bar": None, "trend": None, "diff_main": None, "diff_sub": None,
           "actual_text": _fmt(unit, actual) if actual is not None else "\u2014",
           "target_text": _fmt(unit, target) if saved else None,
           "benchmark_text": _fmt(unit, target) if (target is not None and not saved) else None,
           "source": None if target is None else ("Set by you" if saved else "Recorded average"),
           "source_detail": None if (target is None or saved) else ("Average of recorded months" if basis_label.startswith("all") else f"Average of the {basis_label}"),
           "note": None, "variance_text": None, "pct": m["pct"] if saved else None}
    if row["mode"] == "benchmark":
        row["status"] = "benchmark"
    if target is not None and actual is not None:
        v = actual - target
        row.update(variance=v, bar=_bar(unit, actual, target))
        if saved:
            row.update(variance_text=_fmt_var(unit, v), status=_status(key, v))
        else:
            row["diff_main"], row["diff_sub"] = _fmt_diff(unit, v)
            row["variance_text"] = f"{row['diff_main']} {row['diff_sub']}"
    elif target is not None:
        row["note"] = "No data for this period"
        if saved:
            row["status"] = "nodata"
    row["trend"] = _trend(key, actual, view["hist"].get(cmp_month, {}).get(key)) if cmp_month else None
    row["status_text"] = STATUS_TEXT[row["status"]] if row["mode"] == "target" or row["status"] in ("none", "nodata") else ""
    return row


def _prop_block(conn, p, saved_row, months, month, cur, cmp_month, basis_label, start, end):
    view = _property_view(conn, p, saved_row, months, month, cur)
    why = completeness.not_active(conn, p["id"], start, end)
    scored = not (why and why["kind"] == "not_started")      # a property that had not started is not scored against a target
    rows = [_row(k, label, view, month, cmp_month, basis_label, scored, view["model"]) for k, label in MODEL_METRICS[view["model"]]]
    rel = [(0, r["variance"] / r["target"]) for r in rows if r["variance"] is not None and r["target"]] or \
          [(1, r["variance"] / r["benchmark"]) for r in rows if r["variance"] is not None and r["benchmark"]]
    return {"view": view, "id": p["id"], "name": p["name"], "model": view["model"], "rows": rows, "scored": scored,
            "inactive": (f"Not active in this period \u00b7 starts {why['starts']}" if why["starts"] else "Not active in this period") if not scored else None,
            "rank": min(rel) if rel else None}


def _item(label, hint, key, unit, saved, bench, total, note=None):
    """A portfolio summary line. Properties with a SAVED target are summed against those targets (Actual / Target / Variance, scored).
    If none has a saved target, the properties with a recorded average are summed against that benchmark (Actual / Recorded average /
    Difference, neutral). The two are never added together: a benchmark is not a target. A property with a comparison value but no
    data is left out of both sides, never added as 0."""
    if saved:
        return _item_from(label, hint, key, unit, sum(a for a, _ in saved), sum(t for _, t in saved), len(saved), total, "target")
    if bench:
        return _item_from(label, hint, key, unit, sum(a for a, _ in bench), sum(t for _, t in bench), len(bench), total, "benchmark")
    return {"label": label, "hint": hint, "empty": True, "caption": note or "No properties with both a comparison value and data this month"}


def _item_from(label, hint, key, unit, actual, ref, n, total, mode):
    v = actual - ref
    out = {"label": label, "hint": hint, "empty": False, "unit": unit, "mode": mode, "actual_text": _fmt(unit, actual), "ref_text": _fmt(unit, ref),
           "bar": _bar(unit, actual, ref), "n": n, "total": total}
    noun = "with a set target" if mode == "target" else "using recorded averages"
    out["caption"] = f"{n} of {total} propert{'y' if total == 1 else 'ies'} {noun}"
    if mode == "target":
        out.update(variance_text=_fmt_var(unit, v), status=_status(key, v), status_text=STATUS_TEXT[_status(key, v)])
    else:
        out["diff_main"], out["diff_sub"] = _fmt_diff(unit, v)
        out["status"] = "benchmark"
    return out


def _summary(conn, blocks, start, end):
    ops = [b for b in blocks if b["model"] == "operated"]
    mgd = [b for b in blocks if b["model"] == "managed"]

    def split(group, key):
        saved, bench = [], []
        for b in group:
            r = next((r for r in b["rows"] if r["key"] == key), None)
            if r and r["actual"] is not None:
                if r["target"] is not None:
                    saved.append((r["actual"], r["target"]))
                elif r["benchmark"] is not None:
                    bench.append((r["actual"], r["benchmark"]))
        return saved, bench

    items = [
        _item("Operated revenue", METRIC_INFO["revenue"], "revenue", "money", *split(ops, "revenue"), len(ops)),
        _item("Operated property profit", "Property Profit summed over operated properties only: Company revenue from them minus their own costs. Management Fee Earned and managed owners' economics are not included.",
              "profit", "money", *split(ops, "profit"), len(ops)),
    ]
    # occupancy: nights-weighted over the counted properties, never an average of percentages
    saved, bench = [], []
    for b in blocks:
        r = next((r for r in b["rows"] if r["key"] == "occupancy"), None)
        if b["scored"] and r and r["actual"] is not None and (r["target"] is not None or r["benchmark"] is not None):
            avail = kpis.available_nights(conn, b["id"], start, end)
            if avail:
                (saved if r["target"] is not None else bench).append((kpis.booked_nights(conn, b["id"], start, end), avail, r["target"] if r["target"] is not None else r["benchmark"]))
    use, mode = (saved, "target") if saved else (bench, "benchmark")
    if use:
        avail = sum(a for _, a, _ in use)
        items.append(_item_from("Occupancy", "Booked nights as a share of available nights across the counted properties (weighted by nights, not an average of percentages).",
                                "occupancy", "pct", sum(b for b, _, _ in use) / avail * 100, sum(t * a for _, a, t in use) / avail, len(use), len(blocks), mode))
    else:
        items.append(_item("Occupancy", "", "occupancy", "pct", [], [], len(blocks)))
    items.append(_item("Managed Gross Booking Revenue", "Guest booking value on managed properties. It mostly belongs to the owners: it is NOT company revenue. The company's own income from them is Management Fee Earned.",
                       "revenue", "money", *split(mgd, "revenue"), len(mgd)))
    items[-1]["owner_side"] = True
    return items


@bp.route("/targets")
def index():
    conn = db.get_conn()
    basis = _basis(request.args)
    months = None if basis == "all" else int(basis)
    basis_label = dict(BASES)[basis].lower()
    cy, cm = kpis.current_period(conn)
    cur = f"{cy}-{cm:02d}"
    ctx = request_context(conn)
    ctx["is_latest"] = ctx["period_is_latest"] and ctx["compare"] == "previous_period"
    month = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    if month > cur:
        month = cur
    start, end = kpis.month_bounds(int(month[:4]), int(month[5:]))
    cmp_month = _compare_month(month, ctx["compare"])
    status_f = request.args.get("status") if request.args.get("status") in STATUS_TEXT else ""
    sort = request.args.get("sort") if request.args.get("sort") in SORTS else "behind"

    saved = {r["property_id"]: r for r in conn.execute("SELECT * FROM property_targets")}
    props = get_properties(conn, include_overhead=False, period=(start, end))
    if ctx["property_id"]:
        props = [p for p in props if p["id"] == ctx["property_id"]]
    blocks = [_prop_block(conn, p, saved.get(p["id"]), months, month, cur, cmp_month, basis_label, start, end) for p in props]
    summary = _summary(conn, blocks, start, end)
    modes = {r["mode"] for b in blocks for r in b["rows"]}
    benchmarks_only = "benchmark" in modes and "target" not in modes       # the scope has no saved target at all: say so once

    all_n = len(blocks)
    for b in blocks:
        if status_f:
            b["rows"] = [r for r in b["rows"] if r["status"] == status_f]
    shown = [b for b in blocks if b["rows"] and (b["scored"] or not status_f)]

    def order(b):
        if sort == "name":
            return (b["name"].lower(),)
        tier, rel = b["rank"] if b["rank"] is not None else (9, 0)       # properties with a saved target first, then benchmark-only, then nothing
        return (tier, -rel if sort == "ahead" else rel, b["name"].lower())
    groups = []
    for model, label in (("operated", "Operated"), ("managed", "Managed")):
        mine = sorted([b for b in shown if b["model"] == model], key=order)
        if mine or (not status_f and any(b["model"] == model for b in blocks)):
            groups.append({"model": model, "label": label, "blocks": mine, "n": len(mine)})

    def U(**changes):
        args = {k: v for k, v in request.args.items() if v}
        for k, v in changes.items():
            if v is None or v == "":
                args.pop(k, None)
            else:
                args[k] = v
        return url_for("targets.index", **args)

    menus = [
        ("Status", "Filter by status", f"Status: {STATUS_TEXT[status_f]}" if status_f else None,
         [("All", U(status=None), not status_f)] + [(STATUS_TEXT[k], U(status=k), status_f == k) for k in ("behind", "on", "ahead", "benchmark", "none")]),
        ("Sort", "Sort the list", f"Sort: {SORTS[sort]}", [(lbl, U(sort=k if k != "behind" else None), sort == k) for k, lbl in SORTS.items()]),
        ("Average", "Choose what a default target averages", f"Average: {dict(BASES)[basis]}", [(lbl, U(basis=k if k != "all" else None), basis == k) for k, lbl in BASES]),
    ]
    chips = [{"label": f"Status: {STATUS_TEXT[status_f]}", "href": U(status=None)}] if status_f else []
    edit_links = [{"id": b["id"], "name": b["name"], "href": url_for("targets.edit", property_id=b["id"], basis=basis, month=month)} for b in blocks]

    return render_template(
        "targets.html", active="targets", all_properties=get_properties(conn), active_property=None,
        summary=summary, groups=groups, menus=menus, chips=chips, U=U, edit_links=edit_links, n_blocks=all_n,
        basis=basis, month=month, month_label=_label(month), any_saved=bool(saved), benchmarks_only=benchmarks_only,
        context_bar=True, ctx=ctx, bar_title="Targets", compare_label=ctx["compare_display"],
        bar_sub=_label(month) + (" \u00b7 last month of the period" if (ctx["start_year"], ctx["start_month"]) != (ctx["end_year"], ctx["end_month"]) else ""),
        viewing=get_property(conn, ctx["property_id"]) if ctx["property_id"] else None, status_f=status_f,
        perf=lambda pid: url_for("properties.performance_tab", property_id=pid, **{"from": month + "-01", "to": month + "-01", "compare": "previous_period"}),
        inspect=lambda pid, key: url_for("targets.inspect", property_id=pid, metric=key, month=month, basis=basis, compare=ctx["compare"]),
    )


@bp.route("/targets/<property_id>/inspect")
def inspect(property_id):
    """The compact, read-only look at one property's target: what it is, where it stands, where the target came from."""
    conn = db.get_conn()
    p = get_property(conn, property_id)
    if not p or p["type"] == "overhead":
        abort(404)
    basis = _basis(request.args)
    months = None if basis == "all" else int(basis)
    cy, cm = kpis.current_period(conn)
    cur = f"{cy}-{cm:02d}"
    month = _month(request.args, cur)
    compare = request.args.get("compare") if request.args.get("compare") in ("previous_period", "previous_year", "none") else "previous_period"
    start, end = kpis.month_bounds(int(month[:4]), int(month[5:]))
    cmp_month = _compare_month(month, compare)
    saved_row = conn.execute("SELECT * FROM property_targets WHERE property_id=?", (property_id,)).fetchone()
    block = _prop_block(conn, p, saved_row, months, month, cur, cmp_month, dict(BASES)[basis].lower(), start, end)
    key = request.args.get("metric")
    row = next((r for r in block["rows"] if r["key"] == key), None)
    if not row:
        abort(404)
    hist = block["view"]["hist"]
    recent = []
    for ym in sorted(hist)[-6:]:
        v = hist[ym].get(key)
        recent.append({"label": _label(ym), "value": _fmt(UNIT[key], v) if v is not None else "\u2014",
                       "status": _status(key, v - row["target"]) if (row["target"] is not None and v is not None) else None, "current": ym == month})
    label = row["label"]
    return render_template(
        "partials/target_inspector.html", b=block, r=row, month_label=_label(month), cmp_label=_label(cmp_month) if cmp_month else None, recent=recent,
        edit_url=url_for("targets.edit", property_id=property_id, basis=basis, month=month, compare=compare),
        perf_url=url_for("properties.performance_tab", property_id=property_id, **{"from": f"{month}-01", "to": f"{month}-01", "compare": "previous_period"}),
        meaning=_MEANING[(block["model"], key)], metric_label=label,
    )


_MEANING = {
    ("operated", "revenue"): "Revenue the company earns from this operated property: guest booking revenue plus ancillary income.",
    ("operated", "profit"): "Property Profit: this property's revenue minus its own recorded costs. Business costs are not included.",
    ("operated", "occupancy"): "Booked nights as a share of the nights available since the property started.",
    ("managed", "revenue"): "Gross Booking Revenue: what guests paid for stays at this managed property. It mostly belongs to the owner and is not company revenue. The company earns Management Fee Earned from it, which has no target.",
    ("managed", "occupancy"): "Booked nights as a share of the nights available since the property started.",
}


@bp.route("/targets/<property_id>/edit")
def edit(property_id):
    conn = db.get_conn()
    p = get_property(conn, property_id)
    if not p or p["type"] == "overhead":
        abort(404)
    basis = _basis(request.args)
    months = None if basis == "all" else int(basis)
    cy, cm = kpis.current_period(conn)
    cur = f"{cy}-{cm:02d}"
    month = _month(request.args, cur)
    saved_row = conn.execute("SELECT * FROM property_targets WHERE property_id=?", (property_id,)).fetchone()
    view = _property_view(conn, p, saved_row, months, month, cur)
    avg_label = "Average of all recorded months" if basis == "all" else f"{basis}-month average"
    start, end = kpis.month_bounds(int(month[:4]), int(month[5:]))
    managed = view["model"] == "managed"
    return render_template(
        "partials/target_drawer.html", p=view, month=month, month_label=_label(month), basis=basis,
        avg_label=avg_label, history_json=json.dumps(view["hist"]), formula=view["hist"].get(month),
        perf_url=url_for("properties.performance_tab", property_id=property_id, **{"from": f"{month}-01", "to": f"{month}-01", "compare": "previous_period"}),
        managed=managed, fields=MODEL_METRICS[view["model"]], fee=kpis.business_income(conn, property_id, start, end) if managed else None,
        ly_label=f"{MONTH_NAMES[int(month[5:])]} {int(month[:4]) - 1}",
    )


@bp.route("/targets/<property_id>/save", methods=["POST"])
def save(property_id):
    conn = db.get_conn()
    p = get_property(conn, property_id)
    if not p or p["type"] == "overhead":
        abort(404)
    vals = [_num(request.form.get(k)) for k in METRICS]
    if _model_of(conn, property_id) == "managed":
        # a managed property has no profit target on this page: whatever is stored is left exactly as it is
        old = conn.execute("SELECT profit_target FROM property_targets WHERE property_id=?", (property_id,)).fetchone()
        vals[1] = old["profit_target"] if old else None
    if vals[2] is not None:
        vals[2] = min(vals[2], 100.0)
    if all(v is None for v in vals):
        conn.execute("DELETE FROM property_targets WHERE property_id=?", (property_id,))
    else:
        conn.execute(
            """INSERT INTO property_targets (property_id, revenue_target, profit_target, occupancy_target, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(property_id) DO UPDATE SET revenue_target=excluded.revenue_target,
               profit_target=excluded.profit_target, occupancy_target=excluded.occupancy_target,
               updated_at=excluded.updated_at""",
            (property_id, *vals, datetime.datetime.now().isoformat(timespec="seconds")),
        )
    conn.commit()
    flash(f"\u2713 Targets saved for {p['name']}.", "success")
    return redirect(url_for("targets.index", basis=_basis(request.form)))


@bp.route("/targets/<property_id>/reset", methods=["POST"])
def reset_one(property_id):
    conn = db.get_conn()
    p = get_property(conn, property_id)
    if not p:
        abort(404)
    conn.execute("DELETE FROM property_targets WHERE property_id=?", (property_id,))
    conn.commit()
    flash(f"\u2713 {p['name']}: the target is cleared. Its recorded average is shown as a benchmark.", "success")
    return redirect(url_for("targets.index", basis=_basis(request.form)))


@bp.route("/targets/reset", methods=["POST"])
def reset():
    conn = db.get_conn()
    conn.execute("DELETE FROM property_targets")
    conn.commit()
    flash("\u2713 All set targets are cleared. Recorded averages are shown as benchmarks.", "success")
    return redirect(url_for("targets.index", basis=_basis(request.form)))
