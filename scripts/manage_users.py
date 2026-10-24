"""Create and manage the people who can sign in. There is no sign-up page; this is the only way accounts are made.

    manage_users.py list
    manage_users.py add USERNAME [--role owner|viewer] [--name "Display Name"]
    manage_users.py passwd USERNAME          change a password (signs that person out everywhere)
    manage_users.py disable USERNAME         they can no longer sign in, and are signed out on their next click
    manage_users.py enable USERNAME
    manage_users.py role USERNAME owner|viewer

Passwords are typed at a hidden prompt (twice) and stored only as a hash. They are never accepted on the command line, so they never end
up in shell history or a process list. For automation only, `--password-stdin` reads one line from standard input instead.

On the server:  sudo -u dashboard /srv/dashboard/venv/bin/python /srv/dashboard/app/scripts/manage_users.py list
(it reads the same environment file as the service, so it finds the same database)."""
import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))

import db  # noqa: E402
from services import users  # noqa: E402


def read_password(args, confirm=True):
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password (hidden): ")
    if confirm and getpass.getpass("Repeat the password: ") != first:
        raise ValueError("The two passwords didn't match. Nothing was changed.")
    return first


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("username"); a.add_argument("--role", default="owner", choices=users.ROLES); a.add_argument("--name")
    for name in ("passwd", "disable", "enable"):
        sub.add_parser(name).add_argument("username")
    r = sub.add_parser("role"); r.add_argument("username"); r.add_argument("role", choices=users.ROLES)
    ap.add_argument("--password-stdin", action="store_true", help="read the password from stdin (automation only)")
    args = ap.parse_args(argv)

    db.ensure_schema(auth=True)
    conn = db.get_conn()
    try:
        if args.cmd == "list":
            rows = users.listing(conn)
            if not rows:
                print("No users yet. Add one with: manage_users.py add USERNAME")
            for u in rows:
                print(f"{u['username']:<20} {u['role']:<7} {'active' if u['active'] else 'DISABLED':<9} last sign-in: {u['last_login_at'] or 'never'}")
        elif args.cmd == "add":
            users.create(conn, args.username, read_password(args), args.role, args.name)
            print(f"Created {args.role} '{args.username.strip().lower()}'.")
        elif args.cmd == "passwd":
            users.set_password(conn, args.username, read_password(args))
            print(f"Password changed for '{args.username}'. They have been signed out everywhere.")
        elif args.cmd in ("disable", "enable"):
            users.set_active(conn, args.username, args.cmd == "enable")
            print(f"'{args.username}' is now {'active' if args.cmd == 'enable' else 'DISABLED'}.")
        elif args.cmd == "role":
            users.set_role(conn, args.username, args.role)
            print(f"'{args.username}' is now a {args.role}.")
    except ValueError as exc:
        print(f"Not done: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
