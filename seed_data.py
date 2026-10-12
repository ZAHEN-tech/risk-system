import random
from models import (
    db, Department, User, Course, Student, Enrollment,
    AttendanceRecord, Assessment, AssessmentDefinition
)

FIRST_NAMES = ["Amara", "Ben", "Chen", "Dovi", "Elena", "Farida", "Grace", "Hemi",
               "Ilya", "Jina", "Kavi", "Lani", "Mateo", "Nadia", "Oscar", "Priya",
               "Ratu", "Sela", "Tomasi", "Uma", "Viliame", "Wendy", "Xavi", "Yara",
               "Zain", "Litia", "Mere", "Josua", "Ana", "Peni"]
LAST_NAMES = ["Naidu", "Tavita", "Rossi", "Kumar", "Singh", "Chen", "Volavola",
              "Osei", "Petersen", "Kim", "Ali", "Devi", "Lee", "Waqa", "Brown",
              "Sharma", "Delaibatiki", "Nunes", "Patel", "Cruz"]

WEEKS = 12
N_ASSESSMENTS = 6


def name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def make_user(username, full_name, role, department=None, email=None, password="password123"):
    u = User(
        username=username,
        full_name=full_name,
        role=role,
        department_id=department.id if department else None,
        email=email or f"{username}@campus.edu",
    )
    u.set_password(password)
    db.session.add(u)
    return u


def build_student_profile(kind, seq_len=N_ASSESSMENTS, weeks=WEEKS):
    """kind in {'strong','average','improving','declining','chronic_absentee','struggling'}"""
    attendance = []
    scores = []

    if kind == "strong":
        att_p = 0.95
        base, drift = 82, 0
    elif kind == "average":
        att_p = 0.85
        base, drift = 68, 0
    elif kind == "improving":
        att_p = 0.80
        base, drift = 45, 4.5   # climbs over the term
    elif kind == "declining":
        att_p = 0.78
        base, drift = 78, -6.5  # falls over the term
    elif kind == "chronic_absentee":
        att_p = 0.55
        base, drift = 60, -2
    elif kind == "struggling":
        att_p = 0.65
        base, drift = 38, -1.5
    elif kind == "severe":
        att_p = 0.35
        base, drift = 22, -1.0
    else:
        att_p = 0.85
        base, drift = 65, 0

    for w in range(1, weeks + 1):
        present = random.random() < att_p
        attendance.append(present)

    missed_submissions = 0
    if kind in ("chronic_absentee", "struggling"):
        missed_submissions = random.choice([1, 2])
    elif kind == "severe":
        missed_submissions = random.choice([2, 3])

    for i in range(1, seq_len + 1):
        mean = base + drift * (i - 1) / max(1, seq_len - 1) * (seq_len - 1) / 5
        # simpler: linear drift across assessments
        mean = base + drift * (i - 1)
        mean = max(5, min(98, mean))
        noise = random.gauss(0, 6)
        raw = max(0, min(100, mean + noise))
        submitted = True
        score = round(raw, 1)
        if missed_submissions > 0 and i in random.sample(range(1, seq_len + 1), missed_submissions):
            submitted = False
            score = None
        scores.append((submitted, score))

    return attendance, scores


_definitions_cache = {}


def definition_for(course, i):
    """One class-wide AssessmentDefinition per course per item number, so seeded
    marks show up on the lecturer's Assessments page like UI-created ones."""
    key = (course.id, i)
    if key not in _definitions_cache:
        d = AssessmentDefinition.query.filter_by(course_id=course.id, sequence=i).first()
        if d is None:
            d = AssessmentDefinition(course_id=course.id, title=f"Assessment {i}",
                                     assessment_type="Coursework", sequence=i, max_score=100.0)
            db.session.add(d)
            db.session.flush()
        _definitions_cache[key] = d
    return _definitions_cache[key]


def seed():
    if Department.query.first() or User.query.filter(User.role != "admin").first():
        return  # already seeded, or real records exist: never mix demo data in
    _definitions_cache.clear()

    departments = {}
    for dname in ["Computing & IT", "Business Studies", "Engineering"]:
        d = Department(name=dname)
        db.session.add(d)
        db.session.flush()
        departments[dname] = d

    # --- Staff ---
    # Reuse the administrator if one survived a "delete all" (usernames are unique).
    admin = User.query.filter_by(username="admin").first() or make_user("admin", "System Administrator", "admin")

    hods = {}
    for dname, d in departments.items():
        h = make_user(f"hod_{d.id}", f"Dr. {name()}", "hod", department=d)
        hods[dname] = h

    counsellors = [
        make_user("counsellor1", f"{name()} (Student Counsellor)", "counsellor"),
        make_user("counsellor2", f"{name()} (Student Counsellor)", "counsellor"),
    ]

    lecturers_by_dept = {}
    for dname, d in departments.items():
        lecturers_by_dept[dname] = [
            make_user(f"lect_{d.id}_{i}", f"{name()}", "lecturer", department=d)
            for i in range(1, 3)
        ]

    db.session.flush()

    # --- Courses ---
    course_catalog = {
        "Computing & IT": [("CS101", "Intro to Programming"), ("CS210", "Databases"), ("CS305", "Networks")],
        "Business Studies": [("BUS110", "Principles of Management"), ("BUS220", "Financial Accounting")],
        "Engineering": [("ENG150", "Engineering Mathematics I"), ("ENG240", "Mechanics of Materials")],
    }
    courses = []
    for dname, d in departments.items():
        for i, (code, cname) in enumerate(course_catalog[dname]):
            lect = lecturers_by_dept[dname][i % len(lecturers_by_dept[dname])]
            c = Course(code=code, name=cname, department_id=d.id, lecturer_id=lect.id)
            db.session.add(c)
            courses.append(c)
    db.session.flush()

    # --- Students ---
    profile_mix = (
        ["strong"] * 5 + ["average"] * 6 + ["improving"] * 3 +
        ["declining"] * 4 + ["chronic_absentee"] * 3 + ["struggling"] * 3 + ["severe"] * 3
    )
    random.shuffle(profile_mix)

    demo_student_username = None
    for idx, kind in enumerate(profile_mix, start=1):
        dname = random.choice(list(departments.keys()))
        d = departments[dname]
        full = name()
        username = f"student{idx}"
        parent_username = f"parent{idx}"

        s_user = make_user(username, full, "student", department=d)
        p_user = make_user(parent_username, f"{full.split()[-1]} Family Guardian", "parent",
                            email=f"{parent_username}@family.com")
        db.session.flush()

        student = Student(
            user_id=s_user.id,
            student_number=f"S{2024000+idx}",
            department_id=d.id,
            year_of_study=random.choice([1, 2, 3]),
            counsellor_id=random.choice(counsellors).id,
            parent_id=p_user.id,
            parent_consent=True if random.random() > 0.1 else False,
        )
        db.session.add(student)
        db.session.flush()

        # Enroll in 2 courses from their department
        dept_courses = [c for c in courses if c.department_id == d.id]
        chosen = random.sample(dept_courses, k=min(2, len(dept_courses)))
        for course in chosen:
            enr = Enrollment(student_id=student.id, course_id=course.id)
            db.session.add(enr)
            db.session.flush()

            attendance, scores = build_student_profile(kind)
            for w, present in enumerate(attendance, start=1):
                db.session.add(AttendanceRecord(enrollment_id=enr.id, week=w, present=present))
            for i, (submitted, score) in enumerate(scores, start=1):
                db.session.add(Assessment(
                    enrollment_id=enr.id,
                    definition_id=definition_for(course, i).id,
                    score=score,
                    submitted=submitted,
                ))

        if kind == "declining" and demo_student_username is None:
            demo_student_username = username

    db.session.commit()
    return {
        "admin": "admin",
        "hod_sample": f"hod_{list(departments.values())[0].id}",
        "counsellor_sample": "counsellor1",
        "lecturer_sample": f"lect_{list(departments.values())[0].id}_1",
        "student_sample": demo_student_username or "student1",
    }
