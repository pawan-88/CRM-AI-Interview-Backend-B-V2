"""Candidate profile pipeline: server-side transition map, role authority, serializers.

The transition map and stage-authority map live HERE (server side) — the UI only
renders what GET /api/candidate-profiles/{id} returns in allowed_next_statuses.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser
from models import (
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, OfferHistory,
    Opportunity, PipelineStatus, Requirement, Skill, SkillEvaluation,
)
from services.crm_common import log_activity

PS = PipelineStatus

#: Statuses from which NO further transition is possible.
TERMINAL_STATUSES: set[str] = {
    PS.JOINED.value, PS.SALES_REJECTED.value, PS.RMG_REJECTED.value,
    PS.CUSTOMER_REJECTED.value, PS.SELF_WITHDRAWN.value, PS.REJECTED.value,
}

#: Happy-path forward moves per stage.
_FORWARD: dict[str, list[str]] = {
    PS.SOURCING.value: [PS.TECHNICAL_SCREENING.value],
    PS.TECHNICAL_SCREENING.value: [PS.RMG_REVIEW.value],
    PS.RMG_REVIEW.value: [PS.SALES_SCREENING.value],
    PS.SALES_SCREENING.value: [PS.CUSTOMER_SCREENING.value],
    PS.CUSTOMER_SCREENING.value: [PS.CUSTOMER_INTERVIEW.value],
    PS.CUSTOMER_INTERVIEW.value: [PS.SHORTLISTED.value],
    PS.SHORTLISTED.value: [PS.CUSTOMER_APPROVAL.value],
    PS.CUSTOMER_APPROVAL.value: [PS.PREBOARDING.value],
    PS.PREBOARDING.value: [PS.JOINED.value],
}

#: Stage-specific rejection moves.
_STAGE_REJECTIONS: dict[str, list[str]] = {
    PS.RMG_REVIEW.value: [PS.RMG_REJECTED.value],
    PS.SALES_SCREENING.value: [PS.SALES_REJECTED.value],
    PS.CUSTOMER_SCREENING.value: [PS.CUSTOMER_REJECTED.value],
    PS.CUSTOMER_INTERVIEW.value: [PS.CUSTOMER_REJECTED.value],
    PS.SHORTLISTED.value: [PS.CUSTOMER_REJECTED.value],
    PS.CUSTOMER_APPROVAL.value: [PS.CUSTOMER_REJECTED.value],
}

#: Every non-terminal stage can also end in generic withdrawal/rejection.
_ALWAYS: list[str] = [PS.SELF_WITHDRAWN.value, PS.REJECTED.value]

#: Full transition map: stage -> ordered list of allowed next statuses.
TRANSITION_MAP: dict[str, list[str]] = {
    stage: _FORWARD[stage] + _STAGE_REJECTIONS.get(stage, []) + _ALWAYS
    for stage in _FORWARD
}

#: Which roles may move a profile OUT of each stage (Admin always may).
STAGE_AUTHORITY: dict[str, set[str]] = {
    PS.SOURCING.value: {"TA"},
    PS.TECHNICAL_SCREENING.value: {"TA"},
    PS.RMG_REVIEW.value: {"RMG"},
    PS.SALES_SCREENING.value: {"Sales"},
    PS.CUSTOMER_SCREENING.value: {"Sales"},
    PS.CUSTOMER_INTERVIEW.value: {"Sales", "Sales_Head"},
    PS.SHORTLISTED.value: {"Sales", "Sales_Head"},
    PS.CUSTOMER_APPROVAL.value: {"Sales", "Sales_Head"},
    PS.PREBOARDING.value: {"HR", "Sales_Head"},
}

#: The 5 rejection/withdrawal states (used by the ?bucket=rejected list filter).
REJECTED_BUCKET: set[str] = {
    PS.SALES_REJECTED.value, PS.RMG_REJECTED.value, PS.CUSTOMER_REJECTED.value,
    PS.SELF_WITHDRAWN.value, PS.REJECTED.value,
}


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def allowed_next_statuses(current_status) -> list[str]:
    """All allowed next statuses from a stage (regardless of role)."""
    return list(TRANSITION_MAP.get(_status_value(current_status), []))


def user_may_transition_from(current_status, user: CurrentUser) -> bool:
    if user.is_admin:
        return True
    return bool(user.roles & STAGE_AUTHORITY.get(_status_value(current_status), set()))


def allowed_next_statuses_for_user(current_status, user: CurrentUser) -> list[str]:
    """What the transition dropdown should show for THIS user."""
    if not user_may_transition_from(current_status, user):
        return []
    return allowed_next_statuses(current_status)


def compute_hike_percent(current_ctc, expected_ctc):
    """(expected - current) / current * 100, rounded to 2 decimals; None if not computable."""
    if current_ctc is None or expected_ctc is None:
        return None
    current = Decimal(str(current_ctc))
    expected = Decimal(str(expected_ctc))
    if current == 0:
        return None
    return ((expected - current) / current * Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_profile_or_404(db: Session, profile_id: int) -> CandidateProfile:
    profile = db.get(CandidateProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidate profile not found")
    return profile


def perform_transition(db: Session, profile: CandidateProfile, new_status: str,
                       comment: str | None, user: CurrentUser) -> str:
    """Validate + apply one pipeline transition. Caller commits. Returns the old status."""
    clean_comment = (comment or "").strip()
    if len(clean_comment) < 5:
        raise HTTPException(status_code=400,
                            detail="A comment is mandatory for every status transition (minimum 5 characters)")

    current = _status_value(profile.pipeline_status)
    valid_values = {m.value for m in PS}
    if new_status not in valid_values:
        raise HTTPException(status_code=400,
                            detail=f"Unknown pipeline status '{new_status}'. "
                                   f"Valid values: {', '.join(sorted(valid_values))}")

    if current in TERMINAL_STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"'{current}' is a terminal status; no further transitions are allowed")

    allowed = allowed_next_statuses(current)
    if new_status not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"Invalid transition {current} -> {new_status}. "
                                   f"Allowed next statuses: {', '.join(allowed)}")

    if not user_may_transition_from(current, user):
        required = sorted(STAGE_AUTHORITY.get(current, set()) | {"Admin"})
        raise HTTPException(status_code=403,
                            detail=f"Your role(s) cannot move a profile out of '{current}'. "
                                   f"Requires one of: {', '.join(required)}")

    profile.pipeline_status = PS(new_status)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "STATUS_CHANGE", f"{current} -> {new_status}: {clean_comment}")

    if new_status == PS.JOINED.value:
        # Lazy import: requirements service is owned by another module; never fail the join.
        try:
            from services.requirements import check_and_mark_fulfilled
            req_ids = db.execute(
                select(Requirement.id).where(Requirement.opportunity_id == profile.opportunity_id)
            ).scalars().all()
            for rid in req_ids:
                try:
                    check_and_mark_fulfilled(db, rid, user.id)
                except Exception:
                    continue
        except Exception:
            pass

    return current


def upsert_skill_evaluations(db: Session, profile: CandidateProfile,
                             items: list, user: CurrentUser) -> int:
    """Upsert per (profile_id, skill_id); only overwrite fields the payload provided."""
    from services.candidates import ensure_skills_exist  # shared validator

    ensure_skills_exist(db, [item.skill_id for item in items])
    touched = 0
    for item in items:
        provided = item.model_dump(exclude_unset=True)
        provided.pop("skill_id", None)
        row = db.execute(
            select(SkillEvaluation).where(SkillEvaluation.profile_id == profile.id,
                                          SkillEvaluation.skill_id == item.skill_id)
        ).scalar_one_or_none()
        if row is None:
            row = SkillEvaluation(profile_id=profile.id, skill_id=item.skill_id)
            db.add(row)
        for field, value in provided.items():
            setattr(row, field, value)
        touched += 1
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "SKILL_EVALUATION", f"Upserted skill evaluation for {touched} skill(s)")
    return touched


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _num(value):
    return float(value) if value is not None else None


def _dt(value):
    return value.isoformat() if value is not None else None


def _candidate_full_name(candidate: Candidate | None) -> str | None:
    if candidate is None:
        return None
    parts = [candidate.first_name, candidate.middle_name, candidate.last_name]
    name = " ".join(p for p in parts if p)
    return name or None


def profile_to_dict(
    profile: CandidateProfile,
    *,
    candidate: Candidate | None = None,
    opportunity: Opportunity | None = None,
) -> dict:
    data = {
        "id": profile.id,
        "candidate_id": profile.candidate_id,
        "opportunity_id": profile.opportunity_id,
        "current_ctc": _num(profile.current_ctc),
        "expected_ctc": _num(profile.expected_ctc),
        "hike_percent": _num(profile.hike_percent),
        "pipeline_status": _status_value(profile.pipeline_status),
        "commercial_approved": bool(profile.commercial_approved),
        "ctc_approval_amount": _num(profile.ctc_approval_amount),
        "created_at": _dt(profile.created_at),
        "updated_at": _dt(profile.updated_at),
    }
    if candidate is not None:
        data["candidate_name"] = _candidate_full_name(candidate)
        data["email"] = candidate.email
        data["phone"] = candidate.phone
        data["experience_years"] = _num(candidate.experience_years)
        data["notice_period"] = candidate.notice_period
        data["technical_domain"] = candidate.technical_domain
        data["cv_url"] = candidate.cv_url
        # Candidate-master CTCs (profile commercials remain above).
        data["candidate_current_ctc"] = _num(candidate.current_ctc)
        data["candidate_expected_ctc"] = _num(candidate.expected_ctc)
    if opportunity is not None:
        data["opportunity_opp_id"] = opportunity.opp_id
        data["opportunity_title"] = opportunity.title
    return data


def enrich_profiles_list(db: Session, profiles: list[CandidateProfile]) -> list[dict]:
    """Serialize a page of profiles with candidate + opportunity summaries (no N+1)."""
    if not profiles:
        return []
    cand_ids = {p.candidate_id for p in profiles}
    opp_ids = {p.opportunity_id for p in profiles}
    candidates = {
        c.id: c
        for c in db.execute(select(Candidate).where(Candidate.id.in_(cand_ids))).scalars().all()
    }
    opportunities = {
        o.id: o
        for o in db.execute(select(Opportunity).where(Opportunity.id.in_(opp_ids))).scalars().all()
    }
    return [
        profile_to_dict(
            p,
            candidate=candidates.get(p.candidate_id),
            opportunity=opportunities.get(p.opportunity_id),
        )
        for p in profiles
    ]


def offer_to_dict(offer: OfferHistory) -> dict:
    return {
        "id": offer.id,
        "profile_id": offer.profile_id,
        "offer_date": _dt(offer.offer_date),
        "ctc": _num(offer.ctc),
        "joining_date": _dt(offer.joining_date),
        "offer_letter_url": offer.offer_letter_url,
        "acceptance_date": _dt(offer.acceptance_date),
        "expiry_date": _dt(offer.expiry_date),
        "status": _status_value(offer.status),
    }


def profile_detail(db: Session, profile: CandidateProfile, user: CurrentUser) -> dict:
    data = profile_to_dict(profile)

    candidate = db.get(Candidate, profile.candidate_id)
    data["candidate"] = {
        "id": candidate.id,
        "full_name": " ".join(p for p in (candidate.first_name, candidate.last_name) if p),
        "email": candidate.email,
        "phone": candidate.phone,
        "technical_domain": candidate.technical_domain,
        "cv_url": candidate.cv_url,
    } if candidate else None

    opp_row = db.execute(
        select(Opportunity.id, Opportunity.opp_id, Opportunity.title, Customer.name)
        .join(Customer, Customer.id == Opportunity.customer_id)
        .where(Opportunity.id == profile.opportunity_id)
    ).first()
    data["opportunity"] = {
        "id": opp_row[0], "opp_id": opp_row[1], "title": opp_row[2], "customer_name": opp_row[3],
    } if opp_row else None

    eval_rows = db.execute(
        select(SkillEvaluation, Skill.name)
        .join(Skill, Skill.id == SkillEvaluation.skill_id)
        .where(SkillEvaluation.profile_id == profile.id)
        .order_by(Skill.name)
    ).all()
    data["skill_evaluations"] = [
        {
            "id": ev.id,
            "skill_id": ev.skill_id,
            "skill_name": skill_name,
            "required_level": ev.required_level,
            "self_rated": ev.self_rated,
            "reviewer_rated": ev.reviewer_rated,
        }
        for ev, skill_name in eval_rows
    ]

    data["offers"] = [offer_to_dict(o) for o in profile.offers]
    data["allowed_next_statuses"] = allowed_next_statuses_for_user(profile.pipeline_status, user)
    return data
