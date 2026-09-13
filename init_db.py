"""
Run this ONCE per deploy, before the web server starts — never automatically inside
a web worker. On Render this is wired up as the web service's preDeployCommand (see
render.yaml), which runs after the build and before the new version starts serving
traffic.

Why this is a separate script rather than just running at app import time: gunicorn
runs multiple worker processes, and every worker imports app.py. If database setup
ran automatically at import time, concurrent workers would race to initialize the
same database simultaneously — under SQLite this throws "database is locked"; under
Postgres it's still unsafe (duplicate-row races on the bootstrap admin account, etc).
Running it once, here, before any worker starts, avoids that entirely.

Usage:
    python init_db.py
"""
from app import initialize_database

if __name__ == "__main__":
    initialize_database()
    print("Database initialized.")
