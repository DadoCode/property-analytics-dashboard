"""WSGI entry point for the hosted deployment:

    gunicorn --chdir dashboard wsgi:app

(`app.py` imports its sibling modules by plain name, so gunicorn must start inside the dashboard/ folder.) The Flask development
server and debug mode are never used on the server."""
from app import app  # noqa: F401
