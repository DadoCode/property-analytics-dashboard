"""Where this process is running, and what it is allowed to persist.

Two environments exist on purpose:
  * local app -- the authoritative one: a writable data/ folder, so uploads
    and confirmed records are durable.
  * hosted Vercel preview -- a READ-ONLY demo: its filesystem is read-only
    and any /tmp scratch space vanishes between invocations, so nothing
    written there can be trusted. Every mutation is refused up front rather
    than half-working and then disappearing.
"""
import os
from pathlib import Path

from services import env

ROOT = Path(__file__).resolve().parent.parent.parent

DEMO_MESSAGE = "Demo mode — changes and uploads are disabled on this hosted preview."


def share_token():
    """DASH_SHARE_TOKEN: when set, this deployment is a private read-only share
    of real data -- reachable only under /s/<token>/ (see app.py)."""
    return env.get("DASH_SHARE_TOKEN") or None


def refusal_message():
    """Shown when a write is refused."""
    if share_token():
        return "This is a read-only view. Changes and uploads are disabled."
    return DEMO_MESSAGE


def banner_text():
    if share_token():
        return "Private read-only view. Changes and uploads are disabled."
    return DEMO_MESSAGE + " What you see is sample data."


def is_demo():
    """True on Vercel (it always sets VERCEL), when a share token is set, or
    when DASH_DEMO_MODE is set, so the read-only behaviour can also be
    exercised locally."""
    if os.environ.get("VERCEL") or share_token():
        return True
    return (env.get("DASH_DEMO_MODE") or "").lower() in ("1", "true", "yes", "on")


def uploads_dir():
    """The configured upload folder: DASHBOARD_UPLOADS_PATH if set,
    otherwise data/uploads next to the database."""
    configured = env.get("DASHBOARD_UPLOADS_PATH")
    return Path(configured) if configured else ROOT / "data" / "uploads"


def max_upload_bytes():
    """Per-request upload ceiling (DASH_MAX_UPLOAD_MB, default 20 MB). Kept
    below the extraction API's own request limit once a file is base64'd."""
    try:
        mb = float(env.get("DASH_MAX_UPLOAD_MB") or 20)
    except ValueError:
        mb = 20
    return int(mb * 1024 * 1024)


# ---- hosted (server) settings -------------------------------------------------------------------------------------------
def production():
    """DASH_ENV=production: the hosted private deployment. Stricter on purpose -- login is mandatory, cookies are HTTPS-only, a missing
    secret key is a startup error, and security headers (HSTS) are sent. Unset locally, so a Mac run behaves exactly as before."""
    return (env.get("DASH_ENV") or "").lower() == "production"


def login_required():
    """Whether every page needs a sign-in. Always in production (even before any user exists: then nobody gets in, which is the safe way
    to fail); locally only when asked for or when logins are already set up."""
    return production() or (env.get("DASH_REQUIRE_LOGIN") or "").lower() in ("1", "true", "yes", "on")


def proxy_hops():
    """How many reverse proxies sit in front of the app and are trusted to set X-Forwarded-* (DASH_PROXY_HOPS, default 1: Caddy).
    The app itself listens only on 127.0.0.1, so nothing but that proxy can reach it."""
    try:
        return max(0, int(env.get("DASH_PROXY_HOPS") or 1))
    except ValueError:
        return 1


def session_days():
    try:
        return max(1.0 / 24, float(env.get("DASH_SESSION_DAYS") or 7))
    except ValueError:
        return 7.0


def backup_dir():
    configured = env.get("DASH_BACKUP_DIR")
    return Path(configured) if configured else None


# ---- stored document paths ----------------------------------------------------------------------------------------------
# documents.stored_path is kept RELATIVE to the uploads folder ("<property>/<file>") so the same database works on any machine.
# Older rows hold an absolute path; they still resolve (see resolve_stored).
def stored_ref(path):
    """What to write into documents.stored_path for a file saved at `path`."""
    p = Path(path)
    try:
        return p.resolve().relative_to(uploads_dir().resolve()).as_posix()
    except ValueError:
        return str(p)


def resolve_stored(ref):
    """The real file for a documents.stored_path, or None. A relative ref must stay inside the uploads folder (no '..' escapes); an old
    absolute ref is honoured only when it points inside the uploads folder or, failing that, is re-found there by its last two parts."""
    if not ref:
        return None
    base = uploads_dir().resolve()
    p = Path(ref)
    if p.is_absolute():
        try:
            if p.resolve().is_relative_to(base) and p.is_file():
                return p.resolve()
        except OSError:
            pass
        tail = Path(*p.parts[-2:]) if len(p.parts) >= 2 else Path(p.name)
        candidate = (base / tail).resolve()
        return candidate if candidate.is_relative_to(base) and candidate.is_file() else None
    candidate = (base / p).resolve()
    return candidate if candidate.is_relative_to(base) and candidate.is_file() else None
