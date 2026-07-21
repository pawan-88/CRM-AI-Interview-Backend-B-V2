"""Candidate master service helpers: serialization + lookup validation."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    Candidate, CandidateEducation, CandidateExperience, CandidateProfile, CandidateSkill,
    Opportunity, Skill,
)


def _num(value):
    return float(value) if value is not None else None


def _dt(value):
    return value.isoformat() if value is not None else None


def get_candidate_or_404(db: Session, candidate_id: int) -> Candidate:
    candidate = db.get(Candidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


def ensure_skills_exist(db: Session, skill_ids: list[int]) -> list[int]:
    """Validate every skill id exists; 400 with the missing ids otherwise."""
    unique_ids = sorted(set(int(s) for s in skill_ids))
    if not unique_ids:
        return []
    found = set(db.execute(select(Skill.id).where(Skill.id.in_(unique_ids))).scalars().all())
    missing = [s for s in unique_ids if s not in found]
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"Unknown skill id(s): {', '.join(str(m) for m in missing)}")
    return unique_ids


def candidate_to_dict(candidate: Candidate) -> dict:
    return {
        "id": candidate.id,
        "salutation": getattr(candidate, "salutation", None),
        "first_name": candidate.first_name,
        "middle_name": getattr(candidate, "middle_name", None),
        "last_name": candidate.last_name,
        "full_name": " ".join(
            p for p in (candidate.first_name, getattr(candidate, "middle_name", None), candidate.last_name) if p
        ),
        "email": candidate.email,
        "phone": candidate.phone,
        "date_of_birth": _dt(getattr(candidate, "date_of_birth", None)),
        "gender": getattr(candidate, "gender", None),
        "experience_years": _num(getattr(candidate, "experience_years", None)),
        "notice_period": getattr(candidate, "notice_period", None),
        "current_address": candidate.current_address,
        "permanent_address": candidate.permanent_address,
        "technical_domain": candidate.technical_domain,
        "roles": getattr(candidate, "roles", None),
        "designation_id": candidate.designation_id,
        "cv_url": candidate.cv_url,
        "linkedin_url": candidate.linkedin_url,
        "resignation_status": bool(candidate.resignation_status),
        "last_working_day": _dt(candidate.last_working_day),
        "resignation_certificate_url": getattr(candidate, "resignation_certificate_url", None),
        "current_ctc": _num(getattr(candidate, "current_ctc", None)),
        "expected_ctc": _num(candidate.expected_ctc),
        "preferred_location_id": candidate.preferred_location_id,
        "created_at": _dt(candidate.created_at),
        "updated_at": _dt(candidate.updated_at),
    }


def education_to_dict(edu: CandidateEducation) -> dict:
    return {
        "id": edu.id,
        "candidate_id": edu.candidate_id,
        "course": edu.course,
        "institution": edu.institution,
        "start_date": _dt(edu.start_date),
        "end_date": _dt(edu.end_date),
        "certificate_url": edu.certificate_url,
    }


def experience_to_dict(exp: CandidateExperience) -> dict:
    return {
        "id": exp.id,
        "candidate_id": exp.candidate_id,
        "company_name": exp.company_name,
        "job_title": exp.job_title,
        "start_date": _dt(exp.start_date),
        "end_date": _dt(exp.end_date),
        "certificate_url": exp.certificate_url,
        "is_current": bool(exp.is_current),
    }


def candidate_detail(db: Session, candidate: Candidate) -> dict:
    data = candidate_to_dict(candidate)
    data["education"] = [education_to_dict(e) for e in candidate.education]
    data["experience"] = [experience_to_dict(e) for e in candidate.experience]

    skill_rows = db.execute(
        select(CandidateSkill.skill_id, Skill.name)
        .join(Skill, Skill.id == CandidateSkill.skill_id)
        .where(CandidateSkill.candidate_id == candidate.id)
        .order_by(Skill.name)
    ).all()
    data["skills"] = [{"skill_id": sid, "name": name} for sid, name in skill_rows]

    profile_rows = db.execute(
        select(CandidateProfile.id, CandidateProfile.opportunity_id,
               Opportunity.title, CandidateProfile.pipeline_status)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(CandidateProfile.candidate_id == candidate.id)
        .order_by(CandidateProfile.id)
    ).all()
    data["profiles"] = [
        {
            "id": pid,
            "opportunity_id": opp_id,
            "opportunity_title": title,
            "pipeline_status": status.value if hasattr(status, "value") else str(status),
        }
        for pid, opp_id, title, status in profile_rows
    ]
    return data
