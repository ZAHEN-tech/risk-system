"""Milestone 7 additions from lecturer feedback: QR / code check-in attendance
and the early warning when weeks pass with no marked assessment."""
from datetime import datetime, timedelta

from conftest import signed_in, DEMO_PASSWORD
from models import (db, AttendanceRecord, AttendanceSession, Assessment, AssessmentDefinition,
                    Course, Department, Enrollment, Student, User)
from risk_engine import assessment_gap

PW = "Check-In-Pass-2026"


def _setup(tag):
    dept = Department.query.first()
    lect = User(username=f"lect_{tag}", full_name=f"Lecturer {tag}", role="lecturer", department_id=dept.id)
    lect.set_password(PW)
    db.session.add(lect)
    db.session.flush()
    course = Course(code=f"QR{tag}", name=f"Check-in course {tag}", department_id=dept.id, lecturer_id=lect.id)
    db.session.add(course)
    db.session.flush()
    studs = []
    for i in range(3):
        u = User(username=f"st_{tag}_{i}", full_name=f"Student {tag} {i}", role="student", department_id=dept.id)
        u.set_password(PW)
        db.session.add(u)
        db.session.flush()
        s = Student(user_id=u.id, student_number=f"S-{tag}-{i}", department_id=dept.id)
        db.session.add(s)
        db.session.flush()
        db.session.add(Enrollment(student_id=s.id, course_id=course.id))
        studs.append(u)
    db.session.commit()
    return lect, course, studs


def test_qr_checkin_marks_present_then_close_marks_rest_absent(app):
    lect, course, studs = _setup("A1")
    lc = signed_in(app, lect.username, PW)
    r = lc.post(f"/lecturer/course/{course.id}/checkin/start", {"week": 2, "minutes": 15},
                form_url=f"/lecturer/course/{course.id}/attendance")
    page = r.get_data(as_text=True)
    assert "<svg" in page and "Scan to check in" in page
    s = AttendanceSession.query.filter_by(course_id=course.id, week=2).one()
    assert len(s.code) == 6

    sc = signed_in(app, studs[0].username, PW)
    page = sc.text(f"/checkin?code={s.code.lower()}")
    assert course.code in page and "check me in" in page
    r = sc.post("/checkin", {"code": s.code}, form_url=f"/checkin?code={s.code}")
    assert "checked in: " in r.get_data(as_text=True)
    # a second scan does not double count
    r = sc.post("/checkin", {"code": s.code}, form_url=f"/checkin?code={s.code}")
    assert "already checked in" in r.get_data(as_text=True)

    r = lc.post(f"/lecturer/checkin/{s.id}/close", {}, form_url=f"/lecturer/checkin/{s.id}")
    assert "1 present, 2 marked absent" in r.get_data(as_text=True)
    recs = {rec.enrollment.student.user.username: rec.present for rec in
            AttendanceRecord.query.join(Enrollment).filter(Enrollment.course_id == course.id, AttendanceRecord.week == 2)}
    assert recs == {studs[0].username: True, studs[1].username: False, studs[2].username: False}
    assert db.session.get(AttendanceSession, s.id).closed_at is not None


def test_checkin_refused_when_expired_wrong_code_or_not_enrolled(app):
    lect, course, studs = _setup("B2")
    s = AttendanceSession(course_id=course.id, week=1, code="EXPRD2", created_by_id=lect.id,
                          opened_at=datetime.utcnow() - timedelta(minutes=30),
                          expires_at=datetime.utcnow() - timedelta(minutes=15))
    db.session.add(s)
    db.session.commit()
    sc = signed_in(app, studs[0].username, PW)
    assert "has closed" in sc.text("/checkin?code=EXPRD2")
    assert "not recognised" in sc.text("/checkin?code=ZZZZZZ")
    # a student from another course
    _, other, others = _setup("B3")
    s2 = AttendanceSession(course_id=other.id, week=1, code="OTHER3", created_by_id=lect.id,
                           expires_at=datetime.utcnow() + timedelta(minutes=10))
    db.session.add(s2)
    db.session.commit()
    assert "not enrolled" in sc.text("/checkin?code=OTHER3")
    assert AttendanceRecord.query.join(Enrollment).filter(Enrollment.course_id == other.id).count() == 0


def test_only_students_check_in_and_only_owner_sees_session(app):
    lect, course, studs = _setup("C4")
    s = AttendanceSession(course_id=course.id, week=1, code="OWNER4", created_by_id=lect.id,
                          expires_at=datetime.utcnow() + timedelta(minutes=10))
    db.session.add(s)
    db.session.commit()
    other_lect = signed_in(app, "lect_1_2")
    assert other_lect.get(f"/lecturer/checkin/{s.id}").status_code in (403, 404)
    assert other_lect.get("/checkin?code=OWNER4").status_code == 403


def test_assessment_gap_flagged_after_three_weeks_and_cleared_by_marks(app):
    lect, course, studs = _setup("D5")
    enr = Enrollment.query.filter_by(course_id=course.id).all()
    for week in (1, 2):
        for e in enr:
            db.session.add(AttendanceRecord(enrollment_id=e.id, week=week, present=True))
    db.session.commit()
    assert not assessment_gap(course)["flagged"]
    for e in enr:
        db.session.add(AttendanceRecord(enrollment_id=e.id, week=3, present=True))
    db.session.commit()
    g = assessment_gap(course)
    assert g["flagged"] and g["weeks"] == 3 and g["marked"] == 0
    lc = signed_in(app, lect.username, PW)
    assert "no marked assessment yet" in lc.text("/lecturer")
    assert course.code in signed_in(app, "admin").text("/admin")

    d = AssessmentDefinition(course_id=course.id, title="Quiz 1", sequence=1, max_score=10)
    db.session.add(d)
    db.session.flush()
    db.session.add(Assessment(enrollment_id=enr[0].id, definition_id=d.id, score=7, submitted=True))
    db.session.commit()
    assert not assessment_gap(course)["flagged"]
    assert "no marked assessment yet" not in lc.text("/lecturer")


def test_login_returns_student_to_checkin(app):
    lect, course, studs = _setup("E6")
    s = AttendanceSession(course_id=course.id, week=1, code="NEXTE6", created_by_id=lect.id,
                          expires_at=datetime.utcnow() + timedelta(minutes=10))
    db.session.add(s)
    db.session.commit()
    from conftest import Client, token
    c = Client(app)
    t = token(c.text("/login?next=/checkin?code=NEXTE6"))
    r = c.c.post("/login?next=/checkin%3Fcode%3DNEXTE6",
                 data={"username": studs[1].username, "password": PW, "csrf_token": t}, follow_redirects=True)
    assert "check me in" in r.get_data(as_text=True)
