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
    AssessmentDefinition, RiskScore, RiskSnapshot, Notification, InterventionNote, ActivityLog,
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
        for model in (AttendanceRecord, Assessment, RiskScore, RiskSnapshot):
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


# ---------------------------------------------------------------- Bulk clearing

def clear_activity_log(start_utc=None, end_utc=None):
    """Deletes activity-log entries, all of them or those with
    start_utc <= created_at < end_utc. Returns how many were removed."""
    q = ActivityLog.query
    if start_utc is not None:
        q = q.filter(ActivityLog.created_at >= start_utc)
    if end_utc is not None:
        q = q.filter(ActivityLog.created_at < end_utc)
    return q.delete(synchronize_session=False)


def data_counts():
    """What 'Clear all data' would remove."""
    return {
        "departments": Department.query.count(),
        "courses": Course.query.count(),
        "students": Student.query.count(),
        "lecturers": User.query.filter_by(role="lecturer").count(),
        "heads": User.query.filter_by(role="hod").count(),
        "counsellors": User.query.filter_by(role="counsellor").count(),
        "parents": User.query.filter_by(role="parent").count(),
        "enrolments": Enrollment.query.count(),
        "attendance": AttendanceRecord.query.count(),
        "marks": Assessment.query.count(),
        "notes": InterventionNote.query.count(),
        "alerts": Notification.query.count(),
        "log": ActivityLog.query.count(),
    }


def _chunks(ids, n=500):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _delete_in(model, column, ids):
    total = 0
    for part in _chunks(ids):
        total += model.query.filter(column.in_(part)).delete(synchronize_session=False)
    return total


def _forget_users(user_ids, delete_logs):
    """Clears every reference to these accounts so they can be removed. Their
    activity-log lines are deleted, or kept and labelled with their name."""
    user_ids = list(user_ids)
    if not user_ids:
        return 0
    for part in _chunks(user_ids):
        Enrollment.query.filter(Enrollment.withdrawn_by_id.in_(part)).update(
            {"withdrawn_by_id": None}, synchronize_session=False)
    _delete_in(Notification, Notification.recipient_id, user_ids)
    if delete_logs:
        return _delete_in(ActivityLog, ActivityLog.user_id, user_ids)
    labels = {}
    for part in _chunks(user_ids):
        for u in User.query.filter(User.id.in_(part)).all():
            labels[u.id] = f"{DELETED_PREFIX}{u.full_name} ({u.username})] "
    for part in _chunks(user_ids):
        for row in ActivityLog.query.filter(ActivityLog.user_id.in_(part)).all():
            row.detail = (labels.get(row.user_id, DELETED_PREFIX + "] ") + (row.detail or "")).strip()[:300]
            row.user_id = None
    db.session.flush()
    return 0


def _delete_enrolments(enr_ids):
    for model in (AttendanceRecord, Assessment, RiskScore, RiskSnapshot):
        _delete_in(model, model.enrollment_id, enr_ids)
    return _delete_in(Enrollment, Enrollment.id, enr_ids)


def bulk_delete_students(student_ids, delete_logs=False):
    """Permanently removes these students with all their enrolments, attendance,
    marks, risk scores, alerts and intervention notes, plus parent accounts left
    with no child. Returns counts. The caller logs and commits."""
    student_ids = list(student_ids)
    c = {"students": len(student_ids), "parents": 0, "enrolments": 0, "notes": 0, "log_entries": 0}
    if not student_ids:
        return c
    user_ids, parent_ids, enr_ids = [], set(), []
    for part in _chunks(student_ids):
        for s in Student.query.filter(Student.id.in_(part)).all():
            user_ids.append(s.user_id)
            if s.parent_id:
                parent_ids.add(s.parent_id)
        enr_ids += [e.id for e in Enrollment.query.filter(Enrollment.student_id.in_(part)).all()]
    c["enrolments"] = _delete_enrolments(enr_ids)
    _delete_in(Notification, Notification.student_id, student_ids)
    c["notes"] = _delete_in(InterventionNote, InterventionNote.student_id, student_ids)
    _delete_in(Student, Student.id, student_ids)
    db.session.flush()
    orphans = [pid for pid in parent_ids if Student.query.filter_by(parent_id=pid).count() == 0]
    c["parents"] = len(orphans)
    gone = user_ids + orphans
    c["log_entries"] = _forget_users(gone, delete_logs)
    _delete_in(User, User.id, gone)
    db.session.expire_all()
    return c


def bulk_delete_courses(course_ids):
    """Permanently removes these courses with their enrolments, attendance, marks,
    risk scores, assessment items and alerts. Students stay."""
    course_ids = list(course_ids)
    c = {"courses": len(course_ids), "enrolments": 0}
    if not course_ids:
        return c
    enr_ids = []
    for part in _chunks(course_ids):
        enr_ids += [e.id for e in Enrollment.query.filter(Enrollment.course_id.in_(part)).all()]
    c["enrolments"] = _delete_enrolments(enr_ids)
    _delete_in(AssessmentDefinition, AssessmentDefinition.course_id, course_ids)
    _delete_in(Notification, Notification.course_id, course_ids)
    _delete_in(Course, Course.id, course_ids)
    db.session.expire_all()
    return c


def bulk_delete_staff(user_ids, delete_logs=False):
    """Permanently removes lecturer, head of department and counsellor accounts.
    Their courses become unassigned, their students lose that counsellor, and
    intervention notes they wrote are deleted."""
    user_ids = [u.id for part in _chunks(user_ids)
                for u in User.query.filter(User.id.in_(part), User.role.in_(STAFF_ROLES)).all()]
    c = {"accounts": len(user_ids), "courses_unassigned": 0, "students_unassigned": 0, "notes": 0, "log_entries": 0}
    if not user_ids:
        return c
    for part in _chunks(user_ids):
        c["courses_unassigned"] += Course.query.filter(Course.lecturer_id.in_(part)).update(
            {"lecturer_id": None}, synchronize_session=False)
        c["students_unassigned"] += Student.query.filter(Student.counsellor_id.in_(part)).update(
            {"counsellor_id": None}, synchronize_session=False)
    c["notes"] = _delete_in(InterventionNote, InterventionNote.author_id, user_ids)
    c["log_entries"] = _forget_users(user_ids, delete_logs)
    _delete_in(User, User.id, user_ids)
    db.session.expire_all()
    return c


def bulk_delete_departments(department_ids, delete_logs=False):
    """Removes these departments and everything in them: students (and their
    parents), courses, and the lecturers, heads and counsellors who belong to them."""
    department_ids = list(department_ids)
    c = {"departments": len(department_ids)}
    if not department_ids:
        return c
    students = [s.id for s in Student.query.filter(Student.department_id.in_(department_ids)).all()]
    courses = [x.id for x in Course.query.filter(Course.department_id.in_(department_ids)).all()]
    staff = [u.id for u in User.query.filter(User.department_id.in_(department_ids), User.role.in_(STAFF_ROLES)).all()]
    s = bulk_delete_students(students, delete_logs)
    k = bulk_delete_courses(courses)
    t = bulk_delete_staff(staff, delete_logs)
    User.query.filter(User.department_id.in_(department_ids)).update({"department_id": None}, synchronize_session=False)
    _delete_in(Department, Department.id, department_ids)
    db.session.expire_all()
    c.update({"students": s["students"], "parents": s["parents"], "courses": k["courses"],
              "staff": t["accounts"], "enrolments": s["enrolments"] + k["enrolments"],
              "notes": s["notes"] + t["notes"], "log_entries": s["log_entries"] + t["log_entries"]})
    return c
