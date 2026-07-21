"""Candidate master CRUD: candidates + education + experience + skills + CV upload.

Write access: TA / RMG / Sales / HR (Admin implicit). Reads: any CRM role.
"""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, any_crm_role, get_crm_db, page_params, role_required
from models import (
    Candidate, CandidateEducation, CandidateExperience, CandidateProfile, CandidateSkill,
)
from schemas.candidates import (
    CandidateCreate, CandidateUpdate, EducationCreate, EducationUpdate, ExperienceCreate,
    ExperienceUpdate, SkillSetIn,
)
from schemas.common import envelope
from services.candidates import (
    candidate_detail, candidate_to_dict, education_to_dict, ensure_skills_exist,
    experience_to_dict, get_candidate_or_404,
)
from services.crm_common import paginate, save_upload

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
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(any_crm_role)):
    stmt = select(Candidate)
    if pp.search:
        like = f"%{pp.search}%"
        stmt = stmt.where(sa.or_(
            Candidate.first_name.ilike(like),
            Candidate.last_name.ilike(like),
            Candidate.email.ilike(like),
        ))
    if skill_id is not None:
        stmt = stmt.where(Candidate.id.in_(
            select(CandidateSkill.candidate_id).where(CandidateSkill.skill_id == skill_id)))
    if technical_domain:
        stmt = stmt.where(Candidate.technical_domain.ilike(f"%{technical_domain.strip()}%"))
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
    candidate = get_candidate_or_404(db, candidate_id)
    has_profiles = db.execute(
        select(CandidateProfile.id).where(CandidateProfile.candidate_id == candidate.id).limit(1)
    ).first() is not None
    if has_profiles:
        raise HTTPException(status_code=409,
                            detail="Candidate has linked candidate-profiles and cannot be deleted")
    db.delete(candidate)
    db.commit()
    return envelope(message="Candidate deleted")


# ---------------------------------------------------------------------------
# CV upload
# ---------------------------------------------------------------------------

@router.post("/{candidate_id}/cv")
def upload_cv(candidate_id: int,
              file: UploadFile = File(...),
              db: Session = Depends(get_crm_db),
              user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    candidate.cv_url = save_upload(file, "cv")
    db.commit()
    return envelope(data={"cv_url": candidate.cv_url}, message="CV uploaded")


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
