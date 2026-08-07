"""Role-specific CRM dashboards (read-only aggregates)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from schemas.common import envelope
from services import dashboards as svc

router = APIRouter(prefix="/api/dashboard", tags=["CRM: Dashboards"])


@router.get("/executive")
def executive_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("Sales_Head")),
):
    return envelope(svc.executive_dashboard(db))


@router.get("/rmg")
def rmg_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("RMG")),
):
    return envelope(svc.rmg_dashboard(db))


@router.get("/ta")
def ta_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    return envelope(svc.ta_dashboard(db))


@router.get("/finance")
def finance_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("Finance")),
):
    return envelope(svc.finance_dashboard(db))


@router.get("/interviews")
def upcoming_interviews(
    days: int = 30,
    db: Session = Depends(get_crm_db),
    # Sales and Sales_Head removed (Aug 2026): the upcoming-interviews view moved
    # to the Interview Calendar tab, which they do not have. Leaving them on this
    # endpoint would keep the data reachable by URL after removing the widget.
    user: CurrentUser = Depends(role_required("RMG", "TA")),
):
    """Upcoming human interview rounds (L2 face-to-face / customer interviews).

    Superseded for display purposes by GET /api/calendar/interviews, which also
    includes AI L1 sessions. Kept because it is a simpler flat list and is still
    the cheapest way to ask "what is coming up".
    """
    return envelope(svc.upcoming_interview_events(db, days=max(1, min(days, 120))))


@router.get("/bench")
def bench_dashboard(
    days: int = 60,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("RMG", "Sales_Head")),
):
    """Project employees rolling off within `days` — the redeployment radar."""
    return envelope(svc.bench_rolloffs(db, days=max(1, min(days, 180))))


@router.get("/bench/{employee_id}/matches")
def bench_matches(
    employee_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("RMG", "Sales_Head")),
):
    """Ranked open requirements for a rolling-off employee (ATS-scored on their CV)."""
    return envelope(svc.bench_requirement_matches(db, employee_id))


@router.get("/requirements")
def requirements_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("Sales", "Sales_Head", "RMG")),
):
    return envelope(svc.requirements_dashboard(db))
