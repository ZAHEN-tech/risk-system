# Student Risk Prediction & Early Intervention System — Prototype

A working prototype of a predictive analytics system that monitors attendance,
coursework submission and assessment performance to flag at-risk students
early, and routes alerts to the right people: **lecturers, Heads of
Department, counsellors, students, and (with consent) parents/guardians**.

## Project scope & status

This started as a predictive-analytics prototype and is being built out
module by module toward the full brief (student management, attendance/
assessment entry, dashboards, reports). Current status:

| Module | Status |
|---|---|
| 1. Login (role-based access) | ✅ Done |
| 2. Student Management (register/edit/view, enroll/unenroll, deactivate, reset password) | ✅ Done |
| 2b. Lecturer Management (register/edit/view, assign courses, deactivate, reset password) | ✅ Done |
| 3. Attendance Module (recording UI) | ✅ Done |
| 4. Assessment Module (marks entry UI) | ✅ Done |
| 5. Risk Prediction (rule-based + ML) | ✅ Done |
| 6. Notification Module | ✅ Done |
| 7. Dashboard (totals shown; graphs) | ✅ Done |
| 8. Reports Module (exportable reports) | ✅ Done |

## Quick start

**Windows:** just double-click `run.bat` in the project folder. The first time,
it creates the virtual environment and installs everything automatically
(takes a minute); every time after that, it just starts the server. When you
see `Running on http://127.0.0.1:5050`, open that address in your browser.

**Mac/Linux, or manual setup on any OS:**
```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

> **Windows tip:** if `pip install` or `python app.py` ever complain about a
> missing module even though you "activated" the venv, it usually means
> something on your PATH is shadowing it. The reliable fix is to call the
> venv's own python directly instead of relying on activation:
> ```
> venv\Scripts\python.exe -m pip install -r requirements.txt
> venv\Scripts\python.exe app.py
> ```
> `run.bat` does exactly this automatically, which is why it's the easiest
> way to run this on Windows.

Open **http://127.0.0.1:5050**. On first run the app automatically creates
the database, seeds a synthetic demo dataset (departments, courses,
lecturers, ~25 students with a realistic mix of attendance/performance
profiles, counsellors, and linked parents), and runs the risk engine once.

### Demo logins
All demo accounts use the password `password123`.

| Role | Username | Notes |
|---|---|---|
| Administrator | `admin` | Seed/reset data, trigger recompute |
| Head of Department | `hod_1` | Department-wide risk overview |
| Lecturer | `lect_1_1` | Own courses only |
| Counsellor | `counsellor1` | Caseload of flagged students, can log interventions |
| Student | `student1` | Own standing + notifications |
| Parent/Guardian | `parent1` | Read-only summary of their child |

(You can also try `hod_2`, `hod_3`, `lect_1_2`, `counsellor2`, `student2`…`student25`, `parent2`…`parent25`.)

## How it works

### 1. Data model (`models.py`)
`Department → Course → Enrollment ← Student`, with `AttendanceRecord` and
`Assessment` rows per enrollment. A single `User` table holds every role
(admin/lecturer/hod/counsellor/student/parent) so the same login and
permission system covers everyone. `RiskScore` caches the latest computed
score per enrollment; `Notification` and `InterventionNote` capture the
intervention workflow.

In production, `AttendanceRecord` and `Assessment` rows would sync in
automatically from the institution's student information system and LMS
(e.g. via a nightly import job or API) rather than being entered by hand.

### 2. Student Management (`/admin/students`)
Full CRUD for student records, restricted to the admin role:
- **Register** — creates the student's login account, a `Student` profile,
  and (optionally, inline) a linked parent/guardian account with a consent
  checkbox controlling whether that parent can receive High-risk alerts.
- **Edit** — update department, year, assigned counsellor, and consent.
  Student number and username stay fixed once created to avoid breaking
  historical records tied to them.
- **Enroll / unenroll** in courses from the student's own profile page —
  enrolling immediately computes an initial risk score (and fires
  notifications if it lands in Medium/High); unenrolling cleans up that
  enrollment's attendance, assessment, and risk records. Registration
  itself can also enroll the student into one or more courses immediately
  (which is how they end up on a lecturer's roster — see below).
- **Deactivate / reactivate** — soft-disables login rather than deleting
  the record, so historical attendance, assessments, and notifications
  stay intact for reporting.
- **Set or reset password** — admin can type a specific password directly,
  or leave it blank to reset to the default (`password123`). Students and
  lecturers can't change their own password — this is the only way.

### 2b. Lecturer Management (`/admin/lecturers`)
Mirrors Student Management, admin-only: register a lecturer, edit their
name/email/department, deactivate/reactivate, and set/reset their
password. The one thing specific to lecturers is **course assignment** —
each lecturer's profile page shows their currently assigned courses and
lets the admin assign them another (from the courses in their department)
or unassign one. A student only ever appears on a lecturer's attendance
and assessment lists because they're enrolled in a course that lecturer is
assigned to — there's no separate "add this student to this lecturer"
step, enrollment *is* the assignment. Reassigning a course to a different
lecturer immediately moves that course's whole roster over; the previous
lecturer loses access to it.

The list view supports search by name/student number, filtering by
department and current risk band, and a toggle to show deactivated
accounts.

### 3. Attendance & Assessment entry (lecturer)
Each lecturer sees their own courses on their dashboard with **Attendance**
and **Assessments** links per course:

- **Attendance** — a weekly register: pick a week number, tick/untick who
  was present, save. Existing weeks are listed so a lecturer can revisit
  and correct one. Saving immediately recomputes risk for every affected
  student and fires any new alerts.
- **Assessments** — a lecturer first creates a class-wide assessment
  (title, type — Coursework / Assignment / Quiz / Examination — sequence
  number, max score), which generates one gradable row per enrolled
  student. They then open its **grade entry sheet** and enter a score per
  student; leaving a score blank marks that student as not having
  submitted (which factors into their submission-rate risk signal, same
  as everywhere else in the system). A student who enrolls after an
  assessment was created is automatically backfilled with a blank row the
  next time the grade sheet is opened, so nobody silently falls outside
  the gradebook.

Demo note: the synthetic seed data was generated directly as historical
`Assessment`/`AttendanceRecord` rows (not through this UI), so a freshly
seeded course's Assessments page will initially show "no assessments
created yet" even though its students already have risk scores — that's
expected, and is called out on the page itself. Any assessment created
through the UI from that point on appears normally.

### 4. Two risk engines, side by side

**Rule-based (`risk_engine.py`)** — a transparent, weighted composite score
(0–100, higher = more at risk), deliberately explainable rather than a
black-box model, because staff, students and parents all need to understand
*why* someone was flagged:

```
risk_score = 0.35 × (100 − attendance %)
           + 0.35 × (100 − assessment average)
           + 0.20 × (100 − submission rate)
           + 0.10 × trend penalty (recent assessments vs. early ones)

Bands:  ≥60 High   ·   30–59 Medium   ·   <30 Low
```

Weights and thresholds are constants at the top of the file — an
institution would recalibrate these against its own historical
retention/dropout data rather than take them as fixed.

**Machine-learned (`ml_engine.py`)** — a logistic regression trained on
historical *outcomes* (did the student actually fail or withdraw?), using
the same four features. It's trained on a synthetic stand-in for past
terms (`ml_engine.generate_historical_dataset()`); in production you'd
swap that for a real query against completed enrollments. Logistic
regression was chosen deliberately over a more complex model — in an
early-warning-for-students context, a model a non-technical registrar or
dean can sanity-check and explain to a parent is worth more than a
marginal accuracy gain from a black box.

Every enrollment is scored by **both** engines and both scores are stored.
The rule-based score is what drives notifications; the ML score runs in
**shadow mode** alongside it — the standard way to validate a new model
against an existing process before trusting it to trigger real alerts on
its own. The admin "Predictive model" page (`/admin/model`) shows accuracy,
precision/recall, ROC-AUC, a confusion matrix, the learned feature
coefficients, how often the two engines agree, and a retrain button.
Counsellors and students also see a plain-language breakdown of which
signal the ML model weighed most heavily for that student
(`ml_engine.explain()`).

Once real historical data is available, retraining and cutting the ML
model over to drive notifications is a two-line change in
`risk_engine.generate_notifications_for_enrollment()`.

### 5. Notification fan-out
When an enrollment's risk band changes to Medium or High, the engine
generates targeted alerts:
- **Lecturer** of the course
- **Head of Department** for that department
- **Counsellor** assigned to the student
- **The student themself**, with plain-language next steps
- **Parent/guardian** — only for **High** risk, and only if consent is on
  file (`Student.parent_consent`) — reflecting that parental notification
  of an adult student's academic performance is often legally/ethically
  sensitive and should be opt-in.

Each recipient only ever sees what's relevant to their role: lecturers see
their own courses, HoDs see their department, counsellors see their
caseload, parents see a high-level summary only (not full assessment
detail), and students see full transparency into their own data.

### 6. Intervention loop
Counsellors can open a flagged student's case file and log intervention
notes and actions taken, building a support history over time — closing
the loop from *detection* to *action*, which is the actual point of an
early-warning system.

### 7. Dashboard graphs
Every dashboard now includes real charts (Chart.js, self-hosted in
`static/js/` — no CDN dependency, so the app works fully offline) built
from live data, not screenshots:

- **Admin** — institution-wide risk distribution (donut) and a
  high/medium-risk-by-department comparison (bar).
- **HoD** — department risk distribution (donut) and risk by lecturer (bar) —
  replacing what used to be a text-only breakdown.
- **Lecturer** — risk distribution across their students, a weekly
  attendance trend line, and an average-score-by-assessment trend line.
- **Counsellor** — caseload composition (Medium vs. High).
- **Student** — per course, their own attendance-by-week and
  score-by-assessment trend lines, so a student can see their own
  trajectory, not just a single snapshot number.

Chart data is prepared in `analytics.py`, kept separate from the risk
scoring logic since it's purely presentational aggregation. Charts are
rendered by a single generic script (`static/js/charts-init.js`) that
reads `data-*` attributes off `<canvas>` elements — templates just declare
what to show via the macros in `templates/_charts.html`, with no chart
config duplicated per page.

### 8. Reports (`reports.py`)
Four report types, each downloadable as **CSV** (full untruncated data, for
spreadsheets) or **PDF** (formatted, print-ready, text intelligently
truncated to fit each column):

- **Student Performance Report** — attendance %, assessment average,
  submission rate, and risk band per enrollment.
- **Risk Report** — full risk breakdown per enrollment, rule-based and ML
  side by side.
- **Attendance Report** — weeks recorded, weeks present, attendance % per
  enrollment.
- **Intervention Report** — logged counselling interventions.

Each role sees only the report types and scope appropriate to it, mirroring
the confidentiality boundaries already enforced elsewhere in the app:

| Role | Reports available | Scope |
|---|---|---|
| Admin | Performance, Risk, Attendance | All departments, or filter to one |
| HoD | Performance, Risk, Attendance | Own department |
| Lecturer | Performance, Risk, Attendance | Own courses |
| Counsellor | Risk, **Intervention** | Own caseload |
| Student | Performance, Attendance | Own record only |

Notably, the **Intervention Report is counsellor-only** — lecturers, HoDs,
and admins can't pull it, even though they can see everything else. This
mirrors a real access-control principle (counselling notes are more
sensitive than academic performance data) without needing to build a full
permissions/approval system to get the important part right.

## Deploying as a real, live website

This app is set up to deploy on **Render** (recommended) with a real Postgres
database, a custom domain, and HTTPS. Recommended budget: roughly **$12–15/month**
(Render web service + database) plus **~$10-12/year** for a domain (e.g. via
Cloudflare Registrar, which charges no markup over the registry price). Free tiers
exist but aren't recommended for a real site: free web services sleep after 15
minutes of inactivity, and free Postgres databases auto-delete after 30 days.

### One-time setup

1. **Buy a domain** — e.g. [Cloudflare Registrar](https://www.cloudflare.com/products/registrar/)
   or Namecheap. You don't need to configure anything with it yet.
2. **Push this project to a GitHub repository** (Render deploys from GitHub).
3. **Create a Render account** at [render.com](https://render.com) and connect your
   GitHub account.
4. **New → Blueprint**, point it at your repository. Render reads `render.yaml` in
   this project and sets up both the web service and the database automatically.
5. Render will prompt you for two values it can't generate itself:
   - `ADMIN_PASSWORD` — the password for the first admin login. Set a strong one now;
     you'll use it to sign in and create real departments, courses, lecturers, and
     students from there.
   - Everything else (`SECRET_KEY`, `DATABASE_URL`, `SEED_DEMO_DATA=false`) is
     already wired up in `render.yaml`.
6. Deploy. Render will run `pip install -r requirements.txt` (build),
   then `python init_db.py` (creates tables + the admin account, once), then start
   `gunicorn app:app` (the actual server).
7. Once it's live at `your-app.onrender.com`, go to your Render service's **Settings
   → Custom Domain**, add your domain, and follow Render's instructions to point your
   domain's DNS at it (usually a CNAME record — done in your domain registrar's
   dashboard). Render issues the HTTPS certificate automatically.
8. Log in with `admin` / the password you set, and start with **Departments** →
   **Courses** → **Lecturers** → **Students** — that's the required order, since
   each depends on the one before it.

### Why `SEED_DEMO_DATA=false` in production
The ~25 synthetic students with default passwords are for local trial use only —
`render.yaml` explicitly turns that off for the real deployment, so the only account
that exists on first boot is the one admin account, with the password you chose.

### Why there's an `init_db.py` at all, instead of just starting the server
Under gunicorn, multiple worker processes import `app.py` independently. If database
setup (creating tables, seeding, bootstrapping the admin account) ran automatically
at import time, concurrent workers would race to do it at the same moment — this is
not hypothetical, it reliably throws `database is locked` under SQLite and is unsafe
under Postgres too. `init_db.py` runs once, before any worker starts (wired in as
Render's `preDeployCommand`), so initialization never runs twice.

### Updating the live site later
Once deployed, further changes are just `git push` to your connected branch — Render
rebuilds and redeploys automatically.

## Extending this prototype
- **Scaling the database**: SQLite (the current default) comfortably
  handles a single institution's data volume — the practical limit is
  concurrent *writes*, not data size, since SQLite locks the whole file per
  write transaction. Foreign-key columns used in dashboard/report filters
  (department, course, enrollment, recipient, etc.) already have indexes
  (see `models.py`) for query performance as data grows. Because the app
  uses SQLAlchemy's ORM rather than hand-written SQL, moving to PostgreSQL
  or MySQL later is a one-line change to `SQLALCHEMY_DATABASE_URI` in
  `app.py`, not a rewrite — do this once you're running real concurrent
  multi-user production traffic rather than a pilot.
- Once real historical outcome data is available, point
  `ml_engine.generate_historical_dataset()` at it and consider cutting the
  ML model over to drive notifications instead of running in shadow mode.
- Replace `seed_data.py` with a real import pipeline from the SIS/LMS.
- Add email/SMS delivery for notifications (currently in-app only).
- Add a scheduled job (e.g. cron / Celery beat) to call
  `risk_engine.recompute_all()` nightly instead of the manual admin button.
- Add audit logging and role-based data-retention rules for compliance
  (FERPA/GDPR-style requirements around student data and parental access).
- A more granular access-control model (Registrar/Dean/Advisor roles,
  faculty-level scoping, and approval workflows for onboarding and for
  altering already-approved marks/attendance) is a natural next layer once
  the core modules are stable — happy to design and build this against a
  specific permissions matrix when you're ready for it.

## Project structure
```
app.py            Flask routes, auth, role-based dashboards, environment-aware config
init_db.py         One-time production DB setup (see Deploying section above)
models.py         SQLAlchemy models
risk_engine.py     Rule-based risk scoring + notification fan-out logic
ml_engine.py        Logistic-regression risk model: training, prediction, explainability
analytics.py         Chart data-prep for the dashboard graphs
reports.py            Report row-building + CSV/PDF rendering
seed_data.py       Synthetic demo dataset generator (local trial use only)
templates/         Jinja2 templates (one per dashboard, shared partials)
static/css/        Stylesheet
static/js/          Chart.js (self-hosted) + generic chart initializer
ml_artifacts/       Trained model, scaler, and metrics (created on first run)
render.yaml        Render Blueprint — one-click deploy config
Procfile            Process definition (gunicorn) for Render/Heroku-style hosts
run.bat             Windows local-dev launcher (double-click to start)
```

