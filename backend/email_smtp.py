"""Optional SMTP email sending for invite links."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger("karnex.smtp")


def smtp_enabled() -> bool:
    return (os.getenv("SMTP_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"})


def smtp_configured() -> bool:
    if not smtp_enabled():
        return False
    host = (os.getenv("SMTP_HOST") or "").strip()
    user = (os.getenv("SMTP_USER") or "").strip()
    pwd = (os.getenv("SMTP_PASSWORD") or "").strip()
    return bool(host and user and pwd)


def send_email(to_address: str, subject: str, body_text: str, body_html: str | None = None,
               attachments: list[tuple[str, bytes, str]] | None = None) -> dict[str, Any]:
    """`attachments`: optional list of (filename, content_bytes, mime_type) —
    e.g. ("invite.ics", b"BEGIN:VCALENDAR...", "text/calendar")."""
    if not smtp_configured():
        return {"ok": False, "error": "SMTP not configured (set SMTP_HOST, SMTP_USER, SMTP_PASSWORD)."}

    host = (os.getenv("SMTP_HOST") or "").strip()
    port = int((os.getenv("SMTP_PORT") or "587").strip() or "587")
    user = (os.getenv("SMTP_USER") or "").strip()
    pwd = (os.getenv("SMTP_PASSWORD") or "").strip()
    from_addr = (os.getenv("SMTP_FROM") or user).strip()
    use_tls = (os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"})

    if attachments:
        # mixed container wrapping the alternative body + file parts
        msg = MIMEMultipart("mixed")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)
        from email.mime.base import MIMEBase
        from email import encoders
        for fname, content, mime in attachments:
            main, _, sub = (mime or "application/octet-stream").partition("/")
            part = MIMEBase(main or "application", sub or "octet-stream")
            part.set_payload(content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
            msg.attach(part)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_address

    try:
        if use_tls and port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, pwd)
                server.sendmail(from_addr, [to_address], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                server.login(user, pwd)
                server.sendmail(from_addr, [to_address], msg.as_string())
        logger.info("smtp.sent", extra={"event": "smtp.sent", "to": to_address, "subject": subject[:80]})
        return {"ok": True}
    except Exception as err:
        logger.warning("smtp.failed", extra={"event": "smtp.failed", "to": to_address, "error": str(err)})
        return {"ok": False, "error": str(err)}


def send_password_reset_email(to_email: str, full_name: str, reset_url: str) -> dict[str, Any]:
    subject = "Reset your Karnex password"
    display_name = (full_name or "").strip() or "there"
    text = (
        f"Hello {display_name},\n\n"
        f"We received a request to reset the password for your Karnex account.\n\n"
        f"Open this link to choose a new password (valid for 45 minutes, single use):\n{reset_url}\n\n"
        f"If you didn't request a password reset, you can safely ignore this email — "
        f"your password will stay unchanged.\n\n"
        f"— KARNEX AI HR\n"
    )
    html = f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;line-height:1.6;color:#1e293b;">
      <p>Hello <strong>{display_name}</strong>,</p>
      <p>We received a request to reset the password for your <strong>Karnex</strong> account.</p>
      <p><a href="{reset_url}" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none;font-weight:700;">Reset password</a></p>
      <p style="word-break:break-all;font-size:13px;color:#64748b;">{reset_url}</p>
      <p style="font-size:13px;color:#64748b;">This link is valid for <strong>45 minutes</strong> and can be used once.</p>
      <p style="font-size:13px;color:#64748b;">If you didn't request a password reset, you can safely ignore this email — your password will stay unchanged.</p>
      <p>— KARNEX AI HR</p>
    </div>
    """
    return send_email(to_email, subject, text, html)


def send_interview_invite_email(
    to_email: str,
    candidate_name: str,
    invite_url: str,
    scheduled_at_local: str,
    notes: str = "",
    access_key: str = "",
) -> dict[str, Any]:
    subject = "Your KARNEX AI Interview — interview link"
    text = (
        f"Hello {candidate_name},\n\n"
        f"Your AI interview is scheduled.\n"
        f"When: {scheduled_at_local or 'See HR'}\n\n"
        f"Open this link to start (same Wi‑Fi/LAN as HR if applicable):\n{invite_url}\n\n"
    )
    if access_key:
        text += f"Your Secure Access Key: {access_key}\n"
        text += f"Your Registered Email: {to_email}\n\n"
        text += "You will need both your email and access key to enter the interview.\nDo NOT share these credentials with anyone.\n\n"
    if notes:
        text += f"Notes: {notes}\n\n"
    text += "— KARNEX AI HR\n"

    access_key_html = ""
    if access_key:
        access_key_html = f"""
      <div style="margin:16px 0;padding:16px 20px;background:#f1f5f9;border:2px dashed #6366f1;border-radius:12px;">
        <p style="margin:0 0 4px;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#64748b;font-weight:700;">Secure Access Key</p>
        <p style="margin:0;font-size:22px;font-weight:900;letter-spacing:0.15em;color:#1e293b;font-family:monospace;">{access_key}</p>
        <p style="margin:8px 0 0;font-size:12px;color:#64748b;">Registered email: <strong>{to_email}</strong></p>
      </div>
      <p style="font-size:13px;color:#ef4444;font-weight:600;">⚠ Do NOT share your access key or interview link with anyone.</p>
"""

    html = f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;line-height:1.6;color:#1e293b;">
      <p>Hello <strong>{candidate_name}</strong>,</p>
      <p>Your <strong>KARNEX AI interview</strong> is scheduled.</p>
      <p><strong>When:</strong> {scheduled_at_local or "See HR"}</p>
      {access_key_html}
      <p><a href="{invite_url}" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none;font-weight:700;">Open interview</a></p>
      <p style="word-break:break-all;font-size:13px;color:#64748b;">{invite_url}</p>
      {"<p><strong>Notes:</strong> " + notes + "</p>" if notes else ""}
      <p>— KARNEX AI HR</p>
    </div>
    """
    return send_email(to_email, subject, text, html)
