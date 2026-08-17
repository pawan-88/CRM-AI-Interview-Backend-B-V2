"""Admin email administration: routing flows, per-user pause, and invitations.

This is the Users-tab backend for "never edit code when people change":

* /api/email-flows          — which roles receive which application email.
* /api/users/{id}/email-pause — stop mailing a leaver without touching data.
* /api/users/invite         — add a person by email alone; they get an
  invitation mail with a set-password link and finish their own profile.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from models import NotificationRoute, RoleName, UserNotifyPref
from schemas.common import envelope
from services import users_admin as users_svc
from services.email_outbox import app_url, queue_email, render_html, render_text
from services.notify import paused_user_ids

router = APIRouter(prefix="/api", tags=["CRM: Email flows (Admin)"])

admin_only = role_required()

#: Every routable event, with the DEFAULT the code falls back to when the
#: admin has not saved a row. Adding a new notify_roles(..., event=...) call
#: site only needs a line here to become admin-editable.
EVENTS: list[dict] = [
    {"event": "timesheet.submitted",
     "label": "Timesheet submitted for approval",
     "description": "Sent when an employee submits a monthly timesheet.",
     "default_roles": ["HR", "RMG", "Sales", "CEO"]},
    {"event": "leave.submitted",
     "label": "Leave application submitted",
     "description": "Sent when an employee applies for leave.",
     "default_roles": ["HR"]},
    {"event": "leave.credit_repaired",
     "label": "Leave accrual repaired a missed month",
     "description": "Sent when the monthly leave-credit job finds a month that never ran and "
                    "credits it late. Balances are corrected automatically; this tells you which "
                    "months were affected, because anything already settled from them was wrong.",
     "default_roles": ["HR", "CEO"]},
    {"event": "invoice.generated",
     "label": "Invoice generated from a timesheet",
     "description": "Sent when a reviewer generates the invoice for an approved timesheet.",
     "default_roles": ["Finance"]},
    {"event": "opportunity.submitted",
     "label": "Opportunity awaiting approval",
     "description": "Sent when an opportunity is created or resubmitted for Sales Head approval.",
     "default_roles": ["Sales_Head"]},
    {"event": "opportunity.approved",
     "label": "Opportunity approved — engineering review",
     "description": "Sent when an approved opportunity auto-creates a requirement that needs engineering review.",
     "default_roles": ["RMG"]},
    {"event": "requirement.submitted",
     "label": "Requirement submitted for approval",
     "description": "Sent when a requirement is submitted for Sales Head approval.",
     "default_roles": ["Sales_Head"]},
    {"event": "requirement.sales_approved",
     "label": "Requirement approved by Sales Head",
     "description": "Sent when Sales Head approves a requirement and it needs engineering review.",
     "default_roles": ["RMG"]},
    {"event": "requirement.engineering_approved",
     "label": "Requirement open for sourcing",
     "description": "Sent when engineering approves a requirement and sourcing can start.",
     "default_roles": ["TA"]},
    {"event": "candidate.stage_arrival",
     "label": "Candidate reaches a pipeline stage",
     "description": "Sent when a candidate profile arrives at a stage that needs someone's action. "
                    "Note: by default the receiving role varies per stage (TA, RMG, Sales…); "
                    "customising this flow routes EVERY stage arrival to the roles you pick.",
     "default_roles": []},
    {"event": "candidate.l2_scheduled",
     "label": "L2 face-to-face scheduled",
     "description": "Sent when an L2 face-to-face round is scheduled for a candidate.",
     "default_roles": ["TA"]},
    {"event": "ai_interview.completed",
     "label": "AI interview completed",
     "description": "Sent when a candidate finishes the AI interview, with the score.",
     "default_roles": ["TA"]},
    {"event": "ai_interview.passed_review",
     "label": "AI L1 passed — needs review",
     "description": "Sent when a candidate passes the AI L1 threshold and the report needs a decision.",
     "default_roles": ["RMG"]},
    {"event": "slot.confirmed",
     "label": "Interview slot confirmed",
     "description": "Sent when a candidate confirms an interview slot and AI L1 is scheduled.",
     "default_roles": ["TA"]},
    {"event": "slot.manual_followup",
     "label": "Shortlisted — manual follow-up needed",
     "description": "Sent when a resume clears the auto-shortlist threshold but has no email/phone for the invite.",
     "default_roles": ["TA"]},
    {"event": "timesheet.due_reminder",
     "label": "Timesheet due — reminder to the employee",
     "description": "Sent by the daily scheduler to employees whose previous-month timesheet is not submitted.",
     "default_roles": []},
    {"event": "timesheet.due_digest",
     "label": "Timesheet due — digest to approvers",
     "description": "Daily scheduler digest listing everyone still due for the previous month.",
     "default_roles": ["HR", "RMG"]},
    {"event": "po.expiry_warning",
     "label": "Purchase order expiring / expired",
     "description": "Milestone notices before expiry (45/30/15/5/1 days), on the expiry day, and while overdue.",
     "default_roles": ["Finance", "Sales_Head"]},
    {"event": "invoice.auto_drafted",
     "label": "Invoice auto-generated (recurring billing)",
     "description": "Sent when the scheduler raises an invoice for a recurring-billing project, or when one needs manual attention.",
     "default_roles": ["Finance"]},
    {"event": "template_request.created",
     "label": "Interview template requested",
     "description": "Sent when TA raises a request for an interview template.",
     "default_roles": ["RMG"]},
]

ALL_ROLES = [m.value for m in RoleName]


# Deliberately NOT pydantic's EmailStr: that type needs the optional
# email-validator package, and importing this module without it installed
# raises — which silently unregisters this whole router (the registry
# isolates failures) and turns every endpoint here into a 405 from the SPA
# catch-all. A plain regex has no such failure mode.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(value: str) -> str:
    addr = (value or "").strip().lower()
    if not _EMAIL_RE.match(addr):
        raise ValueError(f"'{value}' is not a valid email address")
    return addr


class FlowIn(BaseModel):
    roles: list[str] = Field(default_factory=list)
    extra_emails: list[str] = Field(default_factory=list)
    enabled: bool = True
    #: Admin-authored wording (0071). None/empty = code-composed text.
    #: Placeholders: {subject} {body} {recipient} {company}.
    subject_template: str | None = Field(default=None, max_length=255)
    body_template: str | None = Field(default=None, max_length=8000)

    @field_validator("extra_emails")
    @classmethod
    def _valid_extras(cls, v):
        return [_clean_email(e) for e in (v or [])]


class PauseIn(BaseModel):
    paused: bool


class InviteIn(BaseModel):
    email: str
    full_name: str = ""
    roles: list[str] = Field(default_factory=list)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v):
        return _clean_email(v)


@router.get("/email-flows")
def list_email_flows(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    rows = {
        r.event: r
        for r in db.execute(select(NotificationRoute)).scalars().all()
    }
    flows = []
    for spec in EVENTS:
        row = rows.get(spec["event"])
        flows.append({
            **spec,
            "roles": [str(x) for x in (row.roles or [])] if row else list(spec["default_roles"]),
            "extra_emails": [str(x) for x in (row.extra_emails or [])] if row else [],
            "enabled": bool(row.enabled) if row else True,
            "subject_template": row.subject_template if row else None,
            "body_template": row.body_template if row else None,
            "customized": row is not None,
        })
    return envelope(data={
        "flows": flows,
        "all_roles": ALL_ROLES,
        "paused_user_ids": sorted(paused_user_ids(db)),
    })


@router.put("/email-flows/{event}")
def save_email_flow(event: str, body: FlowIn,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
    spec = next((s for s in EVENTS if s["event"] == event), None)
    if spec is None:
        raise HTTPException(status_code=404, detail="Unknown email flow")
    bad = [r for r in body.roles if r not in ALL_ROLES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown role(s): {', '.join(bad)}")
    if body.enabled and not body.roles and not body.extra_emails:
        raise HTTPException(
            status_code=400,
            detail="An enabled flow needs at least one role or extra email — or disable it instead",
        )
    row = db.execute(
        select(NotificationRoute).where(NotificationRoute.event == event)
    ).scalars().first()
    if row is None:
        row = NotificationRoute(event=event)
        db.add(row)
    row.roles = list(body.roles)
    row.extra_emails = [str(e) for e in body.extra_emails]
    row.enabled = body.enabled
    # Empty string = clear the template (back to code-composed text).
    row.subject_template = (body.subject_template or "").strip() or None
    row.body_template = (body.body_template or "").strip() or None
    row.updated_by = user.id
    db.commit()
    return envelope(
        data={"event": event, "roles": row.roles, "extra_emails": row.extra_emails,
              "enabled": row.enabled, "subject_template": row.subject_template,
              "body_template": row.body_template},
        message=f"Email flow '{spec['label']}' saved",
    )


@router.delete("/email-flows/{event}")
def reset_email_flow(event: str, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    """Back to the code default (removes the customization row)."""
    row = db.execute(
        select(NotificationRoute).where(NotificationRoute.event == event)
    ).scalars().first()
    if row is not None:
        db.delete(row)
        db.commit()
    return envelope(data={"event": event}, message="Flow reset to default")


# ------------------------------------------------------------- email pause


@router.post("/users/{user_id}/email-pause")
def set_email_pause(user_id: int, body: PauseIn,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
    """Pause / resume application email for one user (leavers, long absences).

    Deliberately separate from deactivation: pausing keeps their login and
    history intact while making sure nothing is sent to a dead mailbox.
    """
    pref = db.get(UserNotifyPref, user_id)
    if pref is None:
        pref = UserNotifyPref(user_id=user_id)
        db.add(pref)
    pref.email_paused = body.paused
    pref.updated_by = user.id
    db.commit()
    return envelope(
        data={"user_id": user_id, "email_paused": pref.email_paused},
        message="Email paused for this user" if body.paused else "Email resumed for this user",
    )


# ------------------------------------------------------------ daily scheduler


@router.get("/scheduler/status")
def scheduler_status(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    """Is the scheduler alive, and what did each job do last time?

    This exists so "the reminders stopped" is answerable in one look instead of
    by reading server logs.
    """
    from datetime import datetime, timedelta, timezone

    from services.scheduler import DEFAULTS, JOBS, last_runs
    from services.scheduler import _flag as job_enabled

    labels = {"timesheet_reminders": "Timesheet due reminders",
              "po_expiry": "Purchase order expiry notices",
              "recurring_invoices": "Recurring invoice drafts",
              "pe_leave_credit": "Monthly leave credit"}
    runs = last_runs()

    def _stale(entry: dict) -> bool:
        """No run in 48h. Every job is daily, so a two-day silence means the
        worker is not running — which for leave credit means balances are
        quietly drifting, the exact failure this panel exists to surface."""
        stamp = (entry or {}).get("at")
        if not stamp:
            return True
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - when > timedelta(hours=48)

    return envelope(data={
        "jobs": [
            {"job": job, "setting": flag_key,
             "label": labels.get(job, job),
             "last_run": runs.get(job, {}),
             "enabled": job_enabled(flag_key),
             "stale": _stale(runs.get(job, {}))}
            for job, (flag_key, _fn) in JOBS.items()
        ],
        "defaults": DEFAULTS,
    })


@router.get("/backup/status")
def backup_status(user: CurrentUser = Depends(admin_only)):
    """Age and outcome of the last backup, so silent failure is impossible.

    Reads the status file `scripts/backup_karnex.py` writes. Backups that stop
    running are the classic silent disaster: everything looks fine until the
    day you need a restore. `stale` goes true after 48h without a good run.
    """
    import json as _json
    import os as _os
    from datetime import datetime, timezone
    from pathlib import Path

    configured = (_os.getenv("BACKUP_DIR") or "").strip()
    root = Path(__file__).resolve().parents[3]
    candidates = [Path(configured)] if configured else []
    candidates += [root.parent / "KarnexBackups", root / "KarnexBackups"]
    for folder in candidates:
        status_file = folder / "backup-status.json"
        if status_file.is_file():
            try:
                data = _json.loads(status_file.read_text(encoding="utf-8"))
            except Exception as exc:
                return envelope(data={"configured": True, "readable": False,
                                      "error": str(exc)[:120]},
                                message="Backup status file is unreadable")
            age_hours = None
            stamp = data.get("finished_at") or data.get("started_at")
            if stamp:
                try:
                    then = datetime.fromisoformat(stamp)
                    if then.tzinfo is None:
                        then = then.replace(tzinfo=timezone.utc)
                    age_hours = round((datetime.now(timezone.utc) - then).total_seconds() / 3600, 1)
                except Exception:
                    pass
            stale = data.get("ok") is not True or (age_hours is not None and age_hours > 48)
            return envelope(data={
                "configured": True, "readable": True, "folder": str(folder),
                "last": data, "age_hours": age_hours, "stale": stale,
            }, message="Backup status")
    return envelope(
        data={"configured": False, "stale": True,
              "hint": "Schedule backup_karnex.bat daily in Windows Task Scheduler."},
        message="No backup has run yet",
    )


@router.post("/scheduler/run")
def scheduler_run_now(job: str | None = None,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(admin_only)):
    """Run the daily jobs immediately (all, or one by name).

    Safe to press repeatedly: every message the jobs send carries a milestone
    dedupe key, so a forced run cannot re-send anything already sent.
    """
    from services.scheduler import JOBS, run_due_jobs

    if job and job not in JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job '{job}'")
    results = run_due_jobs(force=True, only=job)
    return envelope(data=results, message="Scheduler run complete")


# -------------------------------------------------------- action permissions


class ActionPermissionIn(BaseModel):
    roles: list[str] = Field(default_factory=list)


@router.get("/action-permissions")
def list_action_permissions(db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(admin_only)):
    """Which roles may perform each configurable write action. Admin/CEO
    always pass regardless — an empty role list means admins only."""
    from services.action_permissions import effective as ap_effective

    return envelope(data={"actions": ap_effective(db), "all_roles": ALL_ROLES})


@router.put("/action-permissions/{action}")
def save_action_permission(action: str, body: ActionPermissionIn,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    from models import ActionPermission
    from services.action_permissions import ACTIONS, invalidate as ap_invalidate

    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown action")
    bad = [r for r in body.roles if r not in ALL_ROLES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown role(s): {', '.join(bad)}")
    row = db.execute(
        select(ActionPermission).where(ActionPermission.action == action)
    ).scalars().first()
    if row is None:
        row = ActionPermission(action=action)
        db.add(row)
    row.roles = list(dict.fromkeys(body.roles))
    row.updated_by = user.id
    db.commit()
    ap_invalidate()
    label = ACTIONS[action][0]
    who = ", ".join(row.roles) if row.roles else "Admin/CEO only"
    return envelope(data={"action": action, "roles": row.roles},
                    message=f"'{label}' can now be done by: {who}")


@router.delete("/action-permissions/{action}")
def reset_action_permission(action: str, db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(admin_only)):
    """Back to the code default (removes the customization row)."""
    from models import ActionPermission
    from services.action_permissions import invalidate as ap_invalidate

    row = db.execute(
        select(ActionPermission).where(ActionPermission.action == action)
    ).scalars().first()
    if row is not None:
        db.delete(row)
        db.commit()
        ap_invalidate()
    return envelope(data={"action": action}, message="Permission reset to default")


# -------------------------------------------------------- organisation settings


class OrgSettingsIn(BaseModel):
    """Bulk upsert: {key: value}. Only known org-settings keys are accepted."""

    values: dict[str, str] = Field(default_factory=dict)


@router.get("/org-settings")
def get_org_settings(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    """Company identity, email/link and interview settings with provenance —
    each value says whether it comes from the Settings page, the server
    environment, or the code default."""
    from services.org_settings import KEYS, effective

    return envelope(data={"settings": effective(db), "keys": sorted(KEYS)})


@router.put("/org-settings")
def put_org_settings(body: OrgSettingsIn,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    from models import AppSetting
    from services.org_settings import KEYS, invalidate

    unknown = [k for k in body.values if k not in KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown setting(s): {', '.join(unknown)}")
    url = (body.values.get("email.public_base_url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400,
                            detail="Public base URL must start with http:// or https://")
    for key, value in body.values.items():
        row = db.get(AppSetting, key)
        value = (value or "").strip()
        if row is None:
            if value:
                db.add(AppSetting(key=key, value=value,
                                  description="Organisation setting (Settings page)"))
        elif value:
            row.value = value
        else:
            # Cleared in the UI -> back to the environment/code fallback.
            db.delete(row)
    db.commit()
    invalidate()
    return envelope(data={"saved": sorted(body.values)}, message="Organisation settings saved")


class TestEmailIn(BaseModel):
    to: str

    @field_validator("to")
    @classmethod
    def _valid_to(cls, v):
        return _clean_email(v)


@router.post("/org-settings/test-email")
def send_test_email(body: TestEmailIn,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
    """Queue a test email through the real outbox so the admin can verify the
    whole chain — settings, sender identity, SMTP — with one click."""
    from services.org_settings import setting

    base = setting("email.public_base_url") or "(no public base URL set)"
    text = render_text(
        "Karnex test email",
        f"This is a test email sent from the Settings page by "
        f"{user.full_name or user.username}. If you can read this, SMTP and the "
        f"email outbox are working. Links currently point at: {base}",
        action_label="Open Karnex" if base.startswith("http") else "",
        action_url=base if base.startswith("http") else "",
    )
    html = render_html(
        "Karnex test email",
        f"This is a test email sent from the Settings page by "
        f"{user.full_name or user.username}. If you can read this, SMTP and the "
        f"email outbox are working. Links currently point at: {base}",
        action_label="Open Karnex" if base.startswith("http") else "",
        action_url=base if base.startswith("http") else "",
    )
    row = queue_email(
        db,
        to_email=body.to,
        to_name="",
        subject="Karnex test email",
        body_text=text,
        body_html=html,
        event="settings.test_email",
        actor=user,
    )
    db.commit()
    if row is None:
        raise HTTPException(status_code=400,
                            detail="Email is disabled in settings — the test was not queued")
    return envelope(
        data={"queued": True, "outbox_id": row.id},
        message=f"Test email queued to {body.to} — check the inbox (and Email Outbox for status)",
    )


# -------------------------------------------------------------- invitations


@router.post("/users/invite")
def invite_user(body: InviteIn,
                db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    """Create an account from an email address and send an invitation.

    The account is created with a random unusable password; the invitation
    email carries a set-password link (the existing reset-token flow), so the
    new person chooses their own password and fills their own details. The
    admin only types an email and picks roles.
    """
    from auth_db import create_password_reset

    email = str(body.email).strip().lower()
    full_name = (body.full_name or "").strip() or email.split("@", 1)[0].replace(".", " ").title()
    username = email  # unique, memorable, and what they will type at login

    created = users_svc.create_user(
        db,
        full_name=full_name,
        email=email,
        username=username,
        password=secrets.token_urlsafe(24),  # unusable until they set their own
        legacy_role="hr",
        role_names=body.roles,
    )
    uid = int(created["id"])

    # Set-password link via the existing single-use reset-token flow, but with
    # a longer window than a forgot-password (the invitee may open it tomorrow).
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    create_password_reset(users_svc._legacy_db_target(), uid, token_hash, expires_at)

    base = app_url("") or ""
    link = f"{base}/?reset_token={token}" if base else f"/?reset_token={token}"
    roles_txt = ", ".join(created["roles"]) or "none"
    title = "You're invited to Karnex"
    message = (
        f"{user.full_name or user.username} invited you to the Karnex application "
        f"with the role(s): {roles_txt}. Set your password to activate your account; "
        f"the link works for 7 days. After signing in you can complete your own profile."
    )
    text = render_text(title, message, action_label="Set your password", action_url=link)
    html = render_html(title, message, action_label="Set your password", action_url=link)
    queue_email(
        db,
        to_email=email,
        to_name=full_name,
        subject="Invitation to Karnex — set your password",
        body_text=text,
        body_html=html,
        event="user.invited",
        actor=user,
        dedupe_key=f"user.invited:{uid}:{token_hash[:12]}",
    )
    db.commit()
    return envelope(
        data={**created, "invited": True},
        message=f"Invitation sent to {email} (roles: {roles_txt})",
    )
