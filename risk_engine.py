"""
Core predictive logic.

The scoring model is a transparent, weighted rule-based composite score
(0-100, higher = higher risk). This is intentionally explainable rather than
a black-box classifier, because in an academic-intervention setting staff,
students and parents need to understand *why* a student was flagged.

    risk_score = 0.35 * (100 - attendance_pct)
               + 0.35 * (100 - assessment_avg)
               + 0.20 * (100 - submission_rate)
               + 0.10 * trend_penalty

Bands:
    risk_score >= 60   -> High
    30 <= score < 60   -> Medium
    score < 30         -> Low

The weights and thresholds live in one place (WEIGHTS / BANDS below) so an
institution can recalibrate them against its own historical dropout data
without touching the rest of the system.

An alternative, data-driven score is also computed for every enrollment by
ml_engine.py (a logistic regression trained on historical outcomes). Both
scores are stored on RiskScore for side-by-side comparison. The rule-based
score remains the one that drives notifications below — the ML score runs
in "shadow mode" alongside it, which is the standard way to validate a new
model against an existing process before trusting it to trigger real alerts
on its own. See risk_engine.model_agreement_summary() and the admin
"Predictive model" page for how closely the two currently agree.
"""
from datetime import datetime
from models import (
    db, Enrollment, AttendanceRecord, Assessment, AssessmentDefinition, RiskScore,
    RiskSnapshot, Notification, Student, Course, Department, User
)
import ml_engine
import mailer

WEIGHTS = {
    "attendance": 0.35,
    "assessment": 0.35,
    "submission": 0.20,
    "trend": 0.10,
}

BAND_HIGH = 60
BAND_MEDIUM = 30

NOTIFY_ON_BAND = {"High", "Medium"}  # bands that trigger notifications
NO_DATA = "No data"                    # band when there is nothing to score yet (FR-10)
LIMITED_WEEKS = 3                      # fewer recorded weeks than this = "limited data"
PARENT_NOTIFY_BAND = {"High"}         # only High risk reaches parents (and only with consent)


def _attendance_pct(enrollment_id):
    records = AttendanceRecord.query.filter_by(enrollment_id=enrollment_id).all()
    if not records:
        return 100.0
    present = sum(1 for r in records if r.present)
    return round(100.0 * present / len(records), 1)


def _marked_definition_ids(enrollment_id):
    """Assessment items of this enrolment's course that the lecturer has started
    marking (at least one student has a mark). Items that exist but have not been
    marked yet are not counted as "not submitted"."""
    enr = db.session.get(Enrollment, enrollment_id)
    if enr is None:
        return set()
    rows = (db.session.query(Assessment.definition_id)
            .join(AssessmentDefinition, Assessment.definition_id == AssessmentDefinition.id)
            .filter(AssessmentDefinition.course_id == enr.course_id,
                    db.or_(Assessment.submitted.is_(True), Assessment.score.isnot(None)))
            .distinct().all())
    return {r[0] for r in rows}


def _assessment_stats(enrollment_id):
    marked = _marked_definition_ids(enrollment_id)
    assessments = [a for a in (
        Assessment.query.filter_by(enrollment_id=enrollment_id)
        .join(AssessmentDefinition, Assessment.definition_id == AssessmentDefinition.id)
        .order_by(AssessmentDefinition.sequence).all()) if a.definition_id in marked]
    if not assessments:
        return 100.0, 100.0, 0.0

    scored = [a for a in assessments if a.submitted and a.score is not None]
    submission_rate = round(100.0 * sum(1 for a in assessments if a.submitted) / len(assessments), 1)

    if not scored:
        avg = 0.0
    else:
        avg = round(sum(100.0 * a.score / a.max_score for a in scored) / len(scored), 1)

    # Trend: compare the average of the most recent third against the first third.
    trend_penalty = 0.0
    if len(scored) >= 3:
        n = len(scored)
        split = max(1, n // 3)
        early = scored[:split]
        recent = scored[-split:]
        early_avg = sum(100.0 * a.score / a.max_score for a in early) / len(early)
        recent_avg = sum(100.0 * a.score / a.max_score for a in recent) / len(recent)
        drop = early_avg - recent_avg
        if drop > 0:
            # scale the drop in percentage points onto a 0-100 penalty
            trend_penalty = min(100.0, drop * 2.5)

    return avg, submission_rate, round(trend_penalty, 1)


GAP_WEEKS = 3   # weeks of attendance with nothing marked before a course is flagged


def assessment_gap(course):
    """Early-warning check for a course (lecturer feedback, final presentation):
    once GAP_WEEKS teaching weeks of attendance are recorded but no assessment
    has been marked, risk scores rest on attendance alone, so a student who is
    struggling academically cannot be seen yet. Returns a dict with weeks
    recorded, items marked and whether the course is flagged."""
    weeks = {w for (w,) in db.session.query(AttendanceRecord.week)
             .join(Enrollment, AttendanceRecord.enrollment_id == Enrollment.id)
             .filter(Enrollment.course_id == course.id).distinct().all()}
    marked = (db.session.query(AssessmentDefinition.id)
              .join(Assessment, Assessment.definition_id == AssessmentDefinition.id)
              .filter(AssessmentDefinition.course_id == course.id,
                      db.or_(Assessment.submitted.is_(True), Assessment.score.isnot(None)))
              .distinct().count())
    latest = max(weeks) if weeks else 0
    return {"course": course, "weeks": len(weeks), "latest_week": latest, "marked": marked,
            "flagged": len(weeks) >= GAP_WEEKS and marked == 0}


def assessment_gaps(courses):
    """Flagged courses only, for dashboards."""
    return [g for g in (assessment_gap(c) for c in courses) if g["flagged"]]


def data_quality(enrollment_id):
    """(quality, weeks recorded, items marked) for an enrolment."""
    weeks = AttendanceRecord.query.filter_by(enrollment_id=enrollment_id).count()
    marked = _marked_definition_ids(enrollment_id)
    items = (Assessment.query.filter(Assessment.enrollment_id == enrollment_id,
                                     Assessment.definition_id.in_(marked)).count() if marked else 0)
    if weeks == 0 and items == 0:
        return "insufficient", weeks, items
    if weeks < LIMITED_WEEKS or items == 0:
        return "limited", weeks, items
    return "good", weeks, items


def _snapshot(rs):
    """Adds a history row when the score or band differs from the last one (FR-11)."""
    last = (RiskSnapshot.query.filter_by(enrollment_id=rs.enrollment_id)
            .order_by(RiskSnapshot.taken_at.desc(), RiskSnapshot.id.desc()).first())
    if last is not None and last.risk_band == rs.risk_band and last.risk_score == rs.risk_score:
        return
    db.session.add(RiskSnapshot(enrollment_id=rs.enrollment_id, taken_at=datetime.utcnow(),
                                risk_score=rs.risk_score, risk_band=rs.risk_band,
                                attendance_pct=rs.attendance_pct, assessment_avg=rs.assessment_avg,
                                submission_rate=rs.submission_rate))


def compute_risk_for_enrollment(enrollment_id):
    quality, _weeks, _items = data_quality(enrollment_id)
    attendance_pct = _attendance_pct(enrollment_id)
    assessment_avg, submission_rate, trend_penalty = _assessment_stats(enrollment_id)

    score = (
        WEIGHTS["attendance"] * (100 - attendance_pct)
        + WEIGHTS["assessment"] * (100 - assessment_avg)
        + WEIGHTS["submission"] * (100 - submission_rate)
        + WEIGHTS["trend"] * trend_penalty
    )
    score = round(min(100.0, max(0.0, score)), 1)

    if quality == "insufficient":
        score, band = 0.0, NO_DATA
    elif score >= BAND_HIGH:
        band = "High"
    elif score >= BAND_MEDIUM:
        band = "Medium"
    else:
        band = "Low"

    rs = RiskScore.query.filter_by(enrollment_id=enrollment_id).first()
    if rs is None:
        rs = RiskScore(enrollment_id=enrollment_id)
        db.session.add(rs)

    prev_band = rs.risk_band

    rs.attendance_pct = attendance_pct
    rs.assessment_avg = assessment_avg
    rs.submission_rate = submission_rate
    rs.trend_penalty = trend_penalty
    rs.risk_score = score
    rs.risk_band = band

    if quality == "insufficient":
        rs.ml_probability, rs.ml_band = None, None
    else:
        rs.ml_probability, rs.ml_band = ml_engine.predict(attendance_pct, assessment_avg, submission_rate, trend_penalty)
    rs.data_quality = quality

    rs.last_computed = datetime.utcnow()
    if quality != "insufficient":
        _snapshot(rs)

    return rs, prev_band


def get_ml_explanation(rs: RiskScore, top_n=4):
    """Per-student breakdown of which signal drives the ML model's score, for display
    on student/counsellor views."""
    if rs is None or rs.attendance_pct is None or rs.risk_band == NO_DATA:
        return []
    return ml_engine.explain(rs.attendance_pct, rs.assessment_avg, rs.submission_rate, rs.trend_penalty, top_n=top_n)


def _notify(recipient_id, student_id, course_id, message, severity):
    if recipient_id is None:
        return
    n = Notification(
        recipient_id=recipient_id,
        student_id=student_id,
        course_id=course_id,
        message=message,
        severity=severity,
    )
    db.session.add(n)
    mailer.queue_alert(recipient_id, message, severity)


def generate_notifications_for_enrollment(enrollment: Enrollment, rs: RiskScore, prev_band: str):
    """Fan out an alert to lecturer, HoD, counsellor, student, and (if consented) parent."""
    if rs.risk_band not in NOTIFY_ON_BAND:
        return 0
    # Avoid re-spamming: only notify when the band changed, or it's a fresh High-risk case
    # (kept simple for the prototype: notify whenever risk is Medium/High and band changed
    # or there was no previous score at all).
    if prev_band == rs.risk_band:
        return 0

    student = Student.query.get(enrollment.student_id)
    course = Course.query.get(enrollment.course_id)
    reasons = []
    if rs.attendance_pct < 75:
        reasons.append(f"attendance at {rs.attendance_pct}%")
    if rs.assessment_avg < 50:
        reasons.append(f"assessment average at {rs.assessment_avg}%")
    if rs.submission_rate < 80:
        reasons.append(f"submission rate at {rs.submission_rate}%")
    if rs.trend_penalty > 20:
        reasons.append("a declining performance trend")
    reason_text = "; ".join(reasons) if reasons else "a combination of risk indicators"

    base_msg = (
        f"{student.user.full_name} ({student.student_number}) is now flagged "
        f"{rs.risk_band.upper()} RISK in {course.code} - {course.name}: {reason_text}."
    )

    count = 0
    # Lecturer
    if course.lecturer_id:
        _notify(course.lecturer_id, student.id, course.id, base_msg, rs.risk_band)
        count += 1
    # Head of Department (any user with role hod in this department)
    hod = User.query.filter_by(role="hod", department_id=course.department_id).first()
    if hod:
        _notify(hod.id, student.id, course.id, base_msg, rs.risk_band)
        count += 1
    # Counsellor assigned to the student
    if student.counsellor_id:
        counsel_msg = base_msg + " Consider scheduling a support check-in."
        _notify(student.counsellor_id, student.id, course.id, counsel_msg, rs.risk_band)
        count += 1
    # Student themself
    student_msg = (
        f"You're currently flagged {rs.risk_band.upper()} RISK in {course.code}. "
        f"Reasons: {reason_text}. Please reach out to your lecturer or counsellor for support."
    )
    _notify(student.user_id, student.id, course.id, student_msg, rs.risk_band)
    count += 1
    # Parent - only for High risk, and only with consent on file
    if rs.risk_band in PARENT_NOTIFY_BAND and student.parent_id and student.parent_consent:
        parent_msg = (
            f"{student.user.full_name} has been flagged HIGH RISK in {course.code} - {course.name} "
            f"due to {reason_text}. The institution's support team has been notified and will "
            f"reach out to offer assistance."
        )
        _notify(student.parent_id, student.id, course.id, parent_msg, rs.risk_band)
        count += 1

    return count


def recompute_all():
    """Recompute risk for every enrollment and generate any new notifications. Returns a summary dict."""
    enrollments = Enrollment.query.filter_by(status="active").all()
    summary = {"total": 0, "high": 0, "medium": 0, "low": 0, "notifications": 0}
    for e in enrollments:
        rs, prev_band = compute_risk_for_enrollment(e.id)
        db.session.flush()  # ensure rs.id / relationships are usable
        summary["notifications"] += generate_notifications_for_enrollment(e, rs, prev_band)
        summary["total"] += 1
        key = rs.risk_band.lower() if rs.risk_band != NO_DATA else "no_data"
        summary[key] = summary.get(key, 0) + 1
    db.session.commit()
    return summary


def model_agreement_summary():
    """Compares the rule-based band against the ML model's band across all scored
    enrollments — useful on the admin/model page to judge whether the ML model is
    ready to drive notifications on its own, or should stay in shadow mode."""
    rows = RiskScore.query.all()
    total = len(rows)
    if total == 0:
        return {"total": 0, "agree": 0, "agree_pct": 0.0, "ml_high_rule_low": 0, "rule_high_ml_low": 0}
    agree = sum(1 for r in rows if r.risk_band == r.ml_band)
    ml_high_rule_low = sum(1 for r in rows if r.ml_band == "High" and r.risk_band == "Low")
    rule_high_ml_low = sum(1 for r in rows if r.risk_band == "High" and r.ml_band == "Low")
    return {
        "total": total,
        "agree": agree,
        "agree_pct": round(100.0 * agree / total, 1),
        "ml_high_rule_low": ml_high_rule_low,
        "rule_high_ml_low": rule_high_ml_low,
    }
