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


@router.get("/requirements")
def requirements_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("Sales", "Sales_Head", "RMG")),
):
    return envelope(svc.requirements_dashboard(db))
