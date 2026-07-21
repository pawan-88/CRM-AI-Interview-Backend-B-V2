"""AI interview sessions scoped to a candidate profile (Phase 5).

Backs the "AI Interview" tab on the profile detail page: trigger a new
interview, list past sessions with scores, and deep-link to the full report
in the admin dashboard (?view=candidateReport&cid=<email>&iid=<record_id>).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from models import (
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, Requirement,
)
from schemas.common import envelope
from services.ai_interview_bridge import ai_interview_autosend_enabled, schedule_l1_interview
from services.candidate_comms import interview_link_message, notify_candidate
from services.crm_common import log_activity, to_dict

router = APIRouter(prefix="/api/candidate-profiles", tags=["CRM: AI Interviews"])

VIEW_ROLES = ("TA", "RMG", "Sales", "Sales_Head", "HR")
TRIGGER_ROLES = ("TA", "RMG", "Sales")


def _profile_or_404(db: Session, profile_id: int) -> CandidateProfile:
    profile = db.get(CandidateProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Candidate profile not found")
    return profile


def _link_out(db: Session, link: AiInterviewLink, candidate: Candidate | None) -> dict:
    email = (candidate.email or "").lower() if candidate else ""
    data = to_dict(link)
    data["report_link"] = (
        f"/admin?view=candidateReport&cid={email}&iid={link.interview_record_id}"
        if link.interview_record_id and email else None
    )
    data["pending"] = link.result == "Pending"
    return data


@router.get("/{profile_id}/ai-interviews")
def list_ai_interviews(profile_id: int, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(role_required(*VIEW_ROLES))):
    profile = _profile_or_404(db, profile_id)
    candidate = db.get(Candidate, profile.candidate_id)
    links = db.execute(
        select(AiInterviewLink).where(AiInterviewLink.profile_id == profile.id)
        .order_by(AiInterviewLink.created_at.desc())
    ).scalars().all()
    return envelope(
        data=[_link_out(db, l, candidate) for l in links],
        meta={"pending_count": sum(1 for l in links if l.result == "Pending"),
              "page": 1, "limit": len(links) or 1, "total": len(links), "pages": 1},
    )


@router.post("/{profile_id}/ai-interviews")
def trigger_ai_interview(profile_id: int, db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(role_required(*TRIGGER_ROLES))):
    profile = _profile_or_404(db, profile_id)
    candidate = db.get(Candidate, profile.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found for this profile")

    pending = db.execute(
        select(AiInterviewLink).where(
            AiInterviewLink.profile_id == profile.id, AiInterviewLink.result == "Pending"
        )
    ).scalars().first()
    if pending is not None:
        raise HTTPException(status_code=409,
                            detail="An AI interview is already pending for this profile")

    requirement = db.execute(
        select(Requirement).where(Requirement.opportunity_id == profile.opportunity_id)
        .order_by(Requirement.id.desc())
    ).scalars().first()

    bridge = schedule_l1_interview(db, candidate, requirement, profile, scheduled_by=user.id)
    if not bridge.get("scheduled"):
        raise HTTPException(status_code=502,
                            detail=f"AI interview scheduling failed: {bridge.get('error')}")
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "AI_INTERVIEW_SCHEDULED",
                 f"AI L1 interview scheduled (session {bridge.get('session_ref')})")

    # Best-effort notify only when AI_INTERVIEW_AUTOSEND is enabled (default: off).
    notified = {"email": False, "whatsapp": False}
    if ai_interview_autosend_enabled():
        from datetime import datetime, timezone
        candidate_name = f"{candidate.first_name} {candidate.last_name or ''}".strip()
        role_title = requirement.title if requirement is not None else "Karnex screening"
        when_text = datetime.now(timezone.utc).strftime("%A, %d %B %Y at %H:%M UTC")
        msg = interview_link_message(candidate_name, role_title, when_text,
                                     bridge.get("invite_url", ""), bridge.get("access_key", ""))
        notified = notify_candidate(candidate.email, candidate.phone,
                                    msg["subject"], msg["text"], msg["html"])

    db.commit()
    candidate_name = f"{candidate.first_name} {candidate.last_name or ''}".strip()
    return envelope(
        data={
            "session_ref": bridge.get("session_ref"),
            "invite_url": bridge.get("invite_url"),
            "access_key": bridge.get("access_key"),
            "link_id": bridge.get("link_id"),
            "job_id": bridge.get("job_id", ""),
            "candidate_name": candidate_name,
            "candidate_email": candidate.email,
            "notified": notified,
            "autosend": ai_interview_autosend_enabled(),
        },
        message=(
            "AI interview ready — copy the invite link to share with the candidate"
            if not ai_interview_autosend_enabled()
            else "AI interview scheduled — invite sent to the candidate"
        ),
    )
