"""Projects API: CRUD, team (project employees), timesheets view, communication matrix."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_read, gated_write, get_crm_db, page_params,
)
from models import (
    BillingFrequency, Customer, CustomerLeavePolicy, Employee, LeavePolicyType, Project,
    ProjectCommunicationMatrix, ProjectEmployee, ProjectEmployeeLeaveDetail,
    ProjectEmployeeRate, ProjectLeavePolicy, ProjectStatus, Timesheet, TimesheetStatus,
)
from schemas.common import envelope
from schemas.projects import (
    CommMatrixIn, ProjectCreate, ProjectEmployeeIn, ProjectEmployeeLeaveDetailUpdate,
    ProjectEmployeeRateIn, ProjectEmployeeRateUpdate, ProjectEmployeeUpdate,
    ProjectLeavePolicyCreate, ProjectLeavePolicyUpdate, ProjectUpdate,
)
from services.crm_common import paginate
from services.project_employees import (
    clear_other_current_rates, enrich_pe_list_row, ensure_initial_rate, exit_project_employee,
    get_pe_or_404, group_pe_rows_by_employee, leave_detail_out, project_employee_detail_out,
    rate_out, seed_leave_details_from_customer_policy, sync_pe_billing_from_current_rate,
    sync_pe_leave_from_customer_policy,
)
from services.projects import (
    add_history_entry, comm_entry_out, get_project_or_404, open_history_row,
    project_detail_out, project_employee_out, project_history, project_leave_policy_out,
    project_out, validate_project_refs,
)
from services.timesheets import timesheet_out

router = APIRouter(prefix="/api/projects", tags=["CRM: Projects"])

read_projects = gated_read("projects")
write_projects = gated_write("projects", "Sales_Head", "Finance")
read_pe = gated_read("project-employees")
write_pe = gated_write("project-employees", "Sales_Head", "HR", "Finance")


def _get_leave_policy_or_404(db: Session, policy_id: int) -> ProjectLeavePolicy:
    policy = db.get(ProjectLeavePolicy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Leave policy not found")
    return policy


def _apply_leave_policy_update(
    db: Session, policy: ProjectLeavePolicy, body: ProjectLeavePolicyUpdate,
) -> ProjectLeavePolicy:
    changes = body.model_dump(exclude_unset=True)
    if "leave_type_id" in changes and changes["leave_type_id"] is not None:
        if db.get(LeavePolicyType, changes["leave_type_id"]) is None:
            raise HTTPException(status_code=400, detail="Leave policy type not found")
        other = db.execute(
            select(ProjectLeavePolicy).where(
                ProjectLeavePolicy.project_id == policy.project_id,
                ProjectLeavePolicy.leave_type_id == changes["leave_type_id"],
                ProjectLeavePolicy.id != policy.id,
            ).limit(1)
        ).scalars().first()
        if other is not None:
            raise HTTPException(
                status_code=409,
                detail="A leave policy for this project/leave type already exists",
            )
    for field, value in changes.items():
        setattr(policy, field, value)
    db.commit()
    db.refresh(policy)
    return policy


# Flat leave-policy routes MUST be registered before /{project_id} so FastAPI
# does not try to coerce "leave-policies" into an int project_id.
@router.put("/leave-policies/{policy_id}")
def update_project_leave_policy_by_id(
    policy_id: int,
    body: ProjectLeavePolicyUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    policy = _get_leave_policy_or_404(db, policy_id)
    policy = _apply_leave_policy_update(db, policy, body)
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy updated")


@router.delete("/leave-policies/{policy_id}")
def delete_project_leave_policy_by_id(
    policy_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    policy = _get_leave_policy_or_404(db, policy_id)
    policy.is_active = False
    db.commit()
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy deactivated")


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
    # Resolve explicit branch: client branch_id (wizard) → opportunity.branch_id.
    from models import CustomerBranch, Opportunity
    resolved_branch_id = body.branch_id
    if resolved_branch_id is not None:
        branch_row = db.get(CustomerBranch, resolved_branch_id)
        if branch_row is None or branch_row.customer_id != body.customer_id:
            raise HTTPException(status_code=400, detail="Branch not found for this customer")
    else:
        opp = db.get(Opportunity, body.opportunity_id)
        if opp is not None and opp.branch_id is not None:
            resolved_branch_id = opp.branch_id
    # Apply only fields the client sent so unset policy columns can seed from branch.
    provided = body.model_fields_set
    project = Project(
        opportunity_id=body.opportunity_id,
        customer_id=body.customer_id,
        branch_id=resolved_branch_id,
        name=body.name,
        billing_cycle_start_day=body.billing_cycle_start_day,
        billing_cycle_end_day=body.billing_cycle_end_day,
        billing_frequency=body.billing_frequency,
        recurring_billing=body.recurring_billing,
        max_billable_hours_day=body.max_billable_hours_day,
        max_billable_hours_month=body.max_billable_hours_month,
        max_billable_days_month=body.max_billable_days_month,
        no_billing_period_days=body.no_billing_period_days,
        status=body.status,
    )
    for attr in (
        "holidays_billable", "weekoff_billable", "leave_billable", "comp_off_billable",
        "hours_required_half_day", "hours_required_full_day",
        "hours_required_half_day_comp_off", "hours_required_full_day_comp_off",
        "working_hours_per_day",
        "is_max_billable_hours_per_day", "is_max_billable_hours_per_month",
        "is_max_billable_days_per_month", "is_initial_no_billing_period",
        "initial_no_billing_qty", "initial_no_billing_period",
    ):
        if attr in provided:
            setattr(project, attr, getattr(body, attr))
    db.add(project)
    # Seed unset billing props from the branch this project bills against
    # (project.branch_id → opportunity.branch_id). Only fills fields the client left unset;
    # never overwrites a provided value. Fields the branch doesn't set are left as-is.
    from services.timesheets import _project_branch
    branch = _project_branch(db, project)
    if branch is not None:
        provided = body.model_fields_set
        # (Project column, CustomerBranch column) — only pairs where both exist.
        for proj_attr, branch_attr in (
            ("billing_cycle_start_day", "billing_cycle_start_day"),
            ("billing_cycle_end_day", "billing_cycle_end_day"),
            ("max_billable_hours_day", "max_billable_hours_per_day"),
            ("max_billable_hours_month", "max_billable_hours_per_month"),
            ("max_billable_days_month", "max_billable_days_per_month"),
            ("no_billing_period_days", "initial_no_billing_qty"),
            ("holidays_billable", "holidays_billable"),
            ("weekoff_billable", "weekoff_billable"),
            ("leave_billable", "leave_billable"),
            ("comp_off_billable", "comp_off_billable"),
            ("is_max_billable_hours_per_day", "is_max_billable_hours_per_day"),
            ("is_max_billable_hours_per_month", "is_max_billable_hours_per_month"),
            ("is_max_billable_days_per_month", "is_max_billable_days_per_month"),
            ("hours_required_half_day", "hours_required_half_day"),
            ("hours_required_full_day", "hours_required_full_day"),
            ("hours_required_half_day_comp_off", "hours_required_half_day_comp_off"),
            ("hours_required_full_day_comp_off", "hours_required_full_day_comp_off"),
            ("working_hours_per_day", "working_hours_per_day"),
            ("is_initial_no_billing_period", "is_initial_no_billing_period"),
            ("initial_no_billing_qty", "initial_no_billing_qty"),
            ("initial_no_billing_period", "initial_no_billing_period"),
        ):
            if proj_attr in provided:
                continue
            val = getattr(branch, branch_attr, None)
            if val is not None:
                setattr(project, proj_attr, val)
        # billing_frequency is free-text on the branch; map to the project enum.
        # Accept both Bi_Weekly (canonical) and Bi-Weekly (UI label) from branch.
        if "billing_frequency" not in provided and branch.billing_frequency:
            raw = str(branch.billing_frequency).strip().replace("-", "_")
            try:
                project.billing_frequency = BillingFrequency(raw)
            except ValueError:
                # try title-case Monthly/Weekly variants already matching enum values
                try:
                    project.billing_frequency = BillingFrequency(branch.billing_frequency)
                except ValueError:
                    pass
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
        # Spec: role_title from PE.role_title, else employee designation name,
        # else employee.role_title (already folded into project_employee_out).
        if not d.get("role_title") and emp is not None and emp.designation_id:
            from models.masters import Designation
            desig = db.get(Designation, emp.designation_id)
            if desig and desig.name:
                d["role_title"] = desig.name
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


@router.post("/employees/{pe_id}/leave/sync")
def sync_project_employee_leave_policies(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("project-employees", "Admin", "HR")),
):
    """Back-fill missing PE leave types from customer/branch policy (idempotent).

    Existing balances/consumed are never modified — only new leave types are added.
    """
    pe = get_pe_or_404(db, pe_id)
    result = sync_pe_leave_from_customer_policy(db, pe)
    db.commit()
    detail = project_employee_detail_out(db, pe)
    added_names = [a.get("leave_type_name") or f"#{a.get('leave_type_id')}" for a in result["added"]]
    msg = (
        f"Added {result['added_count']} leave type(s): {', '.join(added_names)}"
        if result["added_count"]
        else "Leave policies already in sync — nothing to add"
    )
    return envelope(
        data={
            **result,
            "leave_details": detail.get("leave_details"),
            "leave_summary": detail.get("leave_summary"),
            "leave_eligibility": detail.get("leave_eligibility"),
            "leave_balance_total": detail.get("leave_balance_total"),
        },
        message=msg,
    )


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


@router.delete("/employees/{pe_id}")
def delete_project_employee(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    """Hard-delete a PE mapping.

    Cascades draft/non-approved timesheets and deletable invoices (+ unpaid TDS)
    linked to this assignment. Blocks when approved timesheets remain after
    invoice cascade fails, or when invoices have payments / credit notes
    (message lists invoice numbers).
    """
    from models import LeaveApplication
    from services.crm_common import commit_or_conflict
    from services.crm_delete import (
        cascade_delete_invoices,
        cascade_pe_draft_timesheets,
        invoices_for_timesheet_ids,
        pe_timesheet_query,
        purge_timesheet,
    )

    pe = get_pe_or_404(db, pe_id)
    ts_rows = list(db.execute(pe_timesheet_query(pe_id, pe.project_id, pe.employee_id)).scalars().all())
    ts_ids = [t.id for t in ts_rows]
    linked_invoices = invoices_for_timesheet_ids(db, ts_ids)
    if linked_invoices:
        # Cascade unpaid invoices; payments / credit notes raise 409 with numbers.
        cascade_delete_invoices(db, linked_invoices)
        db.flush()
        # Refresh timesheet list after invoice removal.
        ts_rows = list(db.execute(pe_timesheet_query(pe_id, pe.project_id, pe.employee_id)).scalars().all())

    # Cascade draft / rejected sheets; then purge remaining approved (invoices gone).
    cascade_pe_draft_timesheets(db, pe_id, pe.project_id, pe.employee_id)
    remaining = list(db.execute(pe_timesheet_query(pe_id, pe.project_id, pe.employee_id)).scalars().all())
    approved_left = [t for t in remaining if (
        t.status.value if hasattr(t.status, "value") else str(t.status)
    ) == TimesheetStatus.APPROVED.value]
    # After invoices cascaded, approved sheets are safe to purge for cleanup.
    for ts in approved_left:
        purge_timesheet(db, ts)

    approved_leave = db.execute(
        select(func.count()).select_from(LeaveApplication).where(
            LeaveApplication.project_employee_id == pe_id,
            LeaveApplication.status == "Approved",
        )
    ).scalar() or 0
    if approved_leave:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete: {approved_leave} approved leave application(s) exist. "
                "Cancel or remove them first."
            ),
        )
    for app in db.execute(
        select(LeaveApplication).where(LeaveApplication.project_employee_id == pe_id)
    ).scalars().all():
        app.project_employee_id = None
    db.delete(pe)
    commit_or_conflict(
        db,
        "Cannot delete: project employee is still referenced by other records.",
    )
    return envelope(data={"id": pe_id}, message="Project employee deleted")


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
    if "branch_id" in changes and changes["branch_id"] is not None:
        from models import CustomerBranch
        cust_id = changes.get("customer_id", project.customer_id)
        branch_row = db.get(CustomerBranch, changes["branch_id"])
        if branch_row is None or branch_row.customer_id != cust_id:
            raise HTTPException(status_code=400, detail="Branch not found for this customer")
    # Guard hour threshold ordering when both (or one + existing) are present.
    half = changes.get("hours_required_half_day", project.hours_required_half_day)
    full = changes.get("hours_required_full_day", project.hours_required_full_day)
    if half is not None and full is not None and Decimal(str(half)) > Decimal(str(full)):
        raise HTTPException(
            status_code=400,
            detail="hours_required_half_day cannot exceed hours_required_full_day",
        )
    for field, value in changes.items():
        setattr(project, field, value)
    # If opportunity changed and branch_id was not explicitly set, refresh from opp.
    if "opportunity_id" in changes and "branch_id" not in changes:
        from models import Opportunity
        opp = db.get(Opportunity, project.opportunity_id)
        if opp is not None and opp.branch_id is not None:
            project.branch_id = opp.branch_id
    db.commit()
    db.refresh(project)
    return envelope(data=project_out(project), message="Project updated")


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    """Hard-delete a project.

    Cascades team assignments, timesheets, and deletable invoices (+ unpaid TDS).
    Approved leave apps still block. Invoices with payments / credit notes 409
    with invoice numbers.
    """
    from models import (
        EmployeeProjectHistory, LeaveApplication, POProjectAllocation, TimesheetEntry,
    )
    from services.crm_common import commit_or_conflict
    from services.crm_delete import (
        cascade_delete_invoices,
        cascade_project_timesheets,
        invoices_for_project,
        purge_timesheet,
    )

    project = get_project_or_404(db, project_id)
    approved_leave = db.execute(
        select(func.count()).select_from(LeaveApplication).where(
            LeaveApplication.project_id == project_id,
            LeaveApplication.status == "Approved",
        )
    ).scalar() or 0
    if approved_leave:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete: {approved_leave} approved leave application(s) exist. "
                "Remove them first."
            ),
        )

    # Cascade deletable invoices first (payments / CNs block with numbers).
    invs = invoices_for_project(db, project_id)
    if invs:
        cascade_delete_invoices(db, invs)
        db.flush()

    # Draft sheets, then any remaining (approved) after invoices are gone.
    cascade_project_timesheets(db, project_id, include_approved=False)
    for ts in db.execute(
        select(Timesheet).where(Timesheet.project_id == project_id)
    ).scalars().all():
        purge_timesheet(db, ts)

    # HR project history has no cascade — drop with the project.
    for hist in db.execute(
        select(EmployeeProjectHistory).where(EmployeeProjectHistory.project_id == project_id)
    ).scalars().all():
        db.delete(hist)
    # Soft-clear leave apps that still point at this project (non-Approved only).
    for app in db.execute(
        select(LeaveApplication).where(LeaveApplication.project_id == project_id)
    ).scalars().all():
        app.project_id = None
        app.project_employee_id = None
    # Split-billing entries on *other* timesheets may still reference this project.
    for entry in db.execute(
        select(TimesheetEntry).where(TimesheetEntry.entry_project_id == project_id)
    ).scalars().all():
        entry.entry_project_id = None
    # Purge all PE rows (active + inactive) after finance/timesheet cascade.
    for pe in db.execute(
        select(ProjectEmployee).where(ProjectEmployee.project_id == project_id)
    ).scalars().all():
        for app in db.execute(
            select(LeaveApplication).where(LeaveApplication.project_employee_id == pe.id)
        ).scalars().all():
            app.project_employee_id = None
        db.delete(pe)
    # Drop PO allocations that only reference this project (non-financial orphan).
    allocs = db.execute(
        select(POProjectAllocation).where(POProjectAllocation.project_id == project_id)
    ).scalars().all()
    for a in allocs:
        db.delete(a)
    db.delete(project)
    commit_or_conflict(
        db,
        "Cannot delete: project is still referenced by other records. Remove dependencies first.",
    )
    return envelope(data={"id": project_id}, message="Project deleted")


# ---------------------------------------------------------------- leave policies (project-scoped)

@router.get("/{project_id}/leave-policies")
def list_project_leave_policies(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    get_project_or_404(db, project_id)
    pols = db.execute(
        select(ProjectLeavePolicy)
        .where(ProjectLeavePolicy.project_id == project_id,
               ProjectLeavePolicy.is_active.is_(True))
        .order_by(ProjectLeavePolicy.id)
    ).scalars().all()
    return envelope(
        data=[project_leave_policy_out(db, p) for p in pols],
        message="Project leave policies",
    )


@router.post("/{project_id}/leave-policies")
def create_project_leave_policy(
    project_id: int,
    body: ProjectLeavePolicyCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    get_project_or_404(db, project_id)
    if db.get(LeavePolicyType, body.leave_type_id) is None:
        raise HTTPException(status_code=400, detail="Leave policy type not found")
    dup = db.execute(
        select(ProjectLeavePolicy).where(
            ProjectLeavePolicy.project_id == project_id,
            ProjectLeavePolicy.leave_type_id == body.leave_type_id,
        ).limit(1)
    ).scalars().first()
    if dup is not None:
        if not dup.is_active:
            # Reactivate soft-deleted row with new values
            for field, value in body.model_dump().items():
                setattr(dup, field, value)
            dup.is_active = True
            db.commit()
            db.refresh(dup)
            return envelope(data=project_leave_policy_out(db, dup),
                            message="Leave policy reactivated")
        raise HTTPException(
            status_code=409,
            detail="A leave policy for this project/leave type already exists",
        )
    policy = ProjectLeavePolicy(project_id=project_id, **body.model_dump())
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy created")


@router.put("/{project_id}/leave-policies/{policy_id}")
def update_project_leave_policy(
    project_id: int,
    policy_id: int,
    body: ProjectLeavePolicyUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    """Nested alias — prefer PUT /api/projects/leave-policies/{id}."""
    get_project_or_404(db, project_id)
    policy = db.get(ProjectLeavePolicy, policy_id)
    if policy is None or policy.project_id != project_id:
        raise HTTPException(status_code=404, detail="Leave policy not found for this project")
    policy = _apply_leave_policy_update(db, policy, body)
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy updated")


@router.delete("/{project_id}/leave-policies/{policy_id}")
def delete_project_leave_policy(
    project_id: int,
    policy_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    """Nested alias — prefer DELETE /api/projects/leave-policies/{id}."""
    get_project_or_404(db, project_id)
    policy = db.get(ProjectLeavePolicy, policy_id)
    if policy is None or policy.project_id != project_id:
        raise HTTPException(status_code=404, detail="Leave policy not found for this project")
    policy.is_active = False
    db.commit()
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy deactivated")


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
