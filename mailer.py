"""Email copies of risk alerts (FR-14), sent through the Resend web API.

Switched on by two settings (environment variables on Render):
    RESEND_API_KEY   the API key from resend.com (Sending access)
    MAIL_FROM        e.g.  UniAlert <alerts@unialert.com.mx>  (a verified domain)
Optional:
    EMAIL_ALERT_ROLES  who gets emails, default "lecturer,counsellor"
    SITE_URL           link in the email, default https://unialert.com.mx

How it works: whenever an in-app alert is created, queue_alert() notes the
recipient's address on the database session. Only after that save has been
committed are the emails sent, in a background thread, one email per person
per save (an attendance save that flags five students sends each lecturer one
email listing all five). If the save fails, nothing is sent. If email is not
configured, or Resend is down, the in-app alerts work exactly as before.

Resend is used over its HTTPS API rather than SMTP, because some Render plans
block outbound SMTP ports.
"""
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime
from html import escape

from sqlalchemy import event
from sqlalchemy.orm import Session

API_URL = "https://api.resend.com/emails"
_QUEUE_KEY = "unialert_mail_queue"

# Status for the admin page (per server process).
status = {"sent": 0, "failed": 0, "last_error": None, "last_sent_at": None}


def api_key():
    return os.environ.get("RESEND_API_KEY", "").strip()


def mail_from():
    return os.environ.get("MAIL_FROM", "").strip()


def enabled():
    return bool(api_key() and mail_from())


def alert_roles():
    raw = os.environ.get("EMAIL_ALERT_ROLES", "lecturer,counsellor")
    return {r.strip().lower() for r in raw.split(",") if r.strip()}


def site_url():
    return os.environ.get("SITE_URL", "https://unialert.com.mx").rstrip("/")


def _sync():
    return os.environ.get("MAIL_SYNC", "").strip().lower() == "true"


# ------------------------------------------------------------ transport ----

def _resend_transport(payload):
    """Posts one email to Resend. Returns (ok, detail)."""
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
                 "User-Agent": "UniAlert/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            return 200 <= resp.status < 300, body[:300]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:  # network errors, timeouts
        return False, f"{type(e).__name__}: {e}"


TRANSPORT = _resend_transport   # tests replace this


def send(to, subject, text, html=None):
    """Sends one email now. Returns (ok, detail). Never raises."""
    if not enabled():
        return False, "Email is not configured (RESEND_API_KEY and MAIL_FROM)."
    payload = {"from": mail_from(), "to": [to], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    try:
        ok, detail = TRANSPORT(payload)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    if ok:
        status["sent"] += 1
        status["last_sent_at"] = datetime.utcnow()
    else:
        status["failed"] += 1
        status["last_error"] = detail
        print(f"[mailer] could not email {to}: {detail}", flush=True)
    return ok, detail


# --------------------------------------------------------------- alerts ----

def queue_alert(recipient_id, message, severity):
    """Called for every new in-app alert. Notes the email to send after commit."""
    if not enabled() or recipient_id is None:
        return
    from models import db, User   # local import: avoids a circular import
    user = db.session.get(User, recipient_id)
    if user is None or not user.active or not user.email or user.role not in alert_roles():
        return
    db.session.info.setdefault(_QUEUE_KEY, []).append(
        {"email": user.email, "name": user.first_name, "message": message, "severity": severity})


def _build(items):
    n = len(items)
    high = sum(1 for i in items if i["severity"] == "High")
    first = items[0]
    if n == 1:
        subject = f"UniAlert: {first['severity']} risk alert"
    else:
        subject = f"UniAlert: {n} students need attention" + (f" ({high} high risk)" if high else "")
    link = f"{site_url()}/notifications"
    lines = [f"Hello {first['name']},", "",
             "UniAlert has flagged the following:" if n > 1 else "UniAlert has flagged a student:", ""]
    lines += [f"- [{i['severity']}] {i['message']}" for i in items]
    lines += ["", f"Open UniAlert to see the details and follow up: {link}", "",
              "You receive this email because you are the lecturer or counsellor for these students.",
              "Alerts are sent only when a student's risk band rises to Medium or High."]
    colours = {"High": "#A2402F", "Medium": "#B9821F"}
    rows = "".join(
        f'<tr><td style="padding:8px 10px;border-bottom:1px solid #E5E7EB;vertical-align:top;">'
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;color:#fff;font-size:12px;'
        f'background:{colours.get(i["severity"], "#5A6478")};">{escape(i["severity"])}</span></td>'
        f'<td style="padding:8px 10px;border-bottom:1px solid #E5E7EB;">{escape(i["message"])}</td></tr>'
        for i in items)
    html = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;color:#16213A;max-width:620px;">'
        '<div style="background:#16213A;color:#fff;padding:14px 18px;font-size:18px;font-weight:bold;">UniAlert</div>'
        f'<div style="padding:18px;"><p>Hello {escape(first["name"])},</p>'
        f'<p>{"UniAlert has flagged the following:" if n > 1 else "UniAlert has flagged a student:"}</p>'
        f'<table style="border-collapse:collapse;width:100%;font-size:14px;">{rows}</table>'
        f'<p style="margin-top:18px;"><a href="{escape(link)}" style="background:#16213A;color:#fff;'
        'padding:10px 16px;text-decoration:none;border-radius:4px;">Open UniAlert</a></p>'
        '<p style="color:#5A6478;font-size:12px;margin-top:22px;">You receive this email because you are the '
        'lecturer or counsellor for these students. Alerts are sent only when a student\'s risk band rises '
        'to Medium or High.</p></div></div>')
    return subject, "\n".join(lines), html


def deliver(items):
    """Groups queued alerts by address and sends one email to each person."""
    by_email = {}
    for i in items:
        by_email.setdefault(i["email"].lower(), []).append(i)
    for group in by_email.values():
        subject, text, html = _build(group)
        send(group[0]["email"], subject, text, html)


@event.listens_for(Session, "after_commit")
def _after_commit(session):
    items = session.info.pop(_QUEUE_KEY, None)
    if not items:
        return
    if _sync():
        deliver(items)
    else:
        threading.Thread(target=deliver, args=(items,), daemon=True).start()


@event.listens_for(Session, "after_rollback")
def _after_rollback(session):
    session.info.pop(_QUEUE_KEY, None)


def send_test(to, name):
    subject = "UniAlert: test email"
    text = (f"Hello {name},\n\nThis is a test email from UniAlert. Email alerts are working.\n\n"
            f"Lecturers and counsellors will get an email like this when a student's risk rises to "
            f"Medium or High.\n\n{site_url()}")
    html = ('<div style="font-family:Segoe UI,Arial,sans-serif;color:#16213A;">'
            '<div style="background:#16213A;color:#fff;padding:14px 18px;font-size:18px;font-weight:bold;">UniAlert</div>'
            f'<div style="padding:18px;"><p>Hello {escape(name)},</p><p>This is a test email from UniAlert. '
            'Email alerts are working.</p><p>Lecturers and counsellors will get an email like this when a '
            "student's risk rises to Medium or High.</p></div></div>")
    return send(to, subject, text, html)
