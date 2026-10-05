"""
Schema migrations for databases created by older versions of the app.

db.create_all() only creates *missing tables*; it never changes tables that
already exist. The live PostgreSQL database was created before these changes,
so this module brings it up to date. Every step checks the current schema first
and does nothing if it has already been applied, so it is safe to run on every
deploy. It is called from initialize_database() (init_db.py) AND when the app
starts against PostgreSQL (see app.py), so it runs even if the hosting platform
does not execute init_db.py before deploys.

All steps run inside ONE transaction: on PostgreSQL, if anything fails the whole
migration is rolled back and the database is left exactly as it was.

Changes applied:
  1. user: add must_change_password, failed_logins, locked_until (security).
  2. student: any NULL parent_consent becomes FALSE (existing TRUE/FALSE kept).
  3. assessment: normalise to 3NF -
       a. link every mark that has no definition to an AssessmentDefinition
          (re-using a matching one for the same course and item number, or
          creating it from the row's own title / sequence / max score);
       b. remove duplicate marks for the same student and item;
       c. drop the repeated columns title, sequence, max_score;
       d. make definition_id required (PostgreSQL).
  4. Unique rules: course.code; enrollment(student_id, course_id);
     attendance_record(enrollment_id, week); assessment(enrollment_id, definition_id).
     Duplicate attendance rows (same student, same week) are reduced to the most
     recent one first. If duplicate courses or enrolments exist they are NOT
     merged automatically - that rule is skipped and a warning is printed so a
     person can decide.
  5. enrollment: add status ('active' / 'withdrawn'), withdrawn_at,
     withdrawn_reason, withdrawn_by_id, so a lecturer can withdraw a student
     from a course without deleting their attendance and marks.
  6. New tables (for example activity_log) are created if missing.
"""
from datetime import datetime
from sqlalchemy import inspect, text


def _log(msg):
    print(f"[migrations] {msg}", flush=True)


def _columns(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _index_names(conn, table):
    insp = inspect(conn)
    names = {i["name"] for i in insp.get_indexes(table)}
    names |= {u["name"] for u in insp.get_unique_constraints(table) if u.get("name")}
    return names


def _has_unique_on(conn, table, cols):
    insp = inspect(conn)
    wanted = list(cols)
    for u in insp.get_unique_constraints(table):
        if list(u["column_names"]) == wanted:
            return True
    for i in insp.get_indexes(table):
        if i.get("unique") and list(i["column_names"]) == wanted:
            return True
    return False


def run_migrations(db):
    engine = db.engine
    dialect = engine.dialect.name
    true, false = ("TRUE", "FALSE") if dialect == "postgresql" else ("1", "0")

    with engine.begin() as conn:
        if dialect == "postgresql":
            # Several gunicorn workers may start at once and all call this. A
            # transaction-level advisory lock makes them take turns: the first
            # one migrates, the others wait, then find nothing left to do.
            conn.execute(text("SELECT pg_advisory_xact_lock(724501)"))
        tables = set(inspect(conn).get_table_names())

        # ---- 6. new tables (created first; create_all never alters existing ones)
        missing = [t for t in db.metadata.sorted_tables if t.name not in tables]
        if missing and "user" in tables:
            db.metadata.create_all(conn, tables=missing)
            _log("created table(s): " + ", ".join(t.name for t in missing))
            tables = set(inspect(conn).get_table_names())

        # ---- 1. user security columns ---------------------------------------
        if "user" in tables:
            cols = _columns(conn, "user")
            if "must_change_password" not in cols:
                conn.execute(text(f'ALTER TABLE "user" ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT {false}'))
                _log("added user.must_change_password")
            if "failed_logins" not in cols:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0'))
                _log("added user.failed_logins")
            if "locked_until" not in cols:
                ts = "TIMESTAMP" if dialect == "postgresql" else "DATETIME"
                conn.execute(text(f'ALTER TABLE "user" ADD COLUMN locked_until {ts}'))
                _log("added user.locked_until")

        # ---- 2. parent consent: no NULLs ---------------------------------------
        if "student" in tables:
            n = conn.execute(text(f"UPDATE student SET parent_consent = {false} WHERE parent_consent IS NULL")).rowcount
            if n:
                _log(f"set parent_consent=FALSE on {n} student(s) that had no value")

        # ---- 3. assessment normalisation ---------------------------------------
        if "assessment" in tables and "title" in _columns(conn, "assessment"):
            orphans = conn.execute(text(
                "SELECT a.id, e.course_id, a.title, a.sequence, a.max_score "
                "FROM assessment a JOIN enrollment e ON e.id = a.enrollment_id "
                "WHERE a.definition_id IS NULL"
            )).fetchall()
            created = linked = 0
            cache = {}
            for aid, course_id, title, seq, max_score in orphans:
                key = (course_id, seq)
                if key not in cache:
                    found = conn.execute(text(
                        "SELECT id FROM assessment_definition WHERE course_id = :c AND sequence = :s ORDER BY id LIMIT 1"
                    ), {"c": course_id, "s": seq}).scalar()
                    if found is None:
                        found = conn.execute(text(
                            "INSERT INTO assessment_definition (course_id, title, assessment_type, sequence, max_score, created_at) "
                            "VALUES (:c, :t, 'Coursework', :s, :m, :now)" + (" RETURNING id" if dialect == "postgresql" else "")
                        ), {"c": course_id, "t": title or f"Assessment {seq}", "s": seq,
                            "m": max_score or 100.0, "now": datetime.utcnow()})
                        found = found.scalar() if dialect == "postgresql" else found.lastrowid
                        created += 1
                    cache[key] = found
                conn.execute(text("UPDATE assessment SET definition_id = :d WHERE id = :a"),
                             {"d": cache[key], "a": aid})
                linked += 1
            if linked:
                _log(f"linked {linked} mark(s) to definitions ({created} definition(s) created)")

            # duplicates: keep the newest row per (enrollment, definition)
            dupes = conn.execute(text(
                "DELETE FROM assessment WHERE id NOT IN ("
                "  SELECT keep_id FROM (SELECT MAX(id) AS keep_id FROM assessment "
                "  GROUP BY enrollment_id, definition_id) k)"
            )).rowcount
            if dupes:
                _log(f"removed {dupes} duplicate mark(s)")

            for col in ("title", "sequence", "max_score"):
                conn.execute(text(f"ALTER TABLE assessment DROP COLUMN {col}"))
            _log("dropped repeated columns assessment.title, sequence, max_score")
            if dialect == "postgresql":
                conn.execute(text("ALTER TABLE assessment ALTER COLUMN definition_id SET NOT NULL"))
                _log("assessment.definition_id is now required")

        # ---- 4. unique rules ---------------------------------------------------
        if "attendance_record" in tables and not _has_unique_on(conn, "attendance_record", ["enrollment_id", "week"]):
            n = conn.execute(text(
                "DELETE FROM attendance_record WHERE id NOT IN ("
                "  SELECT keep_id FROM (SELECT MAX(id) AS keep_id FROM attendance_record "
                "  GROUP BY enrollment_id, week) k)"
            )).rowcount
            if n:
                _log(f"removed {n} duplicate attendance record(s) (kept the most recent per week)")

        rules = [
            ("course", ["code"], "uq_course_code"),
            ("enrollment", ["student_id", "course_id"], "uq_enrollment_student_course"),
            ("attendance_record", ["enrollment_id", "week"], "uq_attendance_enrollment_week"),
            ("assessment", ["enrollment_id", "definition_id"], "uq_assessment_enrollment_definition"),
        ]
        for table, cols, name in rules:
            if table not in tables or _has_unique_on(conn, table, cols):
                continue
            col_list = ", ".join(cols)
            dup = conn.execute(text(
                f"SELECT COUNT(*) FROM (SELECT {col_list} FROM {table} GROUP BY {col_list} HAVING COUNT(*) > 1) d"
            )).scalar()
            if dup:
                _log(f"WARNING: {dup} duplicate value(s) in {table}({col_list}); unique rule NOT added. "
                     "Fix the duplicates by hand, then redeploy.")
                continue
            conn.execute(text(f"CREATE UNIQUE INDEX {name} ON {table} ({col_list})"))
            _log(f"added unique rule {name}")

        # ---- 5. enrolment status (withdraw without deleting history) -----------
        if "enrollment" in tables:
            cols = _columns(conn, "enrollment")
            ts = "TIMESTAMP" if dialect == "postgresql" else "DATETIME"
            added = []
            if "status" not in cols:
                conn.execute(text("ALTER TABLE enrollment ADD COLUMN status VARCHAR(12) NOT NULL DEFAULT 'active'"))
                added.append("status")
            if "withdrawn_at" not in cols:
                conn.execute(text(f"ALTER TABLE enrollment ADD COLUMN withdrawn_at {ts}"))
                added.append("withdrawn_at")
            if "withdrawn_reason" not in cols:
                conn.execute(text("ALTER TABLE enrollment ADD COLUMN withdrawn_reason VARCHAR(200)"))
                added.append("withdrawn_reason")
            if "withdrawn_by_id" not in cols:
                conn.execute(text('ALTER TABLE enrollment ADD COLUMN withdrawn_by_id INTEGER REFERENCES "user"(id)'))
                added.append("withdrawn_by_id")
            if added:
                _log("enrollment: added " + ", ".join(added))
