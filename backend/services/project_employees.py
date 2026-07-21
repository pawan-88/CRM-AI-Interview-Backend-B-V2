"""Project Employee ownership helpers: leave seeding, rate history, detail rollups.

Policy precedence (Section 5): Customer/branch leave policy governs while deployed
on a client project. Karnex/bench balances are not used for PE-scoped leave apps.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, or_, and_, func
from sqlalchemy.orm import Session

from models import (
    AttendanceStatus, Customer, CustomerLeavePolicy, Employee, Holiday, LeaveAccrualEvent,
    LeaveApplication, LeavePolicyType, POProjectAllocation, POStatus, Project,
    ProjectEmployee, ProjectEmployeeLeaveDetail, ProjectEmployeeRate, PurchaseOrder,
    Timesheet, TimesheetEntry, TimesheetStatus,
)
from services.project_employee_billing import (
    RateRow, compute_billable_days, monthly_accrual, po_status_label, po_utilization,
)
from services.projects import project_employee_out
from services.timesheets import _project_branch


def _weekdays_in_month(year: int, month: int) -> int:
    """Number of Mon–Fri days in a month (the T&M working-day base)."""
    return sum(
        1 for day in range(1, calendar.monthrange(year, month)[1] + 1)
        if date(year, month, day).weekday() < 5
    )


def get_pe_or_404(db: Session, pe_id: int) -> ProjectEmployee:
    pe = db.get(ProjectEmployee, pe_id)
    if not pe:
        raise HTTPException(status_code=404, detail="Project employee not found")
    return pe


def _num(v):
    return float(v) if v is not None else None


def _fmt_days(v: float | Decimal | int | None) -> str:
    if v is None:
        return "0"
    f = float(v)
    return f"{f:g}"


def eligibility_from_policy(policy: CustomerLeavePolicy | None) -> dict:
    """Human-readable + numeric entitlement from a CustomerLeavePolicy copy/link."""
    if policy is None:
        return {
            "yearly_days": None,
            "monthly_days": None,
            "credit_type": None,
            "credit_timing": None,
            "initial_credit": None,
            "period_credit": None,
            "label": "No linked customer leave policy",
        }
    credit_type = (policy.leave_credit_type or "Monthly")
    timing = (policy.leave_credit_timing or "Start_Of_Period")
    period = Decimal(policy.leave_credit_balance or 0)
    initial = Decimal(policy.initial_credit_balance or 0)
    accrual_timing = timing == "End_Of_Period"

    if credit_type == "One_Time":
        total = initial + period
        return {
            "yearly_days": _num(total),
            "monthly_days": None,
            "credit_type": credit_type,
            "credit_timing": timing,
            "initial_credit": _num(initial),
            "period_credit": _num(period),
            "label": f"{_fmt_days(total)} days upfront (one-time)",
        }
    if credit_type == "Yearly":
        yearly = period
        monthly = monthly_accrual(period, 12) if period else Decimal("0")
        if accrual_timing:
            label = f"{_fmt_days(yearly)} days/year (monthly accrual)"
        else:
            label = f"{_fmt_days(yearly)} days/year (credited upfront)"
        if initial:
            label = f"{label}; +{_fmt_days(initial)} initial"
        return {
            "yearly_days": _num(yearly),
            "monthly_days": _num(monthly),
            "credit_type": credit_type,
            "credit_timing": timing,
            "initial_credit": _num(initial),
            "period_credit": _num(period),
            "label": label,
        }
    if credit_type == "Quarterly":
        yearly = period
        per_q = (period / Decimal(4)).quantize(Decimal("0.01")) if period else Decimal("0")
        monthly = monthly_accrual(period, 12) if period else Decimal("0")
        mode = "accrual" if accrual_timing else "upfront each quarter"
        label = f"{_fmt_days(yearly)} days/year (quarterly · {_fmt_days(per_q)}/quarter · {mode})"
        if initial:
            label = f"{label}; +{_fmt_days(initial)} initial"
        return {
            "yearly_days": _num(yearly),
            "monthly_days": _num(monthly),
            "credit_type": credit_type,
            "credit_timing": timing,
            "initial_credit": _num(initial),
            "period_credit": _num(period),
            "label": label,
        }
    # Monthly (default) — leave_credit_balance is the per-month amount
    monthly = period
    yearly = period * Decimal(12)
    if accrual_timing:
        label = f"{_fmt_days(yearly)} days/year (monthly accrual · {_fmt_days(monthly)}/month)"
    else:
        label = f"{_fmt_days(yearly)} days/year (monthly credit · {_fmt_days(monthly)}/month upfront)"
    if initial:
        label = f"{label}; +{_fmt_days(initial)} initial"
    return {
        "yearly_days": _num(yearly),
        "monthly_days": _num(monthly),
        "credit_type": credit_type,
        "credit_timing": timing,
        "initial_credit": _num(initial),
        "period_credit": _num(period),
        "label": label,
    }


def leave_detail_out(row: ProjectEmployeeLeaveDetail, leave_type: LeavePolicyType | None = None,
                     *, settlement: bool = False,
                     policy: CustomerLeavePolicy | None = None) -> dict:
    leave_type = leave_type or row.leave_type
    eligibility = eligibility_from_policy(policy)
    data = {
        "id": row.id,
        "project_employee_id": row.project_employee_id,
        "leave_type_id": row.leave_type_id,
        "leave_type_name": leave_type.name if leave_type else None,
        "customer_leave_policy_id": row.customer_leave_policy_id,
        "initial_balance": _num(row.initial_balance),
        "opening_balance": _num(row.opening_balance),
        "leave_accrual": _num(row.leave_accrual),
        "leave_consumed": _num(row.leave_consumed),
        "leave_balance": _num(row.leave_balance),
        "eligibility": eligibility,
        "eligibility_label": eligibility.get("label"),
        "yearly_entitlement": eligibility.get("yearly_days"),
        "monthly_entitlement": eligibility.get("monthly_days"),
    }
    if settlement:
        data["settlement_leave_balance"] = _num(row.leave_balance)
        data["needs_settlement"] = True
    return data


def rate_out(row: ProjectEmployeeRate) -> dict:
    return {
        "id": row.id,
        "project_employee_id": row.project_employee_id,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "rate": _num(row.rate),
        "billing_unit": getattr(row.billing_unit, "value", row.billing_unit) if row.billing_unit else None,
        "is_current_rate": bool(row.is_current_rate),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def load_rate_rows(db: Session, pe_id: int) -> list[RateRow]:
    rows = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == pe_id)
        .order_by(ProjectEmployeeRate.effective_from)
    ).scalars().all()
    return [RateRow.of(r.effective_from, r.rate) for r in rows]


def sync_pe_billing_from_current_rate(pe: ProjectEmployee, rate: ProjectEmployeeRate) -> None:
    """Keep denormalized pe.billing_rate / billing_unit aligned with current commercial rate."""
    pe.billing_rate = rate.rate
    if rate.billing_unit is not None:
        pe.billing_unit = rate.billing_unit


def clear_other_current_rates(db: Session, pe_id: int, keep_id: int | None = None) -> None:
    rows = db.execute(
        select(ProjectEmployeeRate).where(
            ProjectEmployeeRate.project_employee_id == pe_id,
            ProjectEmployeeRate.is_current_rate.is_(True),
        )
    ).scalars().all()
    for r in rows:
        if keep_id is not None and r.id == keep_id:
            continue
        r.is_current_rate = False


def ensure_initial_rate(db: Session, pe: ProjectEmployee, *, flush: bool = True) -> ProjectEmployeeRate:
    """Ensure the PE has at least one current rate row (from billing_rate)."""
    existing = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == pe.id)
        .order_by(ProjectEmployeeRate.is_current_rate.desc(), ProjectEmployeeRate.effective_from.desc())
    ).scalars().first()
    if existing:
        return existing
    effective = pe.billing_date or pe.onboarding_date or date.today()
    row = ProjectEmployeeRate(
        project_employee_id=pe.id,
        effective_from=effective,
        rate=pe.billing_rate,
        billing_unit=pe.billing_unit,
        is_current_rate=True,
    )
    db.add(row)
    if flush:
        db.flush()
    return row


def _seed_balance_from_policy(policy: CustomerLeavePolicy) -> tuple[Decimal, Decimal]:
    """Return (initial_balance, leave_accrual) respecting leave_credit_timing.

    Start_Of_Period / One_Time / Yearly: credit initial + period balance upfront.
    End_Of_Period (monthly/quarterly): open with initial only; accrual tracks the
    per-period amount that will credit later.
    """
    initial = Decimal(policy.initial_credit_balance or 0)
    period = Decimal(policy.leave_credit_balance or 0)
    timing = (policy.leave_credit_timing or "Start_Of_Period")
    credit_type = (policy.leave_credit_type or "Monthly")

    if credit_type == "One_Time":
        return initial + period, Decimal("0")
    if credit_type == "Yearly":
        if timing == "End_Of_Period":
            return initial, monthly_accrual(period, 12) if period else Decimal("0")
        return initial + period, Decimal("0")
    if credit_type == "Quarterly":
        per = (period / Decimal(4)).quantize(Decimal("0.01")) if period else Decimal("0")
        if timing == "End_Of_Period":
            return initial, per
        return initial + per, per
    # Monthly (default)
    if timing == "End_Of_Period":
        return initial, period
    return initial + period, period


def resolve_customer_leave_policies(db: Session, project: Project) -> list[CustomerLeavePolicy]:
    """Active customer leave policies for the project (branch-specific preferred, else customer-wide)."""
    branch = _project_branch(db, project)
    all_pols = db.execute(
        select(CustomerLeavePolicy).where(
            CustomerLeavePolicy.customer_id == project.customer_id,
            CustomerLeavePolicy.is_active.is_(True),
        )
    ).scalars().all()
    if not all_pols:
        return []
    if branch is not None:
        branch_pols = [p for p in all_pols if p.branch_id == branch.id]
        if branch_pols:
            return branch_pols
    return [p for p in all_pols if p.branch_id is None] or all_pols


def seed_leave_details_from_customer_policy(db: Session, pe: ProjectEmployee,
                                            project: Project | None = None) -> list[ProjectEmployeeLeaveDetail]:
    """Seed PE leave rows from Customer Leave Policy. Skip types that already exist."""
    project = project or db.get(Project, pe.project_id)
    if project is None:
        return []
    existing_types = {
        r.leave_type_id
        for r in db.execute(
            select(ProjectEmployeeLeaveDetail)
            .where(ProjectEmployeeLeaveDetail.project_employee_id == pe.id)
        ).scalars().all()
    }
    created: list[ProjectEmployeeLeaveDetail] = []
    for policy in resolve_customer_leave_policies(db, project):
        if policy.leave_type_id in existing_types:
            continue
        initial, accrual = _seed_balance_from_policy(policy)
        # Upfront (One_Time / Yearly-start) policies with proration enabled credit a
        # fraction of the annual grant based on the join month: remaining months / 12
        # (e.g. onboard in July -> 6/12 of 18 = 9). Accrual policies open at 0 and are
        # prorated per-day by the monthly credit job instead.
        _ct = (policy.leave_credit_type or "Monthly")
        _timing = (policy.leave_credit_timing or "Start_Of_Period")
        _is_upfront = _ct == "One_Time" or (_ct == "Yearly" and _timing != "End_Of_Period")
        if _is_upfront and policy.prorate_balance_credit and pe.onboarding_date and initial > 0:
            factor = Decimal(13 - pe.onboarding_date.month) / Decimal(12)
            factor = min(max(factor, Decimal("0")), Decimal("1"))
            initial = (initial * factor).quantize(Decimal("0.01"))
        bal = initial
        if policy.is_max_limit and policy.max_limit is not None:
            bal = min(bal, Decimal(policy.max_limit))
        row = ProjectEmployeeLeaveDetail(
            project_employee_id=pe.id,
            leave_type_id=policy.leave_type_id,
            customer_leave_policy_id=policy.id,
            initial_balance=initial,
            opening_balance=initial,
            leave_accrual=accrual,
            leave_consumed=Decimal("0"),
            leave_balance=bal,
        )
        db.add(row)
        created.append(row)
        existing_types.add(policy.leave_type_id)
        if initial > 0:
            db.add(LeaveAccrualEvent(
                employee_id=pe.employee_id,
                leave_type_id=policy.leave_type_id,
                event_type="Accrual",
                amount=initial,
                balance_after=bal,
                source=f"pe_seed:{pe.id}:{policy.leave_type_id}",
                note=f"PE#{pe.id} leave seeded from customer policy #{policy.id}",
            ))
    if created:
        db.flush()
    return created


def pe_leave_detail_for(db: Session, pe_id: int,
                        leave_type_id: int) -> ProjectEmployeeLeaveDetail | None:
    return db.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe_id,
            ProjectEmployeeLeaveDetail.leave_type_id == leave_type_id,
        )
    ).scalars().first()


def pe_leave_balance_total(db: Session, pe_id: int) -> float:
    total = db.execute(
        select(func.coalesce(func.sum(ProjectEmployeeLeaveDetail.leave_balance), 0)).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe_id
        )
    ).scalar()
    return float(total or 0)


def consume_pe_leave(db: Session, pe_id: int, leave_type_id: int, days: Decimal,
                     *, allow_negative: bool = False) -> ProjectEmployeeLeaveDetail:
    """Debit PE leave balance on leave approval. Creates a zero row if missing."""
    row = pe_leave_detail_for(db, pe_id, leave_type_id)
    if row is None:
        row = ProjectEmployeeLeaveDetail(
            project_employee_id=pe_id,
            leave_type_id=leave_type_id,
            initial_balance=0,
            opening_balance=0,
            leave_accrual=0,
            leave_consumed=0,
            leave_balance=0,
        )
        db.add(row)
        db.flush()
    available = Decimal(row.leave_balance or 0)
    if days > available and not allow_negative:
        leave_type = db.get(LeavePolicyType, leave_type_id)
        name = leave_type.name if leave_type else "Leave"
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient PE {name} balance: requested {float(days):g}, "
                   f"available {float(available):g}",
        )
    row.leave_consumed = Decimal(row.leave_consumed or 0) + days
    row.leave_balance = available - days
    return row


def credit_pe_leave(db: Session, pe_id: int, leave_type_id: int, days: Decimal) -> ProjectEmployeeLeaveDetail:
    """Credit PE leave balance (comp-off earned on PE-scoped apps)."""
    row = pe_leave_detail_for(db, pe_id, leave_type_id)
    if row is None:
        row = ProjectEmployeeLeaveDetail(
            project_employee_id=pe_id,
            leave_type_id=leave_type_id,
            initial_balance=0,
            opening_balance=0,
            leave_accrual=0,
            leave_consumed=0,
            leave_balance=0,
        )
        db.add(row)
        db.flush()
    row.leave_accrual = Decimal(row.leave_accrual or 0) + days
    row.leave_balance = Decimal(row.leave_balance or 0) + days
    return row


def project_po_summary(db: Session, project_id: int, *, on_date: date | None = None) -> dict:
    """PO drawdown summary for a project (PE commercial tab + list chip)."""
    on_date = on_date or date.today()
    alloc = db.execute(
        select(POProjectAllocation, PurchaseOrder)
        .join(PurchaseOrder, PurchaseOrder.id == POProjectAllocation.po_id)
        .where(POProjectAllocation.project_id == project_id)
        .order_by(PurchaseOrder.status.asc(), POProjectAllocation.id)
    ).first()
    if not alloc:
        return {
            "po_id": None,
            "po_number": None,
            "allocated_amount": None,
            "consumed_amount": None,
            "utilization_pct": None,
            "po_status": "ok",
            "expired": False,
            "end_date": None,
        }
    row, po = alloc
    allocated = Decimal(row.allocated_amount or 0)
    consumed = Decimal(row.consumed_amount or 0)
    pct, status = po_utilization(allocated, consumed)
    expired = bool(po.end_date and on_date > po.end_date)
    if getattr(po.status, "value", po.status) == POStatus.EXHAUSTED.value:
        status = "blocked"
    status = po_status_label(status, expired=expired)
    return {
        "po_id": po.id,
        "po_number": po.po_number,
        "allocated_amount": float(allocated),
        "consumed_amount": float(consumed),
        "balance_value": _num(po.balance_value),
        "utilization_pct": float(pct),
        "po_status": status,
        "expired": expired,
        "end_date": po.end_date.isoformat() if po.end_date else None,
    }


def holidays_for_pe(db: Session, pe: ProjectEmployee, *, year: int | None = None) -> list[dict]:
    """Read-only customer (+ global) holiday calendar for this PE's project customer."""
    project = db.get(Project, pe.project_id)
    if project is None:
        return []
    year = year or date.today().year
    branch = _project_branch(db, project)
    stmt = select(Holiday).where(
        Holiday.is_active.is_(True),
        Holiday.year == year,
        or_(
            Holiday.customer_id.is_(None),
            and_(
                Holiday.customer_id == project.customer_id,
                or_(Holiday.branch_id.is_(None),
                    Holiday.branch_id == (branch.id if branch else None)),
            ),
        ),
    ).order_by(Holiday.holiday_date)
    rows = db.execute(stmt).scalars().all()
    return [{
        "id": h.id,
        "name": h.name,
        "holiday_date": h.holiday_date.isoformat() if h.holiday_date else None,
        "holiday_type": h.holiday_type,
        "customer_id": h.customer_id,
        "branch_id": h.branch_id,
        "year": h.year,
        "scope": ("National" if h.customer_id is None
                  else ("Branch" if h.branch_id is not None else "Customer")),
    } for h in rows]


def pe_credit_history(db: Session, pe: ProjectEmployee, *, limit: int = 50) -> list[dict]:
    """LeaveAccrualEvent rows scoped to this PE (seed / credit job / PE leave apps)."""
    pe_note = f"%PE#{pe.id}%"
    rows = db.execute(
        select(LeaveAccrualEvent, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == LeaveAccrualEvent.leave_type_id, isouter=True)
        .where(
            LeaveAccrualEvent.employee_id == pe.employee_id,
            or_(
                LeaveAccrualEvent.source.like(f"pe_credit:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_seed:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_expire:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_carry:{pe.id}:%"),
                LeaveAccrualEvent.note.like(pe_note),
            ),
        )
        .order_by(LeaveAccrualEvent.created_at.desc(), LeaveAccrualEvent.id.desc())
        .limit(limit)
    ).all()
    return [{
        "id": ev.id,
        "event_type": ev.event_type,
        "leave_type_id": ev.leave_type_id,
        "leave_type_name": type_name,
        "amount": _num(ev.amount),
        "balance_after": _num(ev.balance_after),
        "source": ev.source,
        "note": ev.note,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    } for ev, type_name in rows]


def pe_leave_applications(db: Session, pe: ProjectEmployee, *, limit: int = 50) -> list[dict]:
    rows = db.execute(
        select(LeaveApplication, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == LeaveApplication.leave_type_id, isouter=True)
        .where(LeaveApplication.project_employee_id == pe.id)
        .order_by(LeaveApplication.id.desc())
        .limit(limit)
    ).all()
    return [{
        "id": app.id,
        "leave_type_id": app.leave_type_id,
        "leave_type_name": type_name,
        "leave_period_type": app.leave_period_type,
        "from_date": app.from_date.isoformat() if app.from_date else None,
        "to_date": app.to_date.isoformat() if app.to_date else None,
        "days": _num(app.days),
        "status": app.status,
        "reason": app.reason,
        "comp_off_type": app.comp_off_type,
        "applied_at": app.applied_at.isoformat() if app.applied_at else None,
        "decided_at": app.decided_at.isoformat() if app.decided_at else None,
    } for app, type_name in rows]


def pe_accrued_this_year(db: Session, pe: ProjectEmployee, *, year: int | None = None) -> float:
    """Sum of positive PE-scoped credit events in the calendar year."""
    year = year or date.today().year
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    pe_note = f"%PE#{pe.id}%"
    total = db.execute(
        select(func.coalesce(func.sum(LeaveAccrualEvent.amount), 0)).where(
            LeaveAccrualEvent.employee_id == pe.employee_id,
            LeaveAccrualEvent.amount > 0,
            LeaveAccrualEvent.event_type.in_(["Accrual", "Comp_Off_Credit", "Carry_Forward", "Adjustment"]),
            func.date(LeaveAccrualEvent.created_at) >= start,
            func.date(LeaveAccrualEvent.created_at) <= end,
            or_(
                LeaveAccrualEvent.source.like(f"pe_credit:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_seed:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_carry:{pe.id}:%"),
                LeaveAccrualEvent.note.like(pe_note),
            ),
        )
    ).scalar()
    return float(total or 0)


def timesheet_rollups_for_pe(db: Session, pe: ProjectEmployee) -> list[dict]:
    """Per-period rollup of timesheets belonging to this PE mapping."""
    stmt = select(Timesheet).where(
        Timesheet.project_id == pe.project_id,
        Timesheet.employee_id == pe.employee_id,
    ).order_by(Timesheet.year.desc(), Timesheet.month.desc())
    sheets = db.execute(stmt).scalars().all()
    out: list[dict] = []
    # Client holiday calendar for this mapping, counted per period (weekend holidays
    # still count — a client holiday on a Saturday is a non-billable day under T&M).
    holiday_dates_by_month: dict[int, int] = {}
    for h in holidays_for_pe(db, pe):
        hd = h.get("holiday_date")
        if hd:
            m = int(hd[5:7])
            holiday_dates_by_month[m] = holiday_dates_by_month.get(m, 0) + 1
    for ts in sheets:
        entries = db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        ).scalars().all()
        billable_hours = Decimal("0")
        billable_days_stored = Decimal("0")
        leave = 0
        for e in entries:
            if e.attendance_status == AttendanceStatus.LEAVE:
                leave += 1
            billable_days_stored += Decimal(e.billable_days or 0)
            billable_hours += Decimal(e.billable_hours or 0)
        # Working base = weekdays in the period; billable = base − leave − holidays.
        # This is the invoice basis (day-count), independent of hours logged.
        working = _weekdays_in_month(ts.year, ts.month)
        holidays = holiday_dates_by_month.get(ts.month, 0)
        formula_billable = compute_billable_days(working, leave, holidays)
        out.append({
            "timesheet_id": ts.id,
            "month": ts.month,
            "year": ts.year,
            "status": getattr(ts.status, "value", ts.status),
            "working_days": working,
            "leave_days": leave,
            "holiday_days": holidays,
            "billable_days": float(formula_billable),
            "billable_days_formula": float(formula_billable),
            "billable_days_worked": float(billable_days_stored),
            "billable_formula": "working (weekdays) − leave − holidays (client calendar)",
            "billable_hours": float(billable_hours),
        })
    return out


def exit_project_employee(db: Session, pe: ProjectEmployee, *, exit_date: date | None = None) -> dict:
    """Mark PE exited: stop accrual (flag), deactivate, flag open Draft/Submitted sheets."""
    exit_date = exit_date or date.today()
    pe.is_exit = True
    pe.exit_date = exit_date
    pe.is_active = False
    open_sheets = db.execute(
        select(Timesheet).where(
            Timesheet.project_id == pe.project_id,
            Timesheet.employee_id == pe.employee_id,
            Timesheet.status.in_([TimesheetStatus.DRAFT, TimesheetStatus.SUBMITTED,
                                  TimesheetStatus.REJECTED]),
        )
    ).scalars().all()
    closed = []
    for ts in open_sheets:
        note = f"PE exited {exit_date.isoformat()}; period flagged for settlement"
        if not ts.rejection_reason:
            ts.rejection_reason = note
        closed.append({"timesheet_id": ts.id, "month": ts.month, "year": ts.year,
                       "status": getattr(ts.status, "value", ts.status)})
    leave_rows = db.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id
        )
    ).scalars().all()
    settlement = [{
        "leave_type_id": r.leave_type_id,
        "leave_balance": _num(r.leave_balance),
        "needs_settlement": True,
    } for r in leave_rows]
    pe.settlement_pending = bool(settlement)
    return {
        "exit_date": exit_date.isoformat(),
        "closed_periods": closed,
        "settlement_leave": settlement,
        "accrual_stopped": True,
        "settlement_pending": pe.settlement_pending,
    }


def project_employee_detail_out(db: Session, pe: ProjectEmployee) -> dict:
    project = db.get(Project, pe.project_id)
    emp = db.get(Employee, pe.employee_id) or pe.employee
    customer = db.get(Customer, project.customer_id) if project else None
    data = project_employee_out(pe, emp)
    data["project_name"] = project.name if project else None
    data["customer_id"] = project.customer_id if project else None
    data["customer_name"] = customer.name if customer else None
    data["leave_balance_total"] = pe_leave_balance_total(db, pe.id)

    leave_rows = db.execute(
        select(ProjectEmployeeLeaveDetail, LeavePolicyType)
        .join(LeavePolicyType, LeavePolicyType.id == ProjectEmployeeLeaveDetail.leave_type_id, isouter=True)
        .where(ProjectEmployeeLeaveDetail.project_employee_id == pe.id)
        .order_by(ProjectEmployeeLeaveDetail.id)
    ).all()
    settlement = bool(pe.is_exit or getattr(pe, "settlement_pending", False))
    policy_ids = {r.customer_leave_policy_id for r, _ in leave_rows if r.customer_leave_policy_id}
    policies = {
        p.id: p
        for p in db.execute(
            select(CustomerLeavePolicy).where(CustomerLeavePolicy.id.in_(policy_ids or [-1]))
        ).scalars().all()
    } if policy_ids else {}
    details_out = []
    consumed_total = Decimal("0")
    for r, lt in leave_rows:
        policy = policies.get(r.customer_leave_policy_id) if r.customer_leave_policy_id else None
        details_out.append(leave_detail_out(r, lt, settlement=settlement, policy=policy))
        consumed_total += Decimal(r.leave_consumed or 0)
    data["leave_details"] = details_out
    data["leave_eligibility"] = [
        {
            "leave_type_id": d["leave_type_id"],
            "leave_type_name": d["leave_type_name"],
            "label": d.get("eligibility_label"),
            "yearly_days": d.get("yearly_entitlement"),
            "monthly_days": d.get("monthly_entitlement"),
            "credit_type": (d.get("eligibility") or {}).get("credit_type"),
            "credit_timing": (d.get("eligibility") or {}).get("credit_timing"),
        }
        for d in details_out
    ]
    credit_history = pe_credit_history(db, pe)
    accrued_ytd = pe_accrued_this_year(db, pe)
    # Fallback when ledger is empty: opening/initial balances seeded without events
    if accrued_ytd <= 0 and details_out and not credit_history:
        accrued_ytd = float(sum(Decimal(d.get("initial_balance") or 0) for d in details_out))
    data["leave_summary"] = {
        "balance": data["leave_balance_total"],
        "consumed": float(consumed_total),
        "accrued_this_year": accrued_ytd,
        "year": date.today().year,
    }
    data["credit_history"] = credit_history
    data["leave_applications"] = pe_leave_applications(db, pe)

    rates = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == pe.id)
        .order_by(ProjectEmployeeRate.effective_from.desc(), ProjectEmployeeRate.id.desc())
    ).scalars().all()
    data["rates"] = [rate_out(r) for r in rates]
    data["timesheet_rollups"] = timesheet_rollups_for_pe(db, pe)
    holiday_year = date.today().year
    data["holidays"] = holidays_for_pe(db, pe, year=holiday_year)
    branch = _project_branch(db, project) if project else None
    data["holiday_calendar"] = {
        "year": holiday_year,
        "customer_id": project.customer_id if project else None,
        "customer_name": customer.name if customer else None,
        "branch_id": branch.id if branch else None,
        "label": (
            f"{customer.name} / {holiday_year}" if customer
            else f"Client calendar {holiday_year}"
        ),
        "read_only": True,
        "count": len(data["holidays"]),
    }
    data["po"] = project_po_summary(db, pe.project_id)
    data["po_status"] = data["po"].get("po_status")
    return data


def enrich_pe_list_row(db: Session, pe: ProjectEmployee, base: dict) -> dict:
    base["leave_balance_total"] = pe_leave_balance_total(db, pe.id)
    po = project_po_summary(db, pe.project_id)
    base["po_status"] = po.get("po_status")
    base["po_utilization_pct"] = po.get("utilization_pct")
    base["po_number"] = po.get("po_number")
    return base


def group_pe_rows_by_employee(rows: list[dict]) -> list[dict]:
    """Collapse flat PE list rows into one group per employee (UC-12).

    Preserves first-seen employee order; each group carries mappings[] with
    per-mapping rate / leave / PO chips unchanged.
    """
    groups: dict[int, dict] = {}
    order: list[int] = []
    for row in rows:
        eid = int(row["employee_id"])
        if eid not in groups:
            groups[eid] = {
                "employee_id": eid,
                "employee_name": row.get("employee_name"),
                "employee_email": row.get("employee_email"),
                "mapping_count": 0,
                "mappings": [],
            }
            order.append(eid)
        groups[eid]["mappings"].append(row)
        groups[eid]["mapping_count"] = len(groups[eid]["mappings"])
    return [groups[eid] for eid in order]


# --------------------------------------------------------------- invoice rollup
def invoice_rollups_for_pe(db: Session, pe: ProjectEmployee) -> dict:
    """Read-only per-period invoice preview for the Project Employee Invoice sub-tab.

    One row per timesheet period, reusing the same computed preview the Finance
    module uses (effective-dated / split rates, Monthly-vs-Daily-vs-Hourly basis,
    linked invoice + can_generate). Billable days come from the standardised
    day-count roll-up so the Timesheet and Invoice tabs always agree.
    """
    from services.timesheets import timesheet_invoice_preview

    rollup_by_period = {
        (r["year"], r["month"]): r for r in timesheet_rollups_for_pe(db, pe)
    }
    sheets = db.execute(
        select(Timesheet).where(
            Timesheet.project_id == pe.project_id,
            Timesheet.employee_id == pe.employee_id,
        ).order_by(Timesheet.year.desc(), Timesheet.month.desc())
    ).scalars().all()
    periods: list[dict] = []
    for ts in sheets:
        try:
            prev = timesheet_invoice_preview(db, ts)
        except Exception:
            continue
        line = prev["line_items"][0] if prev.get("line_items") else {}
        roll = rollup_by_period.get((ts.year, ts.month), {})
        periods.append({
            "timesheet_id": ts.id,
            "month": ts.month,
            "year": ts.year,
            "status": getattr(ts.status, "value", ts.status),
            "billable_days": roll.get("billable_days"),
            "rate_per_unit": line.get("rate_per_unit"),
            "monthly_cost": line.get("monthly_cost"),
            "rate_split": line.get("rate_split", False),
            "amount": prev.get("totals", {}).get("sub_total"),
            "description": line.get("description"),
            "linked_invoice": prev.get("linked_invoice"),
            "can_generate": prev.get("can_generate", False),
        })
    return {
        "project_employee_id": pe.id,
        "billing_unit": getattr(pe.billing_unit, "value", pe.billing_unit),
        "current_rate": _num(pe.billing_rate),
        "po": project_po_summary(db, pe.project_id),
        "periods": periods,
        "read_only": True,
    }


# --------------------------------------------------- employee cross-project leave
def employee_project_leave(db: Session, employee_id: int) -> dict:
    """Consolidated 'My Leave' rollup: every project mapping's leave balance for one
    employee, grouped by project, with a grand total. This is the employee-facing
    view that makes multi-project leave legible without breaking per-client accounting.
    """
    pes = db.execute(
        select(ProjectEmployee).where(ProjectEmployee.employee_id == employee_id)
        .order_by(ProjectEmployee.is_active.desc(), ProjectEmployee.id)
    ).scalars().all()
    projects_out: list[dict] = []
    grand_total = Decimal("0")
    for pe in pes:
        project = db.get(Project, pe.project_id)
        customer = db.get(Customer, project.customer_id) if project else None
        rows = db.execute(
            select(ProjectEmployeeLeaveDetail, LeavePolicyType)
            .join(LeavePolicyType,
                  LeavePolicyType.id == ProjectEmployeeLeaveDetail.leave_type_id, isouter=True)
            .where(ProjectEmployeeLeaveDetail.project_employee_id == pe.id)
            .order_by(ProjectEmployeeLeaveDetail.id)
        ).all()
        subtotal = sum((Decimal(r.leave_balance or 0) for r, _ in rows), Decimal("0"))
        grand_total += subtotal
        projects_out.append({
            "project_employee_id": pe.id,
            "project_id": pe.project_id,
            "project_name": project.name if project else None,
            "customer_name": customer.name if customer else None,
            "is_active": bool(pe.is_active),
            "is_exit": bool(pe.is_exit),
            "leave_balance_total": float(subtotal),
            "leave_details": [leave_detail_out(r, lt) for r, lt in rows],
        })
    return {
        "employee_id": employee_id,
        "total_leave_balance": float(grand_total),
        "projects": projects_out,
    }
