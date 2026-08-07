"""Human interview rounds on a candidate profile — vocabulary, validation, CRUD.

The dropdown values live HERE and are served to the UI via
GET /api/candidate-profiles/{id}/interview-rounds/options, so the form and the
server can never disagree about what is selectable.

Values mirror the Zoho Interview_Round subform so imported history and rounds
entered in the app are directly comparable.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Employee, InterviewEvent

#: Interviewer_Category
CATEGORIES = ["Internal", "External"]

#: Interview_Round -> stored in interview_events.kind
ROUNDS: list[tuple[str, str]] = [
    ("L1_Interview", "L1 - Interview"),
    ("L2_F2F", "L2 - Interview"),
    ("L3_Interview", "L3 - Interview"),
    ("L4_Interview", "L4 - Interview"),
    # The customer's own round, recorded by Sales after the client interviews
    # the candidate. Zoho-imported history already contains this kind and the
    # display ordering already ranks it last, but the live API rejected it —
    # so the final step of the pipeline could not be written down.
    ("Customer_Interview", "Customer Interview"),
]
ROUND_VALUES = [value for value, _ in ROUNDS]

#: Which roles may write which round.
#:
#: A single WRITE_ROLES tuple could not express this: RMG owns the technical
#: ladder and Sales owns the customer conversation, and neither should be able
#: to write the other's rounds. Sales recording an L2 result would be inventing
#: an engineering opinion; RMG recording customer feedback would be inventing
#: the client's.
ROUND_WRITE_ROLES: dict[str, tuple[str, ...]] = {
    "L1_Interview": ("RMG",),
    "L2_F2F": ("RMG",),
    "L3_Interview": ("RMG",),
    "L4_Interview": ("RMG",),
    "Customer_Interview": ("Sales", "Sales_Head"),
}

#: Interview_Duration, in minutes.
DURATIONS = [15, 30, 45, 60, 90, 120, 180]

#: Interview_Status
STATUSES = [
    "Cancelled",
    "Completed",
    "In-Progress",
    "No Show",
    "Pending",
    "Rescheduled Requested By Candidate",
    "Rescheduled Requested By Panel",
    "Scheduled",
]

#: Result — ordered worst to best, matching the Zoho scale.
RESULTS = ["No Hire", "Leaning No", "Leaning Hire", "Hire", "Strong Hire"]

#: UserRole — who conducted the round.
USER_ROLES = ["Customer", "HR", "Interviewer", "RMG", "Sales", "TA"]

#: Any role that may write SOME round. Used as the coarse endpoint gate; the
#: per-kind check below is what actually decides. (Admin/CEO are implicit.)
WRITE_ROLES = tuple(sorted({role for roles in ROUND_WRITE_ROLES.values() for role in roles}))


def roles_for_round(kind: str | None) -> tuple[str, ...]:
    """Roles permitted to write this round kind. Unknown kinds permit nobody."""
    return ROUND_WRITE_ROLES.get(str(kind or "").strip(), ())


def rounds_writable_by(user) -> list[str]:
    """Round kinds this user may create or edit.

    Drives both the server-side guard and the form's dropdown, so a user is
    never offered a round the save would reject.
    """
    if getattr(user, "is_admin", False):
        return list(ROUND_VALUES)
    user_roles = set(getattr(user, "roles", []) or [])
    return [kind for kind in ROUND_VALUES if user_roles & set(ROUND_WRITE_ROLES.get(kind, ()))]


def ensure_may_write_round(user, kind: str | None) -> None:
    """403 unless this user owns this round kind."""
    allowed = roles_for_round(kind)
    if getattr(user, "is_admin", False):
        return
    if not allowed:
        raise HTTPException(status_code=400, detail=f"Unknown interview round '{kind}'")
    if not (set(getattr(user, "roles", []) or []) & set(allowed)):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your role cannot record a '{kind}' round. "
                f"That round is owned by: {', '.join(allowed)}."
            ),
        )


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").split()).lower()


def _match(value: str | None, allowed: list[str], field: str, *, required: bool = False):
    """Case-insensitive match against the vocabulary, returning the canonical value.

    Imported Zoho rows carry variants like "ReScheduled Requested By Candidate";
    matching on a normalised key keeps those editable instead of rejecting them.
    """
    if value is None or not str(value).strip():
        if required:
            raise HTTPException(status_code=400, detail=f"{field} is required")
        return None
    hit = next((a for a in allowed if _norm(a) == _norm(value)), None)
    if hit is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field} '{value}'. Allowed: {', '.join(allowed)}",
        )
    return hit


def options(db: Session, user=None) -> dict:
    """Everything the feedback form needs to render its dropdowns.

    When `user` is supplied the round list is narrowed to the kinds they may
    actually write, so Sales is never offered an L2 that the save would reject
    and RMG is never offered a customer round.
    """
    employees = db.execute(
        select(Employee.id, Employee.first_name, Employee.middle_name, Employee.last_name,
               Employee.email, Employee.employee_code)
        .where(Employee.is_active.is_(True))
        .order_by(Employee.first_name, Employee.last_name)
    ).all()
    writable = set(rounds_writable_by(user)) if user is not None else set(ROUND_VALUES)
    return {
        "categories": CATEGORIES,
        # Every round is listed so existing rows still render with a label;
        # `writable` tells the form which may be chosen.
        "rounds": [{"value": v, "label": l, "writable": v in writable} for v, l in ROUNDS],
        "writable_rounds": [v for v in ROUND_VALUES if v in writable],
        "durations": DURATIONS,
        "statuses": STATUSES,
        "results": RESULTS,
        "user_roles": USER_ROLES,
        "employees": [
            {
                "id": e_id,
                "full_name": " ".join(p for p in (first, middle, last) if p),
                "email": email,
                "employee_code": code,
            }
            for e_id, first, middle, last, email, code in employees
        ],
    }


def validate_round(db: Session, payload, *, partial: bool = False) -> dict:
    """Payload -> validated column values. Raises 400 with a readable message.

    `partial=True` (PUT) only validates the fields actually supplied, so a caller
    can change one field without resending the whole round.
    """
    data: dict = {}
    given = payload.model_fields_set if partial else set(payload.model_fields.keys())

    if not partial or "kind" in given:
        data["kind"] = _match(payload.kind, ROUND_VALUES, "Interview Round", required=True)
    if not partial or "interview_category" in given:
        data["interview_category"] = _match(
            payload.interview_category, CATEGORIES, "Interview Category")
    if not partial or "status" in given:
        data["status"] = _match(payload.status, STATUSES, "Interview Status")
    if not partial or "result" in given:
        data["result"] = _match(payload.result, RESULTS, "Result")
    if not partial or "user_role" in given:
        data["user_role"] = _match(payload.user_role, USER_ROLES, "User Role")

    if not partial or "duration_minutes" in given:
        dur = payload.duration_minutes
        if dur is not None and dur not in DURATIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Interview Duration '{dur}'. "
                       f"Allowed: {', '.join(str(d) for d in DURATIONS)}",
            )
        data["duration_minutes"] = dur

    if not partial or "employee_id" in given:
        emp_id = payload.employee_id
        if emp_id is not None:
            emp = db.get(Employee, emp_id)
            if emp is None:
                raise HTTPException(status_code=404, detail="Employee not found")
            data["employee_id"] = emp.id
            data["interviewer"] = " ".join(
                p for p in (emp.first_name, emp.middle_name, emp.last_name) if p)[:200]
        else:
            data["employee_id"] = None

    # External panellists have no employee row — accept a typed name instead.
    if "interviewer" in given and payload.interviewer is not None:
        typed = " ".join(str(payload.interviewer).split())
        if typed:
            data["interviewer"] = typed[:200]

    if not partial or "scheduled_at" in given:
        data["scheduled_at"] = payload.scheduled_at
        data["raw_when"] = (payload.scheduled_at.strftime("%Y-%m-%d %H:%M")
                            if payload.scheduled_at else None)
    if not partial or "feedback" in given:
        data["feedback"] = (payload.feedback or "").strip() or None
    if not partial or "meeting_link" in given:
        data["meeting_link"] = ((payload.meeting_link or "").strip() or None)
    if not partial or "stage" in given:
        data["stage"] = ((payload.stage or "").strip()[:120] or None)
    if not partial or "mode" in given:
        data["mode"] = ((payload.mode or "").strip()[:60] or None)
    return data


def get_round_or_404(db: Session, profile_id: int, event_id: int) -> InterviewEvent:
    event = db.get(InterviewEvent, event_id)
    if event is None or event.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Interview round not found")
    return event


def round_label(kind: str | None) -> str:
    return next((l for v, l in ROUNDS if v == kind), kind or "Interview")
