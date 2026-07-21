"""Holiday calendar API (/api/holidays, /api/holiday-names).

Feeds timesheet day generation: entries whose date matches an active mandatory
holiday for the project's customer/branch are pre-marked Holiday / non-working.
Read: any authenticated CRM user. Write: HR (Admin/CEO pass implicitly).
Rows are soft-deactivated (is_active=false) — never hard-deleted.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import extract, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, get_crm_db, get_current_user, page_params, role_required,
)
from models import Customer, CustomerBranch, Holiday, HolidayName
from schemas.common import envelope
from schemas.leave import HolidayCreate, HolidayNameCreate, HolidayUpdate
from services.crm_common import paginate, to_dict
from services.holidays import holiday_out, resolve_holiday_name

router = APIRouter(prefix="/api/holidays", tags=["CRM: Holidays"])
names_router = APIRouter(prefix="/api/holiday-names", tags=["CRM: Holiday Names"])

hr_only = role_required("HR")


def _get_or_404(db: Session, holiday_id: int) -> Holiday:
    obj = db.get(Holiday, holiday_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    return obj


def _validate_refs(db: Session, customer_id: int | None, branch_id: int | None) -> None:
    if customer_id is not None and db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="Customer not found")
    if branch_id is not None:
        branch = db.get(CustomerBranch, branch_id)
        if branch is None:
            raise HTTPException(status_code=400, detail="Customer branch not found")
        if customer_id is not None and branch.customer_id != customer_id:
            raise HTTPException(status_code=400,
                                detail="Branch does not belong to the given customer")


# ---------------------------------------------------------------- holiday names
@names_router.get("")
def list_holiday_names(
    is_active: bool = True,
    q: str | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = select(HolidayName).where(HolidayName.is_active == is_active)
    if q:
        stmt = stmt.where(HolidayName.name.ilike(f"%{q.strip()}%"))
    stmt = stmt.order_by(HolidayName.name.asc())
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[to_dict(n) for n in items], message="Holiday names", meta=meta)


@names_router.post("")
def create_holiday_name(
    body: HolidayNameCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(hr_only),
):
    exists = db.execute(
        select(HolidayName).where(HolidayName.name == body.name.strip())
    ).scalars().first()
    if exists is not None:
        raise HTTPException(status_code=400, detail="Holiday name already exists")
    obj = HolidayName(name=body.name.strip(), is_active=body.is_active)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return envelope(data=to_dict(obj), message="Holiday name created")


# ---------------------------------------------------------------- holidays
@router.get("")
def list_holidays(
    year: int | None = None,
    month: int | None = None,
    customer_id: int | None = None,
    branch_id: int | None = None,
    is_active: bool = True,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = select(Holiday).where(Holiday.is_active == is_active)
    if year is not None:
        stmt = stmt.where(Holiday.year == year)
    if month is not None:
        stmt = stmt.where(extract("month", Holiday.holiday_date) == month)
    if customer_id is not None:
        stmt = stmt.where(Holiday.customer_id == customer_id)
    if branch_id is not None:
        stmt = stmt.where(Holiday.branch_id == branch_id)
    stmt = stmt.order_by(Holiday.holiday_date.asc(), Holiday.id.asc())
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[holiday_out(h) for h in items], message="Holiday list", meta=meta)


@router.post("")
def create_holiday(
    body: HolidayCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(hr_only),
):
    _validate_refs(db, body.customer_id, body.branch_id)
    name_id, name = resolve_holiday_name(db, holiday_name_id=body.holiday_name_id, name=body.name)
    obj = Holiday(
        holiday_name_id=name_id,
        name=name,
        holiday_date=body.holiday_date,
        holiday_type=body.holiday_type,
        observance=body.observance,
        customer_id=body.customer_id,
        branch_id=body.branch_id,
        year=body.holiday_date.year,
        is_active=body.is_active,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return envelope(data=holiday_out(obj), message="Holiday created")


@router.get("/{holiday_id}")
def get_holiday(
    holiday_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    return envelope(data=holiday_out(_get_or_404(db, holiday_id)))


@router.put("/{holiday_id}")
def update_holiday(
    holiday_id: int,
    body: HolidayUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(hr_only),
):
    obj = _get_or_404(db, holiday_id)
    data = body.model_dump(exclude_unset=True)
    customer_id = data.get("customer_id", obj.customer_id)
    branch_id = data.get("branch_id", obj.branch_id)
    _validate_refs(db, customer_id, branch_id)
    if "holiday_name_id" in data or "name" in data:
        name_id, name = resolve_holiday_name(
            db,
            holiday_name_id=data.get("holiday_name_id", obj.holiday_name_id),
            name=data.get("name", obj.name if "holiday_name_id" not in data else None),
        )
        obj.holiday_name_id = name_id
        obj.name = name
        data.pop("holiday_name_id", None)
        data.pop("name", None)
    for field, value in data.items():
        setattr(obj, field, value)
    obj.year = obj.holiday_date.year  # keep the denormalized year in sync
    db.commit()
    db.refresh(obj)
    return envelope(data=holiday_out(obj), message="Holiday updated")


@router.delete("/{holiday_id}")
def deactivate_holiday(
    holiday_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(hr_only),
):
    obj = _get_or_404(db, holiday_id)
    obj.is_active = False  # soft delete — calendar rows are never destroyed
    db.commit()
    return envelope(data=holiday_out(obj), message="Holiday deactivated")
