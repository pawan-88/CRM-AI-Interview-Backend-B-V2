"""Resume / ATS pipeline router: upload, ATS scan, shortlist, AI L1 scheduling.

Paths span two prefixes (/api/requirements/{id}/resumes and /api/resumes/{id}),
so the router carries no prefix of its own.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, get_crm_db, page_params, role_required
from models import (
    AiInterviewStatus, AtsStatus, RequirementActivityLog, RequirementStatus, Resume,
)
from pydantic import BaseModel, Field

from routers.crm.apply import _base_url
from schemas.common import envelope
from schemas.resumes import AI_INTERVIEW_STATUS_VALUES, ATS_STATUS_VALUES, ResumeScanResult
from services.ai_interview_bridge import ai_interview_autosend_enabled, schedule_l1_interview
from services.candidate_comms import interview_link_message, notify_candidate
from services.crm_common import log_activity, paginate, save_upload_hashed
from services.requirements import get_requirement_or_404
from services.resumes import enrich_resumes_with_ai, run_ats_scan, serialize_resume
from services.slot_booking import (
    auto_pipeline_after_scan, find_or_create_candidate_from_resume, get_or_create_profile,
)

router = APIRouter(tags=["CRM: Resumes"])

_UPLOAD_ALLOWED_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)


def _now():
    return datetime.now(timezone.utc)


def _get_resume_or_404(db: Session, resume_id: int) -> Resume:
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume


# ---------------------------------------------------------------- upload + list

@router.post("/api/requirements/{requirement_id}/resumes")
def upload_resume(
    requirement_id: int,
    file: UploadFile = File(...),
    candidate_name: str = Form(..., min_length=1, max_length=255),
    email: str | None = Form(None),
    phone: str | None = Form(None),
    source_portal: str | None = Form(None),
    # Same applicant details as the public apply-link form, so TA-entered
    # uploads carry identical data onto the Resume + Candidate.
    experience: str = Form("", max_length=64),
    education: str = Form("", max_length=120),
    technical_domain: str = Form("", max_length=120),
    skills: str = Form("", max_length=500),
    notice_period: str = Form("", max_length=60),
    current_ctc: str = Form("", max_length=40),
    expected_ctc: str = Form("", max_length=40),
    preferred_location: str = Form("", max_length=120),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    req = get_requirement_or_404(db, requirement_id)
    if req.status not in _UPLOAD_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Resumes can only be uploaded when the requirement is in "
                   f"{', '.join(s.value for s in _UPLOAD_ALLOWED_STATUSES)} "
                   f"(current: {req.status.value})",
        )
    existing = db.execute(
        select(func.count()).select_from(Resume).where(Resume.requirement_id == req.id)
    ).scalar() or 0

    file_url, file_sha256, file_size = save_upload_hashed(file, "resumes")

    # Dedupe: identical file (same bytes) already on this requirement → return it
    # instead of creating a duplicate application.
    dup = db.execute(
        select(Resume).where(Resume.requirement_id == req.id, Resume.file_sha256 == file_sha256)
    ).scalars().first()
    if dup is not None:
        # Same file re-uploaded: still make sure a Candidate exists for it (the
        # original upload may predate immediate candidate creation).
        if dup.candidate_id is None:
            try:
                cand = find_or_create_candidate_from_resume(db, dup)
                dup.candidate_id = cand.id
                db.commit()
            except Exception:
                db.rollback()
        return envelope(serialize_resume(dup), message="Duplicate resume — already on this requirement")

    details = {
        "education": (education or "").strip() or None,
        "technical_domain": (technical_domain or "").strip() or None,
        "skills": (skills or "").strip() or None,
        "notice_period": (notice_period or "").strip() or None,
        "current_ctc": (current_ctc or "").strip() or None,
        "expected_ctc": (expected_ctc or "").strip() or None,
        "preferred_location": (preferred_location or "").strip() or None,
    }
    details = {k: v for k, v in details.items() if v}
    resume = Resume(
        requirement_id=req.id,
        candidate_name=candidate_name.strip(),
        email=(email or "").strip() or None,
        phone=(phone or "").strip() or None,
        source_portal=(source_portal or "").strip() or None,
        applicant_experience=(experience or "").strip() or None,
        application_details=details or None,
        resume_file_url=file_url,
        file_sha256=file_sha256,
        file_size=file_size,
    )
    db.add(resume)
    db.flush()  # assign resume.id before deriving the candidate
    # Create/enrich the Candidate now so the applicant appears in the Candidate
    # tab immediately (name/email/phone/CV; apply-form details when present).
    try:
        from services.slot_booking import find_or_create_candidate_from_resume
        cand = find_or_create_candidate_from_resume(db, resume)
        resume.candidate_id = cand.id
    except Exception:
        pass
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "RESUME_UPLOADED", f"Resume uploaded for {resume.candidate_name}")
    if req.status == RequirementStatus.POSTED_ON_PORTALS and existing == 0:
        req.status = RequirementStatus.IN_PROGRESS
        log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                     "STATUS_CHANGED", "Auto-moved Posted_On_Portals -> In_Progress (first resume received)")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Resume uploaded")


@router.get("/api/requirements/{requirement_id}/resumes")
def list_resumes(
    requirement_id: int,
    ats_status: str | None = None,
    ai_interview_status: str | None = None,
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA", "RMG", "Sales_Head")),
):
    req = get_requirement_or_404(db, requirement_id)
    stmt = select(Resume).where(Resume.requirement_id == req.id)
    if ats_status:
        if ats_status not in ATS_STATUS_VALUES:
            raise HTTPException(status_code=400, detail=f"Invalid ats_status filter '{ats_status}'")
        stmt = stmt.where(Resume.ats_status == AtsStatus(ats_status))
    if ai_interview_status:
        if ai_interview_status not in AI_INTERVIEW_STATUS_VALUES:
            raise HTTPException(
                status_code=400, detail=f"Invalid ai_interview_status filter '{ai_interview_status}'"
            )
        stmt = stmt.where(Resume.ai_interview_status == AiInterviewStatus(ai_interview_status))
    if p.search:
        like = f"%{p.search}%"
        stmt = stmt.where(or_(Resume.candidate_name.ilike(like), Resume.email.ilike(like)))
    stmt = stmt.order_by(Resume.created_at.desc(), Resume.id.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    return envelope(enrich_resumes_with_ai(db, items), meta=meta)


# --------------------------------------------------------------- edit + delete

class ResumeUpdateIn(BaseModel):
    """Editable applicant details on a resume row (all optional; None = untouched)."""
    candidate_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    source_portal: str | None = Field(default=None, max_length=64)
    experience: str | None = Field(default=None, max_length=64)
    education: str | None = Field(default=None, max_length=120)
    technical_domain: str | None = Field(default=None, max_length=120)
    skills: str | None = Field(default=None, max_length=500)
    notice_period: str | None = Field(default=None, max_length=60)
    current_ctc: str | None = Field(default=None, max_length=40)
    expected_ctc: str | None = Field(default=None, max_length=40)
    preferred_location: str | None = Field(default=None, max_length=120)

_DETAIL_KEYS = ("education", "technical_domain", "skills", "notice_period",
                "current_ctc", "expected_ctc", "preferred_location")


@router.put("/api/resumes/{resume_id}")
def update_resume(
    resume_id: int,
    payload: ResumeUpdateIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    resume = _get_resume_or_404(db, resume_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "candidate_name" in changes and changes["candidate_name"]:
        resume.candidate_name = changes["candidate_name"].strip()
    if "email" in changes:
        resume.email = (changes["email"] or "").strip() or None
    if "phone" in changes:
        resume.phone = (changes["phone"] or "").strip() or None
    if "source_portal" in changes:
        resume.source_portal = (changes["source_portal"] or "").strip() or None
    if "experience" in changes:
        resume.applicant_experience = (changes["experience"] or "").strip() or None
    details = dict(resume.application_details or {})
    detail_changed = False
    for k in _DETAIL_KEYS:
        if k in changes:
            v = (changes[k] or "").strip()
            if v:
                details[k] = v
            else:
                details.pop(k, None)
            detail_changed = True
    if detail_changed:
        resume.application_details = details or None
    log_activity(db, RequirementActivityLog, "requirement_id", resume.requirement_id, user.id,
                 "RESUME_UPDATED", f"Resume details updated for {resume.candidate_name}")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Resume updated")


@router.delete("/api/resumes/{resume_id}")
def delete_resume(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    """Delete a resume/application. Unlinks AI-interview rows and removes slot
    bookings for this resume; the Candidate record (if created) is kept."""
    from models import AiInterviewLink, SlotBooking

    resume = _get_resume_or_404(db, resume_id)
    name = resume.candidate_name
    req_id = resume.requirement_id
    for link in db.execute(
        select(AiInterviewLink).where(AiInterviewLink.resume_id == resume.id)
    ).scalars().all():
        link.resume_id = None
    for booking in db.execute(
        select(SlotBooking).where(SlotBooking.resume_id == resume.id)
    ).scalars().all():
        db.delete(booking)
    db.delete(resume)
    log_activity(db, RequirementActivityLog, "requirement_id", req_id, user.id,
                 "RESUME_DELETED", f"Resume deleted for {name}")
    db.commit()
    return envelope(data={"id": resume_id}, message="Resume deleted")


# ---------------------------------------------------------------- ATS scanning

@router.post("/api/resumes/{resume_id}/ats-scan")
def ats_scan(
    resume_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    resume = _get_resume_or_404(db, resume_id)
    req = get_requirement_or_404(db, resume.requirement_id)
    result = run_ats_scan(db, resume, req, user.id)
    # Auto-threshold pipeline (auto-shortlist + slot invite) — never raises.
    auto = auto_pipeline_after_scan(db, resume, req, user.id, _base_url(request))
    db.commit()
    db.refresh(resume)
    data = serialize_resume(resume)
    data["auto_shortlisted"] = bool(auto.get("auto_shortlisted"))
    data["slot_invite_sent"] = bool(auto.get("slot_invite_sent"))
    data["auto_action"] = auto
    return envelope(data, message=f"ATS scan complete: {result['ats_score']}/100")


@router.post("/api/requirements/{requirement_id}/resumes/scan-all")
def scan_all_resumes(
    requirement_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    req = get_requirement_or_404(db, requirement_id)
    base_url = _base_url(request)
    pending = db.execute(
        select(Resume)
        .where(Resume.requirement_id == req.id, Resume.ats_status == AtsStatus.PENDING_SCAN)
        .order_by(Resume.id.asc())
    ).scalars().all()
    results: list[dict] = []
    scored = failed = 0
    for resume in pending:
        try:
            outcome = run_ats_scan(db, resume, req, user.id)
            # Auto-threshold pipeline (auto-shortlist + slot invite) — never raises.
            auto = auto_pipeline_after_scan(db, resume, req, user.id, base_url)
            scored += 1
            results.append(ResumeScanResult(
                resume_id=resume.id, candidate_name=resume.candidate_name,
                status="Scored", ats_score=outcome["ats_score"],
                auto_shortlisted=bool(auto.get("auto_shortlisted")),
                slot_invite_sent=bool(auto.get("slot_invite_sent")),
            ).model_dump())
        except HTTPException as exc:
            failed += 1
            results.append(ResumeScanResult(
                resume_id=resume.id, candidate_name=resume.candidate_name,
                status="Failed", error=str(exc.detail),
            ).model_dump())
        except Exception as exc:  # keep going on unexpected per-file errors
            failed += 1
            results.append(ResumeScanResult(
                resume_id=resume.id, candidate_name=resume.candidate_name,
                status="Failed", error=f"Unexpected error: {exc}",
            ).model_dump())
    db.commit()
    return envelope(
        {"total_pending": len(pending), "scored": scored, "failed": failed, "results": results},
        message=f"Scanned {scored} of {len(pending)} pending resume(s); {failed} failed",
    )


# ---------------------------------------------------------------- shortlist / reject

@router.post("/api/resumes/{resume_id}/shortlist")
def shortlist_resume(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    resume = _get_resume_or_404(db, resume_id)
    if resume.ats_score is None:
        raise HTTPException(status_code=400, detail="Resume must be ATS-scanned before shortlisting")
    resume.ats_status = AtsStatus.SHORTLISTED
    resume.screened_by = resume.screened_by or user.id
    log_activity(db, RequirementActivityLog, "requirement_id", resume.requirement_id, user.id,
                 "RESUME_SHORTLISTED", f"Resume shortlisted for {resume.candidate_name}")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Resume shortlisted")


@router.post("/api/resumes/{resume_id}/reject")
def reject_resume(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    resume = _get_resume_or_404(db, resume_id)
    resume.ats_status = AtsStatus.REJECTED
    resume.screened_by = resume.screened_by or user.id
    log_activity(db, RequirementActivityLog, "requirement_id", resume.requirement_id, user.id,
                 "RESUME_REJECTED", f"Resume rejected for {resume.candidate_name}")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Resume rejected")


# ---------------------------------------------------------------- AI L1 scheduling
# Candidate/profile find-or-create now lives in services/slot_booking.py — it is
# shared with the public slot-confirmation flow (routers/crm/slots.py).

@router.post("/api/resumes/{resume_id}/schedule-ai-interview")
def schedule_ai_interview(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    resume = _get_resume_or_404(db, resume_id)
    if resume.ats_status != AtsStatus.SHORTLISTED:
        raise HTTPException(
            status_code=400,
            detail=f"Only Shortlisted resumes can be scheduled for AI interview "
                   f"(current ats_status: {resume.ats_status.value})",
        )
    req = get_requirement_or_404(db, resume.requirement_id)

    candidate = find_or_create_candidate_from_resume(db, resume)
    profile = get_or_create_profile(db, candidate, req)

    bridge = schedule_l1_interview(db, candidate, req, profile, resume=resume, scheduled_by=user.id)
    if not bridge.get("scheduled"):
        raise HTTPException(status_code=502, detail=f"AI interview scheduling failed: {bridge.get('error')}")

    resume.candidate_id = candidate.id
    resume.ai_interview_status = AiInterviewStatus.SCHEDULED
    resume.ai_interview_scheduled_at = _now()
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "AI_L1_SCHEDULED", f"AI_L1_SCHEDULED for {resume.candidate_name}")

    # Best-effort notify only when AI_INTERVIEW_AUTOSEND is enabled (default: off).
    notified = {"email": False, "whatsapp": False}
    if ai_interview_autosend_enabled():
        when_text = resume.ai_interview_scheduled_at.strftime("%A, %d %B %Y at %H:%M UTC")
        msg = interview_link_message(resume.candidate_name, req.title, when_text,
                                     bridge.get("invite_url", ""), bridge.get("access_key", ""))
        notified = notify_candidate(resume.email, resume.phone, msg["subject"], msg["text"], msg["html"],
                                    db=db, event="candidate.interview_link", actor=user,
                                    to_name=resume.candidate_name)

    db.commit()
    db.refresh(resume)
    return envelope(
        {
            "resume_id": resume.id,
            "candidate_id": candidate.id,
            "profile_id": profile.id,
            "candidate_name": resume.candidate_name,
            "candidate_email": resume.email,
            "ai_interview_status": resume.ai_interview_status.value,
            "scheduled": bool(bridge.get("scheduled")),
            "session_ref": bridge.get("session_ref"),
            "invite_url": bridge.get("invite_url", ""),
            "access_key": bridge.get("access_key", ""),
            "job_id": bridge.get("job_id", ""),
            "notified": notified,
            "autosend": ai_interview_autosend_enabled(),
            "resume": serialize_resume(resume),
        },
        message=(
            f"AI L1 interview ready for {resume.candidate_name} — copy the invite link to share"
            if not ai_interview_autosend_enabled()
            else f"AI L1 interview scheduled for {resume.candidate_name}"
        ),
    )
