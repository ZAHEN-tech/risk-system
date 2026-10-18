"""Final-release fixes and features: BUG-07..10, FR-11, FR-12, FR-14, FR-15."""
import re
from datetime import date, datetime, timedelta

import pytest

import mailer
from conftest import signed_in
from models import (
    db, Assessment, AssessmentDefinition, AttendanceRecord, Course, Department, Enrollment, InterventionNote,
    Notification, RiskScore, RiskSnapshot, Student, User,
)
from risk_engine import compute_risk_for_enrollment, generate_notifications_for_enrollment


def _new_student(c, course, number="", name="Fresh Student"):
    """Registers a student through the admin form, enrolled in one course."""
    r = c.post("/admin/students/new", {"full_name": name, "student_number": number,
                                       "department_id": course.department_id, "year_of_study": 1,
                                       "course_ids": course.id}, form_url="/admin/students/new")
    return r.get_data(as_text=True)


# ---------------------------------------------------------------- BUG-07 ----

def test_blank_student_id_is_generated(app):
    c = signed_in(app, "admin")
    course = Course.query.first()
    page = _new_student(c, course, name="Blank Id One")
    m = re.search(r"student ID (S\d{4}\d{3,}) was generated", page)
    assert m, page[:2000]
    first = m.group(1)
    page = _new_student(c, course, name="Blank Id Two")
    second = re.search(r"student ID (S\d{4}\d{3,}) was generated", page).group(1)
    assert first != second and int(second[5:]) == int(first[5:]) + 1
    assert Student.query.filter_by(student_number=first).count() == 1


def test_csv_import_generates_missing_ids(app):
    import io
    c = signed_in(app, "admin")
    dept = Department.query.first()
    csv_text = (f"full_name,student_number,department\nCsv Blank A,,{dept.name}\n"
                f"Csv Given,S-CSV-GIVEN,{dept.name}\nCsv Blank B,,{dept.name}\n")
    page = c.text("/admin/students/import")
    from conftest import token
    r = c.c.post("/admin/students/import", data={"csrf_token": token(page),
                                                 "file": (io.BytesIO(csv_text.encode()), "s.csv")},
                 content_type="multipart/form-data", follow_redirects=True)
    assert "Nothing was imported" not in r.get_data(as_text=True)
    nums = [s.student_number for s in Student.query.join(User, Student.user_id == User.id)
            .filter(User.full_name.like("Csv Blank%")).all()]
    assert len(nums) == 2 and len(set(nums)) == 2 and all(n.startswith("S20") for n in nums)


# ---------------------------------------------------------------- BUG-08 ----

def test_score_above_maximum_is_rejected_and_previous_kept(app):
    c = signed_in(app, "lect_1_2")
    lecturer = User.query.filter_by(username="lect_1_2").first()
    course = Course.query.filter_by(lecturer_id=lecturer.id).first()
    d = AssessmentDefinition.query.filter_by(course_id=course.id).order_by(AssessmentDefinition.sequence).first()
    url = f"/lecturer/course/{course.id}/assessments/{d.id}/grade"
    entries = (Assessment.query.join(Enrollment).filter(Assessment.definition_id == d.id,
                                                        Enrollment.status == "active").all())
    target, other = entries[0], entries[1]
    before = target.score
    data = {f"score_{a.id}": (a.score if a.score is not None else "") for a in entries}
    data[f"score_{target.id}"] = str(d.max_score + 979)
    data[f"score_{other.id}"] = str(d.max_score)
    r = c.post(url, data, form_url=url)
    page = r.get_data(as_text=True)
    assert "outside 0 to" in page and "previous marks are unchanged" in page
    db.session.expire_all()
    assert db.session.get(Assessment, target.id).score == before
    assert db.session.get(Assessment, other.id).score == d.max_score


# ---------------------------------------------------------------- BUG-09 ----

def test_student_with_no_records_shows_insufficient_data_and_no_alerts(app):
    c = signed_in(app, "admin")
    course = Course.query.first()
    alerts_before = Notification.query.count()
    _new_student(c, course, number="S-NODATA-1", name="No Data Yet")
    s = Student.query.filter_by(student_number="S-NODATA-1").first()
    rs = s.enrollments[0].risk
    # The course already has marked items, so a new student counts as "not
    # submitted" for those: real data, so they are scored. Remove marks to test.
    Assessment.query.filter_by(enrollment_id=s.enrollments[0].id).delete()
    db.session.commit()
    rs, prev = compute_risk_for_enrollment(s.enrollments[0].id)
    generate_notifications_for_enrollment(s.enrollments[0], rs, prev)
    db.session.commit()
    assert rs.risk_band == "No data" and rs.data_quality == "insufficient" and rs.ml_band is None
    profile = c.text(f"/admin/students/{s.id}")
    assert "Insufficient data" in profile
    # A brand-new enrolment with nothing recorded raises no alert for "No data".
    assert not Notification.query.filter(Notification.student_id == s.id,
                                         Notification.message.like("%No data%")).count()


def test_unmarked_items_do_not_count_as_missed_work(app):
    """An item the lecturer created but hasn't marked yet must not lower anyone's submission rate."""
    course = Course.query.first()
    enr = course.enrollments[0]
    before, _ = compute_risk_for_enrollment(enr.id)
    rate = before.submission_rate
    d = AssessmentDefinition(course_id=course.id, title="Not marked yet", sequence=99, max_score=10)
    db.session.add(d)
    db.session.flush()
    for e in course.enrollments:
        db.session.add(Assessment(enrollment_id=e.id, definition_id=d.id, score=None, submitted=False))
    db.session.flush()
    after, _ = compute_risk_for_enrollment(enr.id)
    assert after.submission_rate == rate
    db.session.rollback()


def test_limited_data_tag(app):
    course = Course.query.first()
    enr = course.enrollments[0]
    AttendanceRecord.query.filter(AttendanceRecord.enrollment_id == enr.id, AttendanceRecord.week > 2).delete()
    db.session.flush()
    rs, _ = compute_risk_for_enrollment(enr.id)
    assert rs.data_quality == "limited"
    from app import risk_badge
    assert "limited data" in str(risk_badge(rs))
    db.session.rollback()


# ---------------------------------------------------------------- BUG-10 ----

def test_notification_times_shown_in_fiji_time(app):
    c = signed_in(app, "counsellor1")
    user = User.query.filter_by(username="counsellor1").first()
    n = Notification(recipient_id=user.id, student_id=Student.query.first().id, message="Time check",
                     created_at=datetime(2026, 10, 12, 23, 30))   # 23:30 UTC = 11:30 next day in Fiji
    db.session.add(n)
    db.session.commit()
    page = c.text("/notifications")
    assert "13 Oct 2026, 11:30" in page and "12 Oct 2026, 23:30" not in page


# ---------------------------------------------------------------- FR-11 ----

def test_risk_history_records_changes_only(app):
    lecturer = User.query.filter_by(username="lect_1_1").first()
    course = Course.query.filter_by(lecturer_id=lecturer.id).first()
    enr = course.enrollments[0]
    compute_risk_for_enrollment(enr.id)
    db.session.commit()
    n1 = RiskSnapshot.query.filter_by(enrollment_id=enr.id).count()
    compute_risk_for_enrollment(enr.id)   # nothing changed
    db.session.commit()
    assert RiskSnapshot.query.filter_by(enrollment_id=enr.id).count() == n1
    week = (db.session.query(db.func.max(AttendanceRecord.week)).filter_by(enrollment_id=enr.id).scalar() or 0) + 1
    db.session.add(AttendanceRecord(enrollment_id=enr.id, week=week, present=False))
    db.session.flush()
    compute_risk_for_enrollment(enr.id)
    db.session.commit()
    assert RiskSnapshot.query.filter_by(enrollment_id=enr.id).count() == n1 + 1
    # The student sees a risk-over-time chart.
    c = signed_in(app, enr.student.user.username)
    assert "Your risk score over time" in c.text("/student")


# ---------------------------------------------------------------- FR-12 ----

def test_dashboards_auto_refresh_when_data_changes(app):
    c = signed_in(app, "lect_1_1")
    page = c.text("/lecturer")
    m = re.search(r'data-refresh-token="([^"]+)"', page)
    assert m and 'data-autorefresh="60"' in page
    assert c.get("/api/refresh-state").get_json()["token"] == m.group(1)
    lecturer = User.query.filter_by(username="lect_1_1").first()
    course = Course.query.filter_by(lecturer_id=lecturer.id).first()
    db.session.add(Notification(recipient_id=lecturer.id, student_id=course.enrollments[0].student_id, message="new"))
    db.session.commit()
    assert c.get("/api/refresh-state").get_json()["token"] != m.group(1)
    assert "data-autorefresh" not in c.text(f"/lecturer/course/{course.id}/attendance")


def test_refresh_state_needs_sign_in(client):
    assert client.get("/api/refresh-state").status_code in (302, 401)


# ---------------------------------------------------------------- FR-15 ----

def test_intervention_status_and_follow_up(app):
    counsellor = User.query.filter_by(username="counsellor1").first()
    student = Student.query.filter_by(counsellor_id=counsellor.id).first()
    c = signed_in(app, "counsellor1")
    case = f"/counsellor/student/{student.id}"
    soon = (datetime.utcnow() + timedelta(hours=12)).date() + timedelta(days=2)
    r = c.post(case, {"note": "Follow-up check FR15", "action_taken": "Study plan", "status": "Follow-up planned",
                      "follow_up_date": soon.isoformat()}, form_url=case)
    assert "Intervention note logged" in r.get_data(as_text=True)
    note = InterventionNote.query.filter_by(student_id=student.id, note="Follow-up check FR15").first()
    assert note.status == "Follow-up planned" and note.follow_up_date == soon
    assert "Follow-ups due" in c.text("/counsellor")

    r = c.post(case, {"note": "Past", "status": "Open", "follow_up_date": "2020-01-01"}, form_url=case)
    assert "can&#39;t be in the past" in r.get_data(as_text=True) or "can't be in the past" in r.get_data(as_text=True)

    r = c.post(f"/counsellor/note/{note.id}/status", {"status": "Resolved", "follow_up_date": ""}, form_url=case)
    assert "Updated the note to Resolved" in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(InterventionNote, note.id).status == "Resolved"

    csv_text = c.get("/reports/intervention.csv").get_data(as_text=True)
    assert "status" in csv_text.splitlines()[0] and "Resolved" in csv_text

    other = signed_in(app, "counsellor2")
    assert other.post(f"/counsellor/note/{note.id}/status", {"status": "Open"}, form_url="/counsellor").status_code == 403


# ---------------------------------------------------------------- FR-14 ----

@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("MAIL_FROM", "UniAlert <alerts@unialert.test>")
    monkeypatch.setenv("MAIL_SYNC", "true")
    monkeypatch.setattr(mailer, "TRANSPORT", lambda payload: (sent.append(payload) or True, "ok"))
    return sent


def test_alert_emails_one_per_person_after_save(app, mail):
    lecturer = User.query.filter_by(username="lect_1_1").first()
    lecturer.email = "lecturer@unialert.test"
    course = Course.query.filter_by(lecturer_id=lecturer.id).first()
    counsellor = User.query.filter_by(username="counsellor1").first()
    counsellor.email = "counsellor@unialert.test"
    db.session.commit()
    enrs = course.enrollments[:2]
    for e in enrs:
        e.student.counsellor_id = counsellor.id
        rs = RiskScore.query.filter_by(enrollment_id=e.id).first()
        rs.risk_band = "Low"
    db.session.commit()
    mail.clear()
    for e in enrs:
        rs = RiskScore.query.filter_by(enrollment_id=e.id).first()
        rs.risk_score, rs.risk_band, rs.attendance_pct = 70.0, "High", 40.0
        generate_notifications_for_enrollment(e, rs, "Low")
    assert mail == [], "nothing sent before the save is committed"
    db.session.commit()
    to = sorted(p["to"][0] for p in mail)
    assert to == ["counsellor@unialert.test", "lecturer@unialert.test"]
    lect_mail = next(p for p in mail if p["to"][0] == "lecturer@unialert.test")
    assert "2 students need attention" in lect_mail["subject"] and "HIGH RISK" in lect_mail["text"]


def test_no_email_when_save_rolls_back(app, mail):
    lecturer = User.query.filter_by(username="lect_1_1").first()
    lecturer.email = "lecturer@unialert.test"
    db.session.commit()
    mail.clear()
    e = Course.query.filter_by(lecturer_id=lecturer.id).first().enrollments[0]
    rs = RiskScore.query.filter_by(enrollment_id=e.id).first()
    rs.risk_band, rs.attendance_pct = "High", 40.0
    generate_notifications_for_enrollment(e, rs, "Low")
    db.session.rollback()
    db.session.commit()
    assert mail == []


def test_admin_test_email(app, mail):
    admin = User.query.filter_by(username="admin").first()
    admin.email = "admin@unialert.test"
    db.session.commit()
    c = signed_in(app, "admin")
    assert "Email alerts" in c.text("/admin") and "Send test email" in c.text("/admin")
    r = c.post("/admin/email/test", {}, form_url="/admin")
    assert "Test email sent to admin@unialert.test" in r.get_data(as_text=True)
    assert mail[-1]["to"] == ["admin@unialert.test"] and mail[-1]["subject"] == "UniAlert: test email"


def test_email_off_without_settings(app, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    c = signed_in(app, "admin")
    page = c.text("/admin")
    assert "Alerts appear in the app only" in page
    r = c.post("/admin/email/test", {}, form_url="/admin")
    assert "Email is not set up yet" in r.get_data(as_text=True)
