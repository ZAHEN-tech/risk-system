import os
import re
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, redirect, url_for, request, flash, abort, Response, session
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from flask_wtf.csrf import CSRFProtect, CSRFError

from models import (
    db, User, Department, Course, Student, Enrollment,
    AttendanceRecord, Assessment, AssessmentDefinition, ASSESSMENT_TYPES,
    RiskScore, Notification, InterventionNote
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


@app.context_processor
def inject_globals():
    return {"unread_count": unread_count, "now": local_now(),
            "pop_new_credentials": pop_new_credentials, "APP_NAME": APP_NAME,
            "role_labels": ROLE_LABELS, "themes": THEMES, "default_theme": DEFAULT_THEME,
            "greeting": greeting()}


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
    db.session.commit()
    share_credentials(user.full_name, user.username, new_password, "New password")
    flash(f"Password reset for {user.full_name}. They will be asked to choose their own at next sign-in.", "success")
    return redirect(back_url)




@app.route("/logout")
@login_required
def logout():
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
    return max(risks, key=lambda r: r.risk_score)


def owned_course_or_404(course_id):
    course = Course.query.get_or_404(course_id)
    if course.lecturer_id != current_user.id:
        abort(403)
    return course


# ------------------------------------------------------ Student Management -

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

    students = query.order_by(User.full_name).all()

    rows = []
    for s in students:
        worst = student_worst_risk(s)
        if band and (not worst or worst.risk_band != band):
            continue
        rows.append({"student": s, "worst_risk": worst})

    departments = Department.query.order_by(Department.name).all()
    return render_template(
        "admin_students_list.html", rows=rows, departments=departments,
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
        if not full_name:
            errors.append("Full name is required.")
        if not student_number:
            errors.append("Student ID is required.")
        elif Student.query.filter_by(student_number=student_number).first():
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

        db.session.commit()

        share_credentials(full_name, username, password, "Student")
        if parent_user:
            share_credentials(parent_user.full_name, parent_user.username, parent_password, "Parent / guardian")
        msg = f"Registered {full_name} ({student_number})."
        if enrolled_courses:
            msg += f" Enrolled in: {', '.join(enrolled_courses)}."
        flash(msg, "success")
        return redirect(url_for("admin_student_profile", student_id=student.id))

    return render_template("admin_student_form.html", mode="new", departments=departments,
                            counsellors=counsellors, all_courses=all_courses, form={})


@app.route("/admin/students/<int:student_id>")
@login_required
@role_required("admin")
def admin_student_profile(student_id):
    student = Student.query.get_or_404(student_id)
    rows = [enrollment_row(e) for e in student.enrollments]
    enrolled_course_ids = {e.course_id for e in student.enrollments}
    course_query = Course.query.filter_by(department_id=student.department_id)
    if enrolled_course_ids:
        course_query = course_query.filter(~Course.id.in_(enrolled_course_ids))
    available_courses = course_query.order_by(Course.code).all()
    notes = (
        InterventionNote.query.filter_by(student_id=student.id)
        .order_by(InterventionNote.created_at.desc()).all()
    )
    return render_template(
        "admin_student_profile.html", student=student, rows=rows,
        available_courses=available_courses, notes=notes,
    )


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
    if existing:
        flash(f"{student.user.full_name} is already enrolled in {course.code}.", "error")
        return redirect(url_for("admin_student_profile", student_id=student.id))

    enr = Enrollment(student_id=student.id, course_id=course.id)
    db.session.add(enr)
    db.session.flush()

    rs, prev_band = compute_risk_for_enrollment(enr.id)
    generate_notifications_for_enrollment(enr, rs, prev_band)
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
            db.session.commit()
            flash(f"Created department: {name}.", "success")
        return redirect(url_for("admin_departments"))

    departments = Department.query.order_by(Department.name).all()
    rows = [{"department": d, "course_count": Course.query.filter_by(department_id=d.id).count(),
             "student_count": Student.query.filter_by(department_id=d.id).count()} for d in departments]
    return render_template("admin_departments.html", rows=rows)


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
        db.session.commit()
        flash("Course updated.", "success")
        return redirect(url_for("admin_courses_list"))

    return render_template("admin_course_form.html", course=course, departments=departments, lecturers=lecturers, form={})


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
    )


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
                           show_inactive=show_inactive, roles=STAFF_ROLES)


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
                           caseload=caseload, worst=student_worst_risk)


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
    )


@app.route("/admin/seed", methods=["POST"])
@login_required
@role_required("admin")
def admin_seed():
    seed_data.seed()
    db.session.commit()
    flash("Synthetic demo dataset seeded (or already present).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/recompute", methods=["POST"])
@login_required
@role_required("admin")
def admin_recompute():
    summary = recompute_all()
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
    db.drop_all()
    db.create_all()
    db.session.commit()
    flash("Database reset. Seed the demo dataset again to continue.", "success")
    return redirect(url_for("admin_dashboard"))


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
        entries = Assessment.query.filter_by(definition_id=d.id).all()
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

    entries = Assessment.query.filter_by(definition_id=definition.id).all()
    entries = sorted(entries, key=lambda a: a.enrollment.student.user.full_name)

    if request.method == "POST":
        affected = []
        for a in entries:
            raw = request.form.get(f"score_{a.id}", "").strip()
            if raw == "":
                a.score = None
                a.submitted = False
            else:
                try:
                    a.score = max(0.0, min(definition.max_score, float(raw)))
                    a.submitted = True
                except ValueError:
                    flash(f"Ignored invalid score '{raw}' for {a.enrollment.student.user.full_name}.", "error")
            affected.append(a.enrollment)
        db.session.flush()

        notif_count = 0
        for enr in affected:
            rs, prev_band = compute_risk_for_enrollment(enr.id)
            notif_count += generate_notifications_for_enrollment(enr, rs, prev_band)
        db.session.commit()

        flash(f"Grades saved for '{definition.title}'. {notif_count} new alert(s) generated.", "success")
        return redirect(url_for("lecturer_assessment_grade", course_id=course.id, definition_id=definition.id))

    return render_template("lecturer_assessment_grade.html", course=course, definition=definition, entries=entries)


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
        if worst.risk_band == "Low":
            continue
        rows.append({"student": s, "risk": worst, "n_flagged_courses": sum(1 for r in risks if r.risk_band != "Low")})
    rows.sort(key=lambda r: r["risk"].risk_score, reverse=True)
    notifications = (
        Notification.query.filter_by(recipient_id=current_user.id)
        .order_by(Notification.created_at.desc()).limit(15).all()
    )
    band_counts = analytics.band_counts([r["risk"] for r in rows])
    return render_template("counsellor_dashboard.html", rows=rows, notifications=notifications, band_counts=band_counts)


@app.route("/counsellor/student/<int:student_id>", methods=["GET", "POST"])
@login_required
@role_required("counsellor")
def counsellor_student_detail(student_id):
    student = Student.query.get_or_404(student_id)
    if student.counsellor_id != current_user.id:
        abort(403)
    if request.method == "POST":
        note = InterventionNote(
            student_id=student.id,
            author_id=current_user.id,
            note=request.form.get("note", "").strip(),
            action_taken=request.form.get("action_taken", "").strip(),
        )
        db.session.add(note)
        db.session.commit()
        flash("Intervention note logged.", "success")
        return redirect(url_for("counsellor_student_detail", student_id=student.id))

    rows = [enrollment_row(e) for e in student.enrollments]
    notes = (
        InterventionNote.query.filter_by(student_id=student.id)
        .order_by(InterventionNote.created_at.desc()).all()
    )
    explanations = {r["enrollment"].id: get_ml_explanation(r["risk"]) for r in rows}
    return render_template("student_detail.html", student=student, rows=rows, notes=notes, editable=True, explanations=explanations)


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
        charts[enr_id] = {
            "att_labels": att_labels, "att_values": att_values,
            "score_labels": score_labels, "score_values": score_values,
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
        "fieldnames": ["student", "student_number", "date", "counsellor", "action_taken", "note"],
        "labels": ["Student", "ID", "Date", "Counsellor", "Action taken", "Note"],
        "description": "Logged counselling interventions for your caseload. Confidential — counsellor access only.",
        "col_widths": [2.2, 1.3, 1.6, 2.2, 2.7, 4.0],
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
        q = Enrollment.query.join(Course)
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
        return Enrollment.query.join(Course).filter(Course.department_id == dept.id).all(), dept.name
    if role == "lecturer":
        courses = Course.query.filter_by(lecturer_id=current_user.id).all()
        if not courses:
            return [], "No courses assigned"
        course_ids = [c.id for c in courses]
        label = ", ".join(c.code for c in courses)
        return Enrollment.query.filter(Enrollment.course_id.in_(course_ids)).all(), label
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
