import base64
import csv
import io
import math
import os
import re
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, redirect, url_for, request, flash, abort, Response, session
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from flask_wtf.csrf import CSRFProtect, CSRFError
from markupsafe import Markup, escape

from models import (
    db, User, Department, Course, Student, Enrollment,
    AttendanceRecord, Assessment, AssessmentDefinition, ASSESSMENT_TYPES,
    RiskScore, RiskSnapshot, Notification, InterventionNote, ActivityLog, INTERVENTION_STATUSES
)
from risk_engine import (
    recompute_all, BAND_HIGH, BAND_MEDIUM, model_agreement_summary, get_ml_explanation,
    compute_risk_for_enrollment, generate_notifications_for_enrollment,
)
import seed_data
import passwords
from migrations import run_migrations
from sqlalchemy import inspect
import ml_engine
import analytics
import reports
import deletion
import mailer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

# SECRET_KEY: must be set via environment variable in production (Render etc.).
# Falls back to a dev-only value locally so `python app.py` still works out of the box.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

# DATABASE_URL: Render/Railway/Heroku-style Postgres connection string in production.
# Falls back to a local SQLite file when not set, so local/offline use needs no setup.
database_url = os.environ.get("DATABASE_URL")
if database_url:
    # Older-style "postgres://" URLs (still handed out by some providers) aren't
    # accepted by modern SQLAlchemy, which requires the "postgresql://" scheme.
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'risk_system.db')}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# --- Session cookie security -------------------------------------------------
# HttpOnly stops page scripts reading the cookie; SameSite=Lax blocks it being sent
# on cross-site form posts; Secure (HTTPS only) is switched on whenever we run
# against a real database, i.e. in production behind Render's HTTPS.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
_secure_env = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower()
app.config["SESSION_COOKIE_SECURE"] = (_secure_env == "true") if _secure_env in ("true", "false") else bool(database_url)
# Sign users out after this many minutes of inactivity (the session cookie is
# refreshed on every request, so the clock restarts whenever the user does something).
SESSION_TIMEOUT_MINUTES = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "60"))
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=SESSION_TIMEOUT_MINUTES)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

# --- Password and login rules -------------------------------------------------
MIN_PASSWORD_LENGTH = passwords.MIN_LENGTH
APP_NAME = "UniAlert"
ROLE_LABELS = {"admin": "Administrator", "lecturer": "Lecturer", "hod": "Head of Department",
               "counsellor": "Counsellor", "student": "Student", "parent": "Parent / guardian"}
# Visual themes users can switch between (stored per browser). DEFAULT_THEME sets
# what a first-time visitor sees.
THEMES = {"academic": "Academic (navy and gold)", "lagoon": "Lagoon (light, teal)", "night": "Night (dark)"}
DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "academic")
# "Seed demo dataset" and "Reset database" are for local trials only. They are
# hidden and switched off whenever a real database is configured, unless
# ALLOW_DEMO_TOOLS=true is set on purpose.
DEMO_TOOLS = os.environ.get("ALLOW_DEMO_TOOLS", "false" if os.environ.get("DATABASE_URL") else "true").strip().lower() == "true"
if DEFAULT_THEME not in THEMES:
    DEFAULT_THEME = "academic"
MAX_FAILED_LOGINS = 5          # failed attempts before the account is locked...
LOCKOUT_MINUTES = 15           # ...for this long

db.init_app(app)

# --- CSRF protection ----------------------------------------------------------
# Every POST form must carry a hidden csrf_token field (see templates); requests
# without a valid token are rejected, so other websites cannot submit forms on a
# signed-in user's behalf.
csrf = CSRFProtect(app)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024   # largest upload (CSV import): 4 MB


def _error_page(code, title, message):
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(403)
def forbidden(e):
    return _error_page(403, "You don't have access to this page",
                       "This page belongs to a different role. If you think you should see it, ask your administrator.")


@app.errorhandler(404)
def not_found(e):
    return _error_page(404, "Page not found", "The link may be out of date, or the record may have been removed.")


@app.errorhandler(413)
def too_large(e):
    return _error_page(413, "File too large", "Uploads can be up to 4 MB. Split the file into smaller parts and try again.")


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    return _error_page(500, "Something went wrong", "Nothing was saved. Try again in a moment. If it keeps happening, tell your administrator what you were doing.")


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    flash("Your form expired or was not valid. Please try again.", "error")
    return redirect(request.referrer or url_for("index"))

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)


# Upgrade an existing production database before serving requests. Safe with
# several workers: run_migrations() takes a PostgreSQL advisory lock and every
# step is idempotent. (Locally, `python app.py` runs initialize_database() below.)
if database_url and os.environ.get("RUN_MIGRATIONS_ON_START", "true").strip().lower() == "true":
    with app.app_context():
        if inspect(db.engine).has_table("user"):
            run_migrations(db)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


ROLE_HOME = {
    "admin": "admin_dashboard",
    "lecturer": "lecturer_dashboard",
    "hod": "hod_dashboard",
    "counsellor": "counsellor_dashboard",
    "student": "student_dashboard",
    "parent": "parent_dashboard",
}


def role_required(*roles):
    def wrapper(fn):
        from functools import wraps

        @wraps(fn)
        def inner(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return inner
    return wrapper


def unread_count():
    if not current_user.is_authenticated:
        return 0
    return Notification.query.filter_by(recipient_id=current_user.id, read=False).count()


def local_now():
    # Server time is UTC; Fiji is UTC+12. LOCAL_UTC_OFFSET_HOURS lets another campus change it.
    return datetime.utcnow() + timedelta(hours=float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "12")))


def greeting():
    hour = local_now().hour
    return "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"


def pop_new_credentials():
    return session.pop("new_credentials", None)


ACTIVITY_LABELS = {
    "login": "Signed in", "login.failed": "Failed sign-in", "login.locked": "Account locked",
    "logout": "Signed out", "password.change": "Changed own password", "password.reset": "Password reset by admin",
    "account.create": "Account created", "account.update": "Account updated", "account.deactivate": "Account deactivated",
    "account.reactivate": "Account reactivated", "enrolment.add": "Enrolment added", "enrolment.withdraw": "Enrolment withdrawn",
    "enrolment.restore": "Enrolment restored", "enrolment.delete": "Enrolment deleted", "import.students": "Students imported",
    "attendance.save": "Attendance saved", "marks.save": "Marks saved", "risk.recompute": "Risk recomputed",
    "demo.seed": "Demo data loaded", "demo.reset": "Database reset",
    "account.delete": "Account deleted", "department.create": "Department created",
    "department.update": "Department renamed", "department.delete": "Department deleted",
    "course.create": "Course created", "course.update": "Course updated", "course.delete": "Course deleted",
    "bulk.delete": "Bulk delete", "log.clear": "Activity log cleared",
    "intervention.add": "Intervention logged", "intervention.update": "Intervention updated",
    "email.test": "Test email sent",
}


def log_activity(action, detail="", user=None):
    """Adds a line to the activity log. Committed with the caller's transaction,
    so a failed change leaves no log line. Never stores passwords."""
    who = user if user is not None else (current_user if current_user.is_authenticated else None)
    try:
        ip = (request.access_route[0] if request.access_route else request.remote_addr) or ""
    except RuntimeError:
        ip = ""
    db.session.add(ActivityLog(user_id=who.id if who is not None else None, action=action,
                               detail=(detail or "")[:300], ip_address=ip[:64]))


@app.context_processor
def inject_globals():
    return {"unread_count": unread_count, "now": local_now(),
            "pop_new_credentials": pop_new_credentials, "APP_NAME": APP_NAME,
            "role_labels": ROLE_LABELS, "themes": THEMES, "default_theme": DEFAULT_THEME,
            "greeting": greeting(), "demo_tools": DEMO_TOOLS}


# ---------------------------------------------------------------- Auth ----

@app.route("/", methods=["GET"])
def index():
    if current_user.is_authenticated:
        return redirect(url_for(ROLE_HOME[current_user.role]))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(ROLE_HOME[current_user.role]))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        now = datetime.utcnow()

        if user and user.locked_until and user.locked_until > now:
            minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
            flash(f"Too many failed sign-in attempts. Try again in {minutes} minute(s), "
                  "or ask your administrator to reset your password.", "error")
        elif user and user.check_password(password):
            if not user.active:
                flash("This account has been deactivated. Contact your administrator.", "error")
            else:
                user.failed_logins = 0
                user.locked_until = None
                log_activity("login", "", user=user)
                db.session.commit()
                session.permanent = True   # enables the inactivity timeout
                login_user(user)
                if user.must_change_password:
                    flash("Please choose a new password before continuing.", "info")
                    return redirect(url_for("change_password"))
                return redirect(url_for(ROLE_HOME[user.role]))
        else:
            if user:
                user.failed_logins = (user.failed_logins or 0) + 1
                if user.failed_logins >= MAX_FAILED_LOGINS:
                    user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                    user.failed_logins = 0
                    log_activity("login.locked", f"Locked for {LOCKOUT_MINUTES} minutes after {MAX_FAILED_LOGINS} failed attempts", user=user)
                else:
                    log_activity("login.failed", "Wrong password", user=user)
                db.session.commit()
            flash("Incorrect username or password.", "error")
    return render_template("login.html")


def password_problem(new_password, current_user_obj=None):
    """Returns an error message if the password breaks the rules, else None."""
    problem = passwords.strength_problem(
        new_password, current_user_obj.username if current_user_obj is not None else None)
    if problem:
        return problem
    if current_user_obj is not None and current_user_obj.check_password(new_password):
        return "Your new password must be different from your current one."
    return None


def admin_chosen_password(raw, username=None):
    """For admin-set passwords: blank -> a generated strong password; otherwise the
    typed one must pass the strength rules. Returns (password, error)."""
    raw = (raw or "").strip()
    if not raw:
        return passwords.generate_password(), None
    problem = passwords.strength_problem(raw, username)
    return (None, problem) if problem else (raw, None)


def share_credentials(full_name, username, password, role_label):
    """Queue sign-in details to be shown ONCE, in a panel with a copy button, on the
    next page the admin sees (see templates/_credentials.html). They are never
    stored anywhere else in plain text."""
    creds = session.get("new_credentials", [])
    creds.append({"name": full_name, "username": username, "password": password, "role": role_label})
    session["new_credentials"] = creds


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current):
            flash("Your current password is incorrect.", "error")
        elif new != confirm:
            flash("The new passwords do not match.", "error")
        elif password_problem(new, current_user):
            flash(password_problem(new, current_user), "error")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            log_activity("password.change")
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for(ROLE_HOME[current_user.role]))
    return render_template("change_password.html", min_length=MIN_PASSWORD_LENGTH,
                           forced=current_user.must_change_password)


@app.route("/account/profile", methods=["GET", "POST"])
@login_required
def my_profile():
    """Every user's own profile: non-sensitive details for their role, plus the
    one thing they may change themselves (their contact email)."""
    u = current_user
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address, for example name@example.edu.", "error")
        else:
            u.email = email or None
            db.session.commit()
            flash("Email address saved.", "success")
        return redirect(url_for("my_profile"))

    details, items, items_title = [], [], None
    if u.role == "student":
        st = Student.query.filter_by(user_id=u.id).first()
        if st:
            details += [("Student number", st.student_number), ("Department", st.department.name),
                        ("Year of study", st.year_of_study),
                        ("Counsellor", st.counsellor.full_name if st.counsellor else "Not assigned yet"),
                        ("Parent alerts", "Allowed for High risk" if st.parent_consent else "Not allowed")]
            items_title = "Enrolled courses"
            items = [f"{e.course.code} {e.course.name}" for e in st.enrollments]
    elif u.role == "lecturer":
        details.append(("Department", u.department.name if u.department else "Not set"))
        items_title = "Courses you teach"
        items = [f"{c.code} {c.name}" for c in Course.query.filter_by(lecturer_id=u.id).order_by(Course.code)]
    elif u.role == "hod":
        details.append(("Department", u.department.name if u.department else "Not set"))
        if u.department:
            details += [("Courses in department", Course.query.filter_by(department_id=u.department_id).count()),
                        ("Lecturers in department", User.query.filter_by(role="lecturer", department_id=u.department_id, active=True).count())]
    elif u.role == "counsellor":
        details.append(("Students on caseload", Student.query.filter_by(counsellor_id=u.id).count()))
    elif u.role == "parent":
        items_title = "Linked students"
        items = [s_.user.full_name for s_ in Student.query.filter_by(parent_id=u.id)]
    return render_template("profile.html", u=u, details=details, items=items, items_title=items_title)


@app.before_request
def enforce_password_change():
    """Users flagged must_change_password (new accounts, admin resets) can only
    reach the change-password page and sign-out until they pick their own."""
    if (current_user.is_authenticated and getattr(current_user, "must_change_password", False)
            and request.endpoint not in ("change_password", "logout", "static")):
        return redirect(url_for("change_password"))


def _admin_reset_password(user, back_url):
    """Shared by the student, lecturer and staff profile pages."""
    new_password, problem = admin_chosen_password(request.form.get("new_password"), user.username)
    if problem:
        flash(problem, "error")
        return redirect(back_url)
    user.set_password(new_password)
    user.must_change_password = True
    user.failed_logins = 0
    user.locked_until = None
    log_activity("password.reset", f"{user.username} ({ROLE_LABELS.get(user.role, user.role)})")
    db.session.commit()
    share_credentials(user.full_name, user.username, new_password, "New password")
    flash(f"Password reset for {user.full_name}. They will be asked to choose their own at next sign-in.", "success")
    return redirect(back_url)




@app.route("/logout")
@login_required
def logout():
    log_activity("logout")
    db.session.commit()
    logout_user()
    return redirect(url_for("login"))


# ------------------------------------------------------------- Helpers ----

def enrollment_row(enr: Enrollment):
    rs = enr.risk
    return {
        "enrollment": enr,
        "student": enr.student,
        "course": enr.course,
        "risk": rs,
    }


def band_class(band):
    return {"High": "risk-high", "Medium": "risk-medium", "Low": "risk-low"}.get(band, "risk-unknown")


app.jinja_env.filters["band_class"] = band_class


def local_offset():
    return timedelta(hours=float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "12")))


def to_local(dt):
    """Stored times are UTC; show them in Fiji time (BUG-10)."""
    return dt + local_offset() if dt else dt


app.jinja_env.filters["local"] = to_local

NO_DATA = "No data"


def risk_badge(rs, show_score=True):
    """The risk badge used everywhere. "No data" shows as Insufficient data
    (FR-10), and a score resting on little evidence gets a "limited data" tag."""
    if rs is None:
        return Markup('<span class="badge risk-unknown">Not scored</span>')
    if rs.risk_band == NO_DATA:
        return Markup('<span class="badge risk-unknown" title="No attendance or marked work recorded yet">'
                      'Insufficient data</span>')
    text = rs.risk_band + (f" · {rs.risk_score}" if show_score else "")
    html = f'<span class="badge {band_class(rs.risk_band)}">{escape(text)}</span>'
    if rs.data_quality == "limited":
        html += (' <span class="data-note" title="Fewer than 3 weeks of attendance, or no marked work yet. '
                 'Treat this score with care.">limited data</span>')
    return Markup(html)


def pct(rs, field):
    if rs is None or rs.risk_band == NO_DATA or getattr(rs, field) is None:
        return "—"
    return f"{getattr(rs, field)}%"


app.jinja_env.filters["risk_badge"] = risk_badge
app.jinja_env.filters["pct"] = pct


def unique_username(base):
    """Turn a full name into a login-friendly, unique username (firstname.lastname, .2, .3, ...)."""
    slug = re.sub(r"[^a-z0-9.]", "", base.lower().replace(" ", "."))
    slug = re.sub(r"\.+", ".", slug).strip(".") or "user"
    candidate = slug
    n = 2
    while User.query.filter_by(username=candidate).first():
        candidate = f"{slug}{n}"
        n += 1
    return candidate


def student_worst_risk(student: Student):
    risks = [e.risk for e in student.enrollments if e.risk]
    if not risks:
        return None
    scored = [r for r in risks if r.risk_band != NO_DATA]
    return max(scored or risks, key=lambda r: r.risk_score)


def student_number_allocator(extra_taken=()):
    """Returns a function giving the next free student ID, S<year><nnn>
    (e.g. S2026101), skipping IDs already used or about to be used (BUG-07)."""
    prefix = f"S{local_now().year}"
    taken = {n.lower() for (n,) in db.session.query(Student.student_number).all()}
    taken |= {n.lower() for n in extra_taken if n}
    highest = 0
    for n in taken:
        rest = n[len(prefix):]
        if n.startswith(prefix.lower()) and rest.isdigit():
            highest = max(highest, int(rest))
    state = {"next": highest + 1}

    def allocate():
        while f"{prefix}{state['next']:03d}".lower() in taken:
            state["next"] += 1
        number = f"{prefix}{state['next']:03d}"
        taken.add(number.lower())
        state["next"] += 1
        return number
    return allocate


def owned_course_or_404(course_id):
    course = Course.query.get_or_404(course_id)
    if course.lecturer_id != current_user.id:
        abort(403)
    return course


# ------------------------------------------------------ Enrolment helpers -

WITHDRAW_REASONS = ["Never attended", "Withdrew from the course", "Transferred to another course", "Enrolled by mistake", "Other"]


def enroll_student(student, course):
    """Adds a student to a course, or restores a withdrawn enrolment (the unique
    rule allows one row per student and course). Scores risk straight away.
    Returns the enrolment. The caller commits."""
    enr = Enrollment.query.filter_by(student_id=student.id, course_id=course.id).first()
    if enr is None:
        enr = Enrollment(student_id=student.id, course_id=course.id, status="active")
        db.session.add(enr)
    else:
        enr.status = "active"
        enr.withdrawn_at = None
        enr.withdrawn_reason = None
        enr.withdrawn_by_id = None
    db.session.flush()
    # Marks rows for assessments the course already has.
    have = {a.definition_id for a in Assessment.query.filter_by(enrollment_id=enr.id).all()}
    for d in AssessmentDefinition.query.filter_by(course_id=course.id).all():
        if d.id not in have:
            db.session.add(Assessment(enrollment_id=enr.id, definition_id=d.id, score=None, submitted=False))
    db.session.flush()
    rs, prev_band = compute_risk_for_enrollment(enr.id)
    generate_notifications_for_enrollment(enr, rs, prev_band)
    return enr


def withdraw_enrollment(enr, reason):
    """Marks an enrolment withdrawn. Attendance and marks are kept; the risk
    score is removed so the student drops out of dashboards, alerts and
    reports for that course. The caller commits."""
    enr.status = "withdrawn"
    enr.withdrawn_at = datetime.utcnow()
    enr.withdrawn_reason = (reason or "Other")[:200]
    enr.withdrawn_by_id = current_user.id if current_user.is_authenticated else None
    RiskScore.query.filter_by(enrollment_id=enr.id).delete()


# ------------------------------------------------------ Student Management -

STUDENTS_PER_PAGE = 50


@app.route("/admin/students")
@login_required
@role_required("admin")
def admin_students_list():
    q = request.args.get("q", "").strip()
    dept_id = request.args.get("department_id", type=int)
    band = request.args.get("band", "")
    show_inactive = request.args.get("show_inactive") == "1"

    query = Student.query.join(User, Student.user_id == User.id)
    if dept_id:
        query = query.filter(Student.department_id == dept_id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.full_name.ilike(like), Student.student_number.ilike(like)))
    if not show_inactive:
        query = query.filter(User.active.is_(True))

    if band in ("High", "Medium", "Low"):
        # Worst band = band of the student's highest active risk score; filtered
        # in the database so paging stays fast with thousands of students.
        worst = (db.session.query(Enrollment.student_id.label("sid"),
                                  db.func.max(RiskScore.risk_score).label("worst"))
                 .join(RiskScore, RiskScore.enrollment_id == Enrollment.id)
                 .filter(Enrollment.status == "active", RiskScore.risk_band != NO_DATA)
                 .group_by(Enrollment.student_id).subquery())
        query = query.join(worst, worst.c.sid == Student.id)
        if band == "High":
            query = query.filter(worst.c.worst >= BAND_HIGH)
        elif band == "Medium":
            query = query.filter(worst.c.worst >= BAND_MEDIUM, worst.c.worst < BAND_HIGH)
        else:
            query = query.filter(worst.c.worst < BAND_MEDIUM)
    else:
        band = ""

    page = max(1, request.args.get("page", type=int) or 1)
    pagination = query.order_by(User.full_name, Student.id).paginate(page=page, per_page=STUDENTS_PER_PAGE, error_out=False)
    rows = [{"student": s, "worst_risk": student_worst_risk(s)} for s in pagination.items]

    departments = Department.query.order_by(Department.name).all()
    return render_template(
        "admin_students_list.html", rows=rows, departments=departments, pagination=pagination,
        q=q, dept_id=dept_id, band=band, show_inactive=show_inactive,
    )


@app.route("/admin/students/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_student_new():
    departments = Department.query.order_by(Department.name).all()
    counsellors = User.query.filter_by(role="counsellor", active=True).order_by(User.full_name).all()
    all_courses = Course.query.order_by(Course.department_id, Course.code).all()

    if not departments:
        flash("Create a department first before registering students.", "error")
        return redirect(url_for("admin_departments"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        student_number = request.form.get("student_number", "").strip()
        department_id = request.form.get("department_id", type=int)
        year_of_study = request.form.get("year_of_study", type=int) or 1
        email = request.form.get("email", "").strip()
        counsellor_id = request.form.get("counsellor_id", type=int) or None
        password, password_error = admin_chosen_password(request.form.get("password"))

        parent_name = request.form.get("parent_name", "").strip()
        parent_email = request.form.get("parent_email", "").strip()
        parent_consent = request.form.get("parent_consent") == "on"

        errors = []
        generated_number = not student_number
        if generated_number:
            student_number = student_number_allocator()()
        if not full_name:
            errors.append("Full name is required.")
        if not generated_number and Student.query.filter(
                db.func.lower(Student.student_number) == student_number.lower()).first():
            errors.append(f"Student ID '{student_number}' is already in use.")
        if not department_id:
            errors.append("Department is required.")
        if password_error:
            errors.append(password_error)

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "admin_student_form.html", mode="new", departments=departments,
                counsellors=counsellors, all_courses=all_courses, form=request.form,
            )

        username = unique_username(full_name)
        s_user = User(username=username, full_name=full_name, role="student",
                      department_id=department_id, email=email or None, active=True)
        s_user.set_password(password)
        s_user.must_change_password = True
        db.session.add(s_user)
        db.session.flush()

        parent_user = None
        if parent_name:
            parent_username = unique_username(parent_name)
            parent_user = User(username=parent_username, full_name=parent_name, role="parent",
                                email=parent_email or None, active=True)
            parent_password = passwords.generate_password()
            parent_user.set_password(parent_password)
            parent_user.must_change_password = True
            db.session.add(parent_user)
            db.session.flush()

        student = Student(
            user_id=s_user.id,
            student_number=student_number,
            department_id=department_id,
            year_of_study=year_of_study,
            counsellor_id=counsellor_id,
            parent_id=parent_user.id if parent_user else None,
            parent_consent=parent_consent,
        )
        db.session.add(student)
        db.session.flush()

        enrolled_courses = []
        for cid in request.form.getlist("course_ids", type=int):
            course = Course.query.get(cid)
            if not course:
                continue
            enr = Enrollment(student_id=student.id, course_id=course.id)
            db.session.add(enr)
            db.session.flush()
            rs, prev_band = compute_risk_for_enrollment(enr.id)
            generate_notifications_for_enrollment(enr, rs, prev_band)
            enrolled_courses.append(course.code)

        log_activity("account.create", f"Student {username}")
        db.session.commit()

        share_credentials(full_name, username, password, "Student")
        if parent_user:
            share_credentials(parent_user.full_name, parent_user.username, parent_password, "Parent / guardian")
        msg = f"Registered {full_name} ({student_number})."
        if generated_number:
            msg += f" The student ID {student_number} was generated because the field was left blank."
        if enrolled_courses:
            msg += f" Enrolled in: {', '.join(enrolled_courses)}."
        flash(msg, "success")
        return redirect(url_for("admin_student_profile", student_id=student.id))

    return render_template("admin_student_form.html", mode="new", departments=departments,
                            counsellors=counsellors, all_courses=all_courses, form={})


# ----------------------------------------------------- Bulk CSV import -----

IMPORT_COLUMNS = ["full_name", "student_number", "department", "year_of_study", "email",
                  "course_codes", "counsellor_username", "parent_name", "parent_email", "parent_consent"]
IMPORT_REQUIRED = ["full_name", "department"]   # a blank student_number gets the next free ID
IMPORT_MAX_ROWS = 2000
YES = {"yes", "y", "true", "1"}
NO = {"no", "n", "false", "0", ""}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _read_import_csv(raw_bytes):
    """Parses the upload. Returns (rows, errors); each row is a dict keyed by
    IMPORT_COLUMNS plus its line number in the file."""
    try:
        text_data = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text_data = raw_bytes.decode("cp1252")   # Excel on Windows
        except UnicodeDecodeError:
            return [], ["The file is not a readable CSV. Save it from Excel as 'CSV UTF-8' and try again."]
    reader = csv.DictReader(io.StringIO(text_data))
    if not reader.fieldnames:
        return [], ["The file is empty."]
    headers = {h: (h or "").strip().lower().replace(" ", "_") for h in reader.fieldnames}
    missing = [c for c in IMPORT_REQUIRED if c not in headers.values()]
    if missing:
        return [], [f"Missing column(s): {', '.join(missing)}. Download the template to see the expected headings."]
    rows = []
    for raw in reader:
        row = {c: "" for c in IMPORT_COLUMNS}
        for h, v in raw.items():
            key = headers.get(h) if h is not None else None
            if key in row:
                row[key] = (v or "").strip()
        if not any(row.values()):
            continue
        row["_line"] = reader.line_num
        rows.append(row)
        if len(rows) > IMPORT_MAX_ROWS:
            return [], [f"The file has more than {IMPORT_MAX_ROWS} students. Split it into smaller files."]
    if not rows:
        return [], ["The file has headings but no students."]
    return rows, []


def _validate_import(rows):
    """Checks every row against the database and against the rest of the file.
    Returns (plan, errors). Nothing is written here."""
    depts = {d.name.strip().lower(): d for d in Department.query.all()}
    courses = {c.code.strip().upper(): c for c in Course.query.all()}
    counsellors = {u.username.lower(): u for u in User.query.filter_by(role="counsellor", active=True).all()}
    existing_numbers = {n.lower() for (n,) in db.session.query(Student.student_number).all()}
    seen, plan, errors = {}, [], []
    for row in rows:
        line, problems = row["_line"], []
        number = row["student_number"]
        if not row["full_name"]:
            problems.append("full_name is empty")
        if not number:
            row["_generate"] = True
        elif number.lower() in existing_numbers:
            problems.append(f"student number {number} already exists")
        elif number.lower() in seen:
            problems.append(f"student number {number} is repeated (also on line {seen[number.lower()]})")
        else:
            seen[number.lower()] = line
        dept = depts.get(row["department"].lower())
        if not row["department"]:
            problems.append("department is empty")
        elif dept is None:
            problems.append(f"no department called '{row['department']}'")
        year = 1
        if row["year_of_study"]:
            try:
                year = int(row["year_of_study"])
                if not 1 <= year <= 8:
                    raise ValueError
            except ValueError:
                problems.append(f"year_of_study '{row['year_of_study']}' must be a whole number from 1 to 8")
        for field in ("email", "parent_email"):
            if row[field] and not EMAIL_RE.fullmatch(row[field]):
                problems.append(f"{field} '{row[field]}' is not a valid email address")
        course_list = []
        for code in [c.strip().upper() for c in re.split(r"[;|]", row["course_codes"]) if c.strip()]:
            if code not in courses:
                problems.append(f"no course with code {code}")
            elif courses[code] not in course_list:
                course_list.append(courses[code])
        counsellor = None
        if row["counsellor_username"]:
            counsellor = counsellors.get(row["counsellor_username"].lower())
            if counsellor is None:
                problems.append(f"no active counsellor with username '{row['counsellor_username']}'")
        consent = row["parent_consent"].lower()
        if consent not in YES | NO:
            problems.append(f"parent_consent '{row['parent_consent']}' must be yes or no")
        if consent in YES and not row["parent_name"]:
            problems.append("parent_consent is yes but parent_name is empty")
        if problems:
            errors.append({"line": line, "who": row["full_name"] or number or "(blank)", "problems": problems})
            continue
        plan.append({"row": row, "department": dept, "year": year, "courses": course_list,
                     "counsellor": counsellor, "consent": consent in YES})
    if not errors:
        allocate = student_number_allocator(extra_taken=list(seen))
        for item in plan:
            if item["row"].get("_generate"):
                item["row"]["student_number"] = allocate()
    return plan, errors


def _csv_data_uri(header, records):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(records)
    return "data:text/csv;charset=utf-8;base64," + base64.b64encode(buf.getvalue().encode("utf-8")).decode("ascii")


@app.route("/admin/students/import", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_students_import():
    context = {"columns": IMPORT_COLUMNS, "required": IMPORT_REQUIRED, "max_rows": IMPORT_MAX_ROWS,
               "departments": Department.query.order_by(Department.name).all()}
    if request.method == "GET":
        return render_template("admin_students_import.html", **context)

    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a CSV file to import.", "error")
        return render_template("admin_students_import.html", **context)
    if not upload.filename.lower().endswith(".csv"):
        flash("The file must be a .csv file. In Excel use File, Save As, CSV UTF-8.", "error")
        return render_template("admin_students_import.html", **context)

    rows, file_errors = _read_import_csv(upload.read())
    if file_errors:
        return render_template("admin_students_import.html", file_errors=file_errors, **context)
    plan, errors = _validate_import(rows)
    if errors:
        return render_template("admin_students_import.html", row_errors=errors, checked=len(rows), **context)

    credentials, enrolments = [], 0
    for item in plan:
        row = item["row"]
        password = passwords.generate_password()
        s_user = User(username=unique_username(row["full_name"]), full_name=row["full_name"], role="student",
                      department_id=item["department"].id, email=row["email"] or None, active=True,
                      must_change_password=True)
        s_user.set_password(password)
        db.session.add(s_user)
        db.session.flush()
        credentials.append([row["full_name"], row["student_number"], "Student", s_user.username, password])

        parent_user = None
        if row["parent_name"]:
            parent_password = passwords.generate_password()
            parent_user = User(username=unique_username(row["parent_name"]), full_name=row["parent_name"],
                               role="parent", email=row["parent_email"] or None, active=True,
                               must_change_password=True)
            parent_user.set_password(parent_password)
            db.session.add(parent_user)
            db.session.flush()
            credentials.append([row["parent_name"], row["student_number"], "Parent / guardian",
                                parent_user.username, parent_password])

        student = Student(user_id=s_user.id, student_number=row["student_number"],
                          department_id=item["department"].id, year_of_study=item["year"],
                          counsellor_id=item["counsellor"].id if item["counsellor"] else None,
                          parent_id=parent_user.id if parent_user else None, parent_consent=item["consent"])
        db.session.add(student)
        db.session.flush()
        for course in item["courses"]:
            enroll_student(student, course)
            enrolments += 1

    log_activity("import.students", f"{len(plan)} students, {enrolments} enrolments from {upload.filename[:80]}")
    db.session.commit()
    stamp = local_now().strftime("%Y%m%d-%H%M")
    return render_template(
        "admin_students_import.html", done=True, imported=len(plan), enrolments=enrolments,
        accounts=len(credentials), credentials=credentials[:10],
        credentials_uri=_csv_data_uri(["name", "student_number", "account", "username", "temporary_password"], credentials),
        credentials_filename=f"unialert-credentials-{stamp}.csv", **context)


@app.route("/admin/students/import/template.csv")
@login_required
@role_required("admin")
def admin_students_import_template():
    dept = Department.query.order_by(Department.name).first()
    course = Course.query.filter_by(department_id=dept.id).order_by(Course.code).first() if dept else None
    counsellor = User.query.filter_by(role="counsellor", active=True).first()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(IMPORT_COLUMNS)
    w.writerow(["Example Student", "S2026999", dept.name if dept else "Computing", "1", "example.student@example.edu",
                course.code if course else "CS101", counsellor.username if counsellor else "",
                "Example Parent", "parent@example.com", "yes"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=unialert-student-import-template.csv"})


@app.route("/admin/students/<int:student_id>")
@login_required
@role_required("admin")
def admin_student_profile(student_id):
    student = Student.query.get_or_404(student_id)
    rows = [enrollment_row(e) for e in student.enrollments]
    withdrawn = [e for e in student.all_enrollments if e.status != "active"]
    enrolled_course_ids = {e.course_id for e in student.all_enrollments}
    course_query = Course.query.filter_by(department_id=student.department_id)
    if enrolled_course_ids:
        course_query = course_query.filter(~Course.id.in_(enrolled_course_ids))
    available_courses = course_query.order_by(Course.code).all()
    notes = (
        InterventionNote.query.filter_by(student_id=student.id)
        .order_by(InterventionNote.created_at.desc()).all()
    )
    return render_template(
        "admin_student_profile.html", student=student, rows=rows, withdrawn=withdrawn,
        available_courses=available_courses, notes=notes,
        delete_blockers=deletion.student_blockers(student),
    )


@app.route("/admin/students/<int:student_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_student_delete(student_id):
    student = Student.query.get_or_404(student_id)
    blockers = deletion.student_blockers(student)
    if blockers:
        flash(f"{student.user.full_name} can't be deleted: " + " ".join(blockers) + " Deactivate the account instead.", "error")
        return redirect(url_for("admin_student_profile", student_id=student.id))
    name = student.user.full_name
    label = f"Student {student.student_number} ({name})"
    parent_removed = deletion.delete_student(student)
    if parent_removed:
        label += f" and parent account ({parent_removed})"
    log_activity("account.delete", label)
    db.session.commit()
    msg = f"Deleted {name}."
    if parent_removed:
        msg += f" Their parent account ({parent_removed}) had no other children and was removed too."
    flash(msg, "success")
    return redirect(url_for("admin_students_list"))


@app.route("/admin/students/<int:student_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_student_edit(student_id):
    student = Student.query.get_or_404(student_id)
    departments = Department.query.order_by(Department.name).all()
    counsellors = User.query.filter_by(role="counsellor", active=True).order_by(User.full_name).all()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Full name is required.", "error")
            return redirect(url_for("admin_student_edit", student_id=student.id))

        student.user.full_name = full_name
        student.user.email = request.form.get("email", "").strip() or None
        student.department_id = request.form.get("department_id", type=int) or student.department_id
        student.year_of_study = request.form.get("year_of_study", type=int) or student.year_of_study
        student.counsellor_id = request.form.get("counsellor_id", type=int) or None
        student.parent_consent = request.form.get("parent_consent") == "on"
        log_activity("account.update", f"Student {student.user.username}")
        db.session.commit()
        flash("Student details updated.", "success")
        return redirect(url_for("admin_student_profile", student_id=student.id))

    return render_template(
        "admin_student_form.html", mode="edit", student=student, departments=departments,
        counsellors=counsellors, form={},
    )


@app.route("/admin/students/<int:student_id>/toggle_active", methods=["POST"])
@login_required
@role_required("admin")
def admin_student_toggle_active(student_id):
    student = Student.query.get_or_404(student_id)
    student.user.active = not student.user.active
    log_activity("account.reactivate" if student.user.active else "account.deactivate", f"Student {student.user.username}")
    db.session.commit()
    flash(f"{student.user.full_name} is now {'active' if student.user.active else 'deactivated'}.", "success")
    return redirect(url_for("admin_student_profile", student_id=student.id))


@app.route("/admin/students/<int:student_id>/reset_password", methods=["POST"])
@login_required
@role_required("admin")
def admin_student_reset_password(student_id):
    student = Student.query.get_or_404(student_id)
    return _admin_reset_password(student.user, url_for("admin_student_profile", student_id=student.id))


@app.route("/admin/students/<int:student_id>/enroll", methods=["POST"])
@login_required
@role_required("admin")
def admin_student_enroll(student_id):
    student = Student.query.get_or_404(student_id)
    course_id = request.form.get("course_id", type=int)
    course = Course.query.get_or_404(course_id)

    existing = Enrollment.query.filter_by(student_id=student.id, course_id=course.id).first()
    if existing and existing.status == "active":
        flash(f"{student.user.full_name} is already enrolled in {course.code}.", "error")
        return redirect(url_for("admin_student_profile", student_id=student.id))

    enroll_student(student, course)
    log_activity("enrolment.add", f"{student.student_number} added to {course.code}")
    db.session.commit()

    flash(f"Enrolled {student.user.full_name} in {course.code} — {course.name}.", "success")
    return redirect(url_for("admin_student_profile", student_id=student.id))


@app.route("/admin/students/<int:student_id>/unenroll/<int:enrollment_id>", methods=["POST"])
@login_required
@role_required("admin")
def admin_student_unenroll(student_id, enrollment_id):
    student = Student.query.get_or_404(student_id)
    enr = Enrollment.query.get_or_404(enrollment_id)
    if enr.student_id != student.id:
        abort(404)

    course_code = enr.course.code
    if request.form.get("mode") != "delete":
        withdraw_enrollment(enr, request.form.get("reason") or "Withdrawn by administrator")
        log_activity("enrolment.withdraw", f"{student.student_number} withdrawn from {course_code} by admin")
        db.session.commit()
        flash(f"Withdrew {student.user.full_name} from {course_code}. Their attendance and marks are kept.", "success")
        return redirect(url_for("admin_student_profile", student_id=student.id))

    log_activity("enrolment.delete", f"{student.student_number} enrolment in {course_code} deleted with its records")
    RiskSnapshot.query.filter_by(enrollment_id=enr.id).delete()
    AttendanceRecord.query.filter_by(enrollment_id=enr.id).delete()
    Assessment.query.filter_by(enrollment_id=enr.id).delete()
    RiskScore.query.filter_by(enrollment_id=enr.id).delete()
    db.session.delete(enr)
    db.session.commit()

    flash(f"Unenrolled {student.user.full_name} from {course_code}.", "success")
    return redirect(url_for("admin_student_profile", student_id=student.id))


# -------------------------------------------------- Department & Course ---

@app.route("/admin/departments", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_departments():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Department name is required.", "error")
        elif Department.query.filter_by(name=name).first():
            flash(f"A department named '{name}' already exists.", "error")
        else:
            db.session.add(Department(name=name))
            log_activity("department.create", name)
            db.session.commit()
            flash(f"Created department: {name}.", "success")
        return redirect(url_for("admin_departments"))

    departments = Department.query.order_by(Department.name).all()
    rows = [{"department": d, "course_count": Course.query.filter_by(department_id=d.id).count(),
             "student_count": Student.query.filter_by(department_id=d.id).count(),
             "staff_count": deletion.department_staff_count(d),
             "blockers": deletion.department_blockers(d)} for d in departments]
    return render_template("admin_departments.html", rows=rows)


@app.route("/admin/departments/<int:department_id>/rename", methods=["POST"])
@login_required
@role_required("admin")
def admin_department_rename(department_id):
    dept = Department.query.get_or_404(department_id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Department name is required.", "error")
    elif name == dept.name:
        flash("The name is unchanged.", "info")
    elif Department.query.filter(db.func.lower(Department.name) == name.lower(), Department.id != dept.id).first():
        flash(f"A department named '{name}' already exists.", "error")
    else:
        old = dept.name
        dept.name = name
        log_activity("department.update", f"{old} renamed to {name}")
        db.session.commit()
        flash(f"Renamed {old} to {name}.", "success")
    return redirect(url_for("admin_departments"))


@app.route("/admin/departments/<int:department_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_department_delete(department_id):
    dept = Department.query.get_or_404(department_id)
    blockers = deletion.department_blockers(dept)
    if blockers:
        flash(f"{dept.name} can't be deleted: " + " ".join(blockers), "error")
        return redirect(url_for("admin_departments"))
    name = dept.name
    deletion.delete_department(dept)
    log_activity("department.delete", name)
    db.session.commit()
    flash(f"Deleted the department {name}.", "success")
    return redirect(url_for("admin_departments"))


@app.route("/admin/courses")
@login_required
@role_required("admin")
def admin_courses_list():
    dept_id = request.args.get("department_id", type=int)
    query = Course.query
    if dept_id:
        query = query.filter_by(department_id=dept_id)
    courses = query.order_by(Course.department_id, Course.code).all()
    departments = Department.query.order_by(Department.name).all()
    return render_template("admin_courses_list.html", courses=courses, departments=departments, dept_id=dept_id)


@app.route("/admin/courses/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_course_new():
    departments = Department.query.order_by(Department.name).all()
    lecturers = User.query.filter_by(role="lecturer", active=True).order_by(User.full_name).all()

    if not departments:
        flash("Create a department first before adding courses.", "error")
        return redirect(url_for("admin_departments"))

    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        department_id = request.form.get("department_id", type=int)
        lecturer_id = request.form.get("lecturer_id", type=int) or None

        if not code or not name or not department_id:
            flash("Course code, name, and department are all required.", "error")
            return render_template("admin_course_form.html", departments=departments, lecturers=lecturers, form=request.form)
        if Course.query.filter(db.func.upper(Course.code) == code).first():
            flash(f"Course code {code} is already in use.", "error")
            return render_template("admin_course_form.html", departments=departments, lecturers=lecturers, form=request.form)

        db.session.add(Course(code=code, name=name, department_id=department_id, lecturer_id=lecturer_id))
        log_activity("course.create", f"{code} — {name}")
        db.session.commit()
        flash(f"Created course {code} — {name}.", "success")
        return redirect(url_for("admin_courses_list"))

    return render_template("admin_course_form.html", departments=departments, lecturers=lecturers, form={})


@app.route("/admin/courses/<int:course_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_course_edit(course_id):
    course = Course.query.get_or_404(course_id)
    departments = Department.query.order_by(Department.name).all()
    lecturers = User.query.filter_by(role="lecturer", active=True).order_by(User.full_name).all()

    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        if not code or not name:
            flash("Course code and name are required.", "error")
            return redirect(url_for("admin_course_edit", course_id=course.id))
        if Course.query.filter(db.func.upper(Course.code) == code, Course.id != course.id).first():
            flash(f"Course code {code} is already in use.", "error")
            return redirect(url_for("admin_course_edit", course_id=course.id))
        course.code = code
        course.name = name
        course.department_id = request.form.get("department_id", type=int) or course.department_id
        course.lecturer_id = request.form.get("lecturer_id", type=int) or None
        log_activity("course.update", f"{code} — {name}")
        db.session.commit()
        flash("Course updated.", "success")
        return redirect(url_for("admin_courses_list"))

    return render_template("admin_course_form.html", course=course, departments=departments, lecturers=lecturers,
                           form={}, delete_blockers=deletion.course_blockers(course))


@app.route("/admin/courses/<int:course_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_course_delete(course_id):
    course = Course.query.get_or_404(course_id)
    blockers = deletion.course_blockers(course)
    if blockers:
        flash(f"{course.code} can't be deleted: " + " ".join(blockers), "error")
        return redirect(url_for("admin_course_edit", course_id=course.id))
    label = f"{course.code} — {course.name}"
    deletion.delete_course(course)
    log_activity("course.delete", label)
    db.session.commit()
    flash(f"Deleted the course {label}.", "success")
    return redirect(url_for("admin_courses_list"))


# ----------------------------------------------------- Lecturer Management -

@app.route("/admin/lecturers")
@login_required
@role_required("admin")
def admin_lecturers_list():
    q = request.args.get("q", "").strip()
    dept_id = request.args.get("department_id", type=int)
    show_inactive = request.args.get("show_inactive") == "1"

    query = User.query.filter_by(role="lecturer")
    if dept_id:
        query = query.filter(User.department_id == dept_id)
    if q:
        query = query.filter(User.full_name.ilike(f"%{q}%"))
    if not show_inactive:
        query = query.filter(User.active.is_(True))

    lecturers = query.order_by(User.full_name).all()
    rows = [{"lecturer": l, "course_count": Course.query.filter_by(lecturer_id=l.id).count()} for l in lecturers]
    departments = Department.query.order_by(Department.name).all()
    return render_template(
        "admin_lecturers_list.html", rows=rows, departments=departments,
        q=q, dept_id=dept_id, show_inactive=show_inactive,
    )


@app.route("/admin/lecturers/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_lecturer_new():
    departments = Department.query.order_by(Department.name).all()

    if not departments:
        flash("Create a department first before registering lecturers.", "error")
        return redirect(url_for("admin_departments"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        department_id = request.form.get("department_id", type=int)
        password, password_error = admin_chosen_password(request.form.get("password"))

        if not full_name:
            flash("Full name is required.", "error")
            return render_template("admin_lecturer_form.html", mode="new", departments=departments, form=request.form)
        if password_error:
            flash(password_error, "error")
            return render_template("admin_lecturer_form.html", mode="new", departments=departments, form=request.form)
        if not department_id:
            flash("Department is required.", "error")
            return render_template("admin_lecturer_form.html", mode="new", departments=departments, form=request.form)

        username = unique_username(full_name)
        lecturer = User(username=username, full_name=full_name, role="lecturer",
                         department_id=department_id, email=email or None, active=True)
        lecturer.set_password(password)
        lecturer.must_change_password = True
        db.session.add(lecturer)
        log_activity("account.create", f"Lecturer {username}")
        db.session.commit()

        share_credentials(full_name, username, password, "Lecturer")
        flash(f"Registered {full_name}. They will be asked to choose their own password at first sign-in.", "success")
        return redirect(url_for("admin_lecturer_profile", lecturer_id=lecturer.id))

    return render_template("admin_lecturer_form.html", mode="new", departments=departments, form={})


@app.route("/admin/lecturers/<int:lecturer_id>")
@login_required
@role_required("admin")
def admin_lecturer_profile(lecturer_id):
    lecturer = User.query.filter_by(id=lecturer_id, role="lecturer").first_or_404()
    assigned_courses = Course.query.filter_by(lecturer_id=lecturer.id).order_by(Course.code).all()
    available_courses = (
        Course.query.filter(Course.department_id == lecturer.department_id, Course.lecturer_id != lecturer.id)
        .order_by(Course.code).all()
        if lecturer.department_id else []
    )
    return render_template(
        "admin_lecturer_profile.html", lecturer=lecturer,
        assigned_courses=assigned_courses, available_courses=available_courses,
        delete_blockers=deletion.staff_blockers(lecturer),
    )


@app.route("/admin/lecturers/<int:lecturer_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_lecturer_delete(lecturer_id):
    lecturer = User.query.filter_by(id=lecturer_id, role="lecturer").first_or_404()
    return _admin_delete_staff(lecturer, url_for("admin_lecturer_profile", lecturer_id=lecturer.id),
                               url_for("admin_lecturers_list"))


def _admin_delete_staff(user, back_url, done_url):
    blockers = deletion.staff_blockers(user)
    if blockers:
        flash(f"{user.full_name} can't be deleted: " + " ".join(blockers) + " Deactivate the account instead.", "error")
        return redirect(back_url)
    name, label = user.full_name, f"{ROLE_LABELS.get(user.role)} {user.username} ({user.full_name})"
    deletion.delete_staff(user)
    log_activity("account.delete", label)
    db.session.commit()
    flash(f"Deleted the account for {name}.", "success")
    return redirect(done_url)


@app.route("/admin/lecturers/<int:lecturer_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_lecturer_edit(lecturer_id):
    lecturer = User.query.filter_by(id=lecturer_id, role="lecturer").first_or_404()
    departments = Department.query.order_by(Department.name).all()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Full name is required.", "error")
            return redirect(url_for("admin_lecturer_edit", lecturer_id=lecturer.id))
        lecturer.full_name = full_name
        lecturer.email = request.form.get("email", "").strip() or None
        lecturer.department_id = request.form.get("department_id", type=int) or lecturer.department_id
        log_activity("account.update", f"Lecturer {lecturer.username}")
        db.session.commit()
        flash("Lecturer details updated.", "success")
        return redirect(url_for("admin_lecturer_profile", lecturer_id=lecturer.id))

    return render_template("admin_lecturer_form.html", mode="edit", lecturer=lecturer, departments=departments, form={})


@app.route("/admin/lecturers/<int:lecturer_id>/toggle_active", methods=["POST"])
@login_required
@role_required("admin")
def admin_lecturer_toggle_active(lecturer_id):
    lecturer = User.query.filter_by(id=lecturer_id, role="lecturer").first_or_404()
    lecturer.active = not lecturer.active
    log_activity("account.reactivate" if lecturer.active else "account.deactivate", f"Lecturer {lecturer.username}")
    db.session.commit()
    flash(f"{lecturer.full_name} is now {'active' if lecturer.active else 'deactivated'}.", "success")
    return redirect(url_for("admin_lecturer_profile", lecturer_id=lecturer.id))


@app.route("/admin/lecturers/<int:lecturer_id>/reset_password", methods=["POST"])
@login_required
@role_required("admin")
def admin_lecturer_reset_password(lecturer_id):
    lecturer = User.query.filter_by(id=lecturer_id, role="lecturer").first_or_404()
    return _admin_reset_password(lecturer, url_for("admin_lecturer_profile", lecturer_id=lecturer.id))


@app.route("/admin/lecturers/<int:lecturer_id>/assign_course", methods=["POST"])
@login_required
@role_required("admin")
def admin_lecturer_assign_course(lecturer_id):
    lecturer = User.query.filter_by(id=lecturer_id, role="lecturer").first_or_404()
    course_id = request.form.get("course_id", type=int)
    course = Course.query.get_or_404(course_id)
    previous = course.lecturer.full_name if course.lecturer else None
    course.lecturer_id = lecturer.id
    db.session.commit()
    if previous:
        flash(f"Reassigned {course.code} from {previous} to {lecturer.full_name}.", "success")
    else:
        flash(f"Assigned {course.code} to {lecturer.full_name}.", "success")
    return redirect(url_for("admin_lecturer_profile", lecturer_id=lecturer.id))


@app.route("/admin/lecturers/<int:lecturer_id>/unassign_course/<int:course_id>", methods=["POST"])
@login_required
@role_required("admin")
def admin_lecturer_unassign_course(lecturer_id, course_id):
    lecturer = User.query.filter_by(id=lecturer_id, role="lecturer").first_or_404()
    course = Course.query.get_or_404(course_id)
    if course.lecturer_id != lecturer.id:
        abort(404)
    course.lecturer_id = None
    db.session.commit()
    flash(f"Unassigned {lecturer.full_name} from {course.code}. It now has no lecturer.", "success")
    return redirect(url_for("admin_lecturer_profile", lecturer_id=lecturer.id))


# --------------------------------------------- Staff Accounts (HoD, Counsellor) -

STAFF_ROLES = {"hod": "Head of Department", "counsellor": "Counsellor"}


def staff_or_404(user_id):
    return User.query.filter(User.id == user_id, User.role.in_(STAFF_ROLES)).first_or_404()


def active_hod_for(department_id, exclude_id=None):
    q = User.query.filter_by(role="hod", department_id=department_id, active=True)
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    return q.first()


@app.route("/admin/staff")
@login_required
@role_required("admin")
def admin_staff_list():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    show_inactive = request.args.get("show_inactive") == "1"

    query = User.query.filter(User.role.in_(STAFF_ROLES))
    if role in STAFF_ROLES:
        query = query.filter(User.role == role)
    if q:
        query = query.filter(User.full_name.ilike(f"%{q}%"))
    if not show_inactive:
        query = query.filter(User.active.is_(True))

    staff = query.order_by(User.role, User.full_name).all()
    rows = [{
        "user": u,
        "caseload": Student.query.filter_by(counsellor_id=u.id).count() if u.role == "counsellor" else None,
    } for u in staff]
    return render_template("admin_staff_list.html", rows=rows, q=q, role=role,
                           show_inactive=show_inactive, roles=STAFF_ROLES,
                           departments=Department.query.order_by(Department.name).all())


@app.route("/admin/staff/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_staff_new():
    departments = Department.query.order_by(Department.name).all()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        role = request.form.get("role", "")
        department_id = request.form.get("department_id", type=int) or None
        password, password_error = admin_chosen_password(request.form.get("password"))

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if role not in STAFF_ROLES:
            errors.append("Choose a role: Head of Department or Counsellor.")
        if role == "hod":
            if not department_id:
                errors.append("A Head of Department must belong to a department.")
            elif active_hod_for(department_id):
                errors.append(f"{active_hod_for(department_id).full_name} is already the active Head of "
                              "this department. Deactivate them first, or choose another department.")
        if password_error:
            errors.append(password_error)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_staff_form.html", mode="new", departments=departments,
                                   roles=STAFF_ROLES, form=request.form)

        username = unique_username(full_name)
        user = User(username=username, full_name=full_name, role=role,
                    department_id=department_id, email=email or None, active=True)
        user.set_password(password)
        user.must_change_password = True
        db.session.add(user)
        log_activity("account.create", f"{STAFF_ROLES[role]} {username}")
        db.session.commit()
        share_credentials(full_name, username, password, STAFF_ROLES[role])
        flash(f"Registered {full_name} as {STAFF_ROLES[role]}. They will be asked to choose their "
              "own password at first sign-in.", "success")
        return redirect(url_for("admin_staff_profile", user_id=user.id))

    return render_template("admin_staff_form.html", mode="new", departments=departments,
                           roles=STAFF_ROLES, form={})


@app.route("/admin/staff/<int:user_id>")
@login_required
@role_required("admin")
def admin_staff_profile(user_id):
    user = staff_or_404(user_id)
    caseload = []
    if user.role == "counsellor":
        caseload = (Student.query.filter_by(counsellor_id=user.id)
                    .join(User, Student.user_id == User.id).order_by(User.full_name).all())
    return render_template("admin_staff_profile.html", staff=user, roles=STAFF_ROLES,
                           caseload=caseload, worst=student_worst_risk,
                           delete_blockers=deletion.staff_blockers(user))


@app.route("/admin/staff/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_staff_delete(user_id):
    user = staff_or_404(user_id)
    return _admin_delete_staff(user, url_for("admin_staff_profile", user_id=user.id), url_for("admin_staff_list"))


@app.route("/admin/staff/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_staff_edit(user_id):
    user = staff_or_404(user_id)
    departments = Department.query.order_by(Department.name).all()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        department_id = request.form.get("department_id", type=int) or None
        if not full_name:
            flash("Full name is required.", "error")
            return redirect(url_for("admin_staff_edit", user_id=user.id))
        if user.role == "hod":
            if not department_id:
                flash("A Head of Department must belong to a department.", "error")
                return redirect(url_for("admin_staff_edit", user_id=user.id))
            other = active_hod_for(department_id, exclude_id=user.id)
            if other and user.active:
                flash(f"{other.full_name} is already the active Head of that department.", "error")
                return redirect(url_for("admin_staff_edit", user_id=user.id))
        user.full_name = full_name
        user.email = request.form.get("email", "").strip() or None
        user.department_id = department_id
        log_activity("account.update", f"{ROLE_LABELS.get(user.role)} {user.username}")
        db.session.commit()
        flash("Staff details updated.", "success")
        return redirect(url_for("admin_staff_profile", user_id=user.id))

    return render_template("admin_staff_form.html", mode="edit", staff=user, departments=departments,
                           roles=STAFF_ROLES, form={})


@app.route("/admin/staff/<int:user_id>/toggle_active", methods=["POST"])
@login_required
@role_required("admin")
def admin_staff_toggle_active(user_id):
    user = staff_or_404(user_id)
    if not user.active and user.role == "hod" and active_hod_for(user.department_id, exclude_id=user.id):
        flash("Another active Head of Department already exists for this department. "
              "Deactivate them first.", "error")
        return redirect(url_for("admin_staff_profile", user_id=user.id))
    user.active = not user.active
    log_activity("account.reactivate" if user.active else "account.deactivate", f"{ROLE_LABELS.get(user.role)} {user.username}")
    db.session.commit()
    msg = f"{user.full_name} is now {'active' if user.active else 'deactivated'}."
    if not user.active and user.role == "counsellor":
        n = Student.query.filter_by(counsellor_id=user.id).count()
        if n:
            msg += f" {n} student(s) are still assigned to them — reassign them from each student's Edit page."
    flash(msg, "success")
    return redirect(url_for("admin_staff_profile", user_id=user.id))


@app.route("/admin/staff/<int:user_id>/reset_password", methods=["POST"])
@login_required
@role_required("admin")
def admin_staff_reset_password(user_id):
    user = staff_or_404(user_id)
    return _admin_reset_password(user, url_for("admin_staff_profile", user_id=user.id))


# -------------------------------------------------------- Admin routes ----

@app.route("/admin")
@login_required
@role_required("admin")
def admin_dashboard():
    total_students = Student.query.count()
    total_courses = Course.query.count()
    scored = RiskScore.query.count()
    high = RiskScore.query.filter_by(risk_band="High").count()
    medium = RiskScore.query.filter_by(risk_band="Medium").count()
    low = RiskScore.query.filter_by(risk_band="Low").count()
    notif_count = Notification.query.count()

    departments = Department.query.order_by(Department.name).all()
    dept_labels, dept_high, dept_medium = [], [], []
    for d in departments:
        scores = [enr.risk for c in d.courses for enr in c.enrollments if enr.risk]
        counts = analytics.band_counts(scores)
        dept_labels.append(d.name)
        dept_high.append(counts["High"])
        dept_medium.append(counts["Medium"])

    return render_template(
        "admin_dashboard.html",
        total_students=total_students, total_courses=total_courses,
        scored=scored, high=high, medium=medium, low=low, notif_count=notif_count,
        dept_labels=dept_labels, dept_high=dept_high, dept_medium=dept_medium,
        email=email_status(),
    )


def email_status():
    roles = sorted(mailer.alert_roles())
    staff = User.query.filter(User.role.in_(roles), User.active.is_(True)).all() if roles else []
    names = {"lecturer": "lecturers", "counsellor": "counsellors", "hod": "heads of department"}
    roles_text = " and ".join(names.get(r, r + "s") for r in roles)
    return {"enabled": mailer.enabled(), "from": mailer.mail_from(), "roles": roles, "roles_text": roles_text,
            "missing": [u for u in staff if not u.email], "staff_total": len(staff),
            "status": mailer.status, "admin_email": current_user.email}


@app.route("/admin/email/test", methods=["POST"])
@login_required
@role_required("admin")
def admin_email_test():
    if not mailer.enabled():
        flash("Email is not set up yet. Add RESEND_API_KEY and MAIL_FROM in Render, then redeploy.", "error")
    elif not current_user.email:
        flash("Add your own email address first (account menu → My profile), then send the test.", "error")
    else:
        ok, detail = mailer.send_test(current_user.email, current_user.first_name)
        log_activity("email.test", f"to {current_user.email}: {'sent' if ok else 'failed'}")
        db.session.commit()
        if ok:
            flash(f"Test email sent to {current_user.email}. Check the inbox (and the spam folder).", "success")
        else:
            flash(f"The test email could not be sent: {detail}", "error")
    return redirect(url_for("admin_dashboard"))


# ------------------------------------------------- Auto-refresh (FR-12) -----

AUTO_REFRESH_ENDPOINTS = {"admin_dashboard", "lecturer_dashboard", "hod_dashboard", "counsellor_dashboard",
                          "student_dashboard", "parent_dashboard", "all_notifications"}
AUTO_REFRESH_SECONDS = int(os.environ.get("AUTO_REFRESH_SECONDS", "60"))


def refresh_token():
    """Changes whenever something this user's dashboard shows has changed: a new
    alert for them, a recalculated risk score, or a new intervention note."""
    notif = db.session.query(db.func.max(Notification.id)).filter(Notification.recipient_id == current_user.id).scalar()
    q = db.session.query(db.func.max(RiskScore.last_computed), db.func.count(RiskScore.id))
    if current_user.role in ("student", "parent"):
        ids = ([s.id for s in Student.query.filter_by(user_id=current_user.id).all()] if current_user.role == "student"
               else [s.id for s in Student.query.filter_by(parent_id=current_user.id).all()])
        q = q.join(Enrollment, RiskScore.enrollment_id == Enrollment.id).filter(Enrollment.student_id.in_(ids or [0]))
    latest, n = q.one()
    notes = db.session.query(db.func.max(InterventionNote.id)).scalar()
    return f"{notif or 0}-{latest.isoformat() if latest else 0}-{n}-{notes or 0}"


@app.route("/api/refresh-state")
@login_required
def refresh_state():
    return {"token": refresh_token()}


@app.context_processor
def inject_refresh():
    if current_user.is_authenticated and request.endpoint in AUTO_REFRESH_ENDPOINTS and AUTO_REFRESH_SECONDS > 0:
        return {"auto_refresh": {"seconds": AUTO_REFRESH_SECONDS, "token": refresh_token()}}
    return {"auto_refresh": None}


@app.route("/admin/seed", methods=["POST"])
@login_required
@role_required("admin")
def admin_seed():
    if not DEMO_TOOLS:
        abort(404)
    seed_data.seed()
    log_activity("demo.seed")
    db.session.commit()
    flash("Synthetic demo dataset seeded (or already present).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/recompute", methods=["POST"])
@login_required
@role_required("admin")
def admin_recompute():
    summary = recompute_all()
    log_activity("risk.recompute", f"{summary['total']} enrolments")
    db.session.commit()
    flash(
        f"Recomputed {summary['total']} enrollments — "
        f"{summary['high']} High, {summary['medium']} Medium, {summary['low']} Low. "
        f"{summary['notifications']} new notifications sent.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reset", methods=["POST"])
@login_required
@role_required("admin")
def admin_reset():
    if not DEMO_TOOLS:
        abort(404)
    db.session.remove()
    db.drop_all()
    db.create_all()
    seed_data.seed()
    db.session.commit()
    recompute_all()
    logout_user()
    flash("Database reset to a fresh demo dataset. Sign in again.", "success")
    return redirect(url_for("login"))


@app.route("/admin/activity")
@login_required
@role_required("admin")
def admin_activity():
    action = request.args.get("action", "")
    who = request.args.get("who", "").strip()
    query = ActivityLog.query.outerjoin(User, ActivityLog.user_id == User.id)
    if action in ACTIVITY_LABELS:
        query = query.filter(ActivityLog.action == action)
    elif action.endswith(".*"):
        query = query.filter(ActivityLog.action.like(action[:-1] + "%"))
    if who:
        like = f"%{who}%"
        query = query.filter(db.or_(User.username.ilike(like), User.full_name.ilike(like)))
    page = max(1, request.args.get("page", type=int) or 1)
    pagination = query.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).paginate(
        page=page, per_page=50, error_out=False)
    offset = timedelta(hours=float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "12")))
    return render_template("admin_activity.html", pagination=pagination, entries=pagination.items,
                           labels=ACTIVITY_LABELS, action=action, who=who, offset=offset)


# ------------------------------------------------- Bulk delete (admin only) -

BULK_WORD = "DELETE"


def _bulk_confirmed(back_url):
    """Bulk deletes need the word DELETE typed and the admin's own password.
    Returns None when confirmed, otherwise a redirect back with a message."""
    if request.form.get("confirm_word", "").strip() != BULK_WORD:
        flash(f"Nothing was deleted. Type {BULK_WORD} in capitals to confirm.", "error")
        return redirect(back_url)
    if not current_user.check_password(request.form.get("admin_password", "")):
        flash("Nothing was deleted. Your password was not correct.", "error")
        return redirect(back_url)
    return None


def _bulk_scope():
    dept_id = request.form.get("department_id", type=int)
    dept = db.session.get(Department, dept_id) if dept_id else None
    return dept, (f"in {dept.name}" if dept else "in all departments")


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


@app.context_processor
def inject_bulk_helpers():
    def bulk_count(kind):
        if kind == "students":
            return Student.query.count()
        if kind == "courses":
            return Course.query.count()
        if kind == "lecturers":
            return User.query.filter_by(role="lecturer").count()
        if kind == "staff":
            return User.query.filter(User.role.in_(STAFF_ROLES)).count()
        if kind == "departments":
            return Department.query.count()
        if kind == "log":
            return ActivityLog.query.count()
        return 0
    return {"bulk_count": bulk_count, "bulk_word": BULK_WORD}


@app.route("/admin/students/bulk_delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_students_bulk_delete():
    back = url_for("admin_students_list")
    refused = _bulk_confirmed(back)
    if refused:
        return refused
    dept, where = _bulk_scope()
    q = Student.query.filter_by(department_id=dept.id) if dept else Student.query
    ids = [s.id for s in q.all()]
    if not ids:
        flash(f"There are no students {where} to delete.", "info")
        return redirect(back)
    logs = request.form.get("delete_logs") == "on"
    c = deletion.bulk_delete_students(ids, delete_logs=logs)
    msg = (f"Deleted {_plural(c['students'], 'student')} {where}, with {_plural(c['enrolments'], 'enrolment')}, "
           f"{_plural(c['notes'], 'intervention note')} and {_plural(c['parents'], 'parent account')}"
           + (f"; removed {_plural(c['log_entries'], 'activity-log entry', 'activity-log entries')}." if logs else "."))
    log_activity("bulk.delete", msg)
    db.session.commit()
    flash(msg, "success")
    return redirect(back)


@app.route("/admin/courses/bulk_delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_courses_bulk_delete():
    back = url_for("admin_courses_list")
    refused = _bulk_confirmed(back)
    if refused:
        return refused
    dept, where = _bulk_scope()
    q = Course.query.filter_by(department_id=dept.id) if dept else Course.query
    ids = [x.id for x in q.all()]
    if not ids:
        flash(f"There are no courses {where} to delete.", "info")
        return redirect(back)
    c = deletion.bulk_delete_courses(ids)
    msg = (f"Deleted {_plural(c['courses'], 'course')} {where}, with {_plural(c['enrolments'], 'enrolment')} "
           "and their attendance, marks and assessment items. Students were kept.")
    log_activity("bulk.delete", msg)
    db.session.commit()
    flash(msg, "success")
    return redirect(back)


def _bulk_delete_staff(roles, label, back):
    refused = _bulk_confirmed(back)
    if refused:
        return refused
    dept, where = _bulk_scope()
    q = User.query.filter(User.role.in_(roles))
    if dept:
        q = q.filter(User.department_id == dept.id)
    ids = [u.id for u in q.all()]
    if not ids:
        flash(f"There are no {label} {where} to delete.", "info")
        return redirect(back)
    logs = request.form.get("delete_logs") == "on"
    c = deletion.bulk_delete_staff(ids, delete_logs=logs)
    msg = (f"Deleted {_plural(c['accounts'], 'account')} ({label}) {where}. "
           f"{_plural(c['courses_unassigned'], 'course')} now unassigned, "
           f"{_plural(c['students_unassigned'], 'student')} without a counsellor, "
           f"{_plural(c['notes'], 'intervention note')} removed"
           + (f", {_plural(c['log_entries'], 'activity-log entry', 'activity-log entries')} removed." if logs else "."))
    log_activity("bulk.delete", msg)
    db.session.commit()
    flash(msg, "success")
    return redirect(back)


@app.route("/admin/lecturers/bulk_delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_lecturers_bulk_delete():
    return _bulk_delete_staff(["lecturer"], "lecturers", url_for("admin_lecturers_list"))


@app.route("/admin/staff/bulk_delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_staff_bulk_delete():
    role = request.form.get("role", "")
    roles = [role] if role in STAFF_ROLES else list(STAFF_ROLES)
    label = {"hod": "heads of department", "counsellor": "counsellors"}.get(role, "heads and counsellors")
    return _bulk_delete_staff(roles, label, url_for("admin_staff_list"))


@app.route("/admin/departments/bulk_delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_departments_bulk_delete():
    back = url_for("admin_departments")
    refused = _bulk_confirmed(back)
    if refused:
        return refused
    dept, where = _bulk_scope()
    ids = [dept.id] if dept else [d.id for d in Department.query.all()]
    if not ids:
        flash("There are no departments to delete.", "info")
        return redirect(back)
    logs = request.form.get("delete_logs") == "on"
    c = deletion.bulk_delete_departments(ids, delete_logs=logs)
    msg = (f"Deleted {_plural(c['departments'], 'department')} ({dept.name if dept else 'all'}) with "
           f"{_plural(c['courses'], 'course')}, {_plural(c['students'], 'student')}, "
           f"{_plural(c['parents'], 'parent account')}, {_plural(c['staff'], 'staff account')}, "
           f"{_plural(c['enrolments'], 'enrolment')} and {_plural(c['notes'], 'intervention note')}"
           + (f"; removed {_plural(c['log_entries'], 'activity-log entry', 'activity-log entries')}." if logs else "."))
    log_activity("bulk.delete", msg)
    db.session.commit()
    flash(msg, "success")
    return redirect(back)


@app.route("/admin/activity/clear", methods=["POST"])
@login_required
@role_required("admin")
def admin_activity_clear():
    back = url_for("admin_activity")
    refused = _bulk_confirmed(back)
    if refused:
        return refused
    offset = timedelta(hours=float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "12")))
    mode = request.form.get("mode", "range")
    start = end = None
    if mode != "all":
        try:
            d_from = request.form.get("date_from", "").strip()
            d_to = request.form.get("date_to", "").strip()
            if d_from:
                start = datetime.strptime(d_from, "%Y-%m-%d") - offset
            if d_to:
                end = datetime.strptime(d_to, "%Y-%m-%d") + timedelta(days=1) - offset
        except ValueError:
            flash("Nothing was deleted. Choose valid dates.", "error")
            return redirect(back)
        if start is None and end is None:
            flash("Nothing was deleted. Choose a start date, an end date, or both, or pick \"Everything\".", "error")
            return redirect(back)
        if start and end and start >= end:
            flash("Nothing was deleted. The start date must be on or before the end date.", "error")
            return redirect(back)
    n = deletion.clear_activity_log(start, end)
    if mode == "all":
        period = "all entries"
    else:
        period = ("from " + request.form.get("date_from") if start else "from the beginning") + \
                 (" to " + request.form.get("date_to") if end else " to now")
    msg = f"Cleared {_plural(n, 'activity-log entry', 'activity-log entries')} ({period}, Fiji time)."
    log_activity("log.clear", msg)
    db.session.commit()
    flash(msg, "success")
    return redirect(back)


@app.route("/admin/model")
@login_required
@role_required("admin")
def admin_model():
    metrics = ml_engine.get_metrics()
    agreement = model_agreement_summary()
    return render_template("admin_model.html", metrics=metrics, agreement=agreement)


@app.route("/admin/model/train", methods=["POST"])
@login_required
@role_required("admin")
def admin_model_train():
    n = int(request.form.get("n", 3000))
    ml_engine.train(n=n)
    summary = recompute_all()  # re-score every enrollment with the freshly trained model
    flash(
        f"Model retrained on {n} synthetic historical records and every enrollment was re-scored. "
        f"{summary['notifications']} new notifications generated from any rule-based band changes.",
        "success",
    )
    return redirect(url_for("admin_model"))


# ---------------------------------------------------------- Lecturer -----

@app.route("/lecturer")
@login_required
@role_required("lecturer")
def lecturer_dashboard():
    courses = Course.query.filter_by(lecturer_id=current_user.id).all()
    rows = []
    for c in courses:
        for enr in c.enrollments:
            rows.append(enrollment_row(enr))
    rows.sort(key=lambda r: (r["risk"].risk_score if r["risk"] else -1), reverse=True)
    notifications = (
        Notification.query.filter_by(recipient_id=current_user.id)
        .order_by(Notification.created_at.desc()).limit(15).all()
    )

    enrollment_ids = [r["enrollment"].id for r in rows]
    band_counts = analytics.band_counts([r["risk"] for r in rows])
    att_labels, att_values = analytics.weekly_attendance_series(enrollment_ids)
    score_labels, score_values = analytics.assessment_score_series(enrollment_ids)

    return render_template(
        "lecturer_dashboard.html", courses=courses, rows=rows, notifications=notifications,
        band_counts=band_counts, att_labels=att_labels, att_values=att_values,
        score_labels=score_labels, score_values=score_values,
    )


@app.route("/lecturer/course/<int:course_id>/attendance", methods=["GET", "POST"])
@login_required
@role_required("lecturer")
def lecturer_attendance(course_id):
    course = owned_course_or_404(course_id)
    enrollments = sorted(course.enrollments, key=lambda e: e.student.user.full_name)

    if request.method == "POST":
        week = request.form.get("week", type=int)
        if not week or week < 1:
            flash("Enter a valid week number.", "error")
            return redirect(url_for("lecturer_attendance", course_id=course.id))

        for enr in enrollments:
            present = request.form.get(f"present_{enr.id}") == "on"
            rec = AttendanceRecord.query.filter_by(enrollment_id=enr.id, week=week).first()
            if rec is None:
                db.session.add(AttendanceRecord(enrollment_id=enr.id, week=week, present=present))
            else:
                rec.present = present
        db.session.flush()

        notif_count = 0
        for enr in enrollments:
            rs, prev_band = compute_risk_for_enrollment(enr.id)
            notif_count += generate_notifications_for_enrollment(enr, rs, prev_band)
        log_activity("attendance.save", f"{course.code} week {week}, {len(enrollments)} students")
        db.session.commit()

        flash(f"Attendance saved for week {week} ({len(enrollments)} students). {notif_count} new alert(s) generated.", "success")
        return redirect(url_for("lecturer_attendance", course_id=course.id, week=week))

    week = request.args.get("week", type=int) or 1
    existing = {
        r.enrollment_id: r.present
        for r in AttendanceRecord.query.join(Enrollment)
        .filter(Enrollment.course_id == course.id, AttendanceRecord.week == week).all()
    }
    rows = [{"enrollment": enr, "student": enr.student, "present": existing.get(enr.id, True)} for enr in enrollments]
    recorded_weeks = sorted({
        r.week for r in AttendanceRecord.query.join(Enrollment).filter(Enrollment.course_id == course.id).all()
    })
    return render_template("lecturer_attendance.html", course=course, rows=rows, week=week, recorded_weeks=recorded_weeks)


@app.route("/lecturer/course/<int:course_id>/assessments")
@login_required
@role_required("lecturer")
def lecturer_assessments(course_id):
    course = owned_course_or_404(course_id)
    definitions = AssessmentDefinition.query.filter_by(course_id=course.id).order_by(AssessmentDefinition.sequence).all()
    rows = []
    for d in definitions:
        entries = (Assessment.query.join(Enrollment)
                   .filter(Assessment.definition_id == d.id, Enrollment.status == "active").all())
        graded = sum(1 for e in entries if e.submitted and e.score is not None)
        rows.append({"definition": d, "graded": graded, "total": len(course.enrollments)})
    return render_template("lecturer_assessments.html", course=course, rows=rows, has_legacy=False)


@app.route("/lecturer/course/<int:course_id>/assessments/new", methods=["GET", "POST"])
@login_required
@role_required("lecturer")
def lecturer_assessment_new(course_id):
    course = owned_course_or_404(course_id)
    suggested_seq = (
        db.session.query(db.func.max(AssessmentDefinition.sequence)).filter_by(course_id=course.id).scalar() or 0
    ) + 1

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        assessment_type = request.form.get("assessment_type", "Coursework")
        sequence = request.form.get("sequence", type=int) or suggested_seq
        max_score = request.form.get("max_score", type=float) or 100.0

        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("lecturer_assessment_new", course_id=course.id))
        if assessment_type not in ASSESSMENT_TYPES:
            assessment_type = "Coursework"

        definition = AssessmentDefinition(
            course_id=course.id, title=title, assessment_type=assessment_type,
            sequence=sequence, max_score=max_score,
        )
        db.session.add(definition)
        db.session.flush()

        for enr in course.enrollments:
            db.session.add(Assessment(
                enrollment_id=enr.id, definition_id=definition.id, score=None, submitted=False,
            ))
        db.session.commit()

        flash(f"Created '{title}' for {len(course.enrollments)} students. Enter scores now.", "success")
        return redirect(url_for("lecturer_assessment_grade", course_id=course.id, definition_id=definition.id))

    return render_template("lecturer_assessment_new.html", course=course, suggested_seq=suggested_seq, types=ASSESSMENT_TYPES)


@app.route("/lecturer/course/<int:course_id>/assessments/<int:definition_id>/grade", methods=["GET", "POST"])
@login_required
@role_required("lecturer")
def lecturer_assessment_grade(course_id, definition_id):
    course = owned_course_or_404(course_id)
    definition = AssessmentDefinition.query.get_or_404(definition_id)
    if definition.course_id != course.id:
        abort(404)

    # Backfill: any student enrolled after this assessment was created still needs a row.
    existing_enr_ids = {a.enrollment_id for a in Assessment.query.filter_by(definition_id=definition.id).all()}
    for enr in course.enrollments:
        if enr.id not in existing_enr_ids:
            db.session.add(Assessment(
                enrollment_id=enr.id, definition_id=definition.id, score=None, submitted=False,
            ))
    db.session.commit()

    entries = (Assessment.query.join(Enrollment)
               .filter(Assessment.definition_id == definition.id, Enrollment.status == "active").all())
    entries = sorted(entries, key=lambda a: a.enrollment.student.user.full_name)

    if request.method == "POST":
        affected, out_of_range = [], []
        top = definition.max_score
        for a in entries:
            raw = request.form.get(f"score_{a.id}", "").strip()
            if raw == "":
                a.score = None
                a.submitted = False
            else:
                try:
                    value = float(raw)
                except ValueError:
                    value = None
                if value is None or not math.isfinite(value):
                    flash(f"Ignored invalid score '{raw}' for {a.enrollment.student.user.full_name}.", "error")
                elif value < 0 or value > top:
                    out_of_range.append(f"{a.enrollment.student.user.full_name} ({raw})")
                    continue   # keep the previous mark; don't change it silently (BUG-08)
                else:
                    a.score = value
                    a.submitted = True
            affected.append(a.enrollment)
        if out_of_range:
            flash(f"Not saved: {len(out_of_range)} score(s) outside 0 to {top:g}: " + ", ".join(out_of_range)
                  + ". Their previous marks are unchanged. Correct them and save again.", "error")
        db.session.flush()

        notif_count = 0
        for enr in affected:
            rs, prev_band = compute_risk_for_enrollment(enr.id)
            notif_count += generate_notifications_for_enrollment(enr, rs, prev_band)
        log_activity("marks.save", f"{course.code} '{definition.title}'")
        db.session.commit()

        flash(f"Grades saved for '{definition.title}'. {notif_count} new alert(s) generated.", "success")
        return redirect(url_for("lecturer_assessment_grade", course_id=course.id, definition_id=definition.id))

    return render_template("lecturer_assessment_grade.html", course=course, definition=definition, entries=entries)


@app.route("/lecturer/course/<int:course_id>/roster")
@login_required
@role_required("lecturer")
def lecturer_roster(course_id):
    course = owned_course_or_404(course_id)
    everyone = sorted(course.all_enrollments, key=lambda e: e.student.user.full_name.lower())
    active = [e for e in everyone if e.status == "active"]
    withdrawn = [e for e in everyone if e.status != "active"]
    return render_template("lecturer_roster.html", course=course, active=active, withdrawn=withdrawn,
                           reasons=WITHDRAW_REASONS, rows={e.id: enrollment_row(e) for e in active})


@app.route("/lecturer/course/<int:course_id>/roster/add", methods=["POST"])
@login_required
@role_required("lecturer")
def lecturer_roster_add(course_id):
    course = owned_course_or_404(course_id)
    raw = request.form.get("student_numbers", "")
    numbers = [n.strip() for n in raw.replace(",", "\n").replace(";", "\n").split() if n.strip()]
    if not numbers:
        flash("Enter at least one student number.", "error")
        return redirect(url_for("lecturer_roster", course_id=course.id))
    added, restored, already, unknown = [], [], [], []
    for number in dict.fromkeys(numbers):  # keeps order, drops repeats
        student = Student.query.filter(db.func.lower(Student.student_number) == number.lower()).first()
        if student is None or not student.user.active:
            unknown.append(number)
            continue
        enr = Enrollment.query.filter_by(student_id=student.id, course_id=course.id).first()
        if enr and enr.status == "active":
            already.append(student.student_number)
            continue
        (restored if enr else added).append(student.student_number)
        enroll_student(student, course)
    if added or restored:
        log_activity("enrolment.add", f"{course.code}: added {', '.join(added) or '-'}; restored {', '.join(restored) or '-'}")
    db.session.commit()
    parts = []
    if added: parts.append(f"added {len(added)}")
    if restored: parts.append(f"restored {len(restored)}")
    if parts:
        flash(f"{course.code}: " + ", ".join(parts) + " student(s).", "success")
    if already:
        flash(f"Already in {course.code}: {', '.join(already)}.", "info")
    if unknown:
        flash(f"No active student found with number(s): {', '.join(unknown)}. "
              "Check the number, or ask an administrator to create the student record.", "error")
    return redirect(url_for("lecturer_roster", course_id=course.id))


@app.route("/lecturer/course/<int:course_id>/roster/<int:enrollment_id>/withdraw", methods=["POST"])
@login_required
@role_required("lecturer")
def lecturer_roster_withdraw(course_id, enrollment_id):
    course = owned_course_or_404(course_id)
    enr = Enrollment.query.get_or_404(enrollment_id)
    if enr.course_id != course.id:
        abort(404)
    reason = request.form.get("reason", "").strip()
    if reason not in WITHDRAW_REASONS:
        reason = "Other"
    note = request.form.get("note", "").strip()
    if note:
        reason = f"{reason}: {note}"
    if enr.status == "active":
        withdraw_enrollment(enr, reason)
        log_activity("enrolment.withdraw", f"{enr.student.student_number} withdrawn from {course.code} ({reason})")
        db.session.commit()
        flash(f"{enr.student.user.full_name} withdrawn from {course.code}. Their records are kept and you can restore them.", "success")
    return redirect(url_for("lecturer_roster", course_id=course.id))


@app.route("/lecturer/course/<int:course_id>/roster/<int:enrollment_id>/restore", methods=["POST"])
@login_required
@role_required("lecturer")
def lecturer_roster_restore(course_id, enrollment_id):
    course = owned_course_or_404(course_id)
    enr = Enrollment.query.get_or_404(enrollment_id)
    if enr.course_id != course.id:
        abort(404)
    if enr.status != "active":
        enroll_student(enr.student, course)
        log_activity("enrolment.restore", f"{enr.student.student_number} restored to {course.code}")
        db.session.commit()
        flash(f"{enr.student.user.full_name} is back on the {course.code} roster.", "success")
    return redirect(url_for("lecturer_roster", course_id=course.id))


# --------------------------------------------------------------- HoD -----

@app.route("/hod")
@login_required
@role_required("hod")
def hod_dashboard():
    dept = current_user.department
    courses = Course.query.filter_by(department_id=dept.id).all() if dept else []
    rows = []
    for c in courses:
        for enr in c.enrollments:
            rows.append(enrollment_row(enr))
    rows.sort(key=lambda r: (r["risk"].risk_score if r["risk"] else -1), reverse=True)

    high = sum(1 for r in rows if r["risk"] and r["risk"].risk_band == "High")
    medium = sum(1 for r in rows if r["risk"] and r["risk"].risk_band == "Medium")
    low = sum(1 for r in rows if r["risk"] and r["risk"].risk_band == "Low")

    lecturer_breakdown = {}
    for c in courses:
        lname = c.lecturer.full_name if c.lecturer else "Unassigned"
        h = sum(1 for enr in c.enrollments if enr.risk and enr.risk.risk_band == "High")
        m = sum(1 for enr in c.enrollments if enr.risk and enr.risk.risk_band == "Medium")
        lecturer_breakdown.setdefault(lname, {"high": 0, "medium": 0})
        lecturer_breakdown[lname]["high"] += h
        lecturer_breakdown[lname]["medium"] += m

    notifications = (
        Notification.query.filter_by(recipient_id=current_user.id)
        .order_by(Notification.created_at.desc()).limit(15).all()
    )
    lecturer_labels = list(lecturer_breakdown.keys())
    lecturer_high = [lecturer_breakdown[k]["high"] for k in lecturer_labels]
    lecturer_medium = [lecturer_breakdown[k]["medium"] for k in lecturer_labels]

    return render_template(
        "hod_dashboard.html", dept=dept, courses=courses, rows=rows,
        high=high, medium=medium, low=low,
        lecturer_breakdown=lecturer_breakdown, notifications=notifications,
        lecturer_labels=lecturer_labels, lecturer_high=lecturer_high, lecturer_medium=lecturer_medium,
    )


# --------------------------------------------------------- Counsellor ----

@app.route("/counsellor")
@login_required
@role_required("counsellor")
def counsellor_dashboard():
    students = Student.query.filter_by(counsellor_id=current_user.id).all()
    rows = []
    for s in students:
        # take the worst risk band across their enrollments
        risks = [enr.risk for enr in s.enrollments if enr.risk]
        if not risks:
            continue
        worst = max(risks, key=lambda r: r.risk_score)
        if worst.risk_band not in ("Medium", "High"):
            continue
        rows.append({"student": s, "risk": worst,
                     "n_flagged_courses": sum(1 for r in risks if r.risk_band in ("Medium", "High"))})
    rows.sort(key=lambda r: r["risk"].risk_score, reverse=True)
    notifications = (
        Notification.query.filter_by(recipient_id=current_user.id)
        .order_by(Notification.created_at.desc()).limit(15).all()
    )
    band_counts = analytics.band_counts([r["risk"] for r in rows])
    today = local_now().date()
    followups = (InterventionNote.query.join(Student, InterventionNote.student_id == Student.id)
                 .filter(Student.counsellor_id == current_user.id, InterventionNote.follow_up_date.isnot(None),
                         InterventionNote.status != "Resolved",
                         InterventionNote.follow_up_date <= today + timedelta(days=7))
                 .order_by(InterventionNote.follow_up_date).all())
    return render_template("counsellor_dashboard.html", rows=rows, notifications=notifications,
                           band_counts=band_counts, followups=followups, today=today)


@app.route("/counsellor/student/<int:student_id>", methods=["GET", "POST"])
@login_required
@role_required("counsellor")
def counsellor_student_detail(student_id):
    student = Student.query.get_or_404(student_id)
    if student.counsellor_id != current_user.id:
        abort(403)
    if request.method == "POST":
        text_note = request.form.get("note", "").strip()
        status, follow_up, error = _note_status_fields()
        if not text_note:
            error = error or "Write what was discussed or observed."
        if error:
            flash(error, "error")
            return redirect(url_for("counsellor_student_detail", student_id=student.id))
        note = InterventionNote(
            student_id=student.id,
            author_id=current_user.id,
            note=text_note,
            action_taken=request.form.get("action_taken", "").strip(),
            status=status, follow_up_date=follow_up,
        )
        db.session.add(note)
        log_activity("intervention.add", f"{student.student_number}: {status}"
                     + (f", follow up {follow_up:%d %b %Y}" if follow_up else ""))
        db.session.commit()
        flash("Intervention note logged.", "success")
        return redirect(url_for("counsellor_student_detail", student_id=student.id))

    rows = [enrollment_row(e) for e in student.enrollments]
    notes = (
        InterventionNote.query.filter_by(student_id=student.id)
        .order_by(InterventionNote.created_at.desc()).all()
    )
    explanations = {r["enrollment"].id: get_ml_explanation(r["risk"]) for r in rows}
    history = {}
    for r in rows:
        labels, values = analytics.risk_history_series(r["enrollment"].id)
        if len(labels) > 1:
            history[r["enrollment"].id] = {"labels": labels, "values": values}
    return render_template("student_detail.html", student=student, rows=rows, notes=notes, editable=True,
                           explanations=explanations, history=history, statuses=INTERVENTION_STATUSES,
                           today=local_now().date())


def _note_status_fields():
    """Reads status and follow-up date from a form. Returns (status, date, error)."""
    status = request.form.get("status", "Open")
    if status not in INTERVENTION_STATUSES:
        return None, None, "Choose a valid status."
    raw = request.form.get("follow_up_date", "").strip()
    follow_up = None
    if raw:
        try:
            follow_up = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None, None, "Choose a valid follow-up date."
        if follow_up < local_now().date() and status != "Resolved":
            return None, None, "The follow-up date can't be in the past."
    if status == "Resolved":
        follow_up = None
    return status, follow_up, None


@app.route("/counsellor/note/<int:note_id>/status", methods=["POST"])
@login_required
@role_required("counsellor")
def counsellor_note_status(note_id):
    note = InterventionNote.query.get_or_404(note_id)
    if note.student.counsellor_id != current_user.id:
        abort(403)
    status, follow_up, error = _note_status_fields()
    if error:
        flash(error, "error")
    else:
        note.status, note.follow_up_date = status, follow_up
        log_activity("intervention.update", f"{note.student.student_number}: {status}"
                     + (f", follow up {follow_up:%d %b %Y}" if follow_up else ""))
        db.session.commit()
        flash(f"Updated the note to {status}.", "success")
    return redirect(url_for("counsellor_student_detail", student_id=note.student_id))


# ------------------------------------------------------------ Student ----

@app.route("/student")
@login_required
@role_required("student")
def student_dashboard():
    student = Student.query.filter_by(user_id=current_user.id).first_or_404()
    rows = [enrollment_row(e) for e in student.enrollments]
    explanations = {r["enrollment"].id: get_ml_explanation(r["risk"]) for r in rows}
    notifications = (
        Notification.query.filter_by(recipient_id=current_user.id)
        .order_by(Notification.created_at.desc()).limit(15).all()
    )
    charts = {}
    for r in rows:
        enr_id = r["enrollment"].id
        att_labels, att_values = analytics.student_attendance_series(enr_id)
        score_labels, score_values = analytics.student_score_series(enr_id)
        hist_labels, hist_values = analytics.risk_history_series(enr_id)
        charts[enr_id] = {
            "att_labels": att_labels, "att_values": att_values,
            "score_labels": score_labels, "score_values": score_values,
            "hist_labels": hist_labels, "hist_values": hist_values,
        }
    return render_template(
        "student_dashboard.html", student=student, rows=rows, notifications=notifications,
        explanations=explanations, charts=charts,
    )


# ------------------------------------------------------------- Parent ----

@app.route("/parent")
@login_required
@role_required("parent")
def parent_dashboard():
    children = Student.query.filter_by(parent_id=current_user.id).all()
    data = []
    for child in children:
        rows = [enrollment_row(e) for e in child.enrollments]
        data.append({"student": child, "rows": rows})
    notifications = (
        Notification.query.filter_by(recipient_id=current_user.id)
        .order_by(Notification.created_at.desc()).limit(15).all()
    )
    return render_template("parent_dashboard.html", data=data, notifications=notifications)


# ------------------------------------------------------- Notifications ---

@app.route("/notifications/<int:notif_id>/read", methods=["POST"])
@login_required
def mark_notification_read(notif_id):
    n = Notification.query.get_or_404(notif_id)
    if n.recipient_id != current_user.id:
        abort(403)
    n.read = True
    db.session.commit()
    return redirect(request.referrer or url_for(ROLE_HOME[current_user.role]))


@app.route("/notifications")
@login_required
def all_notifications():
    notifications = (
        Notification.query.filter_by(recipient_id=current_user.id)
        .order_by(Notification.created_at.desc()).all()
    )
    return render_template("notifications.html", notifications=notifications)


# ----------------------------------------------------------- Reports -----

REPORT_DEFS = {
    "performance": {
        "title": "Student Performance Report",
        "fieldnames": ["student", "student_number", "course", "attendance_pct", "assessment_avg", "submission_rate", "risk_band"],
        "labels": ["Student", "ID", "Course", "Attendance %", "Assessment avg %", "Submission %", "Risk"],
        "description": "Attendance, assessment average, submission rate, and current risk band per enrollment.",
    },
    "risk": {
        "title": "Risk Report",
        "fieldnames": ["student", "student_number", "course", "risk_score", "risk_band", "ml_probability", "ml_band", "trend_penalty"],
        "labels": ["Student", "ID", "Course", "Risk score", "Risk band", "ML prob.", "ML band", "Trend"],
        "description": "Full risk breakdown for every scored enrollment, rule-based and ML side by side.",
    },
    "attendance": {
        "title": "Attendance Report",
        "fieldnames": ["student", "student_number", "course", "weeks_recorded", "weeks_present", "attendance_pct"],
        "labels": ["Student", "ID", "Course", "Weeks recorded", "Weeks present", "Attendance %"],
        "description": "Weeks recorded, weeks present, and overall attendance percentage per enrollment.",
    },
    "intervention": {
        "title": "Intervention Report",
        "fieldnames": ["student", "student_number", "date", "counsellor", "status", "follow_up", "action_taken", "note"],
        "labels": ["Student", "ID", "Date", "Counsellor", "Status", "Follow-up", "Action taken", "Note"],
        "description": "Logged counselling interventions for your caseload, with status and follow-up date. Confidential — counsellor access only.",
        "col_widths": [2.0, 1.2, 1.5, 1.9, 1.5, 1.3, 2.2, 2.4],
    },
}

ROLE_REPORT_TYPES = {
    "admin": ["performance", "risk", "attendance"],
    "hod": ["performance", "risk", "attendance"],
    "lecturer": ["performance", "risk", "attendance"],
    "counsellor": ["risk", "intervention"],
    "student": ["performance", "attendance"],
}


def scoped_enrollments_for_reports():
    """Returns (enrollments, scope_label) for the current user's role."""
    role = current_user.role
    if role == "admin":
        dept_id = request.args.get("department_id", type=int)
        q = Enrollment.query.join(Course).filter(Enrollment.status == "active")
        if dept_id:
            q = q.filter(Course.department_id == dept_id)
            dept = Department.query.get(dept_id)
            label = dept.name if dept else "All departments"
        else:
            label = "All departments"
        return q.all(), label
    if role == "hod":
        dept = current_user.department
        if not dept:
            return [], "No department assigned"
        return (Enrollment.query.join(Course)
                .filter(Course.department_id == dept.id, Enrollment.status == "active").all(), dept.name)
    if role == "lecturer":
        courses = Course.query.filter_by(lecturer_id=current_user.id).all()
        if not courses:
            return [], "No courses assigned"
        course_ids = [c.id for c in courses]
        label = ", ".join(c.code for c in courses)
        return (Enrollment.query
                .filter(Enrollment.course_id.in_(course_ids), Enrollment.status == "active").all(), label)
    if role == "counsellor":
        students = Student.query.filter_by(counsellor_id=current_user.id).all()
        enrollments = [e for s in students for e in s.enrollments]
        return enrollments, f"Caseload of {current_user.full_name}"
    if role == "student":
        student = Student.query.filter_by(user_id=current_user.id).first()
        if not student:
            return [], "—"
        return list(student.enrollments), f"{student.user.full_name} ({student.student_number})"
    return [], "—"


@app.route("/admin/reports")
@login_required
@role_required("admin")
def admin_reports():
    departments = Department.query.order_by(Department.name).all()
    dept_id = request.args.get("department_id", type=int)
    scope_label = Department.query.get(dept_id).name if dept_id else "All departments"
    return render_template(
        "reports_page.html", report_types=ROLE_REPORT_TYPES["admin"], report_defs=REPORT_DEFS,
        departments=departments, dept_id=dept_id, scope_label=scope_label,
    )


@app.route("/hod/reports")
@login_required
@role_required("hod")
def hod_reports():
    scope_label = current_user.department.name if current_user.department else "No department assigned"
    return render_template(
        "reports_page.html", report_types=ROLE_REPORT_TYPES["hod"], report_defs=REPORT_DEFS,
        departments=None, dept_id=None, scope_label=scope_label,
    )


@app.route("/lecturer/reports")
@login_required
@role_required("lecturer")
def lecturer_reports():
    courses = Course.query.filter_by(lecturer_id=current_user.id).all()
    scope_label = ", ".join(c.code for c in courses) if courses else "No courses assigned"
    return render_template(
        "reports_page.html", report_types=ROLE_REPORT_TYPES["lecturer"], report_defs=REPORT_DEFS,
        departments=None, dept_id=None, scope_label=scope_label,
    )


@app.route("/counsellor/reports")
@login_required
@role_required("counsellor")
def counsellor_reports():
    scope_label = f"Caseload of {current_user.full_name}"
    return render_template(
        "reports_page.html", report_types=ROLE_REPORT_TYPES["counsellor"], report_defs=REPORT_DEFS,
        departments=None, dept_id=None, scope_label=scope_label,
    )


@app.route("/reports/<report_type>.<fmt>")
@login_required
def download_report(report_type, fmt):
    role = current_user.role
    allowed = ROLE_REPORT_TYPES.get(role, [])
    if report_type not in allowed or report_type not in REPORT_DEFS or fmt not in ("csv", "pdf"):
        abort(404)

    definition = REPORT_DEFS[report_type]

    if report_type == "intervention":
        if role != "counsellor":
            abort(403)
        students = Student.query.filter_by(counsellor_id=current_user.id).all()
        rows = reports.intervention_rows(students)
        subtitle = f"Caseload of {current_user.full_name}"
    else:
        enrollments, subtitle = scoped_enrollments_for_reports()
        if report_type == "performance":
            rows = reports.performance_rows(enrollments)
        elif report_type == "risk":
            rows = reports.risk_rows(enrollments)
        elif report_type == "attendance":
            rows = reports.attendance_rows(enrollments)
        else:
            abort(404)

    filename_base = f"{report_type}_report_{datetime.utcnow().strftime('%Y%m%d')}"
    if fmt == "csv":
        csv_text = reports.rows_to_csv(definition["fieldnames"], rows)
        return Response(
            csv_text, mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename_base}.csv"},
        )
    else:
        pdf_bytes = reports.rows_to_pdf(
            definition["title"], subtitle, definition["fieldnames"], definition["labels"], rows,
            col_widths=definition.get("col_widths"),
        )
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename_base}.pdf"},
        )


def ensure_admin_account():
    """Guarantees at least one admin login exists even when demo seeding is off —
    otherwise a fresh production database would have no way to sign in at all.
    Set ADMIN_USERNAME / ADMIN_PASSWORD as environment variables before first boot
    to control the password directly; otherwise a random one is generated and
    printed to the server log exactly once."""
    if User.query.filter_by(role="admin").first():
        return
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)
    admin = User(username=username, full_name="System Administrator", role="admin", active=True)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    if generated:
        print("=" * 70)
        print(f"Created initial admin account — username: {username}")
        print(f"Generated password (shown once in this log): {password}")
        print("Set ADMIN_USERNAME / ADMIN_PASSWORD env vars to control this instead.")
        print("=" * 70)


def initialize_database():
    """Creates tables, optionally seeds demo data, and guarantees an admin account
    exists. Safe to call more than once (everything here is idempotent).

    IMPORTANT: this must run exactly once per deploy, not once per worker process.
    Under gunicorn with multiple workers, every worker imports this module — if this
    function ran automatically at import time, concurrent workers would race to
    initialize the same database simultaneously (this really does throw "database is
    locked" under SQLite, and is unsafe under Postgres too). So it's called
    automatically only for local single-process dev (`python app.py`, see bottom of
    this file); in production it's run once via init_db.py, before the web workers
    start — see render.yaml's preDeployCommand."""
    with app.app_context():
        db.create_all()
        # Bring databases created by older versions up to date (idempotent).
        run_migrations(db)

        # Demo data (~25 synthetic students with a default password) is meant for
        # local trial use only. ON by default when running locally against SQLite,
        # OFF by default the moment a real DATABASE_URL is configured — either way,
        # SEED_DEMO_DATA=true/false overrides the default explicitly.
        seed_demo_default = "false" if database_url else "true"
        if os.environ.get("SEED_DEMO_DATA", seed_demo_default).strip().lower() == "true":
            seed_data.seed()
            db.session.commit()

        ensure_admin_account()
        recompute_all()


if __name__ == "__main__":
    initialize_database()
    debug_mode = os.environ.get("FLASK_DEBUG", "true").strip().lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=debug_mode, use_reloader=False)
