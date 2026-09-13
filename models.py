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

    department = db.relationship("Department", foreign_keys=[department_id])

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False, index=True)
    lecturer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    lecturer = db.relationship("User", foreign_keys=[lecturer_id])
    enrollments = db.relationship("Enrollment", backref="course", lazy=True)


class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    student_number = db.Column(db.String(30), unique=True, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False, index=True)
    year_of_study = db.Column(db.Integer, default=1)
    counsellor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    parent_consent = db.Column(db.Boolean, default=True)  # whether parent may be notified

    user = db.relationship("User", foreign_keys=[user_id])
    department = db.relationship("Department", foreign_keys=[department_id])
    counsellor = db.relationship("User", foreign_keys=[counsellor_id])
    parent = db.relationship("User", foreign_keys=[parent_id])
    enrollments = db.relationship("Enrollment", backref="student", lazy=True)


class Enrollment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)

    attendance_records = db.relationship("AttendanceRecord", backref="enrollment", lazy=True)
    assessments = db.relationship("Assessment", backref="enrollment", lazy=True)
    risk = db.relationship("RiskScore", backref="enrollment", uselist=False)


class AttendanceRecord(db.Model):
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
    id = db.Column(db.Integer, primary_key=True)
    enrollment_id = db.Column(db.Integer, db.ForeignKey("enrollment.id"), nullable=False, index=True)
    definition_id = db.Column(db.Integer, db.ForeignKey("assessment_definition.id"), nullable=True, index=True)
    title = db.Column(db.String(120), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)  # order in the term, for trend analysis
    max_score = db.Column(db.Float, default=100.0)
    score = db.Column(db.Float, nullable=True)  # null = not submitted
    submitted = db.Column(db.Boolean, default=True)


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


class InterventionNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    note = db.Column(db.Text, nullable=False)
    action_taken = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship("Student", foreign_keys=[student_id])
    author = db.relationship("User", foreign_keys=[author_id])
