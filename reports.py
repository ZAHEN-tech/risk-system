"""
Builds the four report types from the brief — Student Performance, Risk,
Attendance, and Intervention — as either CSV or PDF. Kept separate from
app.py so the row-building logic (what goes in a report) is independent of
the download plumbing (how it's served).

Row-builders take already-scoped querysets (e.g. "this HoD's department's
enrollments") — access control and scoping happen in app.py, not here.
"""
import csv
import io
from datetime import datetime
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from models import AttendanceRecord, InterventionNote


def _offset():
    import os
    from datetime import timedelta
    return timedelta(hours=float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "12")))


# ---------------------------------------------------------------- Rows ----

def performance_rows(enrollments):
    rows = []
    for e in sorted(enrollments, key=lambda e: e.student.user.full_name):
        rs = e.risk
        rows.append({
            "student": e.student.user.full_name,
            "student_number": e.student.student_number,
            "course": e.course.code,
            "attendance_pct": rs.attendance_pct if rs else "",
            "assessment_avg": rs.assessment_avg if rs else "",
            "submission_rate": rs.submission_rate if rs else "",
            "risk_band": rs.risk_band if rs else "Not scored",
        })
    return rows


def risk_rows(enrollments):
    rows = []
    for e in sorted(enrollments, key=lambda e: (e.risk.risk_score if e.risk else -1), reverse=True):
        rs = e.risk
        if not rs:
            continue
        rows.append({
            "student": e.student.user.full_name,
            "student_number": e.student.student_number,
            "course": e.course.code,
            "risk_score": rs.risk_score,
            "risk_band": rs.risk_band,
            "ml_probability": rs.ml_probability,
            "ml_band": rs.ml_band,
            "trend_penalty": rs.trend_penalty,
        })
    return rows


def attendance_rows(enrollments):
    rows = []
    for e in sorted(enrollments, key=lambda e: e.student.user.full_name):
        records = AttendanceRecord.query.filter_by(enrollment_id=e.id).all()
        total = len(records)
        present = sum(1 for r in records if r.present)
        pct = round(100 * present / total, 1) if total else ""
        rows.append({
            "student": e.student.user.full_name,
            "student_number": e.student.student_number,
            "course": e.course.code,
            "weeks_recorded": total,
            "weeks_present": present,
            "attendance_pct": pct,
        })
    return rows


def intervention_rows(students):
    rows = []
    for s in sorted(students, key=lambda s: s.user.full_name):
        notes = (
            InterventionNote.query.filter_by(student_id=s.id)
            .order_by(InterventionNote.created_at).all()
        )
        for n in notes:
            rows.append({
                "student": s.user.full_name,
                "student_number": s.student_number,
                "date": (n.created_at + _offset()).strftime("%Y-%m-%d %H:%M"),
                "counsellor": n.author.full_name,
                "action_taken": n.action_taken or "",
                "status": n.status or "Open",
                "follow_up": n.follow_up_date.strftime("%Y-%m-%d") if n.follow_up_date else "",
                "note": n.note,
            })
    return rows


# ----------------------------------------------------------- Rendering ----

def rows_to_csv(fieldnames, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


class _ReportPDF(FPDF):
    def __init__(self, title, subtitle):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.report_title = title
        self.report_subtitle = subtitle
        self.set_auto_page_break(auto=True, margin=15)

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 8, self.report_title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 5, self.report_subtitle, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.cell(0, 5, f"Generated {datetime.now().strftime('%d %b %Y, %H:%M')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(140, 140, 140)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def rows_to_pdf(title, subtitle, fieldnames, labels, rows, col_widths=None):
    pdf = _ReportPDF(title, subtitle)
    pdf.add_page()

    n = len(fieldnames)
    page_width = pdf.w - pdf.l_margin - pdf.r_margin
    if not col_widths:
        col_widths = [page_width / n] * n
    else:
        # col_widths given as relative weights; scale to fill the page width exactly.
        total_weight = sum(col_widths)
        col_widths = [page_width * w / total_weight for w in col_widths]

    def fit(text, width, font_size):
        """Truncate text with an ellipsis so it actually fits the given column width,
        rather than a fixed character count that's wrong for narrow or wide columns."""
        pdf.set_font("Helvetica", "", font_size)
        max_w = width - 2  # small padding
        if pdf.get_string_width(text) <= max_w:
            return text
        while text and pdf.get_string_width(text + "...") > max_w:
            text = text[:-1]
        return text + "..." if text else "..."

    pdf.set_fill_color(30, 43, 69)      # --ink
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8.5)
    for label, w in zip(labels, col_widths):
        pdf.cell(w, 7, fit(label, w, 8.5), border=0, fill=True)
    pdf.ln(7)

    pdf.set_text_color(20, 20, 20)
    fill = False
    for row in rows:
        pdf.set_fill_color(245, 242, 233) if fill else pdf.set_fill_color(255, 255, 255)
        for field, w in zip(fieldnames, col_widths):
            val = fit(str(row.get(field, "")), w, 8)
            pdf.set_font("Helvetica", "", 8)
            pdf.cell(w, 6.5, val, border="B", fill=True)
        pdf.ln(6.5)
        fill = not fill

    if not rows:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 8, "No records match this report.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())
