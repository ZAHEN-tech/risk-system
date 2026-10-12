"""Admin bulk delete and activity-log clearing. Runs last (file name), because the
final tests wipe the shared test database."""
from datetime import datetime, timedelta

from conftest import DEMO_PASSWORD, signed_in
from models import (
    db, ActivityLog, AssessmentDefinition, Assessment, AttendanceRecord, Course, Department, Enrollment,
    InterventionNote, Notification, RiskScore, Student, User,
)

OK = {"confirm_word": "DELETE", "admin_password": DEMO_PASSWORD}


def _user(username, role, dept=None, full_name=None):
    u = User(username=username, full_name=full_name or username.title(), role=role,
             department_id=dept.id if dept else None, active=True)
    u.set_password("Temp-pass-12345")
    db.session.add(u)
    db.session.flush()
    return u


def _faculty(tag):
    """A small separate department: 1 course, 1 lecturer, 1 counsellor, 2 students
    (one with a parent), attendance, a mark and an intervention note."""
    db.session.expunge_all()   # forget rows removed by earlier bulk deletes
    d = Department(name=f"Faculty {tag}")
    db.session.add(d)
    db.session.flush()
    lect = _user(f"lect_{tag}", "lecturer", d)
    couns = _user(f"couns_{tag}", "counsellor", d)
    course = Course(code=f"T{tag}1", name=f"Test {tag}", department_id=d.id, lecturer_id=lect.id)
    db.session.add(course)
    db.session.flush()
    item = AssessmentDefinition(course_id=course.id, title="Quiz", sequence=1, max_score=10)
    db.session.add(item)
    db.session.flush()
    parent = _user(f"par_{tag}", "parent")
    students = []
    for i in range(2):
        su = _user(f"stu_{tag}_{i}", "student", d)
        s = Student(user_id=su.id, student_number=f"S-{tag}-{i}", department_id=d.id,
                    counsellor_id=couns.id, parent_id=parent.id if i == 0 else None)
        db.session.add(s)
        db.session.flush()
        e = Enrollment(student_id=s.id, course_id=course.id, status="active")
        db.session.add(e)
        db.session.flush()
        db.session.add(AttendanceRecord(enrollment_id=e.id, week=1, present=True))
        db.session.add(Assessment(enrollment_id=e.id, definition_id=item.id, score=7, submitted=True))
        db.session.add(RiskScore(enrollment_id=e.id, risk_score=10, risk_band="Low"))
        db.session.add(Notification(recipient_id=su.id, student_id=s.id, course_id=course.id, message="x"))
        db.session.add(ActivityLog(user_id=su.id, action="login"))
        students.append(s)
    db.session.add(InterventionNote(student_id=students[0].id, author_id=couns.id, note="Called"))
    db.session.commit()
    return d, course, lect, couns, students, parent


def test_bulk_delete_needs_word_and_password(app):
    c = signed_in(app, "admin")
    before = Student.query.count()
    r = c.post("/admin/students/bulk_delete", {"confirm_word": "delete", "admin_password": DEMO_PASSWORD},
               form_url="/admin/students")
    assert "Type DELETE in capitals" in r.get_data(as_text=True)
    r = c.post("/admin/students/bulk_delete", {"confirm_word": "DELETE", "admin_password": "wrong"},
               form_url="/admin/students")
    assert "password was not correct" in r.get_data(as_text=True)
    assert Student.query.count() == before


def test_only_admin_can_bulk_delete(app):
    c = signed_in(app, "lect_1_2")
    for url in ["/admin/students/bulk_delete", "/admin/courses/bulk_delete", "/admin/lecturers/bulk_delete",
                "/admin/staff/bulk_delete", "/admin/departments/bulk_delete", "/admin/activity/clear"]:
        assert c.post(url, dict(OK, mode="all"), form_url="/lecturer").status_code == 403


def test_bulk_panels_show_on_admin_pages(app):
    c = signed_in(app, "admin")
    for url in ["/admin/students", "/admin/courses", "/admin/lecturers", "/admin/staff", "/admin/departments"]:
        assert "Delete in bulk" in c.text(url), url
    assert "Clear the activity log" in c.text("/admin/activity")


def test_bulk_delete_students_in_one_department(app):
    d, course, lect, couns, students, parent = _faculty("A")
    other = Student.query.filter(Student.department_id != d.id).count()
    sid, dept_id, course_id = students[0].id, d.id, course.id
    c = signed_in(app, "admin")
    r = c.post("/admin/students/bulk_delete", dict(OK, department_id=dept_id), form_url="/admin/students")
    page = r.get_data(as_text=True)
    assert "Deleted 2 students in Faculty A" in page and "1 parent account" in page
    db.session.expire_all()
    assert Student.query.filter_by(department_id=dept_id).count() == 0
    assert Student.query.count() == other, "other departments untouched"
    assert Enrollment.query.filter_by(course_id=course_id).count() == 0
    assert InterventionNote.query.filter_by(student_id=sid).count() == 0
    assert User.query.filter(User.username.in_(["stu_A_0", "stu_A_1", "par_A"])).count() == 0
    assert db.session.get(Course, course_id) is not None, "courses kept"
    # Logs kept by default, labelled with the deleted person's name.
    assert ActivityLog.query.filter(ActivityLog.detail.like("[Deleted account Stu_A_0%")).count() == 1
    assert ActivityLog.query.filter_by(action="bulk.delete").count() >= 1


def test_bulk_delete_students_and_their_logs(app):
    d, *_ = _faculty("B")
    c = signed_in(app, "admin")
    r = c.post("/admin/students/bulk_delete", dict(OK, department_id=d.id, delete_logs="on"), form_url="/admin/students")
    assert "removed 2 activity-log entries" in r.get_data(as_text=True)
    assert ActivityLog.query.filter(ActivityLog.detail.like("[Deleted account Stu_B_%")).count() == 0


def test_bulk_delete_courses_keeps_students(app):
    d, course, lect, couns, students, parent = _faculty("C")
    dept_id, course_id = d.id, course.id
    c = signed_in(app, "admin")
    r = c.post("/admin/courses/bulk_delete", dict(OK, department_id=dept_id), form_url="/admin/courses")
    assert "Deleted 1 course in Faculty C, with 2 enrolments" in r.get_data(as_text=True)
    db.session.expire_all()
    assert Course.query.filter_by(department_id=dept_id).count() == 0
    assert AssessmentDefinition.query.filter_by(course_id=course_id).count() == 0
    assert Student.query.filter_by(department_id=dept_id).count() == 2


def test_bulk_delete_counsellors_and_lecturers(app):
    d, course, lect, couns, students, parent = _faculty("D")
    dept_id, course_id, lect_id, couns_id = d.id, course.id, lect.id, couns.id
    c = signed_in(app, "admin")
    r = c.post("/admin/staff/bulk_delete", dict(OK, department_id=dept_id, role="counsellor"), form_url="/admin/staff")
    page = r.get_data(as_text=True)
    assert "Deleted 1 account (counsellors) in Faculty D" in page and "2 students without a counsellor" in page
    db.session.expire_all()
    assert db.session.get(User, couns_id) is None
    assert Student.query.filter_by(department_id=dept_id, counsellor_id=None).count() == 2
    assert InterventionNote.query.filter_by(author_id=couns_id).count() == 0

    r = c.post("/admin/lecturers/bulk_delete", dict(OK, department_id=dept_id), form_url="/admin/lecturers")
    assert "1 course now unassigned" in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(User, lect_id) is None
    assert db.session.get(Course, course_id).lecturer_id is None


def test_clear_activity_log_for_a_period(app):
    c = signed_in(app, "admin")
    # Fiji is UTC+12: 1 Jan 2020 10:00 Fiji = 31 Dec 2019 22:00 UTC.
    old = [ActivityLog(action="login", detail="old-1", created_at=datetime(2019, 12, 31, 22, 0)),
           ActivityLog(action="login", detail="old-2", created_at=datetime(2020, 1, 2, 11, 0)),
           ActivityLog(action="login", detail="keep", created_at=datetime(2020, 1, 5, 0, 0))]
    db.session.add_all(old)
    db.session.commit()
    r = c.post("/admin/activity/clear", dict(OK, mode="range", date_from="2020-01-01", date_to="2020-01-02"),
               form_url="/admin/activity")
    assert "Cleared 2 activity-log entries (from 2020-01-01 to 2020-01-02" in r.get_data(as_text=True)
    assert ActivityLog.query.filter(ActivityLog.detail.in_(["old-1", "old-2"])).count() == 0
    assert ActivityLog.query.filter_by(detail="keep").count() == 1

    r = c.post("/admin/activity/clear", dict(OK, mode="range"), form_url="/admin/activity")
    assert "Choose a start date" in r.get_data(as_text=True)


def test_delete_all_departments_then_site_still_works(app):
    c = signed_in(app, "admin")
    r = c.post("/admin/departments/bulk_delete", OK, form_url="/admin/departments")
    assert "Deleted" in r.get_data(as_text=True)
    db.session.expire_all()
    assert Department.query.count() == 0
    assert Course.query.count() == 0 and Student.query.count() == 0 and Enrollment.query.count() == 0
    assert User.query.filter(User.role.in_(["lecturer", "hod", "student"])).count() == 0
    assert User.query.filter_by(role="admin").count() >= 1
    for url in ["/admin", "/admin/students", "/admin/courses", "/admin/lecturers", "/admin/staff",
                "/admin/departments", "/admin/activity", "/admin/reports", "/admin/model"]:
        assert c.get(url).status_code == 200, url


def test_clear_whole_activity_log(app):
    c = signed_in(app, "admin")
    r = c.post("/admin/activity/clear", dict(OK, mode="all"), form_url="/admin/activity")
    assert "Cleared" in r.get_data(as_text=True)
    rows = ActivityLog.query.all()
    assert len(rows) == 1 and rows[0].action == "log.clear"
