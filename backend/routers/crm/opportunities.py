"""Opportunity pipeline API: CRUD, skills, stage transitions, activity log.

Create/Update: Sales / Sales_Head (Admin implicit). Reads: any CRM role —
Sales sees ALL opportunities (the spec restricts requirements, not opportunities).
Moves to Archived: Sales_Head / Admin only. Every mutation writes to
opportunity_activity_log.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, gated_read, gated_write, get_crm_db, page_params
from models import (
    Opportunity,
    OpportunityActivityLog,
    OpportunityApprovalStatus,
    OpportunityCtcSlab,
    OpportunitySkill,
    PipelineStage,
    Priority,
    Requirement,
    RequirementActivityLog,
    RequirementSkill,
    RequirementStatus,
)
from services.opportunity_form_schema import OpportunitySchemaError, strip_details, validate_details
from schemas.common import CommentIn, RejectIn, envelope
from schemas.opportunities import (
    OpportunityCreate,
    OpportunitySkillIn,
    OpportunityUpdate,
    StageTransitionIn,
)
from services.crm_common import log_activity, next_sequence_number, paginate
from services.notify import notify_role, notify_user
from services.opportunity_ctc import derive_ctc_row, normalize_tm_billing_details
from services.opportunities import (
    fetch_activity_log,
    get_opportunity_or_404,
    replace_skills,
    serialize_opportunity,
    serialize_skills,
    validate_refs,
    validate_stage_transition,
)

router = APIRouter(prefix="/api/opportunities", tags=["CRM: Opportunities"])

read_opportunities = gated_read("opportunities")
write_opportunities = gated_write("opportunities", "Sales", "Sales_Head")

_SORTABLE = {"opp_id": Opportunity.opp_id, "title": Opportunity.title,
             "pipeline_stage": Opportunity.pipeline_stage,
             "created_at": Opportunity.created_at, "id": Opportunity.id}


def _now():
    return datetime.now(timezone.utc)


def _spawn_requirement_from_opportunity(db: Session, opp: Opportunity, approver: CurrentUser) -> Requirement:
    """Bridge: an approved opportunity flows into the existing Requirements chain.

    Creates a Requirement linked to the opportunity, already past Sales Head
    approval (the opportunity itself was approved) and sitting in
    Pending_Engineering_Review so it lands in RMG's Engineering Review Queue.
    Skills are copied from the opportunity. Idempotent per opportunity: if a
    requirement already exists for it, nothing is created.
    """
    existing = db.execute(
        select(Requirement.id).where(Requirement.opportunity_id == opp.id)
    ).first()
    if existing:
        return None  # a requirement already exists for this opportunity

    req = Requirement(
        req_number=next_sequence_number(db, Requirement, Requirement.req_number, "REQ"),
        opportunity_id=opp.id,
        customer_id=opp.customer_id,
        title=opp.title,
        no_of_positions=1,
        priority=Priority.MEDIUM,
        status=RequirementStatus.PENDING_ENGINEERING_REVIEW,
        created_by=opp.created_by,
        sales_head_approved_by=approver.id,
        sales_head_approved_at=_now(),
    )
    db.add(req)
    db.flush()
    opp_skills = db.execute(
        select(OpportunitySkill).where(OpportunitySkill.opportunity_id == opp.id)
    ).scalars().all()
    for s in opp_skills:
        db.add(RequirementSkill(requirement_id=req.id, skill_id=s.skill_id, is_mandatory=s.is_mandatory))
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, approver.id,
                 "CREATED",
                 f"Requirement {req.req_number} auto-created from approved opportunity {opp.opp_id}; "
                 f"pending engineering review")
    notify_role(db, "RMG",
                f"Requirement {req.req_number} pending engineering review",
                f"'{req.title}' (from opportunity {opp.opp_id}) needs engineering review.",
                f"/requirements/{req.id}", exclude_user_id=approver.id)
    return req


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_opportunities(
    pipeline_stage: str | None = None,
    approval_status: str | None = None,
    customer_id: int | None = None,
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    stmt = select(Opportunity)
    if pipeline_stage:
        valid = {s.value for s in PipelineStage}
        if pipeline_stage not in valid:
            raise HTTPException(status_code=400,
                                detail=f"Invalid pipeline_stage. Allowed: {', '.join(s.value for s in PipelineStage)}")
        stmt = stmt.where(Opportunity.pipeline_stage == pipeline_stage)
    if approval_status:
        valid_appr = {s.value for s in OpportunityApprovalStatus}
        if approval_status not in valid_appr:
            raise HTTPException(status_code=400,
                                detail=f"Invalid approval_status. Allowed: {', '.join(s.value for s in OpportunityApprovalStatus)}")
        stmt = stmt.where(Opportunity.approval_status == approval_status)
    if customer_id is not None:
        stmt = stmt.where(Opportunity.customer_id == customer_id)
    if p.search:
        like = f"%{p.search}%"
        stmt = stmt.where(or_(Opportunity.title.ilike(like), Opportunity.opp_id.ilike(like)))
    order_col = _SORTABLE.get(p.sort_by or "", Opportunity.id)
    stmt = stmt.order_by(order_col.asc() if p.sort_dir == "asc" else order_col.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    return envelope(data=[serialize_opportunity(db, o) for o in items],
                    message="Opportunities fetched", meta=meta)


def _opp_type_value(opp_type) -> str:
    return opp_type.value if hasattr(opp_type, "value") else str(opp_type)


def _replace_ctc_slab(db: Session, opp: Opportunity, rows: list) -> None:
    details = opp.details or {}
    db.query(OpportunityCtcSlab).filter(OpportunityCtcSlab.opportunity_id == opp.id).delete()
    for idx, row in enumerate(rows or []):
        data = derive_ctc_row(
            row.model_dump() if hasattr(row, "model_dump") else dict(row),
            opportunity_type=_opp_type_value(opp.opp_type),
            details=details,
        )
        db.add(OpportunityCtcSlab(opportunity_id=opp.id, position=idx, **data))


_CTC_FIELDS = (
    "exp_min", "exp_max", "target_exp", "rate", "revenue_monthly",
    "revenue_annual", "management_cost_pct", "engineering_budget",
    "hike_pct", "appraisal_cycle", "approved_ctc_lac",
)


def _existing_ctc_rows(db: Session, opportunity_id: int) -> list[dict]:
    rows = db.execute(
        select(OpportunityCtcSlab)
        .where(OpportunityCtcSlab.opportunity_id == opportunity_id)
        .order_by(OpportunityCtcSlab.position, OpportunityCtcSlab.id)
    ).scalars().all()
    return [{field: getattr(row, field) for field in _CTC_FIELDS} for row in rows]


@router.post("")
def create_opportunity(
    payload: OpportunityCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    validate_refs(db, payload.customer_id, payload.branch_id,
                  payload.contact_person_id, payload.hiring_manager_id)
    # Never trust the client: reject detail fields not valid for this opportunity type.
    try:
        validate_details(payload.details, _opp_type_value(payload.opp_type))
    except OpportunitySchemaError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    opp_type_value = _opp_type_value(payload.opp_type)
    clean_details = strip_details(payload.details, opp_type_value)
    if opp_type_value == "T&M":
        clean_details = normalize_tm_billing_details(clean_details)
    opp = Opportunity(
        opp_id=next_sequence_number(db, Opportunity, Opportunity.opp_id, "OPP"),
        title=payload.title.strip(),
        customer_id=payload.customer_id,
        branch_id=payload.branch_id,
        contact_person_id=payload.contact_person_id,
        hiring_manager_id=payload.hiring_manager_id,
        opp_type=payload.opp_type,
        rfi_value=payload.rfi_value,
        rfi_received_date=payload.rfi_received_date,
        onboarding_status=payload.onboarding_status,
        onboarded_count=payload.onboarded_count or 0,
        details=clean_details or None,
        pipeline_stage=PipelineStage.NEW,
        created_by=user.id,
    )
    # Sales-created opportunities need Sales Head approval; a Sales Head (or Admin)
    # creating one signs off on it immediately.
    privileged = user.has_any("Sales_Head", "Admin")
    if privileged:
        opp.approval_status = OpportunityApprovalStatus.APPROVED
        opp.sales_head_approved_by = user.id
        opp.sales_head_approved_at = _now()
    else:
        opp.approval_status = OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL
    db.add(opp)
    db.flush()
    if payload.skills:
        replace_skills(db, opp, payload.skills)
    if payload.ctc_slab is not None:
        _replace_ctc_slab(db, opp, payload.ctc_slab)
    if privileged:
        log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                     "Created", f"Opportunity {opp.opp_id} created (auto-approved)")
        _spawn_requirement_from_opportunity(db, opp, user)
    else:
        log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                     "Created", f"Opportunity {opp.opp_id} created; submitted for Sales Head approval")
        notify_role(db, "Sales_Head",
                    f"Opportunity {opp.opp_id} awaiting approval",
                    f"'{opp.title}' was created and needs your approval.",
                    f"/opportunities/{opp.id}", exclude_user_id=user.id)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity created")


@router.get("/{opportunity_id}")
def get_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity fetched")


@router.put("/{opportunity_id}")
def update_opportunity(
    opportunity_id: int,
    payload: OpportunityUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Optimistic concurrency: reject a stale write instead of clobbering a newer one.
    client_version = changes.pop("version", None)
    if client_version is not None and client_version != opp.version:
        raise HTTPException(
            status_code=409,
            detail="This opportunity was changed by someone else. Reload and re-apply your edits.",
        )

    target_customer = changes.get("customer_id", opp.customer_id)
    validate_refs(
        db,
        target_customer,
        changes.get("branch_id", opp.branch_id),
        changes.get("contact_person_id", opp.contact_person_id),
        changes.get("hiring_manager_id", opp.hiring_manager_id),
    )
    if "title" in changes and changes["title"] is not None:
        changes["title"] = changes["title"].strip()
        if not changes["title"]:
            raise HTTPException(status_code=400, detail="Title cannot be empty")

    # Effective type after this update (may be unchanged).
    current_type = _opp_type_value(opp.opp_type)
    eff_type = _opp_type_value(changes.get("opp_type", opp.opp_type))
    type_changed = eff_type != current_type

    # details: MERGE a partial payload into the stored object (never null unsent keys),
    # then validate + strip against the effective type.
    incoming_details = changes.pop("details", None)
    details_changed = incoming_details is not None or "opp_type" in changes
    if incoming_details is not None:
        # A type switch starts a fresh type-specific detail set so hidden T&M
        # billing inputs can never fail validation or leak into another branch.
        merged = {} if type_changed else dict(opp.details or {})
        merged.update(incoming_details)
        try:
            validate_details(merged, eff_type)
        except OpportunitySchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        clean_merged = strip_details(merged, eff_type)
        if eff_type == "T&M":
            clean_merged = normalize_tm_billing_details(clean_merged)
        opp.details = clean_merged or None
    elif details_changed and eff_type == "T&M":
        opp.details = normalize_tm_billing_details(strip_details(opp.details, eff_type))
    elif details_changed:
        opp.details = strip_details(opp.details, eff_type) or None

    ctc_provided = "ctc_slab" in changes
    changes.pop("ctc_slab", None)

    for field, value in changes.items():
        setattr(opp, field, value)
    if ctc_provided:
        _replace_ctc_slab(db, opp, payload.ctc_slab or [])
    elif details_changed:
        # Opportunity-type and source-detail changes refresh the complete chain.
        _replace_ctc_slab(db, opp, _existing_ctc_rows(db, opp.id))
    opp.version = (opp.version or 1) + 1  # bump for the next optimistic-concurrency check

    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Updated", f"Fields updated: {', '.join(sorted(changes.keys()))}")
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity updated")


@router.delete("/{opportunity_id}")
def delete_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("opportunities", "Sales_Head")),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    db.delete(opp)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Opportunity is referenced by other records (requirements) and cannot be deleted",
        )
    return envelope(data={"id": opportunity_id}, message="Opportunity deleted")


# ---------------------------------------------------------------------------
# Sales Head approval workflow
# ---------------------------------------------------------------------------

def _require_approval_status(opp: Opportunity, allowed: tuple, action: str) -> None:
    current = opp.approval_status.value if hasattr(opp.approval_status, "value") else str(opp.approval_status)
    allowed_vals = tuple(s.value for s in allowed)
    if current not in allowed_vals:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot {action} an opportunity with approval status '{current}'. "
                   f"Allowed: {', '.join(allowed_vals)}",
        )


@router.post("/{opportunity_id}/approve")
def approve_opportunity(
    opportunity_id: int,
    payload: CommentIn | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("opportunities", "Sales_Head")),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    _require_approval_status(opp, (OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL,), "approve")
    opp.approval_status = OpportunityApprovalStatus.APPROVED
    opp.sales_head_approved_by = user.id
    opp.sales_head_approved_at = _now()
    opp.approval_rejection_reason = None
    comment = (payload.comment if payload else None) or None
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Approved", comment or "Approved by Sales Head")
    # Bridge into the Requirements chain → RMG (Engineering Review) → TA.
    spawned = _spawn_requirement_from_opportunity(db, opp, user)
    if opp.created_by != user.id:
        notify_user(db, opp.created_by,
                    f"Opportunity {opp.opp_id} approved",
                    "Approved by Sales Head; sent to engineering review."
                    if spawned else "Approved by Sales Head.",
                    f"/opportunities/{opp.id}")
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity approved")


@router.post("/{opportunity_id}/reject")
def reject_opportunity(
    opportunity_id: int,
    payload: RejectIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("opportunities", "Sales_Head")),
):
    try:
        reason = payload.validated_reason()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    opp = get_opportunity_or_404(db, opportunity_id)
    _require_approval_status(opp, (OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL,), "reject")
    opp.approval_status = OpportunityApprovalStatus.REJECTED
    opp.approval_rejection_reason = reason
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Rejected", reason)
    if opp.created_by != user.id:
        notify_user(db, opp.created_by,
                    f"Opportunity {opp.opp_id} rejected by Sales Head",
                    reason, f"/opportunities/{opp.id}")
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity rejected")


@router.post("/{opportunity_id}/resubmit")
def resubmit_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    if not user.is_admin and opp.created_by != user.id:
        raise HTTPException(status_code=403, detail="Only the creator (or Admin) can resubmit this opportunity")
    _require_approval_status(opp, (OpportunityApprovalStatus.REJECTED,), "resubmit")
    opp.approval_status = OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL
    opp.approval_rejection_reason = None
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Resubmitted", "Resubmitted for Sales Head approval")
    notify_role(db, "Sales_Head",
                f"Opportunity {opp.opp_id} resubmitted for approval",
                f"'{opp.title}' was resubmitted and needs your approval.",
                f"/opportunities/{opp.id}", exclude_user_id=user.id)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity resubmitted for approval")


# ---------------------------------------------------------------------------
# Stage transition (server-side validated state machine)
# ---------------------------------------------------------------------------

@router.post("/{opportunity_id}/stage-transition")
def stage_transition(
    opportunity_id: int,
    payload: StageTransitionIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    new_stage = (payload.new_stage or "").strip()
    validate_stage_transition(opp.pipeline_stage, new_stage)
    if new_stage == PipelineStage.ARCHIVED.value and not user.has_any("Sales_Head", "Admin"):
        raise HTTPException(status_code=403,
                            detail="Only Sales_Head or Admin can archive an opportunity")
    old_stage = opp.pipeline_stage.value if hasattr(opp.pipeline_stage, "value") else str(opp.pipeline_stage)
    opp.pipeline_stage = PipelineStage(new_stage)
    comment = f"Stage changed: {old_stage} -> {new_stage}"
    if payload.comment and payload.comment.strip():
        comment += f" | {payload.comment.strip()}"
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Stage_Transition", comment)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True),
                    message=f"Opportunity moved to {new_stage}")


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@router.get("/{opportunity_id}/activity-log")
def get_activity_log(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    get_opportunity_or_404(db, opportunity_id)
    return envelope(data=fetch_activity_log(db, opportunity_id), message="Activity log fetched")


# ---------------------------------------------------------------------------
# Skills (replace the whole set)
# ---------------------------------------------------------------------------

@router.post("/{opportunity_id}/skills")
def set_skills(
    opportunity_id: int,
    payload: list[OpportunitySkillIn],
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    replace_skills(db, opp, payload)
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Skills_Updated", f"Skill set replaced ({len(payload)} skill(s))")
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_skills(db, opp), message="Skills updated")
