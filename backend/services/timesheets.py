"""Timesheet service helpers: billable computation, serializers, ownership, summary.

Billable rules are resolved branch-wise: the project's opportunity pins a
customer branch, whose own policy fields (when set) override the customer-level
CustomerBillingPolicy, which in turn overrides built-in defaults. The fallback
is field-by-field — a branch with only some fields set inherits the rest.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from models import (
    USERS_TABLE, AttendanceStatus, BillingUnit, Customer, CustomerBillingPolicy,
    CustomerBranch, DayType, Employee, EmployeeLeaveBalance, EntryLocation, Holiday, Invoice,
    LeaveAccrualEvent, LeavePeriod, LeavePolicyType, Opportunity, Project,
    ProjectEmployee, Timesheet, TimesheetAttachment, TimesheetEntry, TimesheetStatus,
)

ZERO = Decimal("0")
HALF = Decimal("0.5")
ONE = Decimal("1")
TWO = Decimal("2")
EIGHT = Decimal("8")
TWO_PLACES = Decimal("0.01")

STATUS_LABELS = {
    TimesheetStatus.DRAFT.value: "Pending for Submission",
    TimesheetStatus.SUBMITTED.value: "Pending for Approval",
    TimesheetStatus.APPROVED.value: "Approved",
    TimesheetStatus.REJECTED.value: "Rejected",
}


@dataclass
class BillingPolicy:
    """Effective billing policy (defaults when the customer has none)."""

    week_off_billable: bool = False
    leave_billable: bool = False
    holidays_billable: bool = False
    min_hours_full_day: Decimal = Decimal("8.00")
    min_hours_half_day: Decimal = Decimal("4.00")


def _project_branch(db: Session, project: Project) -> CustomerBranch | None:
    """Branch the project bills against: project → opportunity → branch_id."""
    if not project.opportunity_id:
        return None
    opp = db.get(Opportunity, project.opportunity_id)
    if opp is None or opp.branch_id is None:
        return None
    branch = db.get(CustomerBranch, opp.branch_id)
    # Defensive: ignore a branch that somehow belongs to another customer.
    if branch is None or branch.customer_id != project.customer_id:
        return None
    return branch


def effective_billing_policy(db: Session, project: Project) -> BillingPolicy:
    """Resolve the effective policy branch-wise, field by field.

    Order per field: branch policy (when the field is set on the branch the
    project's opportunity points at) → customer-level policy → built-in defaults.
    """
    row = db.execute(
        select(CustomerBillingPolicy).where(CustomerBillingPolicy.customer_id == project.customer_id)
    ).scalars().first()
    base = BillingPolicy()
    if row:
        base = BillingPolicy(
            week_off_billable=bool(row.week_off_billable),
            leave_billable=bool(row.leave_billable),
            holidays_billable=bool(row.holidays_billable),
            min_hours_full_day=Decimal(row.min_hours_full_day),
            min_hours_half_day=Decimal(row.min_hours_half_day),
        )
    branch = _project_branch(db, project)
    if branch is None:
        return base
    return BillingPolicy(
        week_off_billable=base.week_off_billable if branch.weekoff_billable is None
        else bool(branch.weekoff_billable),
        leave_billable=base.leave_billable if branch.leave_billable is None
        else bool(branch.leave_billable),
        holidays_billable=base.holidays_billable if branch.holidays_billable is None
        else bool(branch.holidays_billable),
        min_hours_full_day=base.min_hours_full_day if branch.hours_required_full_day is None
        else Decimal(branch.hours_required_full_day),
        min_hours_half_day=base.min_hours_half_day if branch.hours_required_half_day is None
        else Decimal(branch.hours_required_half_day),
    )


DEFAULT_WORKING_HOURS = Decimal("8.50")
FOUR = Decimal("4")


def attendance_from_hours_worked(hours: Decimal) -> AttendanceStatus:
    """Derive working-day attendance from hours worked (inclusive: 4→Half, 8→Present)."""
    h = Decimal(hours or 0)
    if h >= EIGHT:
        return AttendanceStatus.PRESENT
    if h >= FOUR:
        return AttendanceStatus.HALF_DAY
    return AttendanceStatus.ABSENT


def display_billable_day(billable_hours: Decimal) -> Decimal:
    """Report/display billable day = billable hours ÷ 8 (invoice math stays threshold-based)."""
    bh = Decimal(billable_hours or 0)
    if bh <= ZERO:
        return ZERO
    return (bh / EIGHT).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def period_range_label(year: int, month: int) -> str:
    """Human period range, e.g. "01-Jun-2025 to 30-Jun-2025"."""
    start, end = period_bounds(year, month)
    return f"{start.strftime('%d-%b-%Y')} to {end.strftime('%d-%b-%Y')}"


def classify_calendar_day(d: date, holiday_dates: set[date]) -> tuple[DayType, bool,
                                                                        AttendanceStatus, Decimal]:
    """Auto-classify a calendar day for timesheet generation."""
    if d in holiday_dates:
        return DayType.HOLIDAY, False, AttendanceStatus.HOLIDAY, ZERO
    if d.weekday() >= 5:
        return DayType.WEEK_OFF, False, AttendanceStatus.WEEK_OFF, ZERO
    return DayType.WORKING, True, AttendanceStatus.PRESENT, DEFAULT_WORKING_HOURS


def build_generated_entry(*, timesheet_id: int, d: date, holiday_dates: set[date],
                          project: Project, policy: BillingPolicy) -> TimesheetEntry:
    day_type, is_working, attendance, hours = classify_calendar_day(d, holiday_dates)
    billable_hours, billable_days = compute_billables(
        is_working=is_working,
        hours_worked=hours,
        attendance_status=attendance,
        leave_period=None,
        project=project,
        policy=policy,
    )
    return TimesheetEntry(
        timesheet_id=timesheet_id,
        entry_date=d,
        day_of_week=day_name(d),
        day_type=day_type,
        is_working=is_working,
        hours_worked=hours,
        attendance_status=attendance,
        location=EntryLocation.ONSITE,
        billable_hours=billable_hours,
        billable_days=billable_days,
        view_flag=False,
    )


def resolve_entry_fields(
    *,
    d: date,
    item,
    holiday_dates: set[date],
) -> tuple[DayType, bool, Decimal, AttendanceStatus, str | None, LeavePeriod | None]:
    """Normalize client payload into stored day fields (calendar holidays stay locked)."""
    if d in holiday_dates:
        return DayType.HOLIDAY, False, ZERO, AttendanceStatus.HOLIDAY, None, None

    day_type = item.day_type
    attendance = item.attendance_status
    hours = Decimal(item.hours_worked or 0)
    leave_type = item.leave_type
    leave_period = item.leave_period

    if day_type is None:
        if not item.is_working:
            day_type = DayType.WEEK_OFF
        elif attendance == AttendanceStatus.HOLIDAY:
            day_type = DayType.HOLIDAY
        else:
            day_type = DayType.WORKING

    if day_type == DayType.HOLIDAY and d not in holiday_dates:
        raise HTTPException(
            status_code=400,
            detail=f"{d.isoformat()} is not on the client holiday calendar; "
                   "cannot mark as Holiday manually",
        )

    if day_type == DayType.WEEK_OFF:
        is_working = False
        if attendance not in (AttendanceStatus.LEAVE, AttendanceStatus.ABSENT,
                              AttendanceStatus.HALF_DAY):
            attendance = AttendanceStatus.WEEK_OFF
            leave_type = None
            leave_period = None
    elif day_type == DayType.HOLIDAY:
        is_working = False
        attendance = AttendanceStatus.HOLIDAY
        leave_type = None
        leave_period = None
        hours = ZERO
    else:
        is_working = True
        if attendance in (AttendanceStatus.WEEK_OFF, AttendanceStatus.HOLIDAY):
            attendance = AttendanceStatus.PRESENT

    if attendance == AttendanceStatus.LEAVE:
        if not (leave_type or "").strip():
            leave_type = None
    else:
        leave_type = None
        leave_period = None

    if day_type == DayType.WORKING and is_working and attendance != AttendanceStatus.LEAVE:
        attendance = attendance_from_hours_worked(hours)

    if attendance == AttendanceStatus.HOLIDAY and d not in holiday_dates:
        raise HTTPException(
            status_code=400,
            detail=f"{d.isoformat()} is not on the client holiday calendar; "
                   "cannot mark as Holiday manually",
        )

    return day_type, is_working, hours, attendance, leave_type, leave_period


def _capped_hours(hours: Decimal, project: Project) -> Decimal:
    cap = project.max_billable_hours_day
    if cap is None:
        return hours
    return min(hours, Decimal(cap))


def _days_from_hours(hours: Decimal, policy: BillingPolicy) -> Decimal:
    if hours >= policy.min_hours_full_day:
        return ONE
    if hours >= policy.min_hours_half_day:
        return HALF
    return ZERO


def compute_billables(*, is_working: bool, hours_worked: Decimal,
                      attendance_status: AttendanceStatus,
                      leave_period: LeavePeriod | None,
                      project: Project, policy: BillingPolicy) -> tuple[Decimal, Decimal]:
    """Return (billable_hours, billable_days) for one timesheet entry."""
    hours = Decimal(hours_worked or 0)

    if not is_working:
        # Non-working (weekend/off) day: billable only if hours were actually
        # worked AND the customer's policy makes week-offs billable.
        if hours > ZERO and policy.week_off_billable:
            bh = _capped_hours(hours, project)
            return bh, _days_from_hours(hours, policy)
        return ZERO, ZERO

    if attendance_status == AttendanceStatus.PRESENT:
        bh = _capped_hours(hours, project)
        return bh, _days_from_hours(hours, policy)

    if attendance_status == AttendanceStatus.HALF_DAY:
        return _capped_hours(hours, project), HALF

    if attendance_status == AttendanceStatus.LEAVE:
        if policy.leave_billable:
            days = HALF if leave_period in (LeavePeriod.HALF_AM, LeavePeriod.HALF_PM) else ONE
            return ZERO, days
        return ZERO, ZERO

    if attendance_status == AttendanceStatus.HOLIDAY:
        return (ZERO, ONE) if policy.holidays_billable else (ZERO, ZERO)

    # Absent (or anything else): nothing billable.
    return ZERO, ZERO


def day_name(d: date) -> str:
    return d.strftime("%A")


def month_days(year: int, month: int) -> list[date]:
    _, last = calendar.monthrange(year, month)
    return [date(year, month, day) for day in range(1, last + 1)]


def holidays_for_project_period(db: Session, project: Project,
                                year: int, month: int) -> set[date]:
    """Active holiday dates applying to this project during one month.

    A holiday row matches when its customer is NULL (global) or equals the
    project's customer, AND its branch is NULL (customer-wide) or equals the
    branch the project bills against (project → opportunity → branch, the same
    resolution effective_billing_policy uses).
    """
    start, end = period_bounds(year, month)
    branch = _project_branch(db, project)
    rows = db.execute(
        select(Holiday).where(
            Holiday.is_active.is_(True),
            Holiday.observance == "Mandatory",
            Holiday.holiday_date >= start,
            Holiday.holiday_date <= end,
            or_(Holiday.customer_id.is_(None), Holiday.customer_id == project.customer_id),
        )
    ).scalars().all()
    dates: set[date] = set()
    for h in rows:
        if h.branch_id is not None and (branch is None or h.branch_id != branch.id):
            continue
        dates.add(h.holiday_date)
    return dates


def get_timesheet_or_404(db: Session, timesheet_id: int) -> Timesheet:
    ts = db.get(Timesheet, timesheet_id)
    if not ts:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    return ts


def employee_for_user(db: Session, user_id: int) -> Employee | None:
    return db.execute(select(Employee).where(Employee.user_id == user_id)).scalars().first()


def _num(v):
    return float(v) if v is not None else None


def entry_out(e: TimesheetEntry) -> dict:
    bh = Decimal(e.billable_hours or 0)
    return {
        "id": e.id,
        "timesheet_id": e.timesheet_id,
        "entry_date": e.entry_date.isoformat() if e.entry_date else None,
        "day_of_week": e.day_of_week,
        "day_type": getattr(e.day_type, "value", e.day_type),
        "is_working": e.is_working,
        "hours_worked": _num(e.hours_worked),
        "attendance_status": getattr(e.attendance_status, "value", e.attendance_status),
        "leave_type": e.leave_type,
        "leave_period": getattr(e.leave_period, "value", e.leave_period) if e.leave_period else None,
        "billable_hours": _num(e.billable_hours),
        "billable_days": _num(e.billable_days),
        "billable_day": float(display_billable_day(bh)),
        "location": getattr(e.location, "value", e.location) if e.location else None,
        "view_flag": bool(e.view_flag),
        "entry_project_id": e.entry_project_id,
    }


def timesheet_status_label(status: TimesheetStatus | str, *, due_missing: bool = False) -> str:
    if due_missing:
        return "Due"
    val = getattr(status, "value", status)
    return STATUS_LABELS.get(val, str(val))


def customer_short_name(customer: Customer | None) -> str:
    if customer is None or not (customer.name or "").strip():
        return "Unknown"
    return customer.name.strip().split()[0]


def assignment_role_title(pe: ProjectEmployee | None, project: Project | None) -> str:
    if pe is not None and (pe.role_title or "").strip():
        return pe.role_title.strip()
    return project.name if project and project.name else "Project"


def project_title_label(*, customer: Customer | None, employee: Employee | None,
                        pe: ProjectEmployee | None, project: Project | None) -> str:
    role = assignment_role_title(pe, project)
    first = (employee.first_name or "").strip() if employee else "Employee"
    return f"{customer_short_name(customer)}_{first}__{role}"


def _report_date(ts: Timesheet) -> str | None:
    dt = ts.created_at or ts.submitted_at
    return dt.isoformat() if dt else None


def attachment_out(a: TimesheetAttachment) -> dict:
    return {
        "id": a.id,
        "timesheet_id": a.timesheet_id,
        "file_url": a.file_url,
        "file_name": a.file_name,
        "file_sha256": a.file_sha256,
        "file_size": a.file_size,
        "kind": a.kind,
        "uploaded_by": a.uploaded_by,
        "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None,
    }


def list_attachments(db: Session, ts: Timesheet) -> list[dict]:
    rows = db.execute(
        select(TimesheetAttachment).where(TimesheetAttachment.timesheet_id == ts.id)
        .order_by(TimesheetAttachment.uploaded_at, TimesheetAttachment.id)
    ).scalars().all()
    if rows:
        return [attachment_out(a) for a in rows]
    if ts.file_attachment_url:
        return [{
            "id": None,
            "timesheet_id": ts.id,
            "file_url": ts.file_attachment_url,
            "file_name": "legacy-attachment",
            "file_sha256": None,
            "file_size": None,
            "kind": "legacy",
            "uploaded_by": None,
            "uploaded_at": ts.submitted_at.isoformat() if ts.submitted_at else None,
        }]
    return []


def timesheet_out(ts: Timesheet) -> dict:
    return {
        "id": ts.id,
        "project_id": ts.project_id,
        "employee_id": ts.employee_id,
        "project_employee_id": ts.project_employee_id,
        "month": ts.month,
        "year": ts.year,
        "status": getattr(ts.status, "value", ts.status),
        "status_label": timesheet_status_label(ts.status),
        "rejection_reason": ts.rejection_reason,
        "reason_for_rejection": ts.rejection_reason,
        "submitted_at": ts.submitted_at.isoformat() if ts.submitted_at else None,
        "approved_by": ts.approved_by,
        "approved_at": ts.approved_at.isoformat() if ts.approved_at else None,
        "file_attachment_url": ts.file_attachment_url,
        "created_at": ts.created_at.isoformat() if getattr(ts, "created_at", None) else None,
        "date": _report_date(ts),
    }


def period_label(year: int, month: int) -> str:
    """Human period label for a timesheet, e.g. "July 2026"."""
    return f"{calendar.month_name[month]} {year}"


def period_bounds(year: int, month: int) -> tuple[date, date]:
    """(first day, last day) of the timesheet month."""
    _, last = calendar.monthrange(year, month)
    return date(year, month, 1), date(year, month, last)


def employee_display_name(emp: Employee | None) -> str | None:
    if emp is None:
        return None
    return " ".join(p for p in (emp.first_name, emp.last_name) if p) or None


def _user_display_name(db: Session, user_id: int | None) -> str | None:
    """Display name of an auth user (registration_data): full_name, else username."""
    if not user_id:
        return None
    row = db.execute(
        text(f"SELECT full_name, username FROM {USERS_TABLE} WHERE id = :i"),
        {"i": user_id},
    ).first()
    if not row:
        return None
    return row[0] or row[1]


def timesheet_detail_out(db: Session, ts: Timesheet, entries: list[TimesheetEntry]) -> dict:
    data = timesheet_out(ts)
    project = db.get(Project, ts.project_id)
    customer = db.get(Customer, project.customer_id) if project else None
    opp = db.get(Opportunity, project.opportunity_id) if project and project.opportunity_id else None
    branch = db.get(CustomerBranch, opp.branch_id) if opp and opp.branch_id is not None else None
    emp = db.get(Employee, ts.employee_id)
    start, end = period_bounds(ts.year, ts.month)
    # Header/display enrichment (all null-safe). Note: employees has no
    # employee_code column, so the employee's primary key doubles as the code.
    data["project_title"] = project_title_label(customer=customer, employee=emp,
                                                pe=db.get(ProjectEmployee, ts.project_employee_id)
                                                if ts.project_employee_id else None,
                                                project=project)
    data["customer_id"] = customer.id if customer else None
    data["customer_name"] = customer.name if customer else None
    data["branch_id"] = branch.id if branch else None
    data["branch_name"] = branch.branch_name if branch else None
    data["project_type"] = getattr(opp.opp_type, "value", opp.opp_type) if opp else None
    data["timesheet_period"] = period_range_label(ts.year, ts.month)
    data["period_start_date"] = start.isoformat()
    data["period_end_date"] = end.isoformat()
    data["employee_name"] = employee_display_name(emp)
    data["employee_code"] = emp.id if emp else None
    policy = effective_billing_policy(db, project) if project else BillingPolicy()
    data["billing_policy"] = {
        "week_off_billable": policy.week_off_billable,
        "leave_billable": policy.leave_billable,
        "holidays_billable": policy.holidays_billable,
        "min_hours_full_day": float(policy.min_hours_full_day),
        "min_hours_half_day": float(policy.min_hours_half_day),
    }
    data["max_billable_hours_day"] = (
        float(project.max_billable_hours_day) if project and project.max_billable_hours_day else None
    )
    data["holiday_dates"] = sorted(
        d.isoformat() for d in holidays_for_project_period(db, project, ts.year, ts.month)
    ) if project else []
    data["entries"] = [entry_out(e) for e in entries]
    data["summary"] = timesheet_summary(db, ts, entries)
    data["attachments"] = list_attachments(db, ts)
    return data


def _billable_rollup(project: Project | None,
                     entries: list[TimesheetEntry]) -> dict[str, Decimal | int]:
    """Decimal rollups shared by the summary and the invoice preview.

    total_* are the raw sums of the stored per-entry billables; actual_* are
    the same sums capped by the project's monthly maximums
    (max_billable_hours_month / max_billable_days_month) when those are set.
    """
    billable_hours = ZERO
    billable_days = ZERO
    leave_billable_days = ZERO
    working_days = 0
    for e in entries:
        billable_hours += Decimal(e.billable_hours or 0)
        billable_days += Decimal(e.billable_days or 0)
        if e.attendance_status == AttendanceStatus.LEAVE:
            leave_billable_days += Decimal(e.billable_days or 0)
        if e.is_working:
            working_days += 1
    actual_billable_hours = billable_hours
    actual_billable_days = billable_days
    if project is not None and project.max_billable_hours_month is not None:
        actual_billable_hours = min(billable_hours, Decimal(project.max_billable_hours_month))
    if project is not None and project.max_billable_days_month is not None:
        actual_billable_days = min(billable_days, Decimal(project.max_billable_days_month))
    return {
        "billable_hours": billable_hours,
        "billable_days": billable_days,
        "actual_billable_hours": actual_billable_hours,
        "actual_billable_days": actual_billable_days,
        "leave_billable_days": leave_billable_days,
        "working_days": working_days,
    }


def comp_off_earned(entries: list[TimesheetEntry], policy: BillingPolicy) -> Decimal:
    """Comp-off credit earned by working on week-offs (Sat/Sun) or holidays.

    Eligibility per entry: hours actually worked on a weekend date or on a
    Holiday row. Full credit (1.0) when hours >= the policy's full-day
    threshold, half (0.5) when >= the half-day threshold, else none.
    """
    earned = ZERO
    for e in entries:
        hours = Decimal(e.hours_worked or 0)
        if hours <= ZERO:
            continue
        is_weekend = e.entry_date is not None and e.entry_date.weekday() >= 5
        is_holiday = e.attendance_status == AttendanceStatus.HOLIDAY
        if not (is_weekend or is_holiday):
            continue
        if hours >= policy.min_hours_full_day:
            earned += ONE
        elif hours >= policy.min_hours_half_day:
            earned += HALF
    return earned


def accrue_comp_off(db: Session, ts: Timesheet, entries: list[TimesheetEntry]) -> Decimal:
    """Idempotently credit earned comp-off into the employee's leave balance.

    Called on timesheet approval. Applies only the DELTA versus what this
    timesheet already granted (ts.comp_off_accrued), so re-approval after a
    reject/resubmit cycle never double-credits. Caller commits.
    """
    project = db.get(Project, ts.project_id)
    policy = effective_billing_policy(db, project) if project else BillingPolicy()
    earned = comp_off_earned(entries, policy)
    prior = Decimal(ts.comp_off_accrued or 0)
    delta = earned - prior
    if delta == ZERO:
        return earned

    leave_type = db.execute(
        select(LeavePolicyType).where(func.lower(LeavePolicyType.name).like("comp%off%"))
    ).scalars().first()
    if leave_type is None:
        leave_type = LeavePolicyType(
            name="Comp-Off",
            accrual_rule="Earned on approved weekend/holiday work",
            carry_forward_rule="Expires in 90 days",
        )
        db.add(leave_type)
        db.flush()

    balance = db.execute(
        select(EmployeeLeaveBalance).where(
            EmployeeLeaveBalance.employee_id == ts.employee_id,
            EmployeeLeaveBalance.leave_type_id == leave_type.id,
            EmployeeLeaveBalance.year == ts.year,
        )
    ).scalars().first()
    if balance is None:
        balance = EmployeeLeaveBalance(
            employee_id=ts.employee_id, leave_type_id=leave_type.id, year=ts.year,
            accrued=0, consumed=0, balance=0, carry_forward=0,
        )
        db.add(balance)
        db.flush()

    balance.accrued = Decimal(balance.accrued or 0) + delta
    balance.balance = (Decimal(balance.accrued or 0) + Decimal(balance.carry_forward or 0)
                       - Decimal(balance.consumed or 0))
    ts.comp_off_accrued = earned
    # Ledger entry: every comp-off movement is auditable (migration 0021).
    db.add(LeaveAccrualEvent(
        employee_id=ts.employee_id,
        leave_type_id=leave_type.id,
        event_type="Comp_Off_Credit" if delta > ZERO else "Adjustment",
        amount=delta,
        balance_after=balance.balance,
        source=f"timesheet:{ts.id}",
        note=f"Comp-off from approved timesheet {period_label(ts.year, ts.month)} "
             f"(earned {float(earned):g}, prior {float(prior):g})",
    ))
    return earned


def timesheet_summary(db: Session, ts: Timesheet, entries: list[TimesheetEntry]) -> dict:
    project = db.get(Project, ts.project_id)
    rollup = _billable_rollup(project, entries)
    total_days = len(entries)
    working_days = 0
    comp_off_days = 0
    hours_worked = ZERO
    leave_days = ZERO
    holidays = 0
    present_days = 0
    absent_days = 0
    half_days = 0
    total_week_off = 0
    total_no_of_days_worked = 0
    for e in entries:
        status = e.attendance_status
        if e.is_working:
            working_days += 1
        elif status not in (AttendanceStatus.LEAVE, AttendanceStatus.HOLIDAY):
            # Non-working row that isn't a leave/holiday = weekend / week-off.
            total_week_off += 1
        if (e.leave_type or "").strip().lower() == "comp-off":
            comp_off_days += 1
        hours = Decimal(e.hours_worked or 0)
        hours_worked += hours
        if hours > ZERO:
            total_no_of_days_worked += 1
        if status == AttendanceStatus.LEAVE:
            leave_days += HALF if e.leave_period in (LeavePeriod.HALF_AM, LeavePeriod.HALF_PM) else ONE
        elif status == AttendanceStatus.HOLIDAY:
            holidays += 1
        elif status == AttendanceStatus.PRESENT:
            present_days += 1
        elif status == AttendanceStatus.ABSENT:
            absent_days += 1
        elif status == AttendanceStatus.HALF_DAY:
            half_days += 1
    return {
        "total_days": total_days,
        "working_days": working_days,
        "comp_off_days": comp_off_days,
        "hours_worked": float(hours_worked),
        "total_hours_worked": float(hours_worked),
        "billable_hours": float(rollup["billable_hours"]),
        "billable_days": float(rollup["billable_days"]),
        "leave_days": float(leave_days),
        "holidays": holidays,
        "present_days": present_days,
        "absent_days": absent_days,
        "half_days": half_days,
        # --- Tab 9 additions (existing keys above are kept untouched) ---
        "total_week_off": total_week_off,
        "total_no_of_days_worked": total_no_of_days_worked,
        "total_billable_hours": float(rollup["billable_hours"]),
        "total_billable_days": float(rollup["billable_days"]),
        "actual_billable_hours": float(rollup["actual_billable_hours"]),
        "actual_billable_days": float(rollup["actual_billable_days"]),
        "actual_billable_day": float(display_billable_day(rollup["actual_billable_hours"])),
        "total_leave_days": round(
            float(Decimal(working_days) - display_billable_day(rollup["actual_billable_hours"])), 2
        ),
        "total_leave_billable_days": float(rollup["leave_billable_days"]),
        # Comp-off EARNED by weekend/holiday work in this sheet (comp_off_days
        # above counts comp-off leave CONSUMED). Credited on approval.
        "comp_off_earned": float(
            comp_off_earned(entries,
                            effective_billing_policy(db, project) if project else BillingPolicy())
        ),
        "comp_off_credited": float(ts.comp_off_accrued or 0),
        "approved_time": {
            "approved_at": ts.approved_at.isoformat() if ts.approved_at else None,
            "approver_name": _user_display_name(db, ts.approved_by),
        },
        "reason_for_rejection": ts.rejection_reason,
        "attachment_url": ts.file_attachment_url,
    }


def _entries_for_timesheets(db: Session, ids: list[int]) -> dict[int, list[TimesheetEntry]]:
    if not ids:
        return {}
    rows = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id.in_(ids))
        .order_by(TimesheetEntry.timesheet_id, TimesheetEntry.entry_date)
    ).scalars().all()
    out: dict[int, list[TimesheetEntry]] = {}
    for e in rows:
        out.setdefault(e.timesheet_id, []).append(e)
    return out


def _invoice_map(db: Session, ids: list[int]) -> dict[int, Invoice]:
    if not ids:
        return {}
    rows = db.execute(select(Invoice).where(Invoice.timesheet_id.in_(ids))).scalars().all()
    return {inv.timesheet_id: inv for inv in rows if inv.timesheet_id is not None}


def timesheet_report_out(db: Session, ts: Timesheet,
                         entries: list[TimesheetEntry] | None = None,
                         invoice: Invoice | None = None) -> dict:
    """Enriched row for list/report endpoints (display fields + report-only math)."""
    data = timesheet_out(ts)
    project = db.get(Project, ts.project_id)
    customer = db.get(Customer, project.customer_id) if project else None
    opp = db.get(Opportunity, project.opportunity_id) if project and project.opportunity_id else None
    emp = db.get(Employee, ts.employee_id)
    pe = db.get(ProjectEmployee, ts.project_employee_id) if ts.project_employee_id else None
    if pe is None and project and emp:
        pe = db.execute(
            select(ProjectEmployee).where(
                ProjectEmployee.project_id == ts.project_id,
                ProjectEmployee.employee_id == ts.employee_id,
            )
        ).scalars().first()
    start, end = period_bounds(ts.year, ts.month)
    if entries is None:
        entries = db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
            .order_by(TimesheetEntry.entry_date)
        ).scalars().all()
    rollup = _billable_rollup(project, entries)
    hours_worked = sum((Decimal(e.hours_worked or 0) for e in entries), ZERO)
    actual_billable_hours = rollup["actual_billable_hours"]
    actual_billable_day = display_billable_day(actual_billable_hours)
    working_days = rollup["working_days"]
    total_leave_days = round(float(Decimal(working_days) - actual_billable_day), 2)

    data.update({
        "project_title": project_title_label(customer=customer, employee=emp, pe=pe, project=project),
        "customer_name": customer.name if customer else None,
        "project_type": getattr(opp.opp_type, "value", opp.opp_type) if opp else None,
        "project_employee_name": employee_display_name(emp),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "period_start_date": start.isoformat(),
        "period_end_date": end.isoformat(),
        "timesheet_period": period_range_label(ts.year, ts.month),
        "actual_billable_hours": float(actual_billable_hours),
        "total_hours_worked": float(hours_worked),
        "total_leave_billable_days": float(rollup["leave_billable_days"]),
        "actual_billable_day": float(actual_billable_day),
        "total_leave_days": total_leave_days,
        "attachments": list_attachments(db, ts),
    })
    if invoice is None:
        invoice = linked_invoice_for(db, ts)
    data["invoice"] = {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "payment_status": getattr(invoice.payment_status, "value", invoice.payment_status),
    } if invoice else None
    data["can_generate_invoice"] = (
        ts.status == TimesheetStatus.APPROVED and invoice is None
    )
    return data


def _assignment_start(pe: ProjectEmployee) -> date:
    return pe.billing_date or pe.onboarding_date or date.today()


def _month_range(start: date, end: date) -> list[tuple[int, int]]:
    """Inclusive (year, month) pairs from start through end."""
    y, m = start.year, start.month
    ey, em = end.year, end.month
    out: list[tuple[int, int]] = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _due_context(db: Session, pe: ProjectEmployee, year: int, month: int) -> dict:
    project = db.get(Project, pe.project_id)
    customer = db.get(Customer, project.customer_id) if project else None
    opp = db.get(Opportunity, project.opportunity_id) if project and project.opportunity_id else None
    emp = db.get(Employee, pe.employee_id)
    start, end = period_bounds(year, month)
    return {
        "project_employee_id": pe.id,
        "project_employee_name": employee_display_name(emp),
        "employee_id": pe.employee_id,
        "project_id": pe.project_id,
        "project_type": getattr(opp.opp_type, "value", opp.opp_type) if opp else None,
        "customer_name": customer.name if customer else None,
        "project_title": project_title_label(customer=customer, employee=emp, pe=pe, project=project),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "month": month,
        "year": year,
    }


def due_report_rows(db: Session) -> list[dict]:
    """One row per active assignment/month from start through current month.

    Includes missing timesheets (Due) and Submitted (not yet Approved).
    """
    today = date.today()
    assignments = db.execute(
        select(ProjectEmployee).where(ProjectEmployee.is_active.is_(True))
        .order_by(ProjectEmployee.project_id, ProjectEmployee.employee_id)
    ).scalars().all()
    rows: list[dict] = []
    for pe in assignments:
        start = _assignment_start(pe)
        for year, month in _month_range(start, today):
            ts = db.execute(
                select(Timesheet).where(
                    Timesheet.project_id == pe.project_id,
                    Timesheet.employee_id == pe.employee_id,
                    Timesheet.month == month,
                    Timesheet.year == year,
                )
            ).scalars().first()
            if ts is not None and ts.status == TimesheetStatus.APPROVED:
                continue
            if ts is not None and ts.status not in (TimesheetStatus.SUBMITTED,):
                continue
            ctx = _due_context(db, pe, year, month)
            if ts is None:
                ctx.update({
                    "timesheet_id": None,
                    "status": "Due",
                    "status_label": "Due",
                })
            else:
                ctx.update({
                    "timesheet_id": ts.id,
                    "status": getattr(ts.status, "value", ts.status),
                    "status_label": timesheet_status_label(ts.status),
                })
            rows.append(ctx)
    rows.sort(key=lambda r: (r.get("project_title") or "", r.get("year", 0), r.get("month", 0)))
    return rows


def _due_rows(db: Session, month: int, year: int) -> list[dict]:
    """Legacy month-scoped due list (HR reminders)."""
    assignments = db.execute(
        select(ProjectEmployee).where(ProjectEmployee.is_active.is_(True))
        .order_by(ProjectEmployee.project_id, ProjectEmployee.employee_id)
    ).scalars().all()
    rows: list[dict] = []
    for pe in assignments:
        start = _assignment_start(pe)
        period_start, _ = period_bounds(year, month)
        if period_start < date(start.year, start.month, 1):
            continue
        ts = db.execute(
            select(Timesheet).where(
                Timesheet.project_id == pe.project_id,
                Timesheet.employee_id == pe.employee_id,
                Timesheet.month == month,
                Timesheet.year == year,
            )
        ).scalars().first()
        if ts is not None and ts.status in (TimesheetStatus.SUBMITTED, TimesheetStatus.APPROVED):
            continue
        ctx = _due_context(db, pe, year, month)
        if ts is None:
            due_status = "Due"
        elif ts.status == TimesheetStatus.DRAFT:
            due_status = "Draft"
        else:
            due_status = "Rejected"
        ctx.update({
            "employee_name": ctx["project_employee_name"],
            "status": due_status,
            "status_label": timesheet_status_label(ts.status, due_missing=ts is None)
            if ts else "Due",
            "timesheet_id": ts.id if ts else None,
        })
        rows.append(ctx)
    return rows


def for_submission_report_rows(db: Session) -> list[dict]:
    sheets = db.execute(
        select(Timesheet).where(Timesheet.status.in_([
            TimesheetStatus.DRAFT, TimesheetStatus.REJECTED,
        ])).order_by(Timesheet.year.desc(), Timesheet.month.desc(), Timesheet.id.desc())
    ).scalars().all()
    entries_map = _entries_for_timesheets(db, [t.id for t in sheets])
    return [
        timesheet_report_out(db, ts, entries=entries_map.get(ts.id, []))
        for ts in sheets
    ]


def approvals_report_rows(db: Session) -> list[dict]:
    sheets = db.execute(
        select(Timesheet).where(Timesheet.status.in_([
            TimesheetStatus.SUBMITTED, TimesheetStatus.APPROVED,
        ])).order_by(Timesheet.year.desc(), Timesheet.month.desc(), Timesheet.id.desc())
    ).scalars().all()
    ids = [t.id for t in sheets]
    entries_map = _entries_for_timesheets(db, ids)
    inv_map = _invoice_map(db, ids)
    return [
        timesheet_report_out(db, ts, entries=entries_map.get(ts.id, []),
                             invoice=inv_map.get(ts.id))
        for ts in sheets
    ]


# ---------------------------------------------------------------- invoice preview

def get_assignment_or_400(db: Session, ts: Timesheet) -> ProjectEmployee:
    """The employee's ProjectEmployee row on this timesheet's project, or 400."""
    assignment = db.execute(
        select(ProjectEmployee).where(
            ProjectEmployee.project_id == ts.project_id,
            ProjectEmployee.employee_id == ts.employee_id,
        )
    ).scalars().first()
    if assignment is None:
        raise HTTPException(
            status_code=400,
            detail="Employee has no project assignment (ProjectEmployee) on this project, "
                   "so billing rate/unit cannot be resolved",
        )
    return assignment


def linked_invoice_for(db: Session, ts: Timesheet) -> Invoice | None:
    return db.execute(
        select(Invoice).where(Invoice.timesheet_id == ts.id)
    ).scalars().first()


def timesheet_invoice_preview(db: Session, ts: Timesheet,
                              entries: list[TimesheetEntry] | None = None) -> dict:
    """Computed invoice subform for a timesheet (nothing is persisted).

    One line item per timesheet (one employee, one project, one month).

    Rates come from project_employee_rates (effective-dated). Mid-period rate
    changes split billable days/hours via project_employee_billing.split_period_by_rate.

    Amount formula by the assignment's billing_unit:
      * Hourly : sum over rate sub-periods of (hours_in_sub * rate)
      * Daily  : sum over rate sub-periods of (days_in_sub * rate)
      * Monthly: when leave is BILLABLE, charge each sub-period's monthly rate
        pro-rated by calendar days in the sub-period; when leave is NOT billable,
        use billable_days / working_days against the (split) monthly rates.
    """
    from services.project_employee_billing import (
        RateRow, invoice_amount_split, invoice_amount_uniform, rate_for_date, split_period_by_rate,
    )
    from services.project_employees import load_rate_rows

    project = db.get(Project, ts.project_id)
    emp = db.get(Employee, ts.employee_id)
    assignment = get_assignment_or_400(db, ts)
    if entries is None:
        entries = db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
            .order_by(TimesheetEntry.entry_date)
        ).scalars().all()
    rollup = _billable_rollup(project, entries)
    policy = effective_billing_policy(db, project) if project else BillingPolicy()

    rate_rows = load_rate_rows(db, assignment.id)
    unit = assignment.billing_unit
    period = period_label(ts.year, ts.month)
    employee_name = employee_display_name(emp) or f"Employee #{ts.employee_id}"
    project_name = project.name if project else f"Project #{ts.project_id}"
    period_start, period_end = period_bounds(ts.year, ts.month)

    fallback_rate = Decimal(assignment.billing_rate or 0)
    if not rate_rows:
        rate_rows = [RateRow.of(period_start, fallback_rate)]

    current_rate = rate_for_date(rate_rows, period_end) or fallback_rate
    subs = split_period_by_rate(period_start, period_end, rate_rows)
    rate_changed = len({s.rate for s in subs}) > 1

    def _entry_in_range(e: TimesheetEntry, start: date, end: date) -> bool:
        return start <= e.entry_date <= end

    if unit == BillingUnit.HOURLY:
        if rate_changed:
            parts: list[tuple[Decimal, Decimal]] = []
            for sub in subs:
                hrs = sum(
                    (Decimal(e.billable_hours or 0) for e in entries
                     if _entry_in_range(e, sub.start, sub.end)),
                    ZERO,
                )
                parts.append((hrs, sub.rate))
            amount = invoice_amount_split(parts)
            qty = rollup["actual_billable_hours"]
            rate = current_rate
        else:
            qty = rollup["actual_billable_hours"]
            rate = current_rate
            amount = invoice_amount_uniform(qty, rate)
        monthly_cost = None
    elif unit == BillingUnit.DAILY:
        if rate_changed:
            parts = []
            for sub in subs:
                days_ = sum(
                    (Decimal(e.billable_days or 0) for e in entries
                     if _entry_in_range(e, sub.start, sub.end)),
                    ZERO,
                )
                parts.append((days_, sub.rate))
            amount = invoice_amount_split(parts)
            qty = rollup["actual_billable_days"]
            rate = current_rate
        else:
            qty = rollup["actual_billable_days"]
            rate = current_rate
            amount = invoice_amount_uniform(qty, rate)
        monthly_cost = None
    else:  # Monthly
        qty = ONE
        monthly_cost = current_rate
        working_days = Decimal(rollup["working_days"])
        if rate_changed:
            parts = []
            for sub in subs:
                if policy.leave_billable:
                    cal_days = Decimal((sub.end - sub.start).days + 1)
                    month_days_n = Decimal((period_end - period_start).days + 1)
                    ratio = (cal_days / month_days_n) if month_days_n > ZERO else ZERO
                    parts.append((ratio, sub.rate))
                else:
                    if working_days > ZERO:
                        sub_billable = sum(
                            (Decimal(e.billable_days or 0) for e in entries
                             if _entry_in_range(e, sub.start, sub.end)),
                            ZERO,
                        )
                        parts.append((min(sub_billable / working_days, ONE), sub.rate))
                    else:
                        parts.append((ZERO, sub.rate))
            amount = invoice_amount_split(parts)
            rate = current_rate
        else:
            rate = current_rate
            if policy.leave_billable:
                amount = rate.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            else:
                if working_days > ZERO:
                    ratio = min(rollup["actual_billable_days"] / working_days, ONE)
                else:
                    ratio = ZERO
                amount = (rate * ratio).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    line = {
        "s_no": 1,
        "description": f"{employee_name} — {project_name} professional services, {period}",
        "monthly_cost": float(monthly_cost) if monthly_cost is not None else None,
        "total_billed_qty": float(qty),
        "rate_per_unit": float(rate),
        "leave_billable_days": float(rollup["leave_billable_days"]),
        "amount": float(amount),
        "rate_split": rate_changed,
    }
    linked = linked_invoice_for(db, ts)
    return {
        "line_items": [line],
        "totals": {"sub_total": float(amount)},
        "linked_invoice": {
            "id": linked.id,
            "invoice_number": linked.invoice_number,
            "payment_status": getattr(linked.payment_status, "value", linked.payment_status),
        } if linked else None,
        "can_generate": ts.status == TimesheetStatus.APPROVED and linked is None,
    }
