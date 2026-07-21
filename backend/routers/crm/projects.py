"""Projects API: CRUD, team (project employees), timesheets view, communication matrix."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_read, gated_write, get_crm_db, page_params, role_required,
)
from models import (
    Customer, CustomerLeavePolicy, Employee, Project, ProjectCommunicationMatrix,
    ProjectEmployee, ProjectEmployeeLeaveDetail, ProjectEmployeeRate, ProjectStatus,
    Timesheet, TimesheetStatus,
)
from schemas.common import envelope
from schemas.projects import (
    CommMatrixIn, ProjectCreate, ProjectEmployeeIn, ProjectEmployeeLeaveDetailUpdate,
    ProjectEmployeeRateIn, ProjectEmployeeRateUpdate, ProjectEmployeeUpdate, ProjectUpdate,
)
from services.crm_common import paginate
from services.project_employees import (
    clear_other_current_rates, enrich_pe_list_row, ensure_initial_rate, exit_project_employee,
    get_pe_or_404, group_pe_rows_by_employee, leave_detail_out, project_employee_detail_out,
    rate_out, seed_leave_details_from_customer_policy, sync_pe_billing_from_current_rate,
)
from services.projects import (
    add_history_entry, comm_entry_out, get_project_or_404, open_history_row,
    project_detail_out, project_employee_out, project_history, project_out,
    validate_project_refs,
)
from services.timesheets import timesheet_out

router = APIRouter(prefix="/api/projects", tags=["CRM: Projects"])

read_projects = gated_read("projects")
write_projects = gated_write("projects", "Sales_Head", "Finance")
read_pe = gated_read("project-employees")
write_pe = gated_write("project-employees", "Sales_Head", "HR", "Finance")


# ---------------------------------------------------------------- CRUD

@router.get("")
def list_projects(
    status: str | None = None,
    customer_id: int | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    stmt = select(Project).order_by(Project.id.desc())
    if status:
        try:
            stmt = stmt.where(Project.status == ProjectStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    if customer_id is not None:
        stmt = stmt.where(Project.customer_id == customer_id)
    if params.search:
        stmt = stmt.where(Project.name.ilike(f"%{params.search}%"))
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[project_out(p) for p in items], meta=meta)


@router.post("")
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    validate_project_refs(db, body.opportunity_id, body.customer_id)
    project = Project(
        opportunity_id=body.opportunity_id,
        customer_id=body.customer_id,
        name=body.name,
        billing_cycle_start_day=body.billing_cycle_start_day,
        billing_cycle_end_day=body.billing_cycle_end_day,
        billing_frequency=body.billing_frequency,
        max_billable_hours_day=body.max_billable_hours_day,
        max_billable_hours_month=body.max_billable_hours_month,
        max_billable_days_month=body.max_billable_days_month,
        no_billing_period_days=body.no_billing_period_days,
        status=body.status,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return envelope(data=project_out(project), message="Project created")


@router.get("/all-employees")
def list_all_project_employees(
    p: PageParams = Depends(page_params),
    project_id: int | None = None,
    customer_id: int | None = None,
    status: str | None = None,  # active | exited | all
    group_by: str | None = None,  # employee → nested groups (UC-12)
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    """Global list of every Project Employee mapping across all projects (the
    Project Employees tab). One row per (employee × project) mapping — so the
    same person on two projects shows as two rows, each with its own rate.
    Pass group_by=employee for nested groups (one card per person, mappings[]).
    Defined BEFORE /{project_id} so this literal path wins."""
    base = (
        select(ProjectEmployee, Employee, Project, Customer.name.label("customer_name"))
        .join(Employee, Employee.id == ProjectEmployee.employee_id)
        .join(Project, Project.id == ProjectEmployee.project_id)
        .join(Customer, Customer.id == Project.customer_id, isouter=True)
    )
    if project_id is not None:
        base = base.where(ProjectEmployee.project_id == project_id)
    if customer_id is not None:
        base = base.where(Project.customer_id == customer_id)
    st = (status or "all").strip().lower()
    if st == "active":
        base = base.where(ProjectEmployee.is_active.is_(True), ProjectEmployee.is_exit.is_(False))
    elif st in ("exited", "exit", "inactive"):
        base = base.where(or_(ProjectEmployee.is_exit.is_(True), ProjectEmployee.is_active.is_(False)))
    if p.search:
        like = f"%{p.search.lower()}%"
        base = base.where(
            or_(
                func.lower(func.coalesce(Employee.first_name, "")).like(like),
                func.lower(func.coalesce(Employee.last_name, "")).like(like),
                func.lower(func.coalesce(Employee.email, "")).like(like),
                func.lower(Project.name).like(like),
                func.lower(func.coalesce(Customer.name, "")).like(like),
            )
        )
    grouped = (group_by or "").strip().lower() == "employee"
    if grouped:
        order = (Employee.first_name.asc(), Employee.last_name.asc(), Project.name.asc())
    else:
        order = (Project.name.asc(), Employee.first_name.asc())
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    rows = db.execute(
        base.order_by(*order)
        .limit(p.limit).offset(p.offset)
    ).all()
    flat: list[dict] = []
    for pe, emp, proj, customer_name in rows:
        d = project_employee_out(pe, emp)
        d["project_name"] = proj.name
        d["customer_id"] = proj.customer_id
        d["customer_name"] = customer_name
        enrich_pe_list_row(db, pe, d)
        flat.append(d)
    pages = (total + p.limit - 1) // p.limit if p.limit else 1
    meta = {"page": p.page, "limit": p.limit, "total": total, "pages": pages,
            "group_by": "employee" if grouped else None}
    if grouped:
        data = group_pe_rows_by_employee(flat)
        return envelope(data=data, message="Project employees (grouped by employee)", meta=meta)
    return envelope(data=flat, message="Project employees", meta=meta)


# ---------------------------------------------------------------- PE detail / rates (literal /employees/... before /{project_id})

@router.get("/employees/{pe_id}")
def get_project_employee_detail(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_pe),
):
    pe = get_pe_or_404(db, pe_id)
    return envelope(data=project_employee_detail_out(db, pe))


@router.put("/employees/{pe_id}")
def update_project_employee_by_id(
    pe_id: int,
    body: ProjectEmployeeUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    pe = get_pe_or_404(db, pe_id)
    changes = body.model_dump(exclude_unset=True)
    exiting = (not pe.is_exit and changes.get("is_exit") is True) or (
        pe.is_active and changes.get("is_active") is False and changes.get("is_exit", True) is not False
    )
    deactivating = pe.is_active and changes.get("is_active") is False
    rate_changed = "billing_rate" in changes or "billing_unit" in changes
    for field, value in changes.items():
        setattr(pe, field, value)
    exit_summary = None
    if exiting or (pe.is_exit and deactivating):
        exit_summary = exit_project_employee(db, pe, exit_date=pe.exit_date or date.today())
        history = open_history_row(db, pe.project_id, pe.employee_id)
        if history:
            history.end_date = pe.exit_date or date.today()
    elif deactivating:
        history = open_history_row(db, pe.project_id, pe.employee_id)
        if history:
            history.end_date = date.today()
    if rate_changed:
        clear_other_current_rates(db, pe.id)
        db.add(ProjectEmployeeRate(
            project_employee_id=pe.id,
            effective_from=pe.billing_date or date.today(),
            rate=pe.billing_rate,
            billing_unit=pe.billing_unit,
            is_current_rate=True,
        ))
    db.commit()
    db.refresh(pe)
    data = project_employee_detail_out(db, pe)
    if exit_summary:
        data["exit_summary"] = exit_summary
    return envelope(data=data, message="Project employee updated")


@router.post("/employees/{pe_id}/exit")
def exit_project_employee_endpoint(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
    exit_date: date | None = None,
):
    """Explicit exit: stop accrual, flag open periods, mark leave for settlement."""
    pe = get_pe_or_404(db, pe_id)
    summary = exit_project_employee(db, pe, exit_date=exit_date or date.today())
    history = open_history_row(db, pe.project_id, pe.employee_id)
    if history:
        history.end_date = pe.exit_date or date.today()
    db.commit()
    db.refresh(pe)
    data = project_employee_detail_out(db, pe)
    data["exit_summary"] = summary
    return envelope(data=data, message="Project employee exited")


@router.put("/employees/{pe_id}/leave/{leave_id}")
def update_pe_leave_detail(
    pe_id: int,
    leave_id: int,
    body: ProjectEmployeeLeaveDetailUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("project-employees", "HR", "Sales_Head")),
):
    get_pe_or_404(db, pe_id)
    row = db.get(ProjectEmployeeLeaveDetail, leave_id)
    if not row or row.project_employee_id != pe_id:
        raise HTTPException(status_code=404, detail="Leave detail not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    policy = (
        db.get(CustomerLeavePolicy, row.customer_leave_policy_id)
        if row.customer_leave_policy_id else None
    )
    return envelope(
        data=leave_detail_out(row, policy=policy),
        message="Leave detail updated",
    )

@router.get("/employees/{pe_id}/rates")
def list_pe_rates(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_pe),
):
    get_pe_or_404(db, pe_id)
    rows = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == pe_id)
        .order_by(ProjectEmployeeRate.effective_from.desc(), ProjectEmployeeRate.id.desc())
    ).scalars().all()
    return envelope(data=[rate_out(r) for r in rows])


@router.post("/employees/{pe_id}/rates")
def create_pe_rate(
    pe_id: int,
    body: ProjectEmployeeRateIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("project-employees", "Sales_Head", "Finance", "HR")),
):
    pe = get_pe_or_404(db, pe_id)
    if body.is_current_rate:
        clear_other_current_rates(db, pe.id)
    row = ProjectEmployeeRate(
        project_employee_id=pe.id,
        effective_from=body.effective_from,
        rate=body.rate,
        billing_unit=body.billing_unit or pe.billing_unit,
        is_current_rate=body.is_current_rate,
    )
    db.add(row)
    db.flush()
    if row.is_current_rate:
        sync_pe_billing_from_current_rate(pe, row)
    db.commit()
    db.refresh(row)
    return envelope(data=rate_out(row), message="Rate added")


@router.put("/employees/{pe_id}/rates/{rate_id}")
def update_pe_rate(
    pe_id: int,
    rate_id: int,
    body: ProjectEmployeeRateUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("project-employees", "Sales_Head", "Finance", "HR")),
):
    pe = get_pe_or_404(db, pe_id)
    row = db.get(ProjectEmployeeRate, rate_id)
    if not row or row.project_employee_id != pe_id:
        raise HTTPException(status_code=404, detail="Rate not found")
    changes = body.model_dump(exclude_unset=True)
    make_current = changes.pop("is_current_rate", None)
    for field, value in changes.items():
        setattr(row, field, value)
    if make_current is True:
        clear_other_current_rates(db, pe.id, keep_id=row.id)
        row.is_current_rate = True
        sync_pe_billing_from_current_rate(pe, row)
    elif make_current is False:
        row.is_current_rate = False
    elif row.is_current_rate:
        sync_pe_billing_from_current_rate(pe, row)
    db.commit()
    db.refresh(row)
    return envelope(data=rate_out(row), message="Rate updated")


@router.delete("/employees/{pe_id}/rates/{rate_id}")
def delete_pe_rate(
    pe_id: int,
    rate_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("project-employees", "Sales_Head", "Finance", "HR")),
):
    pe = get_pe_or_404(db, pe_id)
    row = db.get(ProjectEmployeeRate, rate_id)
    if not row or row.project_employee_id != pe_id:
        raise HTTPException(status_code=404, detail="Rate not found")
    was_current = row.is_current_rate
    db.delete(row)
    db.flush()
    if was_current:
        replacement = db.execute(
            select(ProjectEmployeeRate)
            .where(ProjectEmployeeRate.project_employee_id == pe.id)
            .order_by(ProjectEmployeeRate.effective_from.desc())
        ).scalars().first()
        if replacement:
            replacement.is_current_rate = True
            sync_pe_billing_from_current_rate(pe, replacement)
    db.commit()
    return envelope(message="Rate deleted")


@router.get("/{project_id}")
def get_project(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    project = get_project_or_404(db, project_id)
    return envelope(data=project_detail_out(db, project))


@router.get("/{project_id}/history")
def get_project_history(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    """Project 360°: aggregated read-only history/health view of one project."""
    project = get_project_or_404(db, project_id)
    return envelope(data=project_history(db, project))


@router.put("/{project_id}")
def update_project(
    project_id: int,
    body: ProjectUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    project = get_project_or_404(db, project_id)
    changes = body.model_dump(exclude_unset=True)
    validate_project_refs(db, changes.get("opportunity_id"), changes.get("customer_id"))
    for field, value in changes.items():
        setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return envelope(data=project_out(project), message="Project updated")


# ---------------------------------------------------------------- team

@router.post("/{project_id}/employees")
def add_project_employee(
    project_id: int,
    body: ProjectEmployeeIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    project = get_project_or_404(db, project_id)
    employee = db.get(Employee, body.employee_id)
    if not employee:
        raise HTTPException(status_code=400, detail="Employee not found")
    existing = db.execute(
        select(ProjectEmployee).where(
            ProjectEmployee.project_id == project.id,
            ProjectEmployee.employee_id == body.employee_id,
        )
    ).scalars().first()
    if existing and existing.is_active:
        raise HTTPException(status_code=409, detail="Employee is already assigned to this project")
    if existing:
        # Re-activate the previous (deactivated) assignment with the new terms.
        existing.onboarding_date = body.onboarding_date
        existing.experience_years = body.experience_years
        existing.project_experience_years = body.project_experience_years
        existing.work_mode = body.work_mode
        existing.billing_rate = body.billing_rate
        existing.billing_unit = body.billing_unit
        existing.is_active = True
        existing.is_exit = body.is_exit
        existing.exit_date = body.exit_date
        existing.billing_date = body.billing_date
        existing.settlement_pending = False
        pe = existing
    else:
        pe = ProjectEmployee(
            project_id=project.id,
            employee_id=body.employee_id,
            onboarding_date=body.onboarding_date,
            experience_years=body.experience_years,
            project_experience_years=body.project_experience_years,
            work_mode=body.work_mode,
            billing_rate=body.billing_rate,
            billing_unit=body.billing_unit,
            is_active=True,
            is_exit=body.is_exit,
            exit_date=body.exit_date,
            billing_date=body.billing_date,
            settlement_pending=False,
        )
        db.add(pe)
    db.flush()
    ensure_initial_rate(db, pe)
    seed_leave_details_from_customer_policy(db, pe, project)
    add_history_entry(db, project.id, employee, body.onboarding_date)
    db.commit()
    db.refresh(pe)
    return envelope(data=project_employee_out(pe, employee), message="Employee assigned to project")


@router.put("/{project_id}/employees/{pe_id}")
def update_project_employee(
    project_id: int,
    pe_id: int,
    body: ProjectEmployeeUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    get_project_or_404(db, project_id)
    pe = db.get(ProjectEmployee, pe_id)
    if not pe or pe.project_id != project_id:
        raise HTTPException(status_code=404, detail="Project employee not found")
    changes = body.model_dump(exclude_unset=True)
    deactivating = pe.is_active and changes.get("is_active") is False
    rate_changed = "billing_rate" in changes or "billing_unit" in changes
    for field, value in changes.items():
        setattr(pe, field, value)
    if deactivating:
        history = open_history_row(db, project_id, pe.employee_id)
        if history:
            history.end_date = date.today()
    if rate_changed:
        clear_other_current_rates(db, pe.id)
        db.add(ProjectEmployeeRate(
            project_employee_id=pe.id,
            effective_from=pe.billing_date or date.today(),
            rate=pe.billing_rate,
            billing_unit=pe.billing_unit,
            is_current_rate=True,
        ))
    db.commit()
    db.refresh(pe)
    return envelope(data=project_employee_out(pe), message="Project employee updated")


# ---------------------------------------------------------------- timesheets

@router.get("/{project_id}/timesheets")
def project_timesheets(
    project_id: int,
    month: int | None = None,
    year: int | None = None,
    status: str | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("timesheets", "HR", "Finance", "Sales_Head")),
):
    get_project_or_404(db, project_id)
    stmt = select(Timesheet).where(Timesheet.project_id == project_id).order_by(Timesheet.id.desc())
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


# ---------------------------------------------------------------- communication matrix

@router.get("/{project_id}/communication-matrix")
def list_communication_matrix(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    get_project_or_404(db, project_id)
    rows = db.execute(
        select(ProjectCommunicationMatrix)
        .where(ProjectCommunicationMatrix.project_id == project_id)
        .order_by(ProjectCommunicationMatrix.id)
    ).scalars().all()
    return envelope(data=[comm_entry_out(e) for e in rows])


@router.post("/{project_id}/communication-matrix")
def add_communication_entry(
    project_id: int,
    body: CommMatrixIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    get_project_or_404(db, project_id)
    entry = ProjectCommunicationMatrix(
        project_id=project_id,
        name=body.name,
        role=body.role,
        responsible_person=body.responsible_person,
        email=body.email,
        phone=body.phone,
        type=body.type,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return envelope(data=comm_entry_out(entry), message="Communication matrix entry added")


@router.delete("/{project_id}/communication-matrix/{entry_id}")
def delete_communication_entry(
    project_id: int,
    entry_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    get_project_or_404(db, project_id)
    entry = db.get(ProjectCommunicationMatrix, entry_id)
    if not entry or entry.project_id != project_id:
        raise HTTPException(status_code=404, detail="Communication matrix entry not found")
    db.delete(entry)
    db.commit()
    return envelope(message="Communication matrix entry deleted")


@router.get("/employees/{pe_id}/invoices")
def get_project_employee_invoices(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_pe),
):
    """Read-only invoice preview per timesheet period for one Project Employee
    mapping (billable days × rate / split rates, PO drawdown, linked invoice)."""
    from services.project_employees import invoice_rollups_for_pe
    pe = get_pe_or_404(db, pe_id)
    return envelope(data=invoice_rollups_for_pe(db, pe))
