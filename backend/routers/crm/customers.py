"""Customer master API: CRUD + branches, billing policy, documents, contact persons.

Writes: Sales / Sales_Head (Admin implicit). Reads: any CRM role.
No activity-log table exists for customers, so mutations are not logged here.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, gated_read, gated_write, get_crm_db, page_params
from models import (
    ContactPerson,
    Customer,
    CustomerBillingPolicy,
    CustomerBranch,
    CustomerDocument,
    CustomerStatus,
)
from schemas.common import envelope
from schemas.customers import (
    BillingPolicyIn,
    BranchBillingPolicyIn,
    BranchCreate,
    BranchUpdate,
    ContactCreate,
    ContactUpdate,
    CustomerCreate,
    CustomerUpdate,
)
from schemas.leave import BranchHolidayCreate, BranchHolidayUpdate
from services.crm_common import paginate, save_upload
from services.customers import (
    clear_other_primaries,
    ensure_unique_name,
    get_branch_or_404,
    get_contact_or_404,
    get_customer_or_404,
    get_document_or_404,
    serialize_branch,
    serialize_branch_policy,
    serialize_contact,
    serialize_customer,
    serialize_documents,
    serialize_policy,
    validate_document_type,
)

router = APIRouter(prefix="/api/customers", tags=["CRM: Customers"])

read_customers = gated_read("customers")
write_customers = gated_write("customers", "Sales", "Sales_Head")
read_branch_policy = gated_read("branch-policy")
write_branch_policy = gated_write("branch-policy", "Sales", "Sales_Head", "HR")

_SORTABLE = {"name": Customer.name, "status": Customer.status,
             "created_at": Customer.created_at, "id": Customer.id}


# ---------------------------------------------------------------------------
# Customers CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_customers(
    status: str | None = None,
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    stmt = select(Customer)
    if status:
        valid = {s.value for s in CustomerStatus}
        if status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"Invalid status. Allowed: {', '.join(sorted(valid))}")
        stmt = stmt.where(Customer.status == status)
    if p.search:
        stmt = stmt.where(Customer.name.ilike(f"%{p.search}%"))
    order_col = _SORTABLE.get(p.sort_by or "", Customer.id)
    stmt = stmt.order_by(order_col.asc() if p.sort_dir == "asc" else order_col.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    return envelope(data=[serialize_customer(c, db) for c in items], message="Customers fetched", meta=meta)


@router.post("")
def create_customer(
    payload: CustomerCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Customer name is required")
    ensure_unique_name(db, name)
    customer = Customer(
        name=name, legal_entity_name=payload.legal_entity_name,
        # A brand-new customer has no PO yet, so Customer Type is always NN.
        customer_type="NN", status=payload.status,
        address_line_1=payload.address_line_1, address_line_2=payload.address_line_2,
        city=payload.city, state=payload.state, pincode=payload.pincode, country=payload.country,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return envelope(data=serialize_customer(customer, db, detail=True), message="Customer created")


@router.get("/all-branches")
def list_all_branches(
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    """Global list of every branch across all customers (Customer Branches page).

    Defined BEFORE /{customer_id} so the literal path wins. Supports search on
    branch name / customer name / GSTIN and returns the parent customer's name.
    """
    base = (
        select(CustomerBranch, Customer.name.label("customer_name"))
        .join(Customer, Customer.id == CustomerBranch.customer_id)
    )
    if p.search:
        like = f"%{p.search.lower()}%"
        base = base.where(
            or_(
                func.lower(CustomerBranch.branch_name).like(like),
                func.lower(Customer.name).like(like),
                func.lower(func.coalesce(CustomerBranch.gstin, "")).like(like),
                func.lower(func.coalesce(CustomerBranch.branch_legal_name, "")).like(like),
            )
        )
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    rows = db.execute(
        base.order_by(Customer.name.asc(), CustomerBranch.branch_name.asc())
        .limit(p.limit).offset(p.offset)
    ).all()
    data = []
    for br, customer_name in rows:
        d = serialize_branch(br)
        d["customer_name"] = customer_name
        data.append(d)
    pages = (total + p.limit - 1) // p.limit if p.limit else 1
    meta = {"page": p.page, "limit": p.limit, "total": total, "pages": pages}
    return envelope(data=data, message="Customer branches", meta=meta)


@router.get("/{customer_id}")
def get_customer(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    return envelope(data=serialize_customer(customer, db, detail=True), message="Customer fetched")


@router.put("/{customer_id}")
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    customer = get_customer_or_404(db, customer_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is not None:
        new_name = changes["name"].strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="Customer name cannot be empty")
        ensure_unique_name(db, new_name, exclude_id=customer.id)
        customer.name = new_name
    if "legal_entity_name" in changes:
        customer.legal_entity_name = changes["legal_entity_name"]
    if "status" in changes and changes["status"] is not None:
        customer.status = changes["status"]
    # Customer Type rule (server-enforced, never trust the client):
    #   no PO under the customer  -> always NN
    #   has at least one PO        -> EN or EE (default EN)
    from models import PurchaseOrder
    has_po = bool(db.query(PurchaseOrder.id).filter(PurchaseOrder.customer_id == customer.id).first())
    requested_type = changes.get("customer_type")
    if not has_po:
        customer.customer_type = "NN"
    elif requested_type in ("EN", "EE"):
        customer.customer_type = requested_type
    elif customer.customer_type not in ("EN", "EE"):
        customer.customer_type = "EN"
    db.commit()
    db.refresh(customer)
    return envelope(data=serialize_customer(customer, db, detail=True), message="Customer updated")


@router.delete("/{customer_id}")
def delete_customer(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    customer = get_customer_or_404(db, customer_id)
    db.delete(customer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Customer is referenced by other records (opportunities/projects) and cannot be deleted",
        )
    return envelope(data={"id": customer_id}, message="Customer deleted")


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/branches")
def list_branches(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    return envelope(data=[serialize_branch(b) for b in customer.branches], message="Branches fetched")


@router.post("/{customer_id}/branches")
def create_branch(
    customer_id: int,
    payload: BranchCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    get_customer_or_404(db, customer_id)
    branch = CustomerBranch(customer_id=customer_id, **payload.model_dump())
    db.add(branch)
    db.flush()
    if branch.is_primary:
        clear_other_primaries(db, customer_id, keep_branch_id=branch.id)
    db.commit()
    db.refresh(branch)
    return envelope(data=serialize_branch(branch), message="Branch created")


@router.put("/{customer_id}/branches/{branch_id}")
def update_branch(
    customer_id: int,
    branch_id: int,
    payload: BranchUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    branch = get_branch_or_404(db, customer_id, branch_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if field == "is_primary":
            continue
        setattr(branch, field, value)
    if changes.get("is_primary") is True:
        branch.is_primary = True
        clear_other_primaries(db, customer_id, keep_branch_id=branch.id)
    elif changes.get("is_primary") is False:
        branch.is_primary = False
    db.commit()
    db.refresh(branch)
    return envelope(data=serialize_branch(branch), message="Branch updated")


@router.delete("/{customer_id}/branches/{branch_id}")
def delete_branch(
    customer_id: int,
    branch_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    branch = get_branch_or_404(db, customer_id, branch_id)
    db.delete(branch)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Branch is referenced by other records (contacts/opportunities) and cannot be deleted",
        )
    return envelope(data={"id": branch_id}, message="Branch deleted")


# ---------------------------------------------------------------------------
# Branch-level billing policy (branch-wise billing; NULL fields inherit the
# customer-level default policy field-by-field)
# ---------------------------------------------------------------------------

# Booleans that stay NOT NULL in the DB — clearing them just means "off".
_BRANCH_POLICY_NON_NULL_BOOLS = frozenset({
    "is_max_billable_hours_per_day", "is_max_billable_hours_per_month",
    "is_max_billable_days_per_month", "is_initial_no_billing_period",
})


@router.get("/{customer_id}/branches/{branch_id}/billing-policy")
def get_branch_billing_policy(
    customer_id: int,
    branch_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    branch = get_branch_or_404(db, customer_id, branch_id)
    return envelope(data=serialize_branch_policy(branch), message="Branch billing policy fetched")


@router.put("/{customer_id}/branches/{branch_id}/billing-policy")
def update_branch_billing_policy(
    customer_id: int,
    branch_id: int,
    payload: BranchBillingPolicyIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    """Update the branch's own billing-policy fields.

    Partial update: only fields present in the payload change. An explicit null
    clears the field so the branch inherits the customer default for it.
    """
    branch = get_branch_or_404(db, customer_id, branch_id)
    changes = payload.model_dump(exclude_unset=True)

    # Validate half/full-day hours against the values the branch will end up with.
    half = changes.get("hours_required_half_day", branch.hours_required_half_day)
    full = changes.get("hours_required_full_day", branch.hours_required_full_day)
    if half is not None and full is not None and float(half) > float(full):
        raise HTTPException(status_code=400,
                            detail="hours_required_half_day cannot exceed hours_required_full_day")

    for field, value in changes.items():
        if field in _BRANCH_POLICY_NON_NULL_BOOLS and value is None:
            value = False
        setattr(branch, field, value)
    db.commit()
    db.refresh(branch)
    return envelope(data=serialize_branch_policy(branch), message="Branch billing policy updated")


# ---------------------------------------------------------------------------
# Billing policy (single row per customer — upsert; acts as the DEFAULT policy
# that branches without their own values inherit)
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/billing-policy")
def get_billing_policy(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    policy = customer.billing_policy
    message = "Billing policy fetched" if policy else "No billing policy set for this customer"
    return envelope(data=serialize_policy(policy), message=message)


@router.put("/{customer_id}/billing-policy")
def upsert_billing_policy(
    customer_id: int,
    payload: BillingPolicyIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    customer = get_customer_or_404(db, customer_id)
    if payload.min_hours_half_day > payload.min_hours_full_day:
        raise HTTPException(status_code=400,
                            detail="min_hours_half_day cannot exceed min_hours_full_day")
    policy = customer.billing_policy
    created = policy is None
    if created:
        policy = CustomerBillingPolicy(customer_id=customer_id)
        db.add(policy)
    policy.week_off_billable = payload.week_off_billable
    policy.leave_billable = payload.leave_billable
    policy.holidays_billable = payload.holidays_billable
    policy.min_hours_full_day = payload.min_hours_full_day
    policy.min_hours_half_day = payload.min_hours_half_day
    policy.comp_off_billable = payload.comp_off_billable
    policy.comp_off_balance = payload.comp_off_balance
    policy.comp_off_balance_initial = payload.comp_off_balance_initial
    policy.comp_off_max_limit = payload.comp_off_max_limit
    policy.comp_off_max_carry_forward = payload.comp_off_max_carry_forward
    policy.normal_hours_per_day = payload.normal_hours_per_day
    policy.user_role = payload.user_role
    policy.operation = payload.operation
    db.commit()
    db.refresh(policy)
    return envelope(data=serialize_policy(policy),
                    message="Billing policy created" if created else "Billing policy updated")


# ---------------------------------------------------------------------------
# Documents (multipart upload)
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/documents")
def list_documents(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    return envelope(data=serialize_documents(db, list(customer.documents)), message="Documents fetched")


@router.post("/{customer_id}/documents")
def upload_document(
    customer_id: int,
    file: UploadFile = File(...),
    document_type_id: int = Form(...),
    start_date: date | None = Form(None),
    end_date: date | None = Form(None),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    get_customer_or_404(db, customer_id)
    validate_document_type(db, document_type_id)
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date cannot be before start_date")
    file_url = save_upload(file, "customer_docs")
    doc = CustomerDocument(customer_id=customer_id, document_type_id=document_type_id,
                           file_url=file_url, start_date=start_date, end_date=end_date)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return envelope(data=serialize_documents(db, [doc])[0], message="Document uploaded")


@router.delete("/{customer_id}/documents/{doc_id}")
def delete_document(
    customer_id: int,
    doc_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    doc = get_document_or_404(db, customer_id, doc_id)
    db.delete(doc)
    db.commit()
    return envelope(data={"id": doc_id}, message="Document deleted")


# ---------------------------------------------------------------------------
# Contact persons
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/contacts")
def list_contacts(
    customer_id: int,
    branch_id: int | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    rows = list(customer.contacts)
    if branch_id is not None:
        get_branch_or_404(db, customer_id, branch_id)
        rows = [c for c in rows if c.branch_id == branch_id]
    return envelope(data=[serialize_contact(c) for c in rows], message="Contacts fetched")


@router.post("/{customer_id}/contacts")
def create_contact(
    customer_id: int,
    payload: ContactCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    get_customer_or_404(db, customer_id)
    if payload.branch_id is not None:
        get_branch_or_404(db, customer_id, payload.branch_id)
    contact = ContactPerson(customer_id=customer_id, **payload.model_dump())
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return envelope(data=serialize_contact(contact), message="Contact created")


@router.put("/{customer_id}/contacts/{contact_id}")
def update_contact(
    customer_id: int,
    contact_id: int,
    payload: ContactUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    contact = get_contact_or_404(db, customer_id, contact_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("branch_id") is not None:
        get_branch_or_404(db, customer_id, changes["branch_id"])
    for field, value in changes.items():
        setattr(contact, field, value)
    db.commit()
    db.refresh(contact)
    return envelope(data=serialize_contact(contact), message="Contact updated")


@router.delete("/{customer_id}/contacts/{contact_id}")
def delete_contact(
    customer_id: int,
    contact_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    contact = get_contact_or_404(db, customer_id, contact_id)
    db.delete(contact)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Contact is referenced by other records (opportunities) and cannot be deleted",
        )
    return envelope(data={"id": contact_id}, message="Contact deleted")


# ==========================================================================
# Customer Branch-wise Leave & Holiday Policy — §2 holiday-year header +
# one-call branch policy aggregation. (Re-applied after external corruption.)
# ==========================================================================
from pydantic import BaseModel as _BaseModel  # noqa: E402


class _HolidayYearIn(_BaseModel):
    calendar_year: int
    is_freeze: bool = False


class _HolidayYearPatch(_BaseModel):
    is_freeze: bool


def _branch_or_404(db: Session, branch_id: int) -> CustomerBranch:
    b = db.get(CustomerBranch, branch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Branch not found")
    return b


def _branch_leave_policy_out(db: Session, p) -> dict:
    from models import LeavePolicyType
    lt = db.get(LeavePolicyType, p.leave_type_id)
    def _n(v):
        return float(v) if v is not None else None
    return {
        "id": p.id, "leave_type_id": p.leave_type_id, "leave_name": lt.name if lt else None,
        "leave_credit_type": p.leave_credit_type, "leave_expire": p.leave_expire,
        "is_max_limit": bool(p.is_max_limit), "prorate_balance_credit": bool(p.prorate_balance_credit),
        "leave_credit_balance": _n(p.leave_credit_balance), "initial_credit_balance": _n(p.initial_credit_balance),
        "maximum_carry_forward": _n(p.maximum_carry_forward), "leave_credit_timing": p.leave_credit_timing,
        "effective_date": p.effective_date.isoformat() if p.effective_date else None,
    }


@router.get("/branches/{branch_id}/policy")
def get_branch_policy_detail(branch_id: int, db: Session = Depends(get_crm_db),
                             user: CurrentUser = Depends(read_branch_policy)):
    from models import CustomerLeavePolicy, Opportunity, Project
    from services.branch_policy import branch_holiday_years
    branch = _branch_or_404(db, branch_id)
    data = serialize_branch(branch)
    data["holiday_years"] = branch_holiday_years(db, branch_id)
    pols = db.execute(select(CustomerLeavePolicy).where(CustomerLeavePolicy.branch_id == branch_id)
                      .order_by(CustomerLeavePolicy.id)).scalars().all()
    data["leave_policies"] = [_branch_leave_policy_out(db, p) for p in pols]
    projs = db.execute(select(Project).join(Opportunity, Opportunity.id == Project.opportunity_id)
                       .where(Opportunity.branch_id == branch_id).order_by(Project.name)).scalars().all()
    data["linked_projects"] = [{"id": p.id, "name": p.name,
                                "status": getattr(p.status, "value", p.status)} for p in projs]
    return envelope(data=data, message="Branch policy")


@router.get("/branches/{branch_id}/holiday-years")
def list_branch_holiday_years(branch_id: int, db: Session = Depends(get_crm_db),
                              user: CurrentUser = Depends(read_branch_policy)):
    from services.branch_policy import branch_holiday_years
    _branch_or_404(db, branch_id)
    return envelope(data=branch_holiday_years(db, branch_id), message="Holiday years")


@router.post("/branches/{branch_id}/holiday-years")
def create_branch_holiday_year(branch_id: int, body: _HolidayYearIn,
                               db: Session = Depends(get_crm_db),
                               user: CurrentUser = Depends(write_branch_policy)):
    from models import BranchHolidayYear
    from services.branch_policy import branch_holiday_years
    _branch_or_404(db, branch_id)
    exists = db.execute(select(BranchHolidayYear).where(
        BranchHolidayYear.branch_id == branch_id,
        BranchHolidayYear.calendar_year == body.calendar_year)).scalars().first()
    if exists is None:
        db.add(BranchHolidayYear(branch_id=branch_id, calendar_year=body.calendar_year,
                                 is_freeze=body.is_freeze))
        db.commit()
    return envelope(data=branch_holiday_years(db, branch_id), message="Holiday year added")


@router.patch("/branches/{branch_id}/holiday-years/{year_id}")
def patch_branch_holiday_year(branch_id: int, year_id: int, body: _HolidayYearPatch,
                              db: Session = Depends(get_crm_db),
                              user: CurrentUser = Depends(write_branch_policy)):
    from models import BranchHolidayYear
    from services.branch_policy import branch_holiday_years, ensure_year_editable
    row = db.get(BranchHolidayYear, year_id)
    if row is None or row.branch_id != branch_id:
        raise HTTPException(status_code=404, detail="Holiday year not found")
    row.is_freeze = body.is_freeze
    db.commit()
    return envelope(data=branch_holiday_years(db, branch_id), message="Holiday year updated")


@router.get("/branches/{branch_id}/holiday-years/{calendar_year}/holidays")
def list_branch_year_holidays(
    branch_id: int,
    calendar_year: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_branch_policy),
):
    from services.holidays import branch_holidays_for_year, holiday_out

    _branch_or_404(db, branch_id)
    rows = branch_holidays_for_year(db, branch_id, calendar_year)
    return envelope(data=[holiday_out(h) for h in rows], message="Branch holiday dates")


@router.post("/branches/{branch_id}/holiday-years/{calendar_year}/holidays")
def create_branch_year_holiday(
    branch_id: int,
    calendar_year: int,
    body: BranchHolidayCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from services.holidays import create_branch_holiday, holiday_out

    branch = _branch_or_404(db, branch_id)
    obj = create_branch_holiday(
        db,
        branch,
        calendar_year,
        holiday_name_id=body.holiday_name_id,
        name=body.name,
        holiday_date=body.holiday_date,
        observance=body.observance,
        holiday_type=body.holiday_type,
    )
    return envelope(data=holiday_out(obj), message="Branch holiday added")


@router.put("/branches/{branch_id}/holiday-years/{calendar_year}/holidays/{holiday_id}")
def update_branch_year_holiday(
    branch_id: int,
    calendar_year: int,
    holiday_id: int,
    body: BranchHolidayUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from models import Holiday
    from services.holidays import holiday_out, update_branch_holiday

    branch = _branch_or_404(db, branch_id)
    holiday = db.get(Holiday, holiday_id)
    if holiday is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    obj = update_branch_holiday(db, branch, calendar_year, holiday, body.model_dump(exclude_unset=True))
    return envelope(data=holiday_out(obj), message="Branch holiday updated")


@router.delete("/branches/{branch_id}/holiday-years/{calendar_year}/holidays/{holiday_id}")
def delete_branch_year_holiday(
    branch_id: int,
    calendar_year: int,
    holiday_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from models import Holiday
    from services.holidays import deactivate_branch_holiday, holiday_out

    branch = _branch_or_404(db, branch_id)
    holiday = db.get(Holiday, holiday_id)
    if holiday is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    obj = deactivate_branch_holiday(db, branch, calendar_year, holiday)
    return envelope(data=holiday_out(obj), message="Branch holiday removed")
