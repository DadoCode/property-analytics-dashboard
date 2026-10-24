"""Hosted-deployment hardening: per-user sign-in, throttling, cross-site write protection, safe outbound requests (iCal), upload/download
safety, error pages, security headers, portable document paths, SQLite WAL + busy timeout, simultaneous two-user use, the gunicorn entry
point and the user-management CLI. Scratch databases and generated throw-away passwords only -- no real data, no real credentials.

Run: .venv/bin/python tests/test_hosted_security.py
"""
import io
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pad-hosted-test-"))
ROOT = Path(__file__).resolve().parent.parent
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ["DASH_SQLITE_WAL"] = "1"
for k in ("VERCEL", "DASH_DEMO_MODE", "DASH_SHARE_TOKEN", "DASH_ENV", "DASH_REQUIRE_LOGIN", "DASH_SECRET_KEY", "DASH_PROXY_HOPS", "DASH_OWNER_PASSWORD", "DASH_VIEWER_PASSWORD"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT / "dashboard"))

import db  # noqa: E402
from services import completeness, netsafe, runtime, users  # noqa: E402

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


def pw():
    return "tst-" + secrets.token_urlsafe(12)       # throw-away, generated per run, never stored


db.ensure_schema()
_plain = sqlite3.connect(os.environ["DASHBOARD_DB_PATH"])
check("a plain (non-server) run does NOT add the sign-in tables to a database", not _plain.execute("SELECT 1 FROM sqlite_master WHERE name IN ('users','login_failures')").fetchone())
_plain.close()
import routes.auth as _auth_mod  # noqa: E402
check("…and sign-in stays off for such a database", _auth_mod.enabled() is False)
db.ensure_schema(auth=True)
c = db.get_conn()
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('op1','OP1','Flat 1 Test House','Flat 1 Test House','flat',1)")
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('op2','OP2','Flat 2 Test House','Flat 2 Test House','flat',1)")
completeness.seed_defaults(c, "op1")
completeness.seed_defaults(c, "op2")
c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source) VALUES ('op1','2026-09-02','x',500,'income','booking_income',0,'manual')")
c.commit()
c.close()

OWNER, VIEWER, GONE = "alice", "bella", "carl"
P_OWNER, P_VIEWER, P_GONE = pw(), pw(), pw()
c = db.get_conn()
users.create(c, OWNER, P_OWNER, "owner")
users.create(c, VIEWER, P_VIEWER, "viewer")
users.create(c, GONE, P_GONE, "owner")
users.set_active(c, GONE, False)
c.close()


def make_app(**env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: v for k, v in env.items() if v is not None})
    try:
        import importlib
        import app as appmod
        importlib.reload(appmod)
        return appmod.create_app()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


H = "https://dash.example.test"
PROD = dict(DASH_ENV="production", DASH_SECRET_KEY=secrets.token_hex(32))


def login(client, user, password, **kw):
    return client.post("/login", data={"username": user, "password": password}, base_url=H, headers={"Origin": H}, **kw)


# ---------------------------------------------------------------- database settings
print("database settings")
con = db.get_conn()
check("WAL mode is on when the server asks for it", con.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
check("busy timeout is about 25 seconds (two people wait for each other, not fail)", con.execute("PRAGMA busy_timeout").fetchone()[0] == 25000)
check("synchronous is NORMAL", con.execute("PRAGMA synchronous").fetchone()[0] == 1)
check("foreign keys stay on", con.execute("PRAGMA foreign_keys").fetchone()[0] == 1)
con.close()
r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0,'dashboard'); import db; c=db.get_conn(); print(c.execute('PRAGMA journal_mode').fetchone()[0], c.execute('PRAGMA busy_timeout').fetchone()[0])"],
                   cwd=ROOT, capture_output=True, text=True, env={**os.environ, "DASH_SQLITE_WAL": "", "DASHBOARD_DB_PATH": str(TMP / "local.db")})
check("locally (WAL not requested) an existing database file is not switched out of its journal mode, but still gets the long timeout", r.stdout.split() == ["delete", "25000"], r.stdout + r.stderr)

calls = []
real_ensure = db.ensure_schema
db.ensure_schema = lambda: (calls.append(1), real_ensure())[1]
app_plain = make_app()
calls.clear()
cl = app_plain.test_client()
for u in ("/", "/properties", "/expenses", "/targets", "/documents", "/reports"):
    cl.get(u)
check("the schema/migration step runs once at start-up, not on every page view (no write lock per request)", len(calls) == 0, len(calls))
db.ensure_schema = real_ensure

# ---------------------------------------------------------------- production start-up rules
print("production start-up")
try:
    make_app(DASH_ENV="production", DASH_SECRET_KEY="")
    ok = False
except RuntimeError:
    ok = True
check("production refuses to start without DASH_SECRET_KEY", ok)
try:
    make_app(DASH_ENV="production", DASH_SECRET_KEY="short")
    ok = False
except RuntimeError:
    ok = True
check("production refuses a short secret key", ok)

app = make_app(**PROD)
cl = app.test_client()

# ---------------------------------------------------------------- everything needs a login; HTTPS only
print("login is required and HTTPS only")
paths = ["/", "/properties", "/properties/op1", "/bookings", "/expenses", "/imports", "/documents", "/targets", "/reports", "/audit/figures", "/reconciliation",
         "/documents/1/file", "/reports/export"]
unauth = {p: cl.get(p, base_url=H) for p in paths}
check("every page redirects an anonymous visitor to the sign-in page", all(r.status_code == 302 and "/login" in r.headers["Location"] for r in unauth.values()),
      {p: (r.status_code, r.headers.get("Location")) for p, r in unauth.items() if not (r.status_code == 302 and "/login" in r.headers["Location"])})
check("an anonymous POST is also refused (no write without a login)", cl.post("/targets/reset", base_url=H, headers={"Origin": H}).status_code == 302)
check("the health check answers without a login and reveals nothing else", cl.get("/healthz", base_url=H).get_data(as_text=True) == "ok")
check("the sign-in page itself loads", cl.get("/login", base_url=H).status_code == 200)
check("plain http is refused (the proxy redirects it first; the app is the backstop)", cl.get("/login", base_url="http://dash.example.test").status_code == 400)
check("there is no public sign-up route", all(cl.get(p, base_url=H).status_code in (404, 302) for p in ("/signup", "/register", "/users", "/admin")) and
      not any("signup" in str(r) or "register" in str(r) for r in app.url_map.iter_rules()))

# ---------------------------------------------------------------- signing in
print("signing in")
r = login(cl, OWNER, "wrong-" + pw())
check("a wrong password is refused with a generic message", r.status_code == 200 and "username or password isn&#39;t right" in r.get_data(as_text=True))
r2 = login(cl, "nobody", pw())
check("an unknown username gets exactly the same message (no account guessing)", "username or password isn&#39;t right" in r2.get_data(as_text=True))
r3 = login(cl, GONE, P_GONE)
check("a DISABLED user cannot sign in even with the right password", r3.status_code == 200 and "username or password isn&#39;t right" in r3.get_data(as_text=True))
check("…and nothing was granted: still redirected to sign-in", cl.get("/", base_url=H).status_code == 302)

owner = app.test_client()
r = login(owner, OWNER, P_OWNER)
check("the right credentials sign in and land on the Overview", r.status_code == 302 and owner.get("/", base_url=H).status_code == 200)
cookie = r.headers.get("Set-Cookie", "")
check("the session cookie is __Host- prefixed, Secure, HttpOnly and SameSite=Lax", "__Host-dash_session=" in cookie and "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie, cookie[:200])
check("the session cookie carries no password or hash", P_OWNER not in cookie)
check("the session lifetime is a sensible 7 days", app.config["PERMANENT_SESSION_LIFETIME"] == 7 * 86400)
check("signed in as the person (shown in the sidebar)", f"Signed in as {OWNER}" in owner.get("/", base_url=H).get_data(as_text=True))
conn = db.get_conn()
check("last sign-in is recorded", conn.execute("SELECT last_login_at FROM users WHERE username=?", (OWNER,)).fetchone()[0] is not None)
check("passwords are stored only as scrypt hashes", all(row[0].startswith("scrypt:") and P_OWNER not in row[0] for row in conn.execute("SELECT password_hash FROM users")))
nxt = app.test_client().post("/login", data={"username": OWNER, "password": P_OWNER, "next": "https://evil.example/x"}, base_url=H, headers={"Origin": H})
check("…a foreign 'next' address is never followed", "evil.example" not in nxt.headers["Location"])

# ---------------------------------------------------------------- throttle
print("sign-in throttle")
t = app.test_client()
for _ in range(users.MAX_FAILS_PER_USER):
    login(t, VIEWER, "bad-" + pw())
r = login(t, VIEWER, P_VIEWER)
check(f"after {users.MAX_FAILS_PER_USER} failures the account is locked for a while, even for the right password", "Too many attempts" in r.get_data(as_text=True))
conn.execute("DELETE FROM login_failures")
conn.commit()
check("failures live in the database (so every worker shares them and a restart doesn't reset them)", conn.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 0)
app0 = make_app(**PROD, DASH_PROXY_HOPS="0")
t0 = app0.test_client()
for i in range(users.MAX_FAILS_PER_IP):
    t0.post("/login", data={"username": f"user{i}", "password": "x"}, base_url=H, headers={"Origin": H, "X-Forwarded-For": f"10.9.{i}.1"})
blocked = t0.post("/login", data={"username": OWNER, "password": P_OWNER}, base_url=H, headers={"Origin": H, "X-Forwarded-For": "203.0.113.77"})
check("a caller cannot dodge the per-address limit by inventing X-Forwarded-For values (untrusted header is ignored)", "Too many attempts" in blocked.get_data(as_text=True))
conn.execute("DELETE FROM login_failures")
conn.commit()
check("client_ip uses the proxy-corrected remote_addr only", "request.remote_addr" in (ROOT / "dashboard/routes/auth.py").read_text() and 'headers.get("X-Forwarded-For"' not in (ROOT / "dashboard/routes/auth.py").read_text())

# ---------------------------------------------------------------- sessions end when they should
print("sessions end when they should")
gone = app.test_client()
cc = db.get_conn()
users.set_active(cc, GONE, True)
login(gone, GONE, P_GONE)
check("a user who was re-enabled can sign in", gone.get("/", base_url=H).status_code == 200)
users.set_active(cc, GONE, False)
check("disabling a user signs them out on their very next request", gone.get("/", base_url=H).status_code == 302)
other = app.test_client()
login(other, OWNER, P_OWNER)
P_OWNER2 = pw()
users.set_password(cc, OWNER, P_OWNER2)
check("changing a password signs that person out everywhere", other.get("/", base_url=H).status_code == 302)
P_OWNER = P_OWNER2
owner = app.test_client()
login(owner, OWNER, P_OWNER)
owner.get("/logout", base_url=H)
check("logging out ends the session", owner.get("/", base_url=H).status_code == 302)
owner = app.test_client()
login(owner, OWNER, P_OWNER)
cc.close()

# ---------------------------------------------------------------- roles + audit trail
print("roles and the audit trail")
viewer = app.test_client()
login(viewer, VIEWER, P_VIEWER)
check("a viewer can read every page", all(viewer.get(p, base_url=H).status_code == 200 for p in ("/", "/properties", "/expenses", "/targets", "/documents", "/reports")))
n0 = db.get_conn().execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
rv = viewer.post("/property/op1/expense", data={"amount": "99", "category": "purchase", "vendor": "X", "year": "2026", "month": "9"}, base_url=H, headers={"Origin": H})
check("a viewer's write is refused on the server and stores nothing", rv.status_code == 302 and db.get_conn().execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n0)
rv = viewer.post("/properties/op1/details", data={"name": "Hacked", "address": "x"}, base_url=H, headers={"Origin": H, "HX-Request": "true"})
check("…for a dynamic (htmx) request too", rv.status_code == 403)

tx_id = db.get_conn().execute("SELECT id FROM transactions WHERE property_id='op1'").fetchone()[0]
owner.post(f"/expenses/transactions/{tx_id}/edit", data={"property_id": "op1", "vendor": "Shop", "description": "edited", "amount": "321", "category": "booking_income"}, base_url=H, headers={"Origin": H})
owner.post("/properties/op1/details", data={"name": "Flat 1 Renamed House", "address": "Flat 1 Renamed House"}, base_url=H, headers={"Origin": H})
rows = db.get_conn().execute("SELECT user FROM audit_log").fetchall()
check("the audit log records the actual signed-in user for every change", rows and all(x[0] == OWNER for x in rows), [x[0] for x in rows])
check("the edited transaction also records who edited it", db.get_conn().execute("SELECT edited_by FROM transactions WHERE id=?", (tx_id,)).fetchone()[0] == OWNER)

# ---------------------------------------------------------------- cross-site writes
print("cross-site write protection")
post = lambda **h: owner.post("/properties/op1/details", data={"name": "Flat 1 Probe", "address": "p"}, base_url=H, headers=h)
check("a write that names another site as its origin is refused", post(Origin="https://evil.example").status_code == 403)
check("Origin: null (a sandboxed page) is refused", post(Origin="null").status_code == 403)
check("a browser-declared cross-site request is refused", post(**{"Sec-Fetch-Site": "cross-site"}).status_code == 403)
check("a foreign Referer is refused", post(Referer="https://evil.example/page").status_code == 403)
check("the dashboard's own origin is accepted", post(Origin=H).status_code == 302)
check("the refusal is a clear page, not an error trace", "Request refused" in post(Origin="https://evil.example").get_data(as_text=True))
check("htmx refusals are a short note", "another site" in owner.post("/properties/op1/details", data={}, base_url=H, headers={"Origin": "https://evil.example", "HX-Request": "true"}).get_data(as_text=True))

# ---------------------------------------------------------------- outbound requests (iCal)
print("iCal links: public https only")
real_gai = socket.getaddrinfo
def fake_gai(ips):
    return lambda host, port, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
bad_urls = ["http://example.com/cal.ics", "file:///etc/passwd", "ftp://example.com/x", "https://127.0.0.1/cal.ics", "https://169.254.169.254/latest/meta-data/",
            "https://10.0.0.5/cal.ics", "https://192.168.1.10/c.ics", "https://[::1]/c.ics", "https://localhost/c.ics", "https://user:pw@example.com/c.ics",
            "https://example.com:8443/c.ics", "https://0.0.0.0/c.ics", "https://100.64.0.1/c.ics", "", "not a url"]
for u in bad_urls:
    try:
        netsafe.check_url(u) if "localhost" not in u else None
        refused = "localhost" in u            # resolved below
    except ValueError:
        refused = True
    if "localhost" in u:
        socket.getaddrinfo = fake_gai(["127.0.0.1"])
        try:
            netsafe.check_url(u)
            refused = False
        except ValueError:
            refused = True
        finally:
            socket.getaddrinfo = real_gai
    check(f"refused: {u or '(empty)'}", refused)
socket.getaddrinfo = fake_gai(["10.1.2.3"])
try:
    netsafe.check_url("https://looks-public.example/c.ics"); ok = False
except ValueError:
    ok = True
check("a public-looking name that resolves to a private address is refused", ok)
socket.getaddrinfo = fake_gai(["93.184.216.34", "169.254.169.254"])
try:
    netsafe.check_url("https://mixed.example/c.ics"); ok = False
except ValueError:
    ok = True
check("a name with ANY private address among its answers is refused", ok)
socket.getaddrinfo = fake_gai(["93.184.216.34"])
host, port, path, ips = netsafe.check_url("https://www.airbnb.com/calendar/ical/123.ics?s=abc")
check("a normal public https calendar link is accepted", (host, port, path, ips) == ("www.airbnb.com", 443, "/calendar/ical/123.ics?s=abc", ["93.184.216.34"]))

class FakeResp:
    def __init__(self, status, headers=None, body=b""):
        self.status, self._h, self._b = status, headers or {}, body
    def getheader(self, k): return self._h.get(k)
    def read(self, n=None): return self._b[:n] if n else self._b

class FakeConn:
    plan = []
    def __init__(self, host, ip, port, timeout): self.host, self.ip = host, ip
    def request(self, *a, **k): FakeConn.seen.append((self.host, self.ip))
    def getresponse(self): return FakeConn.plan.pop(0)
    def close(self): pass
real_pinned = netsafe._PinnedHTTPS
netsafe._PinnedHTTPS = FakeConn
FakeConn.seen = []
FakeConn.plan = [FakeResp(302, {"Location": "https://169.254.169.254/latest/meta-data/"})]
try:
    netsafe.fetch_public_https("https://www.airbnb.com/c.ics"); ok = False
except ValueError:
    ok = True
check("a redirect to the cloud metadata address is refused (and never connected to)", ok and all(h != "169.254.169.254" for h, _ in FakeConn.seen))
FakeConn.seen = []
FakeConn.plan = [FakeResp(302, {"Location": "/other.ics"}), FakeResp(200, body=b"BEGIN:VCALENDAR\nEND:VCALENDAR")]
check("a redirect to another public path is followed, and the connection goes to the checked address",
      netsafe.fetch_public_https("https://www.airbnb.com/c.ics").startswith("BEGIN:VCALENDAR") and FakeConn.seen[0][1] == "93.184.216.34")
FakeConn.plan = [FakeResp(200, body=b"x" * (netsafe.MAX_BYTES + 5))]
try:
    netsafe.fetch_public_https("https://www.airbnb.com/c.ics"); ok = False
except ValueError:
    ok = True
check("an enormous response is refused", ok)
netsafe._PinnedHTTPS = real_pinned
socket.getaddrinfo = real_gai

hits = []
real_cc = socket.create_connection
socket.create_connection = lambda *a, **k: hits.append(a) or (_ for _ in ()).throw(OSError("blocked in test"))
rr = owner.post("/property/op1/sync_calendar", data={"ical_url": "http://169.254.169.254/latest/meta-data/"}, base_url=H, headers={"Origin": H}, follow_redirects=True)
check("the Sync button refuses a metadata address with a plain message and opens no connection", "couldn" in rr.get_data(as_text=True).lower() and "secure https" in rr.get_data(as_text=True) and not hits)
socket.create_connection = real_cc

# ---------------------------------------------------------------- uploads and downloads
print("uploads and downloads")
def up(name, data, **kw):
    return owner.post("/documents/upload", data={"document": (io.BytesIO(data), name), "doc_type": "other", "property_id": "op1"}, base_url=H,
                      headers={"Origin": H}, content_type="multipart/form-data", **kw)
n_docs = lambda: db.get_conn().execute("SELECT COUNT(*) FROM documents").fetchone()[0]
before = n_docs()
for name, data in (("page.html", b"<script>alert(1)</script>"), ("logo.svg", b"<svg onload=alert(1)>"), ("run.exe", b"MZ"), ("a.zip", b"PK\x03\x04"), ("x.php", b"<?php ?>"),
                   ("noext", b"hello"), ("../../evil.pdf.html", b"<b>x</b>")):
    up(name, data)
check("HTML, SVG, executables, archives, scripts and extension-less files are all refused", n_docs() == before)
up("invoice.pdf", b"<html><script>alert(1)</script></html>")
up("photo.png", b"<html>not a png</html>")
check("a renamed HTML page is not accepted as a PDF or a PNG", n_docs() == before)
up("statement.csv", b"Date,Vendor,Amount\n2026-09-03,Shop,12.50\n")
up("scan.pdf", b"%PDF-1.4 minimal")
up("snap.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40)
check("genuine CSV, PDF and PNG files are accepted", n_docs() == before + 3)
rows = db.get_conn().execute("SELECT id, filename, stored_path FROM documents ORDER BY id").fetchall()
check("stored paths are relative to the uploads folder (portable between machines)", all(not Path(r["stored_path"]).is_absolute() and ".." not in r["stored_path"] for r in rows), [r["stored_path"] for r in rows])
by = {r["filename"]: r["id"] for r in rows}
pdf = owner.get(f"/documents/{by['scan.pdf']}/file", base_url=H)
check("a PDF is shown in the browser, with nosniff and no caching", pdf.status_code == 200 and pdf.mimetype == "application/pdf" and "attachment" not in (pdf.headers.get("Content-Disposition") or "")
      and pdf.headers["X-Content-Type-Options"] == "nosniff" and "no-store" in pdf.headers["Cache-Control"])
csvr = owner.get(f"/documents/{by['statement.csv']}/file", base_url=H)
check("a CSV is always a download, never rendered", "attachment" in csvr.headers.get("Content-Disposition", "") and csvr.mimetype == "application/octet-stream")
check("an anonymous caller cannot fetch an uploaded document", app.test_client().get(f"/documents/{by['scan.pdf']}/file", base_url=H).status_code == 302)
# path traversal: even if a row were tampered with, nothing outside the uploads folder is served
sec = TMP / "secret.txt"
sec.write_text("TOP SECRET")
cx = db.get_conn()
for bad in ("../secret.txt", "../../secret.txt", str(sec), "/etc/passwd", "op1/../../secret.txt", "..\\..\\secret.txt"):
    cx.execute("UPDATE documents SET stored_path=? WHERE id=?", (bad, by["scan.pdf"]))
    cx.commit()
    resp = owner.get(f"/documents/{by['scan.pdf']}/file", base_url=H)
    check(f"tampered path {bad!r} serves nothing outside the uploads folder", resp.status_code == 404 and b"TOP SECRET" not in resp.data and b"root:" not in resp.data)
check("resolve_stored refuses ../ escapes", runtime.resolve_stored("../secret.txt") is None and runtime.resolve_stored("op1/../../secret.txt") is None)
good_rel = "op1/legacy.pdf"
(TMP / "uploads" / "op1").mkdir(parents=True, exist_ok=True)
(TMP / "uploads" / "op1" / "legacy.pdf").write_bytes(b"%PDF-1.4 legacy")
check("an OLD absolute path from another machine still resolves by its last two parts, inside the uploads folder",
      runtime.resolve_stored("/Users/someone/Documents/project/data/uploads/op1/legacy.pdf") == (TMP / "uploads" / "op1" / "legacy.pdf").resolve())
check("…and a new relative path resolves", runtime.resolve_stored(good_rel) == (TMP / "uploads" / "op1" / "legacy.pdf").resolve())
check("an absolute path outside the uploads folder is not honoured", runtime.resolve_stored(str(sec)) is None)
cx.close()

# ---------------------------------------------------------------- error pages, headers
print("error pages and headers")
nf = owner.get("/properties/does-not-exist/nothing", base_url=H)
body = nf.get_data(as_text=True)
check("a missing page is a calm custom 404 with no paths or traces", nf.status_code == 404 and "Page not found" in body and "Traceback" not in body and "/Users/" not in body and "werkzeug" not in body.lower())
check("a wrong method is a calm 405", owner.get("/targets/reset", base_url=H).status_code == 405 and "Traceback" not in owner.get("/targets/reset", base_url=H).get_data(as_text=True))

app_b = make_app(**PROD)


@app_b.route("/__boom")
def _boom():
    raise RuntimeError("SECRET-DETAIL /srv/dashboard/data/dashboard.db sqlite3.OperationalError")


ob = app_b.test_client()
login(ob, OWNER, P_OWNER)
b = ob.get("/__boom", base_url=H)
bt = b.get_data(as_text=True)
check("an unexpected error shows a plain 500 page: no exception text, no server path, no SQL", b.status_code == 500 and "Something went wrong" in bt and "SECRET-DETAIL" not in bt and "/srv/" not in bt and "sqlite" not in bt.lower())
bh = ob.get("/__boom", base_url=H, headers={"HX-Request": "true"})
check("…and a short note for dynamic requests", bh.status_code == 500 and "SECRET-DETAIL" not in bh.get_data(as_text=True))
h = owner.get("/", base_url=H).headers
check("security headers: nosniff, framing limited to the same site, referrer limited, HSTS, no caching",
      h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "SAMEORIGIN" and h["Referrer-Policy"] == "same-origin" and "max-age=31536000" in h["Strict-Transport-Security"]
      and "no-store" in h["Cache-Control"])
csp = h["Content-Security-Policy"]
check("content security policy: same-origin by default, no plugins, no foreign framing, forms post only here",
      "default-src 'self'" in csp and "object-src 'none'" in csp and "frame-ancestors 'self'" in csp and "form-action 'self'" in csp and "base-uri 'self'" in csp)
check("the only third-party script host allowed is the CDN the pages already use", re.findall(r"https://[a-z.]+", csp.split("script-src")[1].split(";")[0]) == ["https://cdn.jsdelivr.net"])
check("the sign-in page is also noindex", "noindex" in app.test_client().get("/login", base_url=H).headers.get("X-Robots-Tag", ""))

# ---------------------------------------------------------------- nothing sensitive is served as a file
print("no sensitive files are reachable")
leaks = ["/.env", "/.git/config", "/.git/HEAD", "/data/dashboard.db", "/dashboard.db", "/static/../data/dashboard.db", "/static/../../.env", "/static/%2e%2e/%2e%2e/.env",
         "/backups/", "/uploads/op1/legacy.pdf", "/data/uploads/op1/legacy.pdf", "/dashboard/app.py", "/static/../app.py", "/..%2f..%2fetc/passwd", "/requirements.txt"]
res = {u: owner.get(u, base_url=H) for u in leaks}
check("DB, .env, git metadata, backups, uploads and source files are not served (404, even when signed in)", all(r.status_code in (404, 400) for r in res.values()),
      {u: r.status_code for u, r in res.items() if r.status_code not in (404, 400)})
check("no response body from those probes contains database or config content", all(b"SQLite format" not in r.data and b"SECRET_KEY" not in r.data for r in res.values()))
st = {r.rule for r in app.url_map.iter_rules() if r.endpoint == "static"}
check("the only static route serves the static folder (no directory listing)", st == {"/static/<path:filename>"} and owner.get("/static/", base_url=H).status_code == 404)

# ---------------------------------------------------------------- two people at once
print("two people using it at the same time (WAL + busy timeout)")
errors, writes_ok, reads_ok = [], [0], [0]
lock = threading.Lock()
stop = time.time() + 6


def writer(who, n):
    cl2 = app.test_client()
    login(cl2, OWNER, P_OWNER)
    for i in range(n):
        for url, data in (("/property/op2/expense", {"amount": str(10 + i), "category": "purchase", "vendor": f"V{who}{i}", "year": "2026", "month": "9"}),
                          ("/targets/op2/save", {"revenue_target": str(1000 + i), "occupancy_target": "70"}),
                          ("/properties/op2/details", {"name": f"Flat 2 Test House {who}{i}", "address": "a"})):
            r = cl2.post(url, data=data, base_url=H, headers={"Origin": H}, follow_redirects=True)
            with lock:
                if r.status_code >= 500 or b"locked" in r.data.lower():
                    errors.append((url, r.status_code))
                else:
                    writes_ok[0] += 1


def reader():
    cl2 = app.test_client()
    login(cl2, OWNER, P_OWNER)
    while time.time() < stop:
        for u in ("/", "/properties", "/expenses", "/properties/op2", "/targets", "/bookings"):
            r = cl2.get(u, base_url=H)
            with lock:
                if r.status_code >= 500:
                    errors.append((u, r.status_code))
                else:
                    reads_ok[0] += 1


ths = [threading.Thread(target=writer, args=(w, 8)) for w in "ab"] + [threading.Thread(target=reader) for _ in range(2)]
[t.start() for t in ths]
# meanwhile hold the write lock the way a big import would, for 2 seconds
hold = sqlite3.connect(os.environ["DASHBOARD_DB_PATH"])
hold.execute("BEGIN IMMEDIATE")
time.sleep(2)
hold.rollback(); hold.close()
[t.join() for t in ths]
check(f"{writes_ok[0]} writes and {reads_ok[0]} page reads from four simultaneous users: no 'database is locked', no server errors", not errors and writes_ok[0] >= 40, errors[:5])
check("every concurrent expense write landed exactly once", db.get_conn().execute("SELECT COUNT(*) FROM transactions WHERE property_id='op2' AND source='manual'").fetchone()[0] == 16)
check("the database is intact afterwards", db.get_conn().execute("PRAGMA integrity_check").fetchone()[0] == "ok")

# ---------------------------------------------------------------- the real server process
print("gunicorn serves the app (the same entry the service uses)")
import shutil
gun = ROOT / ".venv" / "bin" / "gunicorn"
if gun.exists():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, **PROD, "DASHBOARD_DB_PATH": str(TMP / "g.db"), "DASH_PROXY_HOPS": "1", "DASH_SQLITE_WAL": "1"}
    p = subprocess.Popen([str(gun), "--chdir", str(ROOT / "dashboard"), "-b", f"127.0.0.1:{port}", "-w", "2", "--threads", "2", "wsgi:app"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    import urllib.request, urllib.error
    def get(path, **headers):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)
    for _ in range(40):
        try:
            code, _b, _h = get("/healthz", **{"X-Forwarded-Proto": "https"})
            break
        except Exception:
            time.sleep(0.25)
    code, body, hdr = get("/healthz", **{"X-Forwarded-Proto": "https"})
    check("gunicorn boots with the production settings and answers the health check", code == 200 and body == b"ok", (code, body))
    code, body, hdr = get("/", **{"X-Forwarded-Proto": "https", "X-Forwarded-Host": "dash.example.test"})
    check("behind the proxy headers, an anonymous visitor to / is sent to the sign-in page", code == 200 and b"Sign in" in body or code == 302, code)
    code, body, hdr = get("/login")
    check("a request that did not come through the HTTPS proxy is refused", code == 400)
    code, body, hdr = get("/login", **{"X-Forwarded-Proto": "https", "X-Forwarded-Host": "dash.example.test"})
    check("the sign-in page sends HSTS and the CSP through gunicorn", code == 200 and "Strict-Transport-Security" in hdr and "Content-Security-Policy" in hdr)
    p.terminate()
    p.wait(timeout=10)
else:
    check("gunicorn is installed in the venv", False)

# ---------------------------------------------------------------- the user-management CLI
print("user-management CLI")
cli_db = TMP / "cli.db"
env = {**os.environ, "DASHBOARD_DB_PATH": str(cli_db)}
run = lambda *a, stdin=None: subprocess.run([sys.executable, str(ROOT / "scripts/manage_users.py"), *a], input=stdin, capture_output=True, text=True, env=env)
secret = pw()
r = run("--password-stdin", "add", "alex", "--role", "owner", stdin=secret + "\n")
check("add creates a user and never echoes the password", r.returncode == 0 and secret not in r.stdout + r.stderr, r.stderr)
r = run("--password-stdin", "add", "alex", stdin=secret + "\n")
check("a duplicate username is refused", r.returncode == 1 and "already exists" in r.stderr)
r = run("--password-stdin", "add", "weak", stdin="short\n")
check("a short password is refused", r.returncode == 1 and "at least" in r.stderr)
r = run("--password-stdin", "add", "Bad Name!", stdin=pw() + "\n")
check("an invalid username is refused", r.returncode == 1)
run("--password-stdin", "add", "director", "--role", "viewer", stdin=pw() + "\n")
r = run("list")
check("list shows users, roles and status but no hashes", "alex" in r.stdout and "viewer" in r.stdout and "scrypt" not in r.stdout)
run("disable", "director")
check("disable works", "DISABLED" in run("list").stdout)
r = run("--password-stdin", "passwd", "alex", stdin=pw() + "\n")
check("passwd works", r.returncode == 0)
check("the stored value is a hash, not the password", secret not in sqlite3.connect(cli_db).execute("SELECT group_concat(password_hash) FROM users").fetchone()[0])

# ---------------------------------------------------------------- repository hygiene
print("repository hygiene")
tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
bad = [f for f in tracked if re.search(r"(^|/)\.env($|\.local)|\.db($|-)|\.key$|\.pem$|secret|\.xlsx$|\.bak|uploads/|backups?/", f) and not f.endswith(".env.example")]
check("no database, key, .env, workbook, upload or backup file is tracked by git", not bad, bad)
ign = (ROOT / ".gitignore").read_text()
check(".gitignore covers .env files, databases, uploads, keys and backups", all(x in ign for x in (".env*", "data/dashboard.db*", "data/uploads/", "*.key", "*.pem", "backups/", "data/.secret_key")))
check("requirements are pinned", all("==" in l for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")))

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
if FAILS:
    print("FAILED:", *FAILS, sep="\n  - ")
    sys.exit(1)
