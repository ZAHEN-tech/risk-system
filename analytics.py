"""
Prepares small, chart-ready data structures for the dashboard graphs.
Kept separate from risk_engine.py because this is purely presentational
aggregation (for Chart.js), not part of the risk-scoring logic itself.
"""
import os
from datetime import timedelta

from models import AttendanceRecord, Assessment, AssessmentDefinition, Enrollment, RiskSnapshot


def band_counts(risk_scores):
    """risk_scores: iterable of RiskScore (or None) objects -> {'Low':n,'Medium':n,'High':n}"""
    counts = {"Low": 0, "Medium": 0, "High": 0}
    for rs in risk_scores:
        if rs and rs.risk_band in counts:
            counts[rs.risk_band] += 1
    return counts


def weekly_attendance_series(enrollment_ids):
    """Average attendance % per week, across the given enrollments. Returns (labels, values)."""
    if not enrollment_ids:
        return [], []
    records = AttendanceRecord.query.filter(AttendanceRecord.enrollment_id.in_(enrollment_ids)).all()
    by_week = {}
    for r in records:
        by_week.setdefault(r.week, []).append(1 if r.present else 0)
    weeks = sorted(by_week.keys())
    values = [round(100 * sum(by_week[w]) / len(by_week[w]), 1) for w in weeks]
    return [f"Wk {w}" for w in weeks], values


def assessment_score_series(enrollment_ids):
    """Average assessment score (%) per sequence position, across the given enrollments.
    Only counts submitted, scored assessments. Returns (labels, values)."""
    if not enrollment_ids:
        return [], []
    rows = Assessment.query.filter(
        Assessment.enrollment_id.in_(enrollment_ids), Assessment.submitted.is_(True), Assessment.score.isnot(None)
    ).all()
    by_seq = {}
    titles = {}
    for a in rows:
        pct = 100.0 * a.score / a.max_score if a.max_score else 0
        by_seq.setdefault(a.sequence, []).append(pct)
        titles.setdefault(a.sequence, a.title)
    seqs = sorted(by_seq.keys())
    values = [round(sum(by_seq[s]) / len(by_seq[s]), 1) for s in seqs]
    labels = [titles.get(s, f"#{s}") for s in seqs]
    return labels, values


def student_attendance_series(enrollment_id):
    """One student's own attendance, week by week (100 = present, 0 = absent). Returns (labels, values)."""
    records = (
        AttendanceRecord.query.filter_by(enrollment_id=enrollment_id).order_by(AttendanceRecord.week).all()
    )
    labels = [f"Wk {r.week}" for r in records]
    values = [100 if r.present else 0 for r in records]
    return labels, values


def student_score_series(enrollment_id):
    """One student's own assessment scores (%) in sequence order. Returns (labels, values)."""
    rows = (
        Assessment.query.filter_by(enrollment_id=enrollment_id, submitted=True)
        .filter(Assessment.score.isnot(None))
        .join(AssessmentDefinition, Assessment.definition_id == AssessmentDefinition.id)
        .order_by(AssessmentDefinition.sequence).all()
    )
    labels = [a.title for a in rows]
    values = [round(100.0 * a.score / a.max_score, 1) if a.max_score else 0 for a in rows]
    return labels, values


def risk_history_series(enrollment_id):
    """How one enrolment's risk score moved over time (FR-11). Returns (labels, values);
    labels are Fiji-time dates, e.g. '12 Oct 14:05'."""
    offset = timedelta(hours=float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "12")))
    snaps = (RiskSnapshot.query.filter_by(enrollment_id=enrollment_id)
             .order_by(RiskSnapshot.taken_at, RiskSnapshot.id).all())
    labels = [(sn.taken_at + offset).strftime("%d %b %H:%M") for sn in snaps]
    values = [sn.risk_score for sn in snaps]
    return labels, values
