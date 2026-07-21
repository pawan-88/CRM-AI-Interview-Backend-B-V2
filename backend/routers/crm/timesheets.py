"""Timesheets API: monthly sheets, daily entries with server-computed billables,
submit/approve/reject workflow, attachment upload, summary rollups,
activity log, and the timesheet-due report/reminders (migration 0021)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_write, get_crm_db, get_current_user,
    page_params, require_access,
)
from models import (
    Employee, EntryLocation, Invoice, InvoiceLine, PaymentStatus, Project,
    ProjectEmployee, PurchaseOrder, Timesheet, TimesheetActivityLog, TimesheetAttachment,
    TimesheetEntry, TimesheetStatus,
)
from schemas.common import RejectIn, envelope
from schemas.timesheets import TimesheetCreate, TimesheetEntryIn
from services import tax
from services.crm_common import log_activity, next_sequence_number, paginate, save_upload, save_upload_hashed
from services.finance import (
    active_po_allocation_for_project, assert_po_allows_new_drawdown,
    log_invoice_created_on_po, serialize_invoice,
)
from services.notify import notify_role, notify_user
from services.timesheets import (
    accrue_comp_off, approvals_report_rows, attachment_out, build_generated_entry,
    compute_billables, day_name, due_report_rows, effective_billing_policy,
    employee_for_user, entry_out, for_submission_report_rows,
    get_timesheet_or_404, holidays_for_project_period, linked_invoice_for, list_attachments,
    month_days, period_label, resolve_entry_fields, timesheet_detail_out,
    timesheet_invoice_preview, timesheet_out, timesheet_summary, _due_rows,
)

router = APIRouter(prefix="/api/timesheets", tags=["CRM: Timesheets"])

TS_VIEW = require_access("timesheets", mode="view")
TS_EDIT = require_access("timesheets", mode="edit")

EDITABLE_STATUSES = (TimesheetStatus.DRAFT, TimesheetStatus.REJECTED)


def _is_staff(user: CurrentUser) -> bool:
    """HR/Finance/RMG — due/approvals reports and approve/reject/invoice gates."""
    return user.is_admin or user.has_any("HR", "Finance", "RMG")


def _can_manage_sheets(user: CurrentUser) -> bool:
    """Create/list/edit daily entries for any assigned employee (delivery owners)."""
    return user.is_admin or user.has_any("HR", "Finance", "RMG", "Sales", "Sales_Head")


def _is_owner(db: Session, user: CurrentUser, timesheet: Timesheet) -> bool:
    emp = employee_for_user(db, user.id)
    return bool(emp and emp.id == timesheet.employee_id)


def _require_owner_or(db: Session, user: CurrentUser, timesheet: Timesheet,
                      *roles: str) -> None:
    if user.is_admin or user.has_any(*roles) or _is_owner(db, user, timesheet):
        return
    raise HTTPException(status_code=403, detail="Not allowed to access this timesheet")


# ---------------------------------------------------------------- create / list

@router.post("")
def create_timesheet(
    body: TimesheetCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    if not _can_manage_sheets(user):
        # Self-service: the caller's linked employee must be the timesheet's employee.
        emp = employee_for_user(db, user.id)
        if not emp or emp.id != body.employee_id:
            raise HTTPException(
                status_code=403,
                detail="Requires HR/Finance/RMG/Sales role or a timesheet for your own employee profile",
            )
    project = db.get(Project, body.project_id)
    if not project:
        raise HTTPException(status_code=400, detail="Project not found")
    if not db.get(Employee, body.employee_id):
        raise HTTPException(status_code=400, detail="Employee not found")
    if body.project_employee_id is not None:
        assignment = db.get(ProjectEmployee, body.project_employee_id)
        if assignment is None:
            raise HTTPException(status_code=400, detail="Project assignment not found")
        if assignment.project_id != body.project_id or assignment.employee_id != body.employee_id:
            raise HTTPException(status_code=400, detail="Project assignment does not match project/employee")
    else:
        assignment = db.execute(
            select(ProjectEmployee).where(
                ProjectEmployee.project_id == body.project_id,
                ProjectEmployee.employee_id == body.employee_id,
            )
        ).scalars().first()
    if not assignment:
        raise HTTPException(status_code=400, detail="Employee is not assigned to this project")
    duplicate = db.execute(
        select(Timesheet).where(
            Timesheet.project_id == body.project_id,
            Timesheet.employee_id == body.employee_id,
            Timesheet.month == body.month,
            Timesheet.year == body.year,
        )
    ).scalars().first()
    if duplicate:
        db.refresh(duplicate)
        entries = db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == duplicate.id)
            .order_by(TimesheetEntry.entry_date)
        ).scalars().all()
        return envelope(
            data=timesheet_detail_out(db, duplicate, entries),
            message="Timesheet already exists for this period",
        )
    ts = Timesheet(
        project_id=body.project_id,
        employee_id=body.employee_id,
        project_employee_id=assignment.id,
        month=body.month,
        year=body.year,
        status=TimesheetStatus.DRAFT,
    )
    db.add(ts)
    db.flush()
    if body.generate_days:
        holiday_dates = holidays_for_project_period(db, project, body.year, body.month)
        policy = effective_billing_policy(db, project)
        for d in month_days(body.year, body.month):
            db.add(build_generated_entry(
                timesheet_id=ts.id, d=d, holiday_dates=holiday_dates,
                project=project, policy=policy,
            ))
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_CREATED",
                 f"Timesheet created for {period_label(body.year, body.month)}")
    db.commit()
    db.refresh(ts)
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    return envelope(
        data=timesheet_detail_out(db, ts, entries) if entries else timesheet_out(ts),
        message="Timesheet created",
    )


@router.get("")
def list_timesheets(
    project_id: int | None = None,
    employee_id: int | None = None,
    month: int | None = None,
    year: int | None = None,
    status: str | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    stmt = select(Timesheet).order_by(Timesheet.id.desc())
    if not _can_manage_sheets(user):
        emp = employee_for_user(db, user.id)
        if not emp:
            return envelope(data=[], meta={"page": params.page, "limit": params.limit,
                                           "total": 0, "pages": 0})
        stmt = stmt.where(Timesheet.employee_id == emp.id)
    if project_id is not None:
        stmt = stmt.where(Timesheet.project_id == project_id)
    if employee_id is not None:
        stmt = stmt.where(Timesheet.employee_id == employee_id)
    if month is not None:
        stmt = stmt.where(Timesheet.month == month)
    if year is not None:
        stmt = stmt.where(Timesheet.year == year)
    if status:
        try:
            stmt = stmt.where(Timesheet.status == TimesheetStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[timesheet_out(ts) for ts in items], meta=meta)


# ---------------------------------------------------------------- entries

@router.post("/{timesheet_id}/entries")
def upsert_entries(
    timesheet_id: int,
    entries: list[TimesheetEntryIn],
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    if ts.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Entries can only be edited while the timesheet is Draft or Rejected",
        )
    if not entries:
        raise HTTPException(status_code=400, detail="At least one entry is required")
    project = db.get(Project, ts.project_id)
    policy = effective_billing_policy(db, project)
    holiday_dates = holidays_for_project_period(db, project, ts.year, ts.month)
    existing = {
        e.entry_date: e
        for e in db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        ).scalars().all()
    }
    seen = set()
    for item in entries:
        d = item.entry_date
        if d.month != ts.month or d.year != ts.year:
            raise HTTPException(
                status_code=400,
                detail=f"Entry date {d.isoformat()} is outside the timesheet period "
                       f"{ts.year}-{ts.month:02d}",
            )
        if d in seen:
            raise HTTPException(status_code=400,
                                detail=f"Duplicate entry date in payload: {d.isoformat()}")
        seen.add(d)
        day_type, is_working, hours_worked, attendance_status, leave_type, leave_period = (
            resolve_entry_fields(d=d, item=item, holiday_dates=holiday_dates)
        )
        if item.entry_project_id is not None:
            split_proj = db.get(Project, item.entry_project_id)
            if split_proj is None:
                raise HTTPException(status_code=400, detail=f"Split project #{item.entry_project_id} not found")
        billable_hours, billable_days = compute_billables(
            is_working=is_working,
            hours_worked=hours_worked,
            attendance_status=attendance_status,
            leave_period=leave_period,
            project=project,
            policy=policy,
        )
        row = existing.get(d)
        if row is None:
            row = TimesheetEntry(timesheet_id=ts.id, entry_date=d)
            db.add(row)
            existing[d] = row
        row.day_of_week = day_name(d)
        row.day_type = day_type
        row.is_working = is_working
        row.hours_worked = hours_worked
        row.attendance_status = attendance_status
        row.leave_type = leave_type
        row.leave_period = leave_period
        row.location = item.location or EntryLocation.ONSITE
        row.view_flag = bool(item.view_flag)
        row.entry_project_id = item.entry_project_id
        row.billable_hours = billable_hours
        row.billable_days = billable_days
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "ENTRIES_UPDATED",
                 f"{len(entries)} entries upserted")
    db.commit()
    rows = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    return envelope(data=[entry_out(e) for e in rows],
                    message=f"{len(entries)} entries upserted")


# ---------------------------------------------------------------- workflow

@router.post("/{timesheet_id}/submit")
def submit_timesheet(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "RMG", "Sales", "Sales_Head")
    if ts.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400,
                            detail="Only Draft or Rejected timesheets can be submitted")
    has_entries = db.execute(
        select(TimesheetEntry.id).where(TimesheetEntry.timesheet_id == ts.id).limit(1)
    ).first()
    if not has_entries:
        raise HTTPException(status_code=400, detail="Cannot submit a timesheet with no entries")
    # NEVER block timesheet submit for PO reasons (D8). Invoice generate still gates.
    ts.status = TimesheetStatus.SUBMITTED
    ts.submitted_at = datetime.now(timezone.utc)
    ts.rejection_reason = None
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_SUBMITTED",
                 f"Timesheet for {period_label(ts.year, ts.month)} submitted for approval")
    db.commit()
    db.refresh(ts)
    return envelope(data=timesheet_out(ts), message="Timesheet submitted")


@router.post("/{timesheet_id}/approve")
def approve_timesheet(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    if ts.status != TimesheetStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only Submitted timesheets can be approved")
    ts.status = TimesheetStatus.APPROVED
    ts.approved_by = user.id
    ts.approved_at = datetime.now(timezone.utc)
    # Comp-off earning: weekend/holiday days actually worked credit the
    # employee's Comp-Off leave balance (idempotent — delta vs prior grants).
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    earned = accrue_comp_off(db, ts, entries)
    comp_off_note = ""
    if earned and float(earned) > 0:
        comp_off_note = (f" — {float(earned):g} comp-off day(s) credited to the "
                         f"employee's leave balance")
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_APPROVED",
                 f"Timesheet approved{comp_off_note}")
    db.commit()
    db.refresh(ts)
    msg = "Timesheet approved" + comp_off_note
    return envelope(data=timesheet_out(ts), message=msg)


@router.post("/{timesheet_id}/reject")
def reject_timesheet(
    timesheet_id: int,
    body: RejectIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    if ts.status != TimesheetStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only Submitted timesheets can be rejected")
    try:
        reason = body.validated_reason()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ts.status = TimesheetStatus.REJECTED
    ts.rejection_reason = reason
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_REJECTED",
                 f"Timesheet rejected: {reason}")
    db.commit()
    db.refresh(ts)
    return envelope(data=timesheet_out(ts), message="Timesheet rejected")


# ---------------------------------------------------------------- attachment / summary / detail

@router.post("/{timesheet_id}/attachment")
def upload_attachment(
    timesheet_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    """Legacy single-file upload (kept for back-compat; prefer /attachments)."""
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    ts.file_attachment_url = save_upload(file, "timesheets")
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "ATTACHMENT_ADDED",
                 f"Attachment uploaded: {ts.file_attachment_url}")
    db.commit()
    return envelope(data={"file_attachment_url": ts.file_attachment_url},
                    message="Attachment uploaded")


@router.get("/{timesheet_id}/attachments")
def list_timesheet_attachments(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    return envelope(data=list_attachments(db, ts))


@router.post("/{timesheet_id}/attachments")
def add_timesheet_attachment(
    timesheet_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    url, sha, size = save_upload_hashed(file, "timesheets")
    att = TimesheetAttachment(
        timesheet_id=ts.id,
        file_url=url,
        file_name=file.filename,
        file_sha256=sha,
        file_size=size,
        kind="timesheet",
        uploaded_by=user.id,
    )
    db.add(att)
    if not ts.file_attachment_url:
        ts.file_attachment_url = url
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "ATTACHMENT_ADDED",
                 f"Attachment uploaded: {url}")
    db.commit()
    db.refresh(att)
    return envelope(data=attachment_out(att), message="Attachment uploaded")


@router.delete("/attachments/{attachment_id}")
def delete_timesheet_attachment(
    attachment_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    att = db.get(TimesheetAttachment, attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    ts = get_timesheet_or_404(db, att.timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    db.delete(att)
    remaining = db.execute(
        select(TimesheetAttachment).where(TimesheetAttachment.timesheet_id == ts.id)
        .order_by(TimesheetAttachment.uploaded_at.desc())
    ).scalars().first()
    ts.file_attachment_url = remaining.file_url if remaining else None
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "ATTACHMENT_REMOVED",
                 f"Attachment #{attachment_id} deleted")
    db.commit()
    return envelope(data={"id": attachment_id}, message="Attachment deleted")


@router.get("/{timesheet_id}/summary")
def get_timesheet_summary(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    return envelope(data=timesheet_summary(db, ts, entries))


# ---------------------------------------------------------------- invoice preview / generation

@router.get("/{timesheet_id}/invoice-preview")
def get_invoice_preview(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    """Computed invoice subform (line items + totals); nothing is persisted.

    Visible to Finance, HR, Admin — and to the timesheet's own employee.
    """
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "Finance", "HR")
    return envelope(data=timesheet_invoice_preview(db, ts))


@router.post("/{timesheet_id}/generate-invoice")
def generate_invoice_from_timesheet(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "Finance", "RMG")),
):
    """Create an Invoice from an Approved timesheet (one invoice per timesheet).

    sub_total comes from the invoice preview; tax uses the slab of the
    project's active PO allocation when one exists (otherwise tax 0, no PO),
    and PO/allocation balances are consumed exactly as in POST /api/invoices.
    """
    ts = get_timesheet_or_404(db, timesheet_id)
    if ts.status != TimesheetStatus.APPROVED:
        raise HTTPException(status_code=400,
                            detail="Only Approved timesheets can be invoiced")
    if linked_invoice_for(db, ts) is not None:
        raise HTTPException(status_code=409,
                            detail="An invoice has already been generated for this timesheet")

    preview = timesheet_invoice_preview(db, ts)  # 400s when no assignment exists
    sub_total = Decimal(str(preview["totals"]["sub_total"]))

    alloc = active_po_allocation_for_project(db, ts.project_id)
    po = db.get(PurchaseOrder, alloc.po_id) if alloc is not None else None
    assert_po_allows_new_drawdown(po, action="generate invoice")
    if po is not None and po.tax_slab is not None:
        tax_amount = tax.gst_amount(sub_total, po.tax_slab)
    else:
        tax_amount = Decimal("0")
    grand_total = sub_total + tax_amount
    if po is not None and Decimal(str(po.balance_value)) < grand_total:
        raise HTTPException(status_code=400, detail="PO balance insufficient")

    invoice_number = next_sequence_number(db, Invoice, Invoice.invoice_number, "INV")
    invoice = Invoice(
        invoice_number=invoice_number,
        po_id=po.id if po is not None else None,
        project_id=ts.project_id,
        timesheet_id=ts.id,
        invoice_date=date.today(),
        due_date=None,
        sub_total=sub_total,
        tax_amount=tax_amount,
        grand_total=grand_total,
        paid_amount=Decimal("0"),
        balance_amount=grand_total,
        payment_status=PaymentStatus.UNPAID,
        # Persist the preview line items as InvoiceLine rows (Tab 11).
        lines=[
            InvoiceLine(
                s_no=int(li.get("s_no") or i),
                description=str(li.get("description") or ""),
                qty=Decimal(str(li.get("total_billed_qty") or 0)),
                rate=Decimal(str(li.get("rate_per_unit") or 0)),
                amount=Decimal(str(li.get("amount") or 0)),
            )
            for i, li in enumerate(preview["line_items"], start=1)
        ],
    )
    db.add(invoice)
    if po is not None:
        tax.apply_po_consumption(po, grand_total)
        alloc.consumed_amount = Decimal(str(alloc.consumed_amount)) + grand_total
    db.flush()
    log_invoice_created_on_po(db, po, invoice, user.id)
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "INVOICE_GENERATED",
                 f"Invoice {invoice_number} generated for {float(grand_total):.2f}")
    notify_role(
        db, "Finance",
        f"Invoice {invoice_number} generated from timesheet #{ts.id}",
        f"Timesheet {ts.year}-{ts.month:02d} (project #{ts.project_id}, "
        f"employee #{ts.employee_id}) was invoiced for {float(grand_total):.2f}.",
        f"/invoices/{invoice.id}", exclude_user_id=user.id,
    )
    db.commit()
    db.refresh(invoice)
    return envelope(
        data={"invoice": serialize_invoice(invoice, detail=True, db=db), "timesheet_id": ts.id},
        message="Invoice generated from timesheet",
    )


# ---------------------------------------------------------------- activity log

@router.get("/{timesheet_id}/activity-log")
def get_timesheet_activity_log(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    """Chronological timesheet audit trail with user names (PO-activity-log style)."""
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "Sales_Head")
    rows = db.execute(
        text(
            "SELECT l.id, l.user_id, r.username, r.full_name, l.action_type, l.comment, "
            "       l.timestamp "
            "FROM timesheet_activity_log l "
            "LEFT JOIN registration_data r ON r.id = l.user_id "
            "WHERE l.timesheet_id = :tid "
            "ORDER BY l.timestamp ASC, l.id ASC"
        ),
        {"tid": ts.id},
    ).mappings().all()
    return envelope(data=[
        {
            "id": row["id"],
            "comment": row["comment"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
            "action_type": row["action_type"],
            "user": row["full_name"] or row["username"],
            "user_id": row["user_id"],
        }
        for row in rows
    ])


# ---------------------------------------------------------------- reports (register before /{timesheet_id})

@router.get("/reports/due")
def timesheets_report_due(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    """RMG due report: one row per active assignment/month lacking approval."""
    rows = due_report_rows(db)
    return envelope(data=rows, message=f"{len(rows)} due row(s)")


@router.get("/reports/for-submission")
def timesheets_report_for_submission(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    rows = for_submission_report_rows(db)
    return envelope(data=rows, message=f"{len(rows)} timesheet(s) pending submission")


@router.get("/reports/approvals")
def timesheets_report_approvals(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    rows = approvals_report_rows(db)
    return envelope(data=rows, message=f"{len(rows)} timesheet(s) for approval review")


# ---------------------------------------------------------------- timesheet due (legacy month filter)

def _validate_period_params(month: int, year: int) -> None:
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be between 1 and 12")
    if year < 2000 or year > 2100:
        raise HTTPException(status_code=400, detail="year is out of range")


@router.get("/due")
def timesheets_due(
    month: int,
    year: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    """Active assignments with no Approved/Submitted timesheet for the period.

    NOTE: on-demand report — this app has no cron scheduler, so pair it with
    POST /api/timesheets/due/remind to nudge employees.
    """
    _validate_period_params(month, year)
    rows = _due_rows(db, month, year)
    return envelope(data=rows,
                    message=f"{len(rows)} assignment(s) due for {period_label(year, month)}")


@router.post("/due/remind")
def remind_timesheets_due(
    month: int,
    year: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "RMG")),
):
    """Bell-notify linked users of employees whose timesheets are due (on-demand)."""
    _validate_period_params(month, year)
    rows = _due_rows(db, month, year)
    notified = 0
    for row in rows:
        emp = db.get(Employee, row["employee_id"])
        if not emp or not emp.user_id:
            continue
        project_label = row["project_title"] or f"project #{row['project_id']}"
        notify_user(
            db, emp.user_id,
            f"Timesheet due for {period_label(year, month)} — {project_label}",
            f"Your timesheet for {period_label(year, month)} on {project_label} is "
            f"{row['status'].lower()}. Please create/submit it.",
            "/timesheets",
        )
        notified += 1
    db.commit()
    return envelope(data={"due": len(rows), "notified": notified},
                    message=f"{notified} employee notification(s) created")


@router.get("/{timesheet_id}")
def get_timesheet(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    return envelope(data=timesheet_detail_out(db, ts, entries))
