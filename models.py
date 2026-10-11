from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    courses = db.relationship("Course", backref="department", lazy=True)


class User(UserMixin, db.Model):
    """Single auth table for every role: admin, lecturer, hod, counsellor, student, parent."""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False, index=True)  # admin, lecturer, hod, counsellor, student, parent
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=True, index=True)
    email = db.Column(db.String(120))
    active = db.Column(db.Boolean, default=True, nullable=False)
    # Security fields (added by migration in migrations.py for existing databases)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False, server_default=db.false())
    failed_logins = db.Column(db.Integer, default=0, nullable=False, server_default="0")
    locked_until = db.Column(db.DateTime, nullable=True)

    department = db.relationship("Department", foreign_keys=[department_id])

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    _TITLES = {"dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "prof", "prof.", "professor"}

    @property
    def first_name(self):
        words = [w for w in (self.full_name or "").split() if w.lower() not in self._TITLES]
        return words[0] if words else (self.username or "?")

    @property
    def initials(self):
        """Avatar letter: first letter of the first name (titles like 'Dr.' skipped)."""
        return self.first_name[:1].upper()

    @property
    def avatar_hue(self):
        """A stable colour per person (0-359), so avatars are easy to tell apart."""
        return sum(ord(c) for c in (self.username or "")) * 47 % 360


class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    name = db.Column(db.String(150), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False, index=True)
    lecturer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    lecturer = db.relationship("User", foreign_keys=[lecturer_id])
    # Every enrolment, including withdrawn ones (history, admin view).
    all_enrollments = db.relationship("Enrollment", back_populates="course", lazy=True)
    # Active enrolments only: what dashboards, attendance, marks and risk use.
    enrollments = db.relationship(
        "Enrollment", lazy=True, viewonly=True,
        primaryjoin="and_(Course.id == Enrollment.course_id, Enrollment.status == 'active')")


class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    student_number = db.Column(db.String(30), unique=True, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False, index=True)
    year_of_study = db.Column(db.Integer, default=1)
    counsellor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    parent_consent = db.Column(db.Boolean, default=False, nullable=False, server_default=db.false())  # opt-in: parent alerted only with consent

    user = db.relationship("User", foreign_keys=[user_id])
    department = db.relationship("Department", foreign_keys=[department_id])
    counsellor = db.relationship("User", foreign_keys=[counsellor_id])
    parent = db.relationship("User", foreign_keys=[parent_id])
    all_enrollments = db.relationship("Enrollment", back_populates="student", lazy=True)
    enrollments = db.relationship(
        "Enrollment", lazy=True, viewonly=True,
        primaryjoin="and_(Student.id == Enrollment.student_id, Enrollment.status == 'active')")


class Enrollment(db.Model):
    __table_args__ = (db.UniqueConstraint("student_id", "course_id", name="uq_enrollment_student_course"),)
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    # 'active' or 'withdrawn'. Withdrawn enrolments keep their attendance and
    # marks but are left out of risk scoring, alerts, dashboards and reports.
    status = db.Column(db.String(12), nullable=False, default="active", server_default="active", index=True)
    withdrawn_at = db.Column(db.DateTime, nullable=True)
    withdrawn_reason = db.Column(db.String(200), nullable=True)
    withdrawn_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    course = db.relationship("Course", back_populates="all_enrollments")
    student = db.relationship("Student", back_populates="all_enrollments")
    withdrawn_by = db.relationship("User", foreign_keys=[withdrawn_by_id])

    @property
    def is_active(self):
        return self.status == "active"

    attendance_records = db.relationship("AttendanceRecord", backref="enrollment", lazy=True)
    assessments = db.relationship("Assessment", backref="enrollment", lazy=True)
    risk = db.relationship("RiskScore", backref="enrollment", uselist=False)


class AttendanceRecord(db.Model):
    __table_args__ = (db.UniqueConstraint("enrollment_id", "week", name="uq_attendance_enrollment_week"),)
    id = db.Column(db.Integer, primary_key=True)
    enrollment_id = db.Column(db.Integer, db.ForeignKey("enrollment.id"), nullable=False, index=True)
    week = db.Column(db.Integer, nullable=False, index=True)
    present = db.Column(db.Boolean, default=True)


ASSESSMENT_TYPES = ["Coursework", "Assignment", "Quiz", "Examination"]


class AssessmentDefinition(db.Model):
    """A single graded item set for an entire course (e.g. 'Quiz 2'), created once by
    the lecturer and then graded per-student via the linked Assessment rows below."""
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    assessment_type = db.Column(db.String(20), nullable=False, default="Coursework")
    sequence = db.Column(db.Integer, nullable=False)  # order in the term, for trend analysis
    max_score = db.Column(db.Float, default=100.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    course = db.relationship("Course", foreign_keys=[course_id])
    entries = db.relationship("Assessment", backref="definition", lazy=True)


class Assessment(db.Model):
    """One student's mark for one assessment item. Item details (title, order in
    term, max score) live only on AssessmentDefinition (3NF); the read-only
    properties below expose them so callers can keep writing a.title etc."""
    __table_args__ = (db.UniqueConstraint("enrollment_id", "definition_id", name="uq_assessment_enrollment_definition"),)
    id = db.Column(db.Integer, primary_key=True)
    enrollment_id = db.Column(db.Integer, db.ForeignKey("enrollment.id"), nullable=False, index=True)
    definition_id = db.Column(db.Integer, db.ForeignKey("assessment_definition.id"), nullable=False, index=True)
    score = db.Column(db.Float, nullable=True)  # null = not submitted
    submitted = db.Column(db.Boolean, default=True)

    @property
    def title(self):
        return self.definition.title

    @property
    def sequence(self):
        return self.definition.sequence

    @property
    def max_score(self):
        return self.definition.max_score


class RiskScore(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    enrollment_id = db.Column(db.Integer, db.ForeignKey("enrollment.id"), unique=True, nullable=False)
    attendance_pct = db.Column(db.Float)
    assessment_avg = db.Column(db.Float)
    submission_rate = db.Column(db.Float)
    trend_penalty = db.Column(db.Float)
    risk_score = db.Column(db.Float)          # 0-100, higher = more at risk (rule-based)
    risk_band = db.Column(db.String(10))       # Low / Medium / High (rule-based)
    ml_probability = db.Column(db.Float)       # 0-100, higher = more at risk (logistic regression)
    ml_band = db.Column(db.String(10))         # Low / Medium / High (logistic regression)
    last_computed = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=True, index=True)
    message = db.Column(db.Text, nullable=False)
    severity = db.Column(db.String(10), default="Medium")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    read = db.Column(db.Boolean, default=False)

    recipient = db.relationship("User", foreign_keys=[recipient_id])
    student = db.relationship("Student", foreign_keys=[student_id])
    course = db.relationship("Course", foreign_keys=[course_id])


class ActivityLog(db.Model):
    """Who did what, and when: sign-ins, password and account changes,
    enrolment changes, imports, and attendance or marks saved."""
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    action = db.Column(db.String(40), nullable=False, index=True)
    detail = db.Column(db.String(300))
    ip_address = db.Column(db.String(64))

    user = db.relationship("User", foreign_keys=[user_id])


class InterventionNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    note = db.Column(db.Text, nullable=False)
    action_taken = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship("Student", foreign_keys=[student_id])
    author = db.relationship("User", foreign_keys=[author_id])
