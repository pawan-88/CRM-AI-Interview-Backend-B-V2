"""Monthly PE leave credit / carry-forward / expiry job.

Callable from CLI (`scripts/run_pe_leave_credit.py`) or a scheduler. Credits are
applied to `project_employee_leave_details` only for **active, non-exited**
Project Employees. Policy edits after seed do not rewrite opening balances.

Idempotency: one `leave_accrual_events` row per PE/leave_type/period via
`source = pe_credit:{pe_id}:{leave_type_id}:{YYYY-MM}`.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    CustomerLeavePolicy, LeaveAccrualEvent, ProjectEmployee, ProjectEmployeeLeaveDetail,
)
from services.project_employee_billing import carry_forward, prorate_credit

ZERO = Decimal("0")


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _period_key(as_of: date) -> str:
    return f"{as_of.year:04d}-{as_of.month:02d}"


def _credit_source(pe_id: int, leave_type_id: int, period: str) -> str:
    return f"pe_credit:{pe_id}:{leave_type_id}:{period}"


def _already_credited(db: Session, pe_id: int, leave_type_id: int, period: str) -> bool:
    src = _credit_source(pe_id, leave_type_id, period)
    row = db.execute(
        select(LeaveAccrualEvent.id).where(LeaveAccrualEvent.source == src).limit(1)
    ).first()
    return row is not None


def _policy_period_amount(policy: CustomerLeavePolicy) -> Decimal:
    """The per-cycle accrual amount implied by a customer leave policy (live).

    Monthly    -> leave_credit_balance per month
    Quarterly  -> leave_credit_balance / 4
    Yearly     -> leave_credit_balance / 12 only when credited End_Of_Period
    One_Time   -> 0 (seeded upfront, never accrues per cycle)
    """
    bal = Decimal(policy.leave_credit_balance or 0)
    credit_type = (policy.leave_credit_type or "Monthly")
    timing = (policy.leave_credit_timing or "Start_Of_Period")
    if credit_type == "One_Time":
        return Decimal("0")
    if credit_type == "Yearly":
        return (bal / Decimal(12)).quantize(Decimal("0.01")) if (bal and timing == "End_Of_Period") else Decimal("0")
    if credit_type == "Quarterly":
        return (bal / Decimal(4)).quantize(Decimal("0.01")) if bal else Decimal("0")
    return bal  # Monthly


def _days_present_in_month(pe: ProjectEmployee, as_of: date) -> tuple[int, int]:
    dim = _days_in_month(as_of.year, as_of.month)
    start = date(as_of.year, as_of.month, 1)
    end = date(as_of.year, as_of.month, dim)
    if pe.onboarding_date and pe.onboarding_date > end:
        return 0, dim
    if pe.exit_date and pe.exit_date < start:
        return 0, dim
    present_start = max(start, pe.onboarding_date or start)
    present_end = min(end, pe.exit_date or end)
    if present_end < present_start:
        return 0, dim
    return (present_end - present_start).days + 1, dim


def credit_one_pe_leave_row(
    db: Session,
    pe: ProjectEmployee,
    row: ProjectEmployeeLeaveDetail,
    as_of: date,
    *,
    force: bool = False,
) -> Decimal:
    """Credit one leave-detail row for the month of `as_of`. Return amount credited."""
    if pe.is_exit or not pe.is_active:
        return ZERO
    period = _period_key(as_of)
    if not force and _already_credited(db, pe.id, row.leave_type_id, period):
        return ZERO

    policy = (
        db.get(CustomerLeavePolicy, row.customer_leave_policy_id)
        if row.customer_leave_policy_id else None
    )
    # Seed-by-copy governs the OPENING balance; the recurring per-period accrual
    # rate follows the live customer policy so a mid-engagement rate change takes
    # effect from the next credit cycle (never retroactively rewriting balances).
    accrual = _policy_period_amount(policy) if policy is not None else Decimal(row.leave_accrual or 0)
    if accrual <= 0:
        return ZERO

    credit_type = (policy.leave_credit_type if policy else "Monthly") or "Monthly"
    if credit_type == "One_Time":
        return ZERO
    if credit_type == "Yearly" and (policy is None or (policy.leave_credit_timing or "") != "End_Of_Period"):
        return ZERO

    days_present, dim = _days_present_in_month(pe, as_of)
    do_prorate = bool(policy and policy.prorate_balance_credit)
    amount = prorate_credit(accrual, days_present, dim) if do_prorate else accrual
    if days_present <= 0:
        amount = ZERO
    if amount <= 0:
        return ZERO

    if policy and policy.is_max_limit and policy.max_limit is not None:
        room = Decimal(policy.max_limit) - Decimal(row.leave_balance or 0)
        if room <= 0:
            return ZERO
        amount = min(amount, room)

    row.leave_balance = Decimal(row.leave_balance or 0) + amount
    db.add(LeaveAccrualEvent(
        employee_id=pe.employee_id,
        leave_type_id=row.leave_type_id,
        event_type="Accrual",
        amount=amount,
        balance_after=row.leave_balance,
        source=_credit_source(pe.id, row.leave_type_id, period),
        note=f"PE monthly credit {period}",
    ))
    return amount


def apply_year_end_carry(
    db: Session,
    pe: ProjectEmployee,
    row: ProjectEmployeeLeaveDetail,
    as_of: date,
) -> tuple[Decimal, Decimal]:
    """On Dec 31 apply carry-forward cap and expire remainder when policy says so."""
    if pe.is_exit or not pe.is_active:
        return ZERO, ZERO
    if as_of.month != 12 or as_of.day != 31:
        return ZERO, ZERO
    policy = (
        db.get(CustomerLeavePolicy, row.customer_leave_policy_id)
        if row.customer_leave_policy_id else None
    )
    remaining = Decimal(row.leave_balance or 0)
    if remaining <= 0 or policy is None:
        return ZERO, ZERO
    expire_enabled = bool(policy.leave_expire)
    has_cap = policy.maximum_carry_forward is not None
    if not expire_enabled and not has_cap:
        return ZERO, ZERO
    max_cf = Decimal(policy.maximum_carry_forward) if has_cap else remaining
    carried, expired = carry_forward(remaining, max_cf)
    if not expire_enabled:
        expired = ZERO
        carried = remaining if remaining <= max_cf else max_cf
        expired = remaining - carried
    row.leave_balance = carried
    period = str(as_of.year)
    if expired > 0:
        db.add(LeaveAccrualEvent(
            employee_id=pe.employee_id,
            leave_type_id=row.leave_type_id,
            event_type="Adjustment",
            amount=-expired,
            balance_after=row.leave_balance,
            source=f"pe_expire:{pe.id}:{row.leave_type_id}:{period}",
            note=f"PE leave expiry {period}",
        ))
    if carried > 0 and (expire_enabled or has_cap):
        db.add(LeaveAccrualEvent(
            employee_id=pe.employee_id,
            leave_type_id=row.leave_type_id,
            event_type="Carry_Forward",
            amount=carried,
            balance_after=row.leave_balance,
            source=f"pe_carry:{pe.id}:{row.leave_type_id}:{period}",
            note=f"PE carry forward into {as_of.year + 1}",
        ))
    return carried, expired


def run_pe_leave_credit(db: Session, as_of: date | None = None, *, pe_id: int | None = None) -> dict:
    """Run period credit (+ Dec 31 carry) for all or one PE. Returns summary counts."""
    as_of = as_of or date.today()
    stmt = select(ProjectEmployee).where(
        ProjectEmployee.is_active.is_(True),
        ProjectEmployee.is_exit.is_(False),
    )
    if pe_id is not None:
        stmt = stmt.where(ProjectEmployee.id == pe_id)
    pes = db.execute(stmt).scalars().all()
    credited = Decimal("0")
    rows_touched = 0
    for pe in pes:
        details = db.execute(
            select(ProjectEmployeeLeaveDetail).where(
                ProjectEmployeeLeaveDetail.project_employee_id == pe.id
            )
        ).scalars().all()
        for row in details:
            amt = credit_one_pe_leave_row(db, pe, row, as_of)
            if amt > 0:
                credited += amt
                rows_touched += 1
            apply_year_end_carry(db, pe, row, as_of)
    db.commit()
    return {
        "as_of": as_of.isoformat(),
        "project_employees": len(pes),
        "rows_credited": rows_touched,
        "total_credited": float(credited),
    }
