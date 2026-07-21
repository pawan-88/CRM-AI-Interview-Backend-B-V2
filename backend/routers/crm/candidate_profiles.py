"""Candidate profiles (candidate x opportunity): pipeline, evaluations, offers, activity log.

Pipeline transitions are validated server-side in services/candidate_profiles.py
(transition map + per-stage role authority). Reads: any CRM role.
"""
from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, any_crm_role, get_crm_db, page_params, role_required
from models import (
    Candidate, CandidateProfile, CandidateProfileActivityLog, OfferHistory, OfferStatus,
    Opportunity, PipelineStatus,
)
from schemas.candidate_profiles import (
    OfferCreate, OfferUpdate, ProfileCreate, ProfileUpdate, SkillEvaluationItem,
)
from schemas.common import StatusTransitionIn, envelope
from services.candidate_profiles import (
    REJECTED_BUCKET, compute_hike_percent, enrich_profiles_list, get_profile_or_404,
    offer_to_dict, perform_transition, profile_detail, profile_to_dict, upsert_skill_evaluations,
)
from services.crm_common import log_activity, paginate

router = APIRouter(prefix="/api/candidate-profiles", tags=["CRM: Candidate Profiles"])

create_roles = role_required("TA", "Sales", "RMG")
evaluation_roles = role_required("RMG", "TA", "Sales")
offer_roles = role_required("Sales", "Sales_Head", "HR")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_profiles(pp: PageParams = Depends(page_params),
                  pipeline_status: str | None = None,
                  opportunity_id: int | None = None,
                  candidate_id: int | None = None,
                  bucket: str | None = None,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(any_crm_role)):
    stmt = select(CandidateProfile)
    if pp.search:
        # Search candidate name / email without N+1 (join once for the filter).
        stmt = stmt.join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        like = f"%{pp.search}%"
        stmt = stmt.where(sa.or_(
            Candidate.first_name.ilike(like),
            Candidate.middle_name.ilike(like),
            Candidate.last_name.ilike(like),
            Candidate.email.ilike(like),
        ))
    if pipeline_status:
        valid = {m.value for m in PipelineStatus}
        if pipeline_status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"Unknown pipeline_status '{pipeline_status}'. "
                                       f"Valid values: {', '.join(sorted(valid))}")
        stmt = stmt.where(CandidateProfile.pipeline_status == PipelineStatus(pipeline_status))
    if opportunity_id is not None:
        stmt = stmt.where(CandidateProfile.opportunity_id == opportunity_id)
    if candidate_id is not None:
        stmt = stmt.where(CandidateProfile.candidate_id == candidate_id)
    if bucket:
        bucket = bucket.strip().lower()
        rejected_enums = [PipelineStatus(v) for v in sorted(REJECTED_BUCKET)]
        if bucket == "rejected":
            stmt = stmt.where(CandidateProfile.pipeline_status.in_(rejected_enums))
        elif bucket == "active":
            stmt = stmt.where(CandidateProfile.pipeline_status.not_in(rejected_enums))
        else:
            raise HTTPException(status_code=400, detail="bucket must be 'active' or 'rejected'")
    order = CandidateProfile.id.asc() if pp.sort_dir == "asc" else CandidateProfile.id.desc()
    stmt = stmt.order_by(order)
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    return envelope(data=enrich_profiles_list(db, items), meta=meta)


@router.post("")
def create_profile(payload: ProfileCreate,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(create_roles)):
    if not db.get(Candidate, payload.candidate_id):
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not db.get(Opportunity, payload.opportunity_id):
        raise HTTPException(status_code=404, detail="Opportunity not found")
    duplicate = db.execute(
        select(CandidateProfile.id).where(
            CandidateProfile.candidate_id == payload.candidate_id,
            CandidateProfile.opportunity_id == payload.opportunity_id)
    ).first()
    if duplicate:
        raise HTTPException(status_code=409,
                            detail="A profile for this candidate and opportunity already exists")
    values = payload.model_dump(exclude_unset=True)
    values.pop("commercial_approved", None)
    profile = CandidateProfile(**values)
    if payload.commercial_approved is not None:
        profile.commercial_approved = payload.commercial_approved
    profile.hike_percent = compute_hike_percent(payload.current_ctc, payload.expected_ctc)
    db.add(profile)
    db.flush()
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "CREATED", "Candidate profile created (status Sourcing)")
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile), message="Candidate profile created")


@router.get("/{profile_id}")
def get_profile(profile_id: int,
                db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(any_crm_role)):
    profile = get_profile_or_404(db, profile_id)
    return envelope(data=profile_detail(db, profile, user))


@router.put("/{profile_id}")
def update_profile(profile_id: int, payload: ProfileUpdate,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(create_roles)):
    profile = get_profile_or_404(db, profile_id)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(profile, field, value)
    if "current_ctc" in updates or "expected_ctc" in updates:
        profile.hike_percent = compute_hike_percent(profile.current_ctc, profile.expected_ctc)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "UPDATED", f"Profile fields updated: {', '.join(sorted(updates)) or 'none'}")
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile), message="Candidate profile updated")


@router.delete("/{profile_id}")
def delete_profile(profile_id: int,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(create_roles)):
    profile = get_profile_or_404(db, profile_id)
    db.delete(profile)
    db.commit()
    return envelope(message="Candidate profile deleted")


# ---------------------------------------------------------------------------
# Pipeline status transition
# ---------------------------------------------------------------------------

@router.post("/{profile_id}/status-transition")
def status_transition(profile_id: int, payload: StatusTransitionIn,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(any_crm_role)):
    profile = get_profile_or_404(db, profile_id)
    old_status = perform_transition(db, profile, payload.new_status, payload.comment, user)
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile),
                    message=f"Status changed: {old_status} -> {payload.new_status}")


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@router.get("/{profile_id}/activity-log")
def activity_log(profile_id: int,
                 db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(any_crm_role)):
    get_profile_or_404(db, profile_id)
    rows = db.execute(
        sa.text(
            "SELECT l.id, l.user_id, r.username, l.action_type, l.comment, l.timestamp "
            "FROM candidate_profile_activity_log l "
            "LEFT JOIN registration_data r ON r.id = l.user_id "
            "WHERE l.profile_id = :pid "
            "ORDER BY l.timestamp ASC, l.id ASC"
        ),
        {"pid": profile_id},
    ).mappings().all()
    data = [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "action_type": row["action_type"],
            "comment": row["comment"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] is not None else None,
        }
        for row in rows
    ]
    return envelope(data=data)


# ---------------------------------------------------------------------------
# Skill evaluations
# ---------------------------------------------------------------------------

@router.post("/{profile_id}/skill-evaluation")
def skill_evaluation(profile_id: int, payload: list[SkillEvaluationItem],
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(evaluation_roles)):
    profile = get_profile_or_404(db, profile_id)
    if not payload:
        raise HTTPException(status_code=400, detail="At least one skill evaluation item is required")
    count = upsert_skill_evaluations(db, profile, payload, user)
    db.commit()
    return envelope(data=profile_detail(db, profile, user)["skill_evaluations"],
                    message=f"Skill evaluation saved for {count} skill(s)")


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------

@router.post("/{profile_id}/offer")
def create_offer(profile_id: int, payload: OfferCreate,
                 db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(offer_roles)):
    profile = get_profile_or_404(db, profile_id)
    offer = OfferHistory(profile_id=profile.id, status=OfferStatus.PENDING,
                         **payload.model_dump())
    db.add(offer)
    db.flush()
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "OFFER_CREATED",
                 f"Offer #{offer.id} created (CTC {payload.ctc}, date {payload.offer_date.isoformat()})")
    db.commit()
    db.refresh(offer)
    return envelope(data=offer_to_dict(offer), message="Offer created")


@router.put("/{profile_id}/offer/{offer_id}")
def update_offer(profile_id: int, offer_id: int, payload: OfferUpdate,
                 db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(offer_roles)):
    profile = get_profile_or_404(db, profile_id)
    offer = db.get(OfferHistory, offer_id)
    if not offer or offer.profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Offer not found for this profile")
    updates = payload.model_dump(exclude_unset=True)
    new_status = updates.pop("status", None)
    if new_status is not None:
        valid = {m.value for m in OfferStatus}
        if new_status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"Unknown offer status '{new_status}'. "
                                       f"Valid values: {', '.join(sorted(valid))}")
        offer.status = OfferStatus(new_status)
        if new_status == OfferStatus.ACCEPTED.value and "acceptance_date" not in updates \
                and offer.acceptance_date is None:
            offer.acceptance_date = date.today()
    for field, value in updates.items():
        setattr(offer, field, value)
    changed = sorted(list(updates) + (["status"] if new_status is not None else []))
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "OFFER_UPDATED", f"Offer #{offer.id} updated: {', '.join(changed) or 'none'}")
    db.commit()
    db.refresh(offer)
    return envelope(data=offer_to_dict(offer), message="Offer updated")


@router.get("/{profile_id}/offer-history")
def offer_history(profile_id: int,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(any_crm_role)):
    profile = get_profile_or_404(db, profile_id)
    return envelope(data=[offer_to_dict(o) for o in profile.offers])
