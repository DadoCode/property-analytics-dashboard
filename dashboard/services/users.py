"""People who may sign in (hosted use). Passwords exist only as Werkzeug hashes; nothing here ever logs or returns one.
Accounts are created by scripts/manage_users.py -- there is deliberately no sign-up page."""
import hashlib
import re
import sqlite3
import time

from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("owner", "viewer")
MIN_PASSWORD_LENGTH = 12
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
# a throwaway hash, checked when the username doesn't exist, so "no such user" takes as long as "wrong password"
_DUMMY_HASH = generate_password_hash("not-a-real-password-just-for-timing")

# sign-in throttle: failures inside the window count; reaching a limit blocks further attempts until the oldest ages out
WINDOW_SECONDS = 15 * 60
MAX_FAILS_PER_USER = 5
MAX_FAILS_PER_IP = 12


def check_username(name):
    name = (name or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise ValueError("A username is 2-32 characters: lowercase letters, digits, dot, dash or underscore, starting with a letter or digit.")
    return name


def check_password(password):
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"A password must be at least {MIN_PASSWORD_LENGTH} characters. A few ordinary words strung together is better than a short clever one.")
    if len(password) > 256:
        raise ValueError("That password is too long (256 characters at most).")
    return password


def _hash(password):
    return generate_password_hash(check_password(password), method="scrypt")


def create(conn, username, password, role="owner", display_name=None):
    username = check_username(username)
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise ValueError(f"A user called '{username}' already exists.")
    conn.execute("INSERT INTO users (username, display_name, password_hash, role, password_changed_at) VALUES (?,?,?,?,datetime('now'))",
                 (username, display_name or username, _hash(password), role))
    conn.commit()


def _get(conn, username):
    row = conn.execute("SELECT * FROM users WHERE username=?", (check_username(username),)).fetchone()
    if not row:
        raise ValueError(f"There is no user called '{username}'.")
    return row


def set_password(conn, username, password):
    row = _get(conn, username)
    conn.execute("UPDATE users SET password_hash=?, password_changed_at=datetime('now') WHERE id=?", (_hash(password), row["id"]))
    conn.commit()


def set_active(conn, username, active):
    row = _get(conn, username)
    conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, row["id"]))
    conn.commit()


def set_role(conn, username, role):
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
    row = _get(conn, username)
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, row["id"]))
    conn.commit()


def listing(conn):
    return conn.execute("SELECT username, display_name, role, active, created_at, last_login_at FROM users ORDER BY username").fetchall()


def any_users(conn):
    try:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:          # no users table: sign-in has never been set up on this database
        return False


def verify(conn, username, password):
    """The user row if the credentials are right AND the account is active, else None. Same work either way."""
    row = conn.execute("SELECT * FROM users WHERE username=?", ((username or "").strip().lower(),)).fetchone()
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password or "")
    return row if (row and ok and row["active"]) else None


def session_token(row):
    """Changes whenever the password does, so changing it signs the person out everywhere."""
    return hashlib.sha256(row["password_hash"].encode()).hexdigest()[:20]


def touch_login(conn, user_id):
    conn.execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user_id,))
    conn.commit()


# ---- throttle --------------------------------------------------------------------------------------------------------
def _recent(conn, kind, key):
    return conn.execute("SELECT COUNT(*) n, MIN(at) first FROM login_failures WHERE kind=? AND key=? AND at>?",
                        (kind, key, time.time() - WINDOW_SECONDS)).fetchone()


def blocked_for(conn, ip, username):
    """Seconds to wait (0 = not blocked) for this IP / this username."""
    wait = 0
    for kind, key, limit in (("ip", ip or "?", MAX_FAILS_PER_IP), ("user", (username or "").strip().lower(), MAX_FAILS_PER_USER)):
        if not key:
            continue
        r = _recent(conn, kind, key)
        if r["n"] >= limit:
            wait = max(wait, int(WINDOW_SECONDS - (time.time() - r["first"])) + 1)
    return wait


def record_failure(conn, ip, username):
    now = time.time()
    conn.execute("DELETE FROM login_failures WHERE at<?", (now - WINDOW_SECONDS,))
    conn.execute("INSERT INTO login_failures (kind, key, at) VALUES ('ip', ?, ?)", (ip or "?", now))
    if (username or "").strip():
        conn.execute("INSERT INTO login_failures (kind, key, at) VALUES ('user', ?, ?)", (username.strip().lower(), now))
    conn.commit()


def clear_user_failures(conn, username):
    conn.execute("DELETE FROM login_failures WHERE kind='user' AND key=?", ((username or "").strip().lower(),))
    conn.commit()
