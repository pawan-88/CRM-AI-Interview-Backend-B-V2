"""Candidate master CRUD: candidates + education + experience + skills + CV upload.

Write access: TA / RMG / Sales / HR (Admin implicit). Reads: any CRM role.
"""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pydantic import BaseModel

from crm_deps import CurrentUser, PageParams, any_crm_role, get_crm_db, page_params, role_required
from models import (
    Candidate, CandidateEducation, CandidateExperience, CandidateSkill,
    Requirement, RequirementActivityLog, RequirementStatus, Resume, Skill,
)
from schemas.candidates import (
    CandidateCreate, CandidateUpdate, EducationCreate, EducationUpdate, ExperienceCreate,
    ExperienceUpdate, SkillSetIn,
)
from schemas.common import envelope
from services.candidates import (
    apply_cv_profile_to_candidate, candidate_detail, candidate_search_clause, candidate_to_dict,
    education_to_dict, ensure_skills_exist, experience_to_dict, get_candidate_or_404,
)
from services.crm_common import log_activity, paginate, save_upload

router = APIRouter(prefix="/api/candidates", tags=["CRM: Candidates"])

write_roles = role_required("TA", "RMG", "Sales", "Sales_Head", "HR")


def _email_taken(db: Session, email: str, exclude_id: int | None = None) -> bool:
    stmt = select(Candidate.id).where(func.lower(Candidate.email) == email.lower())
    if exclude_id is not None:
        stmt = stmt.where(Candidate.id != exclude_id)
    return db.execute(stmt).first() is not None


# ---------------------------------------------------------------------------
# Candidate CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_candidates(pp: PageParams = Depends(page_params),
                    skill_id: int | None = None,
                    technical_domain: str | None = None,
                    has_cv: bool | None = None,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(any_crm_role)):
    stmt = select(Candidate)
    if pp.search:
        # Full-name aware: "anand kumar" matches first_name + last_name together.
        stmt = stmt.where(candidate_search_clause(pp.search))
    if skill_id is not None:
        stmt = stmt.where(Candidate.id.in_(
            select(CandidateSkill.candidate_id).where(CandidateSkill.skill_id == skill_id)))
    if technical_domain:
        stmt = stmt.where(Candidate.technical_domain.ilike(f"%{technical_domain.strip()}%"))
    if has_cv is not None:
        # Only ~30% of imported candidates have a CV on file (the rest still need
        # re-downloading from Zoho), so being able to filter to them matters.
        attached = sa.and_(Candidate.cv_url.isnot(None), Candidate.cv_url != "")
        stmt = stmt.where(attached if has_cv else sa.not_(attached))
    order = Candidate.id.asc() if pp.sort_dir == "asc" else Candidate.id.desc()
    stmt = stmt.order_by(order)
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    return envelope(data=[candidate_to_dict(c) for c in items], meta=meta)


@router.post("")
def create_candidate(payload: CandidateCreate,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    if _email_taken(db, payload.email):
        raise HTTPException(status_code=409,
                            detail=f"A candidate with email '{payload.email}' already exists")
    candidate = Candidate(**payload.model_dump())
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return envelope(data=candidate_to_dict(candidate), message="Candidate created")


@router.get("/{candidate_id}")
def get_candidate(candidate_id: int,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(any_crm_role)):
    candidate = get_candidate_or_404(db, candidate_id)
    return envelope(data=candidate_detail(db, candidate))


@router.put("/{candidate_id}")
def update_candidate(candidate_id: int, payload: CandidateUpdate,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    updates = payload.model_dump(exclude_unset=True)
    new_email = updates.get("email")
    if new_email and _email_taken(db, new_email, exclude_id=candidate.id):
        raise HTTPException(status_code=409,
                            detail=f"A candidate with email '{new_email}' already exists")
    for field, value in updates.items():
        setattr(candidate, field, value)
    db.commit()
    db.refresh(candidate)
    return envelope(data=candidate_to_dict(candidate), message="Candidate updated")


@router.delete("/{candidate_id}")
def delete_candidate(candidate_id: int,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    from models import CandidateOutreach, Resume
    from services.crm_common import commit_or_conflict
    from services.crm_delete import cascade_candidate_children

    candidate = get_candidate_or_404(db, candidate_id)
    # Cascade profiles, AI interview links, and slot bookings owned by this candidate.
    cascade_candidate_children(db, candidate.id)
    for row in db.execute(
        select(CandidateOutreach).where(CandidateOutreach.candidate_id == candidate.id)
    ).scalars().all():
        db.delete(row)
    for resume in db.execute(
        select(Resume).where(Resume.candidate_id == candidate.id)
    ).scalars().all():
        resume.candidate_id = None
    db.delete(candidate)
    commit_or_conflict(db, "Cannot delete: candidate is still referenced by other records.")
    return envelope(message="Candidate deleted")


# ---------------------------------------------------------------------------
# CV upload
# ---------------------------------------------------------------------------

def _autofill_from_cv(db: Session, candidate: Candidate) -> dict:
    """Parse the candidate's stored CV and fill empty fields + missing child
    records. Best-effort: swallows extraction errors and returns a summary."""
    summary = {"fields": [], "skills": 0, "education": 0, "experience": 0}
    if not candidate.cv_url:
        return summary
    try:
        from ai import parse_cv_profile
        from services.resumes import extract_resume_text
        cv_text = extract_resume_text(candidate.cv_url)
        profile = parse_cv_profile(cv_text)
        summary = apply_cv_profile_to_candidate(db, candidate, profile)
    except Exception:
        pass  # parsing is best-effort — never block the upload / request
    return summary


@router.post("/{candidate_id}/cv")
def upload_cv(candidate_id: int,
              file: UploadFile = File(...),
              db: Session = Depends(get_crm_db),
              user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    candidate.cv_url = save_upload(file, "cv")
    db.flush()
    # Auto-populate empty candidate details (domain, experience, skills, education,
    # experience history, CTC, LinkedIn) from the freshly uploaded CV.
    filled = _autofill_from_cv(db, candidate)
    db.commit()
    return envelope(
        data={"cv_url": candidate.cv_url, "autofilled": filled},
        message="CV uploaded",
    )


class ApplyToRequirementIn(BaseModel):
    requirement_id: int


_APPLY_ALLOWED_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)


@router.post("/{candidate_id}/apply")
def apply_candidate_to_requirement(
    candidate_id: int,
    payload: ApplyToRequirementIn,
    db: Session = Depends(get_crm_db),
    # Sourcing is TA's job — only TA (and Admin/CEO implicitly) may fast-track apply.
    user: CurrentUser = Depends(role_required("TA")),
):
    """TA fast-track: apply an existing candidate directly to a requirement.

    Reuses the candidate's stored CV and profile details to create the
    application (Resume row) — no re-upload/re-typing — then best-effort runs
    the ATS scan so the score shows immediately in the requirement's Resumes tab.
    """
    candidate = get_candidate_or_404(db, candidate_id)
    req = db.get(Requirement, payload.requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    if req.status not in _APPLY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Candidates can only be applied while the requirement is sourcing "
                   f"(current: {req.status.value})",
        )
    if not candidate.cv_url:
        raise HTTPException(status_code=400,
                            detail="Candidate has no CV on file — upload a CV on the candidate first.")
    dup = db.execute(
        select(Resume.id).where(Resume.requirement_id == req.id,
                                Resume.candidate_id == candidate.id)
    ).first()
    if dup:
        raise HTTPException(status_code=409,
                            detail="This candidate has already been applied to this requirement.")

    # Application details assembled from the candidate's profile.
    skill_names = db.execute(
        select(Skill.name).join(CandidateSkill, CandidateSkill.skill_id == Skill.id)
        .where(CandidateSkill.candidate_id == candidate.id)
    ).scalars().all()
    first_edu = db.execute(
        select(CandidateEducation.course)
        .where(CandidateEducation.candidate_id == candidate.id)
        .order_by(CandidateEducation.id).limit(1)
    ).scalar_one_or_none()
    details = {
        "education": first_edu,
        "technical_domain": candidate.technical_domain,
        "skills": ", ".join(skill_names) or None,
        "current_ctc": (str(candidate.current_ctc) if getattr(candidate, "current_ctc", None) is not None else None),
        "expected_ctc": (str(candidate.expected_ctc) if candidate.expected_ctc is not None else None),
    }
    details = {k: v for k, v in details.items() if v}
    exp_years = getattr(candidate, "experience_years", None)

    full_name = " ".join(p for p in (candidate.first_name, candidate.last_name) if p) or "Candidate"
    resume = Resume(
        requirement_id=req.id,
        candidate_id=candidate.id,
        candidate_name=full_name,
        email=candidate.email,
        phone=candidate.phone,
        source_portal="TA Sourced",
        applicant_experience=(str(exp_years) if exp_years is not None else None),
        application_details=details or None,
        resume_file_url=candidate.cv_url,
    )
    db.add(resume)
    db.flush()
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "RESUME_UPLOADED",
                 f"TA applied existing candidate {full_name} directly from the Candidates tab")
    if req.status == RequirementStatus.POSTED_ON_PORTALS:
        req.status = RequirementStatus.IN_PROGRESS

    # Best-effort immediate ATS scan so the score appears without an extra click.
    ats_score = None
    try:
        from services.resumes import run_ats_scan
        result = run_ats_scan(db, resume, req, user.id)
        ats_score = result.get("ats_score")
    except Exception:
        pass  # scan can be run manually from the Resumes tab

    db.commit()
    db.refresh(resume)
    return envelope(
        data={"resume_id": resume.id, "requirement_id": req.id,
              "req_number": req.req_number, "ats_score": ats_score},
        message=f"{full_name} applied to {req.req_number}"
                + (f" — ATS score {ats_score}" if ats_score is not None else ""),
    )


@router.post("/{candidate_id}/resignation-certificate")
def upload_resignation_certificate(candidate_id: int,
                                   file: UploadFile = File(...),
                                   db: Session = Depends(get_crm_db),
                                   user: CurrentUser = Depends(write_roles)):
    """Attach the candidate's resignation / relieving certificate.

    Stored on the CANDIDATE, not the application: a person resigns from one job
    once, whatever number of opportunities they are put forward for. Every
    Candidate Profile for them then shows it, so Sales and Sales Head can see the
    proof without asking TA for the file.
    """
    candidate = get_candidate_or_404(db, candidate_id)
    candidate.resignation_certificate_url = save_upload(file, "resignation")
    # Uploading the certificate is itself the statement that they have resigned.
    if not candidate.resignation_status:
        candidate.resignation_status = True
    db.commit()
    db.refresh(candidate)
    return envelope(
        data={
            "resignation_certificate_url": candidate.resignation_certificate_url,
            "resignation_status": bool(candidate.resignation_status),
        },
        message="Resignation certificate uploaded",
    )


@router.delete("/{candidate_id}/resignation-certificate")
def remove_resignation_certificate(candidate_id: int,
                                   db: Session = Depends(get_crm_db),
                                   user: CurrentUser = Depends(write_roles)):
    """Detach the certificate (e.g. the wrong file was uploaded).

    Leaves resignation_status alone — the candidate may still have resigned even
    if the document needs replacing.
    """
    candidate = get_candidate_or_404(db, candidate_id)
    candidate.resignation_certificate_url = None
    db.commit()
    return envelope(data={"resignation_certificate_url": None},
                    message="Resignation certificate removed")


@router.post("/{candidate_id}/parse-cv")
def parse_cv(candidate_id: int,
             db: Session = Depends(get_crm_db),
             user: CurrentUser = Depends(write_roles)):
    """Re-parse the candidate's existing CV and fill any still-empty fields /
    missing child records (skills, education, experience). Existing data is kept."""
    candidate = get_candidate_or_404(db, candidate_id)
    if not candidate.cv_url:
        raise HTTPException(status_code=400, detail="No CV on file to parse. Upload a CV first.")
    filled = _autofill_from_cv(db, candidate)
    db.commit()
    return envelope(data={"autofilled": filled}, message="CV parsed")


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------

def _get_education_or_404(db: Session, candidate_id: int, edu_id: int) -> CandidateEducation:
    edu = db.get(CandidateEducation, edu_id)
    if not edu or edu.candidate_id != candidate_id:
        raise HTTPException(status_code=404, detail="Education record not found for this candidate")
    return edu


@router.post("/{candidate_id}/education")
def add_education(candidate_id: int, payload: EducationCreate,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    edu = CandidateEducation(candidate_id=candidate.id, **payload.model_dump())
    db.add(edu)
    db.commit()
    db.refresh(edu)
    return envelope(data=education_to_dict(edu), message="Education added")


@router.put("/{candidate_id}/education/{edu_id}")
def update_education(candidate_id: int, edu_id: int, payload: EducationUpdate,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    edu = _get_education_or_404(db, candidate_id, edu_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(edu, field, value)
    db.commit()
    db.refresh(edu)
    return envelope(data=education_to_dict(edu), message="Education updated")


@router.delete("/{candidate_id}/education/{edu_id}")
def delete_education(candidate_id: int, edu_id: int,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    edu = _get_education_or_404(db, candidate_id, edu_id)
    db.delete(edu)
    db.commit()
    return envelope(message="Education deleted")


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------

def _get_experience_or_404(db: Session, candidate_id: int, exp_id: int) -> CandidateExperience:
    exp = db.get(CandidateExperience, exp_id)
    if not exp or exp.candidate_id != candidate_id:
        raise HTTPException(status_code=404, detail="Experience record not found for this candidate")
    return exp


@router.post("/{candidate_id}/experience")
def add_experience(candidate_id: int, payload: ExperienceCreate,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    exp = CandidateExperience(candidate_id=candidate.id, **payload.model_dump())
    db.add(exp)
    db.commit()
    db.refresh(exp)
    return envelope(data=experience_to_dict(exp), message="Experience added")


@router.put("/{candidate_id}/experience/{exp_id}")
def update_experience(candidate_id: int, exp_id: int, payload: ExperienceUpdate,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    exp = _get_experience_or_404(db, candidate_id, exp_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(exp, field, value)
    db.commit()
    db.refresh(exp)
    return envelope(data=experience_to_dict(exp), message="Experience updated")


@router.delete("/{candidate_id}/experience/{exp_id}")
def delete_experience(candidate_id: int, exp_id: int,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    exp = _get_experience_or_404(db, candidate_id, exp_id)
    db.delete(exp)
    db.commit()
    return envelope(message="Experience deleted")


@router.post("/{candidate_id}/experience/{exp_id}/certificate")
def upload_experience_certificate(candidate_id: int, exp_id: int,
                                  file: UploadFile = File(...),
                                  db: Session = Depends(get_crm_db),
                                  user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    exp = _get_experience_or_404(db, candidate_id, exp_id)
    exp.certificate_url = save_upload(file, "certificates")
    db.commit()
    return envelope(data={"certificate_url": exp.certificate_url}, message="Certificate uploaded")


# ---------------------------------------------------------------------------
# Skills (replace full set)
# ---------------------------------------------------------------------------

@router.post("/{candidate_id}/skills")
def replace_skills(candidate_id: int, payload: SkillSetIn,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    skill_ids = ensure_skills_exist(db, payload.skill_ids)
    db.execute(sa.delete(CandidateSkill).where(CandidateSkill.candidate_id == candidate.id))
    for sid in skill_ids:
        db.add(CandidateSkill(candidate_id=candidate.id, skill_id=sid))
    db.commit()
    return envelope(data={"candidate_id": candidate.id, "skill_ids": skill_ids},
                    message="Candidate skills replaced")
