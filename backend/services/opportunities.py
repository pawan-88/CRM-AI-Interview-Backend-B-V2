"""Opportunity service helpers: pipeline stage machine, FK validation, skills, serialization."""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    ContactPerson,
    Customer,
    CustomerBranch,
    Opportunity,
    OpportunitySkill,
    PipelineStage,
    Skill,
)

# ---------------------------------------------------------------------------
# Server-side pipeline stage machine (Archived is terminal)
# ---------------------------------------------------------------------------

STAGE_TRANSITIONS: dict[str, list[str]] = {
    "New": ["Active", "On_Hold", "Rejected"],
    "Active": ["On_Hold", "Closed_Won", "Closed_Lost", "Closed_Partial", "Rejected"],
    "On_Hold": ["Active", "Closed_Lost", "Closed_Partial", "Rejected"],
    "Closed_Won": ["Archived"],
    "Closed_Lost": ["Archived"],
    "Closed_Partial": ["Archived"],
    "Rejected": ["Archived"],
    "Archived": [],
}


def _ev(value):
    return value.value if hasattr(value, "value") else value


def validate_stage_transition(current_stage, new_stage: str) -> None:
    """400 with the allowed next stages when the requested move is illegal."""
    current = _ev(current_stage)
    valid_stages = {s.value for s in PipelineStage}
    if new_stage not in valid_stages:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown stage '{new_stage}'. Valid stages: {', '.join(s.value for s in PipelineStage)}",
        )
    allowed = STAGE_TRANSITIONS.get(current, [])
    if new_stage not in allowed:
        allowed_txt = ", ".join(allowed) if allowed else "none (terminal stage)"
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition from '{current}' to '{new_stage}'. Allowed next stages: {allowed_txt}",
        )


# ---------------------------------------------------------------------------
# Lookups & FK validation
# ---------------------------------------------------------------------------

def get_opportunity_or_404(db: Session, opportunity_id: int) -> Opportunity:
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return opp


def validate_refs(db: Session, customer_id: int, branch_id: int | None,
                  contact_person_id: int | None, hiring_manager_id: int | None) -> None:
    """Validate that customer exists and branch/contacts belong to that customer."""
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="Invalid customer_id")
    if branch_id is not None:
        branch = db.get(CustomerBranch, branch_id)
        if branch is None or branch.customer_id != customer_id:
            raise HTTPException(status_code=400, detail="branch_id does not belong to this customer")
    for label, contact_id in (("contact_person_id", contact_person_id),
                              ("hiring_manager_id", hiring_manager_id)):
        if contact_id is not None:
            contact = db.get(ContactPerson, contact_id)
            if contact is None or contact.customer_id != customer_id:
                raise HTTPException(status_code=400, detail=f"{label} does not belong to this customer")


# ---------------------------------------------------------------------------
# Skills (replace the whole set)
# ---------------------------------------------------------------------------

def replace_skills(db: Session, opp: Opportunity, items: list) -> None:
    """Replace the opportunity's skill set. items: OpportunitySkillIn list. Caller commits."""
    dedup: dict[int, object] = {}
    for item in items:
        dedup[item.skill_id] = item
    if dedup:
        found = set(db.execute(select(Skill.id).where(Skill.id.in_(dedup.keys()))).scalars().all())
        missing = sorted(set(dedup.keys()) - found)
        if missing:
            raise HTTPException(status_code=400,
                                detail=f"Unknown skill id(s): {', '.join(str(m) for m in missing)}")
    for existing in list(opp.skills):
        db.delete(existing)
    db.flush()
    for skill_id, item in dedup.items():
        db.add(OpportunitySkill(
            opportunity_id=opp.id,
            skill_id=skill_id,
            is_mandatory=bool(getattr(item, "is_mandatory", False)),
            required_level=getattr(item, "required_level", None),
            comment=getattr(item, "comment", None),
        ))


def serialize_skills(db: Session, opp: Opportunity) -> list[dict]:
    skill_ids = {s.skill_id for s in opp.skills}
    names: dict[int, str] = {}
    if skill_ids:
        rows = db.execute(select(Skill.id, Skill.name).where(Skill.id.in_(skill_ids))).all()
        names = {row[0]: row[1] for row in rows}
    return [
        {
            "id": s.id,
            "skill_id": s.skill_id,
            "skill_name": names.get(s.skill_id),
            "is_mandatory": s.is_mandatory,
            "required_level": getattr(s, "required_level", None),
            "comment": getattr(s, "comment", None),
        }
        for s in opp.skills
    ]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_opportunity(db: Session, opp: Opportunity, detail: bool = False) -> dict:
    customer = db.get(Customer, opp.customer_id)
    data = {
        "id": opp.id,
        "opp_id": opp.opp_id,
        "title": opp.title,
        "customer_id": opp.customer_id,
        "customer_name": customer.name if customer else None,
        "branch_id": opp.branch_id,
        "contact_person_id": opp.contact_person_id,
        "hiring_manager_id": opp.hiring_manager_id,
        "opp_type": _ev(opp.opp_type),
        "rfi_value": float(opp.rfi_value) if opp.rfi_value is not None else None,
        "rfi_received_date": opp.rfi_received_date.isoformat() if opp.rfi_received_date else None,
        "pipeline_stage": _ev(opp.pipeline_stage),
        "approval_status": _ev(getattr(opp, "approval_status", None)),
        "sales_head_approved_by": getattr(opp, "sales_head_approved_by", None),
        "sales_head_approved_at": (
            opp.sales_head_approved_at.isoformat()
            if getattr(opp, "sales_head_approved_at", None) else None
        ),
        "approval_rejection_reason": getattr(opp, "approval_rejection_reason", None),
        "onboarding_status": opp.onboarding_status,
        "onboarded_count": getattr(opp, "onboarded_count", 0),
        "details": getattr(opp, "details", None) or {},
        "version": getattr(opp, "version", 1),
        "created_by": opp.created_by,
        "created_at": opp.created_at.isoformat() if opp.created_at else None,
        "updated_at": opp.updated_at.isoformat() if opp.updated_at else None,
    }
    if detail:
        branch = db.get(CustomerBranch, opp.branch_id) if opp.branch_id else None
        contact = db.get(ContactPerson, opp.contact_person_id) if opp.contact_person_id else None
        hiring_mgr = db.get(ContactPerson, opp.hiring_manager_id) if opp.hiring_manager_id else None
        data["branch_name"] = branch.branch_name if branch else None
        data["contact_person_name"] = contact.name if contact else None
        data["hiring_manager_name"] = hiring_mgr.name if hiring_mgr else None
        data["skills"] = serialize_skills(db, opp)
        data["ctc_slab"] = _serialize_ctc_slab(opp)
        data["allowed_next_stages"] = STAGE_TRANSITIONS.get(_ev(opp.pipeline_stage), [])
    return data


def _serialize_ctc_slab(opp: Opportunity) -> list[dict]:
    def _f(v):
        return float(v) if v is not None else None
    rows = sorted(getattr(opp, "ctc_slab", []) or [], key=lambda r: getattr(r, "position", 0))
    return [
        {
            "exp_min": _f(r.exp_min), "exp_max": _f(r.exp_max), "target_exp": _f(r.target_exp),
            "rate": _f(r.rate), "revenue_monthly": _f(r.revenue_monthly), "revenue_annual": _f(r.revenue_annual),
            "management_cost_pct": _f(r.management_cost_pct), "engineering_budget": _f(r.engineering_budget),
            "hike_pct": _f(r.hike_pct), "appraisal_cycle": r.appraisal_cycle, "approved_ctc_lac": _f(r.approved_ctc_lac),
        }
        for r in rows
    ]


def fetch_activity_log(db: Session, opportunity_id: int) -> list[dict]:
    """Chronological activity log with usernames joined from registration_data."""
    rows = db.execute(
        sa.text(
            "SELECT l.id, l.user_id, r.username, r.full_name, l.action_type, l.comment, l.timestamp "
            "FROM opportunity_activity_log l "
            "LEFT JOIN registration_data r ON r.id = l.user_id "
            "WHERE l.opportunity_id = :oid "
            "ORDER BY l.timestamp ASC, l.id ASC"
        ),
        {"oid": opportunity_id},
    ).mappings().all()
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "full_name": row["full_name"],
            "action_type": row["action_type"],
            "comment": row["comment"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
        }
        for row in rows
    ]
