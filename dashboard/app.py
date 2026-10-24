"""
Fernhill Stays business dashboard.

Every KPI on every page is derived on read from `bookings` + `transactions`
via services/kpis.py -- there is no cached totals table to keep in sync.
See scripts/migrate_to_normalized_schema.py for how the original Excel
import's monthly_summary/expense_items/goals became this shape.

Routes live in routes/ (one blueprint module per page area), reusable
non-Flask logic in services/ (kpis, insights, extraction, documents,
ical_sync) -- this file just builds the app and wires them together.

Run with:  python3 dashboard/app.py
"""
import os
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

import db
from routes import register_blueprints
from services import runtime
from services.completeness import seed_defaults

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = runtime.uploads_dir()

RECENT_COOKIE = "recent_properties"
RECENT_MAX = 5


def _secret_key():
    """A random key, created once and kept next to the database so logins
    survive restarts. Falls back to a per-run key if the folder is read-only."""
    import secrets
    from services.env import get
    if get("DASH_SECRET_KEY"):
        key = get("DASH_SECRET_KEY")
        if runtime.production() and len(key) < 32:
            raise RuntimeError("DASH_SECRET_KEY must be at least 32 characters in production.")
        return key
    if runtime.production():
        # a key guessed or regenerated per worker would let sessions be forged or silently break; refuse to start instead
        raise RuntimeError("DASH_SECRET_KEY is not set. Production needs it in the service environment (generate one with: python3 -c 'import secrets; print(secrets.token_hex(32))').")
    if runtime.is_demo():
        # The hosted demo has no login and no durable disk; a per-instance random key
        # would make a flash message set on one serverless instance vanish on the next.
        return "property-analytics-read-only-demo"
    path = ROOT / "data" / ".secret_key"
    try:
        if path.exists():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        path.write_text(key)
        path.chmod(0o600)
        return key
    except OSError:
        return secrets.token_hex(32)


def _token_gate(app, token):
    """Serve the app only under /s/<token>/ ; every other path is a bare 404
    that reveals nothing. The token becomes Flask's SCRIPT_NAME, so every
    url_for() link and asset URL the pages produce keeps it automatically
    (no cookies needed, so a browsing tool can follow links)."""
    import hmac
    prefix = f"/s/{token}"

    def gated(environ, start_response):
        path = environ.get("PATH_INFO", "")
        candidate = path[:len(prefix)]
        if hmac.compare_digest(candidate.encode(), prefix.encode()) and path[len(prefix):len(prefix) + 1] in ("", "/"):
            environ["SCRIPT_NAME"] = prefix
            environ["PATH_INFO"] = path[len(prefix):] or "/"
            return app(environ, start_response)
        start_response("404 Not Found", [("Content-Type", "text/plain"), ("X-Robots-Tag", "noindex")])
        return [b"Not found"]

    return gated


def create_app():
    flask_app = Flask(__name__)
    flask_app.secret_key = _secret_key()
    flask_app.config["MAX_CONTENT_LENGTH"] = runtime.max_upload_bytes()
    demo = runtime.is_demo()
    prod = runtime.production()
    flask_app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                            SESSION_COOKIE_NAME="__Host-dash_session" if prod else "session", SESSION_COOKIE_SECURE=prod,
                            PERMANENT_SESSION_LIFETIME=int(runtime.session_days() * 86400))
    # Behind Caddy (or a tunnel) the real scheme/client/host arrive in X-Forwarded-* headers. Exactly DASH_PROXY_HOPS proxies are trusted
    # (default 1); the app listens only on 127.0.0.1 in production so nothing else can reach it to forge them.
    from werkzeug.middleware.proxy_fix import ProxyFix
    hops = runtime.proxy_hops()
    if hops:
        flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
    token = runtime.share_token()
    if token:
        flask_app.wsgi_app = _token_gate(flask_app.wsgi_app, token)

        @flask_app.after_request
        def _private_headers(response):
            # No search indexing, and never send this URL (it carries the token) to the CDNs the pages load scripts from.
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "private, no-store"
            return response
    @flask_app.before_request
    def _https_only():
        if prod and not request.is_secure and request.endpoint != "auth.healthz":
            # The proxy redirects http -> https before we ever see it; this is the backstop if it is bypassed.
            from flask import abort
            abort(400)
        if not prod:
            flask_app.config["SESSION_COOKIE_SECURE"] = request.is_secure

    @flask_app.after_request
    def _security_headers(response):
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # The pages use inline scripts/styles and Alpine (which evaluates expressions), so script-src must allow them; what the policy does
        # lock down is WHERE things may load from, who may frame the app, where forms may post, and plugins/base-uri.
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
                     "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; img-src 'self' data: blob:; font-src 'self' data: https://fonts.gstatic.com; connect-src 'self'; "
                     "frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
        if prod:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
            if request.endpoint != "static":
                h["Cache-Control"] = "private, no-store"        # financial pages are never kept by a browser cache or an intermediary
        return response

    # The demo database ships already migrated and its filesystem is
    # read-only, so it must never try to alter or seed anything.
    if not demo:
        db.ensure_schema()
        conn = db.get_conn()
        for p in conn.execute("SELECT id FROM properties WHERE type='flat'"):
            seed_defaults(conn, p["id"])
        conn.commit()
        conn.close()

    @flask_app.context_processor
    def _inject_runtime():
        return {"demo_mode": demo, "demo_message": runtime.refusal_message(), "demo_banner": runtime.banner_text(),
                "max_upload_mb": round(runtime.max_upload_bytes() / 1024 / 1024)}

    def _back(default_endpoint):
        ref = request.referrer or ""
        return ref if ref.startswith(request.host_url) else url_for(default_endpoint)

    @flask_app.before_request
    def _demo_read_only():
        """Refuse every write on the hosted preview before it can touch the
        (read-only, non-durable) filesystem -- a calm explanation, never a
        500, and never a half-saved record that would later vanish."""
        if not demo or request.method in ("GET", "HEAD", "OPTIONS") or request.endpoint == "auth.login":
            return None
        if request.headers.get("HX-Request"):
            return f'<div class="note">{runtime.refusal_message()}</div>', 200
        flash(runtime.refusal_message(), "warning")
        return redirect(_back("overview.index"))

    from werkzeug.exceptions import HTTPException

    @flask_app.errorhandler(HTTPException)
    def _http_error(err):
        if err.code == 413:
            return _too_large(err)
        titles = {400: "Bad request", 403: "Not allowed", 404: "Page not found", 405: "Not allowed", 429: "Too many requests"}
        messages = {404: "That page doesn't exist, or the record was removed. Use the sidebar to get back on track.",
                    403: "You don't have permission to do that.", 405: "That action isn't available here.",
                    400: "That request couldn't be understood."}
        code = err.code or 500
        if request.headers.get("HX-Request"):
            return f'<div class="note">{messages.get(code, "Something went wrong.")}</div>', code
        return render_template("errors/error.html", code=code, title=titles.get(code, "Something went wrong"),
                               message=messages.get(code, "Something went wrong. Please go back and try again.")), code

    @flask_app.errorhandler(Exception)
    def _server_error(err):
        # Never show a stack trace, file path or SQL to the browser. The full detail goes to the server log only.
        flask_app.logger.exception("Unhandled error on %s %s", request.method, request.path)
        if request.headers.get("HX-Request"):
            return '<div class="note">Something went wrong on our side. Nothing was lost; please try again.</div>', 500
        return render_template("errors/error.html", code=500, title="Something went wrong",
                               message="That didn't work, and the details have been logged on the server. Please go back and try again."), 500

    @flask_app.errorhandler(413)
    def _too_large(_err):
        limit = round(runtime.max_upload_bytes() / 1024 / 1024)
        msg = f"That upload is larger than the {limit} MB limit, so nothing was saved. Upload a smaller file, or split a long PDF into parts."
        if request.headers.get("HX-Request"):
            return f'<div class="note">{msg}</div>', 413
        flash(msg, "error")
        return redirect(_back("documents.index"))

    @flask_app.context_processor
    def _inject_provenance():
        """Which workbook import last updated the figures on this page (shown quietly under the context bar)."""
        from flask import g
        import db as _db
        from services.workbook import batches as _wb

        def workbook_provenance(ctx):
            try:
                lo = f"{ctx['start_year']}-{ctx['start_month']:02d}"
                hi = f"{ctx['end_year']}-{ctx['end_month']:02d}"
                return _wb.provenance_for_range(_db.get_conn(), ctx.get("property_id"), lo, hi)
            except Exception:
                return None
        return {"workbook_provenance": workbook_provenance}

    @flask_app.context_processor
    def _inject_auth():
        from flask import g
        from routes.auth import enabled
        return {"auth_on": enabled(), "role": g.get("role", "owner"), "current_user": g.get("username")}

    @flask_app.context_processor
    def _asset_versions():
        # file mtime in the URL so a browser never serves stale CSS/JS after an update
        static = Path(__file__).resolve().parent / "static"
        return {"asset_v": max((f.stat().st_mtime_ns for f in static.iterdir() if f.is_file()), default=0)}

    @flask_app.after_request
    def _remember_context(response):
        from flask import g
        saved = g.get("ctx_save")
        if saved:
            response.set_cookie("dash_ctx", saved, max_age=60 * 60 * 24 * 90, samesite="Lax")
        return response

    @flask_app.context_processor
    def _inject_nav_context():
        from flask import g
        qs = g.get("ctx_save") or request.cookies.get("dash_ctx", "")
        return {"nav_qs": ("?" + qs) if qs else ""}

    if not demo:
        UPLOADS.mkdir(parents=True, exist_ok=True)       # once, at start-up (the schema above is also only checked once, not on every page view)

    @flask_app.context_processor
    def _inject_recently_viewed():
        # Replaces the old permanently-listed-every-property sidebar: the
        # last few properties actually visited, read from a plain cookie
        # (no server-side session store needed for a solo-operator tool).
        raw = request.cookies.get(RECENT_COOKIE, "")
        ids = [i for i in raw.split(",") if i][:RECENT_MAX]
        if not ids:
            return {"recently_viewed": []}
        conn = db.get_conn()
        by_id = {p["id"]: p for p in conn.execute(
            f"SELECT id, name FROM properties WHERE id IN ({','.join('?' * len(ids))})", ids
        )}
        return {"recently_viewed": [by_id[i] for i in ids if i in by_id]}

    from services.completeness import DOC_TYPE_LABELS

    def money(v, places=0):
        if v is None:
            return "—"
        if round(v, places) == 0:
            v = 0                                   # never "−£0" or "−£0.00"
        sign = "−" if v < 0 else ""
        return f"{sign}£{abs(v):,.{places}f}"

    def money_k(v):
        if v is None:
            return "—"
        a = abs(v)
        sign = "−" if v < 0 and round(a) != 0 else ""
        return f"{sign}£{a / 1000:.1f}k" if a >= 1000 else f"{sign}£{a:.0f}"

    flask_app.jinja_env.filters["money"] = money
    flask_app.jinja_env.filters["money2"] = lambda v: money(v, 2)
    flask_app.jinja_env.filters["money_k"] = money_k
    flask_app.jinja_env.filters["pct"] = lambda v, places=0: "—" if v is None else f"{v:.{places}f}%"
    from services.common import CHANNELS, METRIC_INFO, channel_key
    flask_app.jinja_env.filters["channel_key"] = channel_key
    flask_app.jinja_env.globals["CHANNELS"] = CHANNELS
    flask_app.jinja_env.globals["METRIC_INFO"] = METRIC_INFO
    flask_app.jinja_env.filters["doc_label"] = lambda k: DOC_TYPE_LABELS.get(k, k)

    def xurl(base, endpoint="expenses.index", **extra):
        """url_for(endpoint) carrying the shared period/property/compare params, plus extras."""
        from flask import url_for
        return url_for(endpoint, **{**base, **extra})

    def cx(endpoint, keep_property=False, **values):
        """url_for(endpoint) that carries the user's current period/compare
        (and the property too when keep_property) so drilling into another
        page never lands on a different month than the one they chose."""
        from flask import url_for
        from services.context import link_params
        return url_for(endpoint, **link_params(keep_property, **values))

    def wsurl(ctx, endpoint, property_id, **extra):
        """A link into one property's workspace tab carrying the selected period and comparison explicitly."""
        from flask import url_for
        from services.context import workspace_params
        return url_for(endpoint, property_id=property_id, **workspace_params(ctx), **extra)

    from services.common import short_name
    flask_app.jinja_env.filters["short"] = short_name
    flask_app.jinja_env.globals["xurl"] = xurl
    flask_app.jinja_env.globals["cx"] = cx
    flask_app.jinja_env.globals["wsurl"] = wsurl

    register_blueprints(flask_app)
    return flask_app


app = create_app()


if __name__ == "__main__":
    if runtime.production():
        raise SystemExit("Don't start the production app with the development server. Use gunicorn: gunicorn --chdir dashboard wsgi:app")
    app.run(debug=True, port=5050)
