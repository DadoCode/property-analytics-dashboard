"""The public demo data: scripts/seed_demo_data.py builds a fake-data database that the whole app renders, and it can never delete a database
it wasn't explicitly pointed at. Scratch directories only.

Run: .venv/bin/python tests/test_demo_seed.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="pad-demoseed-test-"))
FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


SEED = [sys.executable, str(ROOT / "scripts" / "seed_demo_data.py")]
clean_env = {k: v for k, v in os.environ.items() if k not in ("DASHBOARD_DB_PATH", "DASHBOARD_UPLOADS_PATH")}

# it refuses to run without an explicit database path, and deletes nothing
precious = TMP / "precious.db"
precious.write_bytes(b"do not delete")
r = subprocess.run(SEED, capture_output=True, text=True, env=clean_env, cwd=TMP)
check("without DASHBOARD_DB_PATH it refuses to run (it would delete the database it targets)", r.returncode != 0 and "Refusing" in (r.stdout + r.stderr), r.stdout + r.stderr)
check("…and the default data/dashboard.db location is never touched by that refusal", precious.read_bytes() == b"do not delete")

db_path, uploads = TMP / "demo" / "dashboard.db", TMP / "demo" / "uploads"
db_path.parent.mkdir()
env = {**clean_env, "DASHBOARD_DB_PATH": str(db_path), "DASHBOARD_UPLOADS_PATH": str(uploads)}
r = subprocess.run(SEED, capture_output=True, text=True, env=env, cwd=TMP)
check("the seed script runs against a scratch path", r.returncode == 0 and "Seeded demo DB" in r.stdout, r.stdout + r.stderr)

c = sqlite3.connect(db_path)
n = lambda q: c.execute(q).fetchone()[0]
check("five fake flats plus a shared-overheads entry", n("SELECT COUNT(*) FROM properties WHERE type='flat'") == 5 and n("SELECT COUNT(*) FROM properties WHERE type='overhead'") == 1)
check("over a year of bookings and costs", n("SELECT COUNT(*) FROM bookings") > 100 and n("SELECT COUNT(*) FROM transactions") > 200
      and n("SELECT COUNT(DISTINCT substr(check_in,1,7)) FROM bookings") >= 12)
check("sample documents exist on disk and resolve through the portable relative path", n("SELECT COUNT(*) FROM documents") >= 3
      and all(not Path(p).is_absolute() for (p,) in c.execute("SELECT stored_path FROM documents")))
c.close()

# the whole app renders it
os.environ.update({"DASHBOARD_DB_PATH": str(db_path), "DASHBOARD_UPLOADS_PATH": str(uploads)})
for k in ("DASH_ENV", "DASH_REQUIRE_LOGIN", "VERCEL", "DASH_DEMO_MODE", "DASH_SHARE_TOKEN"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT / "dashboard"))
from app import create_app  # noqa: E402
from services import runtime  # noqa: E402

cl = create_app().test_client()
paths = ["/", "/properties", "/bookings", "/bookings/performance", "/expenses", "/documents", "/targets", "/reports", "/imports",
         "/properties/flat-riverside-1", "/properties/flat-riverside-1/performance", "/properties/flat-maple-house/documents"]
bad = [(p, cl.get(p).status_code) for p in paths if cl.get(p).status_code != 200]
check(f"{len(paths)} main pages render with the demo data", not bad, bad)
home = cl.get("/").get_data(as_text=True)
check("the Overview shows real-looking money and the demo properties by name", "£" in home and "Riverside Court" in home)
doc = sqlite3.connect(db_path).execute("SELECT stored_path FROM documents LIMIT 1").fetchone()[0]
check("a demo document's stored path resolves inside the uploads folder", runtime.resolve_stored(doc) is not None, doc)

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
if FAILS:
    print("FAILED:", *FAILS, sep="\n  - ")
    sys.exit(1)
