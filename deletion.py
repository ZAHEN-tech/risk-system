"""Permanent deletion rules for the administrator.

Deactivating is the normal way to remove a person: it keeps their history.
Permanent deletion is only for records created by mistake, so it is allowed
only when nothing important would be lost. Each `*_blockers` function returns
a list of plain-English reasons the record cannot be deleted (empty = safe).
Each `delete_*` function removes the record and its leftovers; the caller logs
the action and commits, so a failure leaves nothing half-deleted.
"""
from models import (
    db, User, Department, Course, Student, Enrollment, AttendanceRecord, Assessment,
    AssessmentDefinition, RiskScore, Notification, InterventionNote, ActivityLog,
)

# Activity-log entries a person makes just by using their account. Anything else
# (attendance or marks saved, enrolments changed, imports...) is recorded work.
_ACCOUNT_ONLY_ACTIONS = {"login", "logout", "login.failed", "login.locked", "password.change", "account.update"}

DELETED_PREFIX = "[Deleted account "
STAFF_ROLES = ("lecturer", "hod", "counsellor")


def department_staff_count(dept):
    return User.query.filter(User.department_id == dept.id, User.role.in_(STAFF_ROLES)).count()


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _work_logged(user):
    return (ActivityLog.query.filter(ActivityLog.user_id == user.id,
                                     ~ActivityLog.action.in_(_ACCOUNT_ONLY_ACTIONS)).count())


def _remove_user(user):
    """Deletes a user account. Their own sign-in lines stay in the activity log,
    labelled with the deleted account's name, so the log still reads correctly."""
    label = f"{DELETED_PREFIX}{user.full_name} ({user.username})] "
    for row in ActivityLog.query.filter_by(user_id=user.id).all():
        row.detail = (label + (row.detail or "")).strip()[:300]
        row.user_id = None
    Notification.query.filter_by(recipient_id=user.id).delete(synchronize_session=False)
    Enrollment.query.filter_by(withdrawn_by_id=user.id).update({"withdrawn_by_id": None}, synchronize_session=False)
    db.session.delete(user)


# ------------------------------------------------------------------ Departments

def department_blockers(dept):
    reasons = []
    courses = Course.query.filter_by(department_id=dept.id).count()
    students = Student.query.filter_by(department_id=dept.id).count()
    staff = department_staff_count(dept)
    if courses:
        reasons.append(f"{_plural(courses, 'course')} belong to it — move or delete them first.")
    if students:
        reasons.append(f"{_plural(students, 'student')} belong to it — move them to another department first.")
    if staff:
        reasons.append(f"{_plural(staff, 'staff account')} (lecturers, heads or counsellors) belong to it — move them first.")
    return reasons


def delete_department(dept):
    # Student and parent logins may still carry the old department; clear it.
    User.query.filter_by(department_id=dept.id).update({"department_id": None}, synchronize_session=False)
    db.session.delete(dept)


# ---------------------------------------------------------------------- Courses

def course_blockers(course):
    reasons = []
    enrolled = Enrollment.query.filter_by(course_id=course.id).count()
    if enrolled:
        reasons.append(f"{_plural(enrolled, 'student enrolment')} (including withdrawn ones) are linked to it.")
    return reasons


def delete_course(course):
    AssessmentDefinition.query.filter_by(course_id=course.id).delete(synchronize_session=False)
    Notification.query.filter_by(course_id=course.id).delete(synchronize_session=False)
    db.session.delete(course)


# ------------------------------------------------------------- Staff accounts

def staff_blockers(user):
    """For lecturers, heads of department and counsellors."""
    reasons = []
    if user.role == "lecturer":
        courses = Course.query.filter_by(lecturer_id=user.id).count()
        if courses:
            reasons.append(f"They teach {_plural(courses, 'course')} — unassign or reassign them first.")
    if user.role == "counsellor":
        caseload = Student.query.filter_by(counsellor_id=user.id).count()
        if caseload:
            reasons.append(f"{_plural(caseload, 'student')} have them as counsellor — "
                           "assign another counsellor on each student's Edit page first.")
    notes = InterventionNote.query.filter_by(author_id=user.id).count()
    if notes:
        reasons.append(f"They wrote {_plural(notes, 'intervention note')}, which must be kept.")
    work = _work_logged(user)
    if work:
        reasons.append(f"They have {_plural(work, 'recorded action')} in the activity log "
                       "(for example attendance or marks saved).")
    return reasons


def delete_staff(user):
    _remove_user(user)


# --------------------------------------------------------------------- Students

def student_blockers(student):
    reasons = []
    enr_ids = [e.id for e in student.all_enrollments]
    if enr_ids:
        attendance = AttendanceRecord.query.filter(AttendanceRecord.enrollment_id.in_(enr_ids)).count()
        marks = Assessment.query.filter(Assessment.enrollment_id.in_(enr_ids),
                                        db.or_(Assessment.score.isnot(None), Assessment.submitted.is_(True))).count()
        if attendance:
            reasons.append(f"{_plural(attendance, 'attendance record')} have been taken for them.")
        if marks:
            reasons.append(f"{_plural(marks, 'mark')} have been entered for them.")
    notes = InterventionNote.query.filter_by(student_id=student.id).count()
    if notes:
        reasons.append(f"Their case file has {_plural(notes, 'intervention note')}.")
    return reasons


def delete_student(student):
    """Removes the student, their (empty) enrolments, their alerts, and their
    parent account if that parent has no other child in the system.
    Returns the parent's name if the parent account was removed too."""
    enr_ids = [e.id for e in student.all_enrollments]
    if enr_ids:
        for model in (AttendanceRecord, Assessment, RiskScore):
            model.query.filter(model.enrollment_id.in_(enr_ids)).delete(synchronize_session=False)
        for e in list(student.all_enrollments):
            db.session.delete(e)
    Notification.query.filter_by(student_id=student.id).delete(synchronize_session=False)

    parent_removed = None
    parent = student.parent
    user = student.user
    db.session.delete(student)
    db.session.flush()
    if parent is not None and Student.query.filter_by(parent_id=parent.id).count() == 0:
        parent_removed = parent.full_name
        _remove_user(parent)
    _remove_user(user)
    return parent_removed
