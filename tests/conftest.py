"""Test set-up: a throwaway SQLite database seeded with the demo data.

Run from the project folder with:   python -m pytest -q
"""
import os
import re
import sys
import tempfile

import pytest

_DB_DIR = tempfile.mkdtemp(prefix="unialert-test-")
# Set TEST_DATABASE_URL to run the suite against an empty PostgreSQL database instead.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite:///" + os.path.join(_DB_DIR, "test.db")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("RUN_MIGRATIONS_ON_START", "false")
os.environ.setdefault("ALLOW_DEMO_TOOLS", "false")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from flask import g  # noqa: E402
from flask.testing import FlaskClient  # noqa: E402
from models import db  # noqa: E402

DEMO_PASSWORD = "password123"   # local demo seed only


@pytest.fixture(scope="session")
def app():
    app_module.app.config["TESTING"] = False   # keep CSRF switched on, as in production
    app_module.initialize_database()
    ctx = app_module.app.app_context()
    ctx.push()

    # The test session shares one app context; clear per-request state so each
    # request starts clean, like a real one.
    original_open = FlaskClient.open

    def clean_open(self, *args, **kwargs):
        for key in list(vars(g)):
            g.pop(key, None)
        db.session.expire_all()
        return original_open(self, *args, **kwargs)

    FlaskClient.open = clean_open
    yield app_module.app
    FlaskClient.open = original_open
    ctx.pop()


def token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


class Client:
    """Small wrapper: signs in and posts forms with a fresh CSRF token."""

    def __init__(self, flask_app):
        self.c = flask_app.test_client()

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def text(self, url):
        return self.c.get(url).get_data(as_text=True)

    def post(self, url, data=None, form_url=None, follow=True, **kw):
        data = dict(data or {})
        data["csrf_token"] = token(self.text(form_url or url)) or token(self.text("/account/profile"))
        return self.c.post(url, data=data, follow_redirects=follow, **kw)

    def login(self, username, password=DEMO_PASSWORD):
        t = token(self.text("/login"))
        return self.c.post("/login", data={"username": username, "password": password, "csrf_token": t},
                           follow_redirects=True)


@pytest.fixture
def client(app):
    return Client(app)


def signed_in(app, username, password=DEMO_PASSWORD):
    c = Client(app)
    c.login(username, password)
    return c
