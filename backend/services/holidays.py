"""Holiday calendar helpers — name resolution and branch-year scoped rows."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import BranchHolidayYear, CustomerBranch, Holiday, HolidayName
from services.crm_common import to_dict


def resolve_holiday_name(
    db: Session,
    *,
    holiday_name_id: int | None,
    name: str | None,
) -> tuple[int | None, str]:
    """Return (holiday_name_id, denormalized name). Requires one of the inputs."""
    if holiday_name_id is not None:
        row = db.get(HolidayName, holiday_name_id)
        if row is None or not row.is_active:
            raise HTTPException(status_code=400, detail="Holiday name not found or inactive")
        return row.id, row.name
    cleaned = (name or "").strip()
    if cleaned:
        return None, cleaned
    raise HTTPException(status_code=400, detail="holiday_name_id or name is required")


def get_branch_year_or_404(
    db: Session,
    branch_id: int,
    calendar_year: int,
) -> BranchHolidayYear:
    row = db.execute(
        select(BranchHolidayYear).where(
            BranchHolidayYear.branch_id == branch_id,
            BranchHolidayYear.calendar_year == calendar_year,
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Holiday year not found for this branch")
    return row


def branch_holidays_for_year(
    db: Session,
    branch_id: int,
    calendar_year: int,
    *,
    is_active: bool = True,
) -> list[Holiday]:
    return db.execute(
        select(Holiday).where(
            Holiday.branch_id == branch_id,
            Holiday.year == calendar_year,
            Holiday.is_active.is_(is_active),
        ).order_by(Holiday.holiday_date.asc(), Holiday.id.asc())
    ).scalars().all()


def create_branch_holiday(
    db: Session,
    branch: CustomerBranch,
    calendar_year: int,
    *,
    holiday_name_id: int | None,
    name: str | None,
    holiday_date,
    observance: str,
    holiday_type: str = "Customer",
) -> Holiday:
    from services.branch_policy import ensure_year_editable

    ensure_year_editable(db, branch.id, calendar_year)
    if holiday_date.year != calendar_year:
        raise HTTPException(
            status_code=400,
            detail=f"Holiday date must fall in calendar year {calendar_year}",
        )
    year_row = get_branch_year_or_404(db, branch.id, calendar_year)
    name_id, resolved_name = resolve_holiday_name(db, holiday_name_id=holiday_name_id, name=name)
    obj = Holiday(
        holiday_name_id=name_id,
        name=resolved_name,
        holiday_date=holiday_date,
        holiday_type=holiday_type,
        observance=observance,
        customer_id=branch.customer_id,
        branch_id=branch.id,
        holiday_calendar_id=year_row.id,
        year=calendar_year,
        is_active=True,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def update_branch_holiday(
    db: Session,
    branch: CustomerBranch,
    calendar_year: int,
    holiday: Holiday,
    data: dict,
) -> Holiday:
    from services.branch_policy import ensure_year_editable

    if holiday.branch_id != branch.id or holiday.year != calendar_year:
        raise HTTPException(status_code=404, detail="Holiday not found in this branch year")
    ensure_year_editable(db, branch.id, calendar_year)
    if "holiday_name_id" in data or "name" in data:
        name_id, resolved_name = resolve_holiday_name(
            db,
            holiday_name_id=data.get("holiday_name_id", holiday.holiday_name_id),
            name=data.get("name", holiday.name if "holiday_name_id" not in data else None),
        )
        holiday.holiday_name_id = name_id
        holiday.name = resolved_name
        data.pop("holiday_name_id", None)
        data.pop("name", None)
    for field, value in data.items():
        setattr(holiday, field, value)
    if holiday.holiday_date.year != calendar_year:
        raise HTTPException(
            status_code=400,
            detail=f"Holiday date must fall in calendar year {calendar_year}",
        )
    holiday.year = holiday.holiday_date.year
    db.commit()
    db.refresh(holiday)
    return holiday


def deactivate_branch_holiday(
    db: Session,
    branch: CustomerBranch,
    calendar_year: int,
    holiday: Holiday,
) -> Holiday:
    from services.branch_policy import ensure_year_editable

    if holiday.branch_id != branch.id or holiday.year != calendar_year:
        raise HTTPException(status_code=404, detail="Holiday not found in this branch year")
    ensure_year_editable(db, branch.id, calendar_year)
    holiday.is_active = False
    db.commit()
    db.refresh(holiday)
    return holiday


def holiday_out(holiday: Holiday) -> dict:
    return to_dict(holiday)
