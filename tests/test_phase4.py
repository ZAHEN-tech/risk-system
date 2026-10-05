"""Roster management, withdrawal, CSV import, activity log, paging and demo tools."""
import base64
import io
import re

from conftest import signed_in
from models import db, ActivityLog, Course, Enrollment, RiskScore, Student, User, Department


def _lecturer_course():
    lecturer = User.query.filter_by(username="lect_1_2").first()
    return lecturer, Course.query.filter_by(lecturer_id=lecturer.id).first()


def test_lecturer_withdraw_and_restore(app):
    lecturer, course = _lecturer_course()
    c = signed_in(app, "lect_1_2")
    roster = f"/lecturer/course/{course.id}/roster"
    assert c.get(roster).status_code == 200
    enr = course.enrollments[0]
    name = enr.student.user.full_name
    active_before = len(course.enrollments)

    r = c.post(f"{roster}/{enr.id}/withdraw", {"reason": "Never attended", "note": "No show weeks 1-3"}, form_url=roster)
    assert "withdrawn from" in r.get_data(as_text=True)
    db.session.expire_all()
    enr = db.session.get(Enrollment, enr.id)
    assert enr.status == "withdrawn" and enr.withdrawn_reason.startswith("Never attended")
    assert enr.withdrawn_by_id == lecturer.id
    assert RiskScore.query.filter_by(enrollment_id=enr.id).count() == 0
    assert len(db.session.get(Course, course.id).enrollments) == active_before - 1
    assert enr.attendance_records, "attendance must be kept"

    # Gone from the attendance sheet and the lecturer dashboard table.
    sheet = c.text(f"/lecturer/course/{course.id}/attendance")
    assert f'present_{enr.id}"' not in sheet

    r = c.post(f"{roster}/{enr.id}/restore", {}, form_url=roster)
    assert "back on the" in r.get_data(as_text=True)
    db.session.expire_all()
    enr = db.session.get(Enrollment, enr.id)
    assert enr.status == "active" and enr.risk is not None
    assert name in c.text(roster)


def test_lecturer_adds_students_by_number(app):
    lecturer, course = _lecturer_course()
    outsider = (Student.query.filter(~Student.id.in_(
        db.session.query(Enrollment.student_id).filter(Enrollment.course_id == course.id))).first())
    c = signed_in(app, "lect_1_2")
    roster = f"/lecturer/course/{course.id}/roster"
    already = course.enrollments[0].student.student_number
    r = c.post(f"{roster}/add", {"student_numbers": f"{outsider.student_number.lower()}, {already}\nNOPE999"}, form_url=roster)
    page = r.get_data(as_text=True)
    assert "added 1" in page and "Already in" in page and "NOPE999" in page
    enr = Enrollment.query.filter_by(student_id=outsider.id, course_id=course.id).first()
    assert enr.status == "active" and enr.risk is not None


def test_lecturer_cannot_touch_other_courses(app):
    _, course = _lecturer_course()
    other = signed_in(app, "lect_1_1")
    assert other.get(f"/lecturer/course/{course.id}/roster").status_code == 403


def test_withdrawn_enrolments_left_out_of_reports_and_recompute(app):
    _, course = _lecturer_course()
    c = signed_in(app, "lect_1_2")
    roster = f"/lecturer/course/{course.id}/roster"
    enr = course.enrollments[-1]
    number = enr.student.student_number
    c.post(f"{roster}/{enr.id}/withdraw", {"reason": "Transferred to another course"}, form_url=roster)
    csv_text = c.text("/reports/risk.csv")
    rows_for = [line for line in csv_text.splitlines() if number in line and course.code in line]
    assert not rows_for
    admin = signed_in(app, "admin")
    admin.post("/admin/recompute", {}, form_url="/admin")
    assert RiskScore.query.filter_by(enrollment_id=enr.id).count() == 0
    # Admin sees it as withdrawn and can restore it from the student profile.
    page = admin.text(f"/admin/students/{enr.student_id}")
    assert "Withdrawn" in page and "Transferred" in page
    admin.post(f"/admin/students/{enr.student_id}/enroll", {"course_id": course.id}, form_url=f"/admin/students/{enr.student_id}")
    db.session.expire_all()
    assert db.session.get(Enrollment, enr.id).status == "active"


def _upload(c, text, name="students.csv"):
    return c.post("/admin/students/import",
                  {"file": (io.BytesIO(text.encode("utf-8")), name)},
                  content_type="multipart/form-data")


def test_csv_import_all_or_nothing(app):
    c = signed_in(app, "admin")
    dept = Department.query.first()
    code = Course.query.filter_by(department_id=dept.id).first().code
    existing = Student.query.first().student_number
    bad = ("full_name,student_number,department,year_of_study,course_codes,parent_consent\n"
           f"Ana Good,IMP001,{dept.name},2,{code},no\n"
           f"Ben Bad,{existing},Nowhere,9,ZZZ999,maybe\n")
    page = _upload(c, bad).get_data(as_text=True)
    assert "Nothing was imported" in page and "Line 3" in page
    assert "already exists" in page and "no department" in page and "no course with code ZZZ999" in page
    assert Student.query.filter_by(student_number="IMP001").count() == 0


def test_csv_import_creates_students_parents_and_enrolments(app):
    c = signed_in(app, "admin")
    dept = Department.query.first()
    codes = [x.code for x in Course.query.filter_by(department_id=dept.id).limit(2)]
    counsellor = User.query.filter_by(role="counsellor", active=True).first().username
    good = ("﻿full_name,student_number,department,year_of_study,email,course_codes,counsellor_username,parent_name,parent_email,parent_consent\n"
            f"Mere Tui,IMP101,{dept.name.upper()},1,mere@example.edu,{';'.join(codes)},{counsellor},Sina Tui,sina@example.com,yes\n"
            f"Raj Prasad,IMP102,{dept.name},3,,{codes[0]},,,,\n")
    r = _upload(c, good)
    page = r.get_data(as_text=True)
    assert "Imported 2 students" in page and "Download sign-in details" in page
    m = re.search(r'href="data:text/csv;charset=utf-8;base64,([^"]+)"', page)
    creds = base64.b64decode(m.group(1)).decode()
    assert creds.count("\n") == 4  # header + 2 students + 1 parent
    s = Student.query.filter_by(student_number="IMP101").first()
    assert s.parent_consent and s.parent.full_name == "Sina Tui" and s.counsellor.username == counsellor
    assert s.user.must_change_password and len(s.enrollments) == 2 and all(e.risk for e in s.enrollments)
    assert ActivityLog.query.filter_by(action="import.students").count() >= 1
    # The template downloads.
    assert c.get("/admin/students/import/template.csv").status_code == 200


def test_activity_log_records_and_filters(app):
    signed_in(app, "hod_1")
    c = signed_in(app, "admin")
    page = c.text("/admin/activity?action=login")
    assert "Signed in" in page and "hod_1" in page
    assert "password123" not in c.text("/admin/activity")


def test_student_list_is_paged_and_band_filter_works(app):
    c = signed_in(app, "admin")
    page = c.text("/admin/students")
    assert "Showing 1" in page
    high = c.text("/admin/students?band=High")
    for match in re.findall(r'class="badge (risk-[a-z]+)"', high):
        assert match == "risk-high"


def test_demo_tools_hidden_and_blocked_with_real_database(app):
    c = signed_in(app, "admin")
    assert "Seed demo dataset" not in c.text("/admin")
    assert c.post("/admin/seed", {}, form_url="/admin", follow=False).status_code == 404
    assert c.post("/admin/reset", {}, form_url="/admin", follow=False).status_code == 404
