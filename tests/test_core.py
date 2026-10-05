"""Sign-in, passwords, staff accounts and the main pages for every role."""
import html as htmllib
import re

from conftest import Client, signed_in, token
from models import (db, User, Department, Course, AttendanceRecord, Assessment,
                    AssessmentDefinition, InterventionNote, Student)


def test_login_page_has_no_demo_details(client):
    page = client.text("/login")
    assert "password123" not in page and "lect_1_1" not in page and "Demo" not in page


def test_login_without_csrf_token_is_rejected(app):
    c = app.test_client()
    r = c.post("/login", data={"username": "admin", "password": "password123"})
    assert not r.headers.get("Location", "").endswith("/admin")


def test_admin_pages_load(app):
    c = signed_in(app, "admin")
    for url in ["/admin", "/admin/students", "/admin/lecturers", "/admin/courses", "/admin/departments",
                "/admin/model", "/admin/reports", "/admin/staff", "/admin/activity", "/admin/students/import",
                "/notifications", "/account/password", "/account/profile"]:
        assert c.get(url).status_code == 200, url
    assert c.get("/reports/risk.csv").status_code == 200
    assert c.get("/reports/performance.pdf").status_code == 200


def test_every_role_reaches_its_dashboard(app):
    for username, path in [("lect_1_1", "/lecturer"), ("hod_1", "/hod"), ("counsellor1", "/counsellor"),
                           ("student1", "/student"), ("parent1", "/parent")]:
        r = Client(app).login(username)
        assert r.request.path == path and r.status_code == 200, username


def test_roles_cannot_open_other_roles_pages(app):
    c = signed_in(app, "student1")
    r = c.get("/admin")
    assert r.status_code == 403
    assert "You don&#39;t have access" in r.get_data(as_text=True) or "have access" in r.get_data(as_text=True)


def test_friendly_404(app):
    r = app.test_client().get("/no-such-page")
    assert r.status_code == 404 and "Page not found" in r.get_data(as_text=True)


def test_staff_accounts_and_password_rules(app):
    c = signed_in(app, "admin")
    dept = Department.query.first()
    r = c.post("/admin/staff/new", {"full_name": "Second Head", "role": "hod", "department_id": dept.id})
    assert "already the active Head" in r.get_data(as_text=True)
    r = c.post("/admin/staff/new", {"full_name": "Kalesi Lal", "role": "counsellor", "password": "short"})
    assert "at least 10" in r.get_data(as_text=True)
    r = c.post("/admin/staff/new", {"full_name": "Kalesi Lal", "role": "counsellor", "password": "abcdefghijkl"})
    assert "mix at least three" in r.get_data(as_text=True)

    r = c.post("/admin/staff/new", {"full_name": "Kalesi Lal", "role": "counsellor", "email": "k@x.com"})
    page = r.get_data(as_text=True)
    m = re.search(r"<code>kalesi\.lal</code>\s*<code data-copy-source>([^<]+)</code>", page)
    assert m, "generated password should be shown once"
    generated = htmllib.unescape(m.group(1))
    assert len(generated) == 14
    assert "Share these sign-in details" not in c.text("/admin/staff")
    user = User.query.filter_by(username="kalesi.lal").first()
    assert user.role == "counsellor" and user.must_change_password

    # First sign-in forces a password change.
    staff = Client(app)
    r = staff.login("kalesi.lal", generated)
    assert "Change password" in r.get_data(as_text=True)
    assert staff.get("/counsellor").status_code == 302
    r = staff.post("/account/password", {"current_password": generated, "new_password": "password123",
                                         "confirm_password": "password123"})
    body = r.get_data(as_text=True)
    assert "mix at least three" in body or "too common" in body
    r = staff.post("/account/password", {"current_password": generated, "new_password": "NewPass#2026",
                                         "confirm_password": "NewPass#2026"})
    assert r.request.path == "/counsellor"

    # Deactivate and reactivate.
    c.post(f"/admin/staff/{user.id}/toggle_active", form_url=f"/admin/staff/{user.id}")
    assert not db.session.get(User, user.id).active
    c.post(f"/admin/staff/{user.id}/toggle_active", form_url=f"/admin/staff/{user.id}")
    assert db.session.get(User, user.id).active


def test_duplicate_course_code_blocked(app):
    c = signed_in(app, "admin")
    dept = Department.query.first()
    r = c.post("/admin/courses/new", {"code": "cs101", "name": "Dup", "department_id": dept.id})
    assert "already in use" in r.get_data(as_text=True)


def test_parent_consent_unticked_by_default(app):
    page = signed_in(app, "admin").text("/admin/students/new")
    assert re.search(r'name="parent_consent"\s*>', page)


def test_lockout_after_five_failures(app):
    c = Client(app)
    for _ in range(5):
        c.login("student2", "wrong")
    assert "Too many failed" in c.login("student2").get_data(as_text=True)
    u = User.query.filter_by(username="student2").first()
    u.locked_until = None
    db.session.commit()
    assert c.login("student2").request.path == "/student"


def test_lecturer_attendance_and_marks(app):
    c = signed_in(app, "lect_1_1")
    lecturer = User.query.filter_by(username="lect_1_1").first()
    course = Course.query.filter_by(lecturer_id=lecturer.id).first()
    assert "Assessment 1" in c.text(f"/lecturer/course/{course.id}/assessments")
    enrolments = course.enrollments
    data = {"week": "13"}
    data.update({f"present_{e.id}": "on" for e in enrolments[1:]})
    url = f"/lecturer/course/{course.id}/attendance"
    r = c.post(url, data, form_url=url + "?week=13")
    assert "Attendance saved for week 13" in r.get_data(as_text=True)
    c.post(url, data, form_url=url + "?week=13")
    assert AttendanceRecord.query.filter_by(enrollment_id=enrolments[0].id, week=13).count() == 1

    r = c.post(f"/lecturer/course/{course.id}/assessments/new",
               {"title": "Quiz Z", "assessment_type": "Quiz", "sequence": "9", "max_score": "20"})
    assert "Quiz Z" in r.get_data(as_text=True)
    d = AssessmentDefinition.query.filter_by(title="Quiz Z").first()
    data = {f"score_{a.id}": "15" for a in Assessment.query.filter_by(definition_id=d.id)}
    r = c.post(f"/lecturer/course/{course.id}/assessments/{d.id}/grade", data)
    assert "Grades saved" in r.get_data(as_text=True)


def test_counsellor_note_and_profiles(app):
    c = signed_in(app, "counsellor1")
    cid = User.query.filter_by(username="counsellor1").first().id
    sid = Student.query.filter_by(counsellor_id=cid).first().id
    c.post(f"/counsellor/student/{sid}", {"note": "Met student", "action_taken": "Tutoring"})
    assert InterventionNote.query.filter_by(note="Met student").count() == 1
    for username, text in [("lect_1_1", "Courses you teach"), ("counsellor1", "Students on caseload"),
                           ("hod_1", "Courses in department"), ("parent1", "Linked students"),
                           ("student1", "Enrolled courses")]:
        assert text in signed_in(app, username).text("/account/profile"), username


def test_profile_email_validation(app):
    c = signed_in(app, "student1")
    assert "valid email" in c.post("/account/profile", {"email": "not-an-email"}).get_data(as_text=True)
    c.post("/account/profile", {"email": "me@example.edu"})
    assert User.query.filter_by(username="student1").first().email == "me@example.edu"


def test_admin_reset_generates_password(app):
    c = signed_in(app, "admin")
    sid = Student.query.filter(Student.student_number != None).order_by(Student.id.desc()).first().id  # noqa: E711
    r = c.post(f"/admin/students/{sid}/reset_password", {"new_password": ""}, form_url=f"/admin/students/{sid}")
    page = r.get_data(as_text=True)
    assert "Share these sign-in details" in page and "password123" not in page
