"""Sign-in for the hosted deployment: one account per person (services/users.py), hashed passwords, a database-backed throttle, and a
same-origin check on every state-changing request.

Off for plain local use (nothing to sign in to on your own Mac). It is ON when DASH_ENV=production or DASH_REQUIRE_LOGIN is set, or as soon
as any user exists. Once on, every page needs a login, and a 'viewer' account can look at everything but change nothing -- enforced here
on the server, not just by hiding buttons. A disabled or deleted user is signed out on their very next request."""
from urllib.parse import urlparse

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

import db
from services import runtime, users

bp = Blueprint("auth", __name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_ENDPOINTS = {"auth.login", "auth.healthz", "static"}


def enabled():
    if runtime.login_required():
        return True
    conn = db.get_conn()
    try:
        return users.any_users(conn)
    finally:
        conn.close()


def client_ip():
    """The caller's address. ProxyFix (app.py) has already applied the ONE trusted proxy's X-Forwarded-For, so remote_addr is the real
    client when behind Caddy and the socket peer otherwise. The raw header is never read here (a caller can write anything in it)."""
    return request.remote_addr or "?"


def _safe_next(target):
    p = urlparse(target or "")
    return target if target and not p.netloc and not p.scheme and target.startswith("/") and not target.startswith("//") else url_for("overview.index")


def _same_origin_ok():
    """Cross-site request protection for anything that changes data (cookies are SameSite=Lax as well). Browsers always send Origin
    on a POST; a request from another site names that other site (or 'null') and is refused. Non-browser callers send neither header
    and cannot ride a victim's cookie, so they are not affected."""
    origin = request.headers.get("Origin")
    if origin:
        return origin != "null" and urlparse(origin).netloc.lower() == request.host.lower()
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    referer = request.headers.get("Referer")
    if referer:
        return urlparse(referer).netloc.lower() == request.host.lower()
    return True


@bp.before_app_request
def block_cross_site_writes():
    if request.method not in SAFE_METHODS and not _same_origin_ok():
        if request.headers.get("HX-Request"):
            return "<p class='note'>That request came from another site, so it was refused.</p>", 403
        return render_template("errors/error.html", code=403, title="Request refused",
                               message="That request didn't come from this dashboard, so it was refused. Go back and try again from the page itself."), 403
    return None


@bp.before_app_request
def require_login():
    g.user, g.username, g.role = None, None, "owner"
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not enabled():
        return None
    uid, token = session.get("uid"), session.get("sv")
    row = None
    if uid:
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        finally:
            conn.close()
    if not row or not row["active"] or token != users.session_token(row):
        session.clear()                      # signed out: never signed in, disabled, deleted, or the password changed since
        if request.headers.get("HX-Request"):
            return jsonify(error="login required"), 401
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?") if request.query_string else request.path))
    g.user, g.username, g.role = row, row["username"], row["role"]
    if row["role"] == "viewer" and request.method not in SAFE_METHODS and request.endpoint != "auth.logout":
        if request.headers.get("HX-Request"):
            return "<p class='note'>This is a view-only account.</p>", 403
        flash("This is a view-only account, so it can't change anything. Ask the owner if something needs updating.", "warning")
        return redirect(request.referrer or url_for("overview.index"))
    return None


@bp.after_app_request
def no_index(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@bp.route("/healthz")
def healthz():
    """For an uptime monitor: says the app is up and the database answers. Reveals nothing else."""
    conn = db.get_conn()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return "ok", 200, {"Content-Type": "text/plain", "Cache-Control": "no-store"}


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not enabled():
        return redirect(url_for("overview.index"))
    error = None
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()[:64]
        password = request.form.get("password") or ""
        ip = client_ip()
        conn = db.get_conn()
        try:
            wait = users.blocked_for(conn, ip, username)
            if wait:
                error = f"Too many attempts. Try again in {wait // 60 + 1} minute{'s' if wait > 60 else ''}."
            else:
                row = users.verify(conn, username, password)
                if row:
                    users.clear_user_failures(conn, username)
                    users.touch_login(conn, row["id"])
                    session.clear()
                    session["uid"], session["sv"] = row["id"], users.session_token(row)
                    session.permanent = True
                    return redirect(_safe_next(request.form.get("next")))
                users.record_failure(conn, ip, username)
                error = "That username or password isn't right."       # same words for a wrong password, an unknown name and a disabled account
        finally:
            conn.close()
    return render_template("login.html", error=error, username=username, next=request.args.get("next") or request.form.get("next") or "")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login") if enabled() else url_for("overview.index"))
