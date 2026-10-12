"""Admin rename and permanent delete: allowed only when no history would be lost."""
from conftest import signed_in
from models import (
    db, ActivityLog, AssessmentDefinition, Course, Department, Enrollment, Notification,
    Student, User,
)


def _user(username, role, dept=None, **kw):
    u = User(username=username, full_name=kw.pop("full_name", username.title()), role=role,
             department_id=dept.id if dept else None, active=True, **kw)
    u.set_password("Temp-pass-12345")
    db.session.add(u)
    db.session.flush()
    return u


def test_department_rename_and_delete_rules(app):
    c = signed_in(app, "admin")
    busy = Department.query.join(Course).first()
    r = c.post(f"/admin/departments/{busy.id}/delete", {}, form_url="/admin/departments")
    assert "be deleted:" in r.get_data(as_text=True)
    assert db.session.get(Department, busy.id) is not None

    empty = Department(name="School of Mistakes")
    db.session.add(empty)
    db.session.commit()
    r = c.post(f"/admin/departments/{empty.id}/rename", {"name": busy.name}, form_url="/admin/departments")
    assert "already exists" in r.get_data(as_text=True)
    r = c.post(f"/admin/departments/{empty.id}/rename", {"name": "School of Typos"}, form_url="/admin/departments")
    assert "Renamed School of Mistakes to School of Typos" in r.get_data(as_text=True)

    r = c.post(f"/admin/departments/{empty.id}/delete", {}, form_url="/admin/departments")
    assert "Deleted the department School of Typos" in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Department, empty.id) is None
    assert ActivityLog.query.filter_by(action="department.delete").count() == 1


def test_course_delete_only_without_enrolments(app):
    c = signed_in(app, "admin")
    busy = Course.query.join(Enrollment).first()
    page = c.text(f"/admin/courses/{busy.id}/edit")
    assert "can't be deleted" in page
    r = c.post(f"/admin/courses/{busy.id}/delete", {}, form_url=f"/admin/courses/{busy.id}/edit")
    assert db.session.get(Course, busy.id) is not None

    spare = Course(code="ZZ999", name="Created by mistake", department_id=busy.department_id)
    db.session.add(spare)
    db.session.flush()
    db.session.add(AssessmentDefinition(course_id=spare.id, title="Quiz 1", sequence=1, max_score=10))
    db.session.commit()
    edit = f"/admin/courses/{spare.id}/edit"
    assert "Delete this course" in c.text(edit)
    r = c.post(f"/admin/courses/{spare.id}/delete", {}, form_url=edit)
    assert "Deleted the course ZZ999" in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Course, spare.id) is None
    assert AssessmentDefinition.query.filter_by(course_id=spare.id).count() == 0


def test_lecturer_with_courses_or_work_cannot_be_deleted(app):
    c = signed_in(app, "admin")
    teaching = User.query.filter_by(username="lect_1_1").first()
    page = c.text(f"/admin/lecturers/{teaching.id}")
    assert "can't be deleted" in page and "Deactivate the account instead" in page
    c.post(f"/admin/lecturers/{teaching.id}/delete", {}, form_url=f"/admin/lecturers/{teaching.id}")
    assert db.session.get(User, teaching.id) is not None

    # A spare lecturer who has signed in (sign-ins alone don't block) ...
    spare = _user("lect_spare", "lecturer", teaching.department, full_name="Spare Lecturer")
    db.session.commit()
    signed_in(app, "lect_spare", "Temp-pass-12345")
    assert ActivityLog.query.filter_by(user_id=spare.id, action="login").count() == 1
    # ... but recorded work does.
    db.session.add(ActivityLog(user_id=spare.id, action="attendance.save", detail="test"))
    db.session.commit()
    c.post(f"/admin/lecturers/{spare.id}/delete", {}, form_url=f"/admin/lecturers/{spare.id}")
    assert db.session.get(User, spare.id) is not None

    ActivityLog.query.filter_by(user_id=spare.id, action="attendance.save").delete()
    db.session.commit()
    r = c.post(f"/admin/lecturers/{spare.id}/delete", {}, form_url=f"/admin/lecturers/{spare.id}")
    assert "Deleted the account for Spare Lecturer" in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(User, spare.id) is None
    login = ActivityLog.query.filter(ActivityLog.detail.like("[Deleted account Spare Lecturer%")).first()
    assert login is not None and login.user_id is None and login.action == "login"
    assert "Deleted account" in c.text("/admin/activity")


def test_counsellor_with_caseload_cannot_be_deleted_but_spare_hod_can(app):
    c = signed_in(app, "admin")
    counsellor = User.query.filter_by(username="counsellor1").first()
    assert Student.query.filter_by(counsellor_id=counsellor.id).count() > 0
    c.post(f"/admin/staff/{counsellor.id}/delete", {}, form_url=f"/admin/staff/{counsellor.id}")
    assert db.session.get(User, counsellor.id) is not None

    hod = _user("hod_spare", "hod", full_name="Spare Head")
    db.session.add(Notification(recipient_id=hod.id, student_id=Student.query.first().id, message="x"))
    db.session.commit()
    profile = f"/admin/staff/{hod.id}"
    assert "Delete this head of department account" in c.text(profile)
    r = c.post(f"{profile}/delete", {}, form_url=profile)
    assert "Deleted the account for Spare Head" in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(User, hod.id) is None
    assert Notification.query.filter_by(recipient_id=hod.id).count() == 0
    assert ActivityLog.query.filter_by(action="account.delete").filter(ActivityLog.detail.like("%hod_spare%")).count() == 1


def test_student_delete_rules_and_parent_cleanup(app):
    c = signed_in(app, "admin")
    with_history = Student.query.join(Enrollment).first()
    page = c.text(f"/admin/students/{with_history.id}")
    assert "can't be deleted" in page and "attendance record" in page
    c.post(f"/admin/students/{with_history.id}/delete", {}, form_url=f"/admin/students/{with_history.id}")
    assert db.session.get(Student, with_history.id) is not None

    # New student, enrolled but no attendance or marks yet, with their own parent.
    dept = with_history.department
    su = _user("stu_mistake", "student", dept, full_name="Mistaken Student")
    pu = _user("par_mistake", "parent", full_name="Mistaken Parent")
    s = Student(user_id=su.id, student_number="S-MISTAKE", department_id=dept.id, parent_id=pu.id)
    db.session.add(s)
    db.session.commit()
    course = Course.query.filter_by(department_id=dept.id).first()
    c.post(f"/admin/students/{s.id}/enroll", {"course_id": course.id}, form_url=f"/admin/students/{s.id}")
    assert Enrollment.query.filter_by(student_id=s.id).count() == 1

    profile = f"/admin/students/{s.id}"
    assert "Delete this student" in c.text(profile)
    r = c.post(f"{profile}/delete", {}, form_url=profile)
    assert "Deleted Mistaken Student" in r.get_data(as_text=True)
    assert "parent account (Mistaken Parent)" in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Student, s.id) is None
    assert User.query.filter(User.username.in_(["stu_mistake", "par_mistake"])).count() == 0
    assert Enrollment.query.filter_by(student_id=s.id).count() == 0
    assert Notification.query.filter_by(student_id=s.id).count() == 0


def test_parent_with_other_children_is_kept(app):
    c = signed_in(app, "admin")
    sibling = Student.query.filter(Student.parent_id.isnot(None)).first()
    parent_id = sibling.parent_id
    su = _user("stu_twin", "student", sibling.department, full_name="Twin Student")
    s = Student(user_id=su.id, student_number="S-TWIN", department_id=sibling.department_id, parent_id=parent_id)
    db.session.add(s)
    db.session.commit()
    r = c.post(f"/admin/students/{s.id}/delete", {}, form_url=f"/admin/students/{s.id}")
    assert "Deleted Twin Student" in r.get_data(as_text=True)
    assert "Their parent account" not in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(User, parent_id) is not None


def test_only_admin_can_delete(app):
    c = signed_in(app, "lect_1_2")
    dept = Department.query.first()
    r = c.post(f"/admin/departments/{dept.id}/delete", {}, form_url="/lecturer")
    assert r.status_code == 403
