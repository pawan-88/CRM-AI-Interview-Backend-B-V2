"""Candidate-facing communications: email (SMTP) + WhatsApp (stub) + templates.

Everything here is BEST-EFFORT and never raises: pipeline flows (ATS auto-invite,
public slot confirmation, manual scheduling) must complete even when no channel
is configured. Each send returns {"sent": bool, "error": str|None} so callers
can surface per-channel results in API responses and activity logs.
"""
from __future__ import annotations

import logging
import os
from html import escape

from email_smtp import send_email, smtp_configured

logger = logging.getLogger("karnex.crm.candidate_comms")


# --------------------------------------------------------------------- email

def send_candidate_email(to: str, subject: str, text: str, html: str | None = None,
                         attachments: list[tuple[str, bytes, str]] | None = None,
                         *, db=None, event: str = "candidate", actor=None,
                         to_name: str = "") -> dict:
    """Send one candidate email. Never raises.

    DURABLE PATH: pass `db` (the caller's session) and the message is QUEUED on
    the email outbox instead of sent inline — it gets the same five retries
    with backoff and the same audit row in the Email Outbox screen that staff
    notifications get, and it only goes out if the caller's transaction
    commits. This closed the old gap where a ZeptoMail hiccup at the wrong
    moment silently lost a candidate's interview invitation with no record.

    INLINE PATH (no `db`, or `attachments` present): the original direct SMTP
    send. Attachments stay inline because the outbox table stores text bodies
    only — today that is exactly one email, the L2 calendar invite (.ics).

    Returns {"sent": bool, "error": str|None} either way; the durable path
    adds {"queued": True} so activity logs can say which road it took.
    """
    try:
        to = (to or "").strip()
        if not to:
            return {"sent": False, "error": "no_email"}
        if db is not None and not attachments:
            try:
                from services.email_outbox import queue_email

                row = queue_email(
                    db,
                    to_email=to,
                    to_name=to_name,
                    subject=subject,
                    body_text=text,
                    body_html=html,
                    event=event or "candidate",
                    actor=actor,
                )
                if row is not None:
                    return {"sent": True, "error": None, "queued": True}
                # Queueing declined (e.g. email disabled in settings) — report
                # honestly rather than silently double-sending inline.
                return {"sent": False, "error": "email_disabled", "queued": False}
            except Exception as exc:
                # The outbox must never take a candidate flow down with it —
                # fall back to the old inline send.
                logger.warning("candidate email queue failed for %s (%s); sending inline", to, exc)
        if not smtp_configured():
            return {"sent": False, "error": "smtp_disabled"}
        result = send_email(to, subject, text, html, attachments=attachments)
        if result.get("ok"):
            return {"sent": True, "error": None}
        return {"sent": False, "error": str(result.get("error") or "send_failed")}
    except Exception as exc:  # defensive: comms must never break the pipeline
        logger.warning("candidate email failed for %s: %s", to, exc)
        return {"sent": False, "error": str(exc)}


# ----------------------------------------------------------------- ics invite

def build_ics_invite(summary: str, starts_at, description: str = "", location: str = "",
                     uid: str = "karnex-interview", duration_minutes: int = 60) -> str:
    """Minimal RFC-5545 VCALENDAR for one interview event (floating local time,
    matching how RMG typed it). Attach as ("invite.ics", bytes, "text/calendar")."""
    from datetime import timedelta

    def _fmt(dt) -> str:
        return dt.strftime("%Y%m%dT%H%M%S")

    def _esc(v: str) -> str:
        return (v or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")

    ends_at = starts_at + timedelta(minutes=duration_minutes)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Karnex//AI HR Suite//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}@karnex",
        f"DTSTAMP:{_fmt(starts_at)}",
        f"DTSTART:{_fmt(starts_at)}",
        f"DTEND:{_fmt(ends_at)}",
        f"SUMMARY:{_esc(summary)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_esc(description)}")
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    lines += ["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


# ------------------------------------------------------------------ whatsapp

def send_candidate_whatsapp(phone: str, message: str) -> dict:
    """STUB WhatsApp channel — provider integration point.

    Provider interface (drop keys in, implement one branch, done):

    * Meta WhatsApp Cloud API — set WHATSAPP_PROVIDER=meta plus
      WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN, then POST
      https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_NUMBER_ID}/messages
      with JSON {"messaging_product": "whatsapp", "to": <E.164 phone>,
      "type": "text", "text": {"body": message}} and header
      "Authorization: Bearer {WHATSAPP_ACCESS_TOKEN}".

    * Twilio WhatsApp — set WHATSAPP_PROVIDER=twilio plus TWILIO_ACCOUNT_SID,
      TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM (e.g. "whatsapp:+14155238886"),
      then POST https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json
      (basic auth SID:token) with form fields From=TWILIO_WHATSAPP_FROM,
      To=f"whatsapp:{phone}", Body=message.

    Until a provider is wired, this returns
    {"sent": False, "error": "whatsapp_not_configured"} (env unset) or
    {"sent": False, "error": "whatsapp_provider_<name>_not_implemented"}.
    Never raises.
    """
    try:
        phone = (phone or "").strip()
        if not phone:
            return {"sent": False, "error": "no_phone"}
        provider = (os.getenv("WHATSAPP_PROVIDER") or "").strip().lower()
        if not provider:
            return {"sent": False, "error": "whatsapp_not_configured"}
        # Provider branches go here (see docstring). Stubbed on purpose.
        logger.info("whatsapp stub: provider=%s to=%s (not implemented)", provider, phone)
        return {"sent": False, "error": f"whatsapp_provider_{provider}_not_implemented"}
    except Exception as exc:
        return {"sent": False, "error": str(exc)}


# ------------------------------------------------------------- multi-channel

def notify_candidate(email: str | None, phone: str | None, subject: str,
                     message_text: str, message_html: str | None = None,
                     *, db=None, event: str = "candidate", actor=None,
                     to_name: str = "") -> dict:
    """Try email first, then WhatsApp. Returns per-channel results:
    {"email": {"sent": bool, "error": ...}, "whatsapp": {"sent": bool, "error": ...}}.

    Pass `db` to route the email through the durable outbox (retries + audit)
    — see send_candidate_email. WhatsApp stays inline either way.
    """
    email_result = (
        send_candidate_email(email, subject, message_text, message_html,
                             db=db, event=event, actor=actor, to_name=to_name)
        if (email or "").strip() else {"sent": False, "error": "no_email"}
    )
    whatsapp_result = (
        send_candidate_whatsapp(phone, message_text)
        if (phone or "").strip() else {"sent": False, "error": "no_phone"}
    )
    return {"email": email_result, "whatsapp": whatsapp_result}


# ----------------------------------------------------------------- templates

def _branded_html(title: str, body_html: str) -> str:
    """Simple branded wrapper (inline styles only — email-client safe)."""
    return f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#1e293b;max-width:560px;">
      <div style="padding:14px 0;border-bottom:2px solid #e2e8f0;margin-bottom:16px;">
        <span style="font-weight:800;font-size:20px;letter-spacing:-0.02em;color:#0f172a;">KARNEX</span>
        <span style="font-weight:800;font-size:20px;letter-spacing:-0.02em;color:#4f46e5;"> Careers</span>
      </div>
      <p style="font-size:16px;font-weight:700;margin:0 0 10px;">{escape(title)}</p>
      {body_html}
      <p style="color:#64748b;font-size:13px;margin-top:20px;">— Karnex Recruitment Team</p>
    </div>
    """


def slot_invite_message(candidate_name: str, role_title: str, booking_url: str) -> dict:
    """Invite the candidate to pick an interview slot via the public booking link.

    Returns {"subject": str, "text": str, "html": str}.
    """
    subject = f"Karnex — pick your interview slot for {role_title}"
    text = (
        f"Hello {candidate_name},\n\n"
        f"Good news! Your application for \"{role_title}\" has been shortlisted.\n\n"
        f"Please pick an interview slot that works for you using this link:\n"
        f"{booking_url}\n\n"
        f"Once you confirm a slot, you will receive your AI interview link and "
        f"secure access key.\n\n"
        f"— Karnex Recruitment Team\n"
    )
    html = _branded_html(
        "You have been shortlisted!",
        f"""
      <p>Hello <strong>{escape(candidate_name)}</strong>,</p>
      <p>Your application for <strong>{escape(role_title)}</strong> has been shortlisted.</p>
      <p>Please pick an interview slot that works for you:</p>
      <p><a href="{escape(booking_url)}" style="display:inline-block;padding:12px 18px;background:#4f46e5;color:#ffffff;border-radius:10px;text-decoration:none;font-weight:700;">Choose my interview slot</a></p>
      <p style="word-break:break-all;font-size:13px;color:#64748b;">{escape(booking_url)}</p>
      <p style="font-size:13px;color:#64748b;">Once you confirm a slot, you will receive your AI interview link and secure access key.</p>
        """,
    )
    return {"subject": subject, "text": text, "html": html}


def interview_link_message(candidate_name: str, role_title: str, when_text: str,
                           invite_url: str, access_key: str) -> dict:
    """Deliver the AI interview link (+ access key) to the candidate.

    Returns {"subject": str, "text": str, "html": str}.
    """
    subject = f"Karnex — your AI interview link for {role_title}"
    text = (
        f"Hello {candidate_name},\n\n"
        f"Your AI interview for \"{role_title}\" is scheduled.\n"
        f"When: {when_text or 'See your recruiter'}\n\n"
        f"Open this link to start your interview:\n{invite_url}\n\n"
    )
    if access_key:
        text += (
            f"Your Secure Access Key: {access_key}\n"
            f"You will need your registered email and this key to enter the interview.\n"
            f"Do NOT share these credentials with anyone.\n\n"
        )
    text += "— Karnex Recruitment Team\n"

    key_html = ""
    if access_key:
        key_html = f"""
      <div style="margin:16px 0;padding:14px 18px;background:#f1f5f9;border:2px dashed #4f46e5;border-radius:12px;">
        <p style="margin:0 0 4px;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#64748b;font-weight:700;">Secure Access Key</p>
        <p style="margin:0;font-size:20px;font-weight:900;letter-spacing:0.15em;color:#1e293b;font-family:monospace;">{escape(access_key)}</p>
      </div>
      <p style="font-size:13px;color:#ef4444;font-weight:600;">Do NOT share your access key or interview link with anyone.</p>
        """
    html = _branded_html(
        "Your AI interview is scheduled",
        f"""
      <p>Hello <strong>{escape(candidate_name)}</strong>,</p>
      <p>Your AI interview for <strong>{escape(role_title)}</strong> is scheduled.</p>
      <p><strong>When:</strong> {escape(when_text or 'See your recruiter')}</p>
      {key_html}
      <p><a href="{escape(invite_url)}" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#ffffff;border-radius:10px;text-decoration:none;font-weight:700;">Open my interview</a></p>
      <p style="word-break:break-all;font-size:13px;color:#64748b;">{escape(invite_url)}</p>
        """,
    )
    return {"subject": subject, "text": text, "html": html}
