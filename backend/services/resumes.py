"""Resume services: text extraction + rule-based ATS scoring.

Scoring weights (out of 100):
  - Mandatory skills pool ..... 50 (proportional to matched/total mandatory)
  - Optional skills pool ...... 20 (proportional)
  - Experience match .......... 15 (detected years within [experience_min, experience_max])
  - Location match ............  5 (requirement location city appears in text)
  - Education keywords ........ 10 (any recognised degree keyword)
An empty pool / missing constraint awards its full points (nothing to fail).
"""
from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AtsStatus, Location, Requirement, RequirementSkill, Resume, Skill
from services.crm_common import resolve_crm_file

CRM_FILES_PREFIX = "/api/crm-files/"

EDUCATION_KEYWORDS = [
    "B.E", "B.Tech", "M.Tech", "BE", "BTech", "MTech",
    "BSc", "MSc", "MCA", "Bachelor", "Master", "Diploma", "PhD",
]

EXPERIENCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs)", re.IGNORECASE)

MANDATORY_POOL = 50.0
OPTIONAL_POOL = 20.0
EXPERIENCE_POINTS = 15.0
LOCATION_POINTS = 5.0
EDUCATION_POINTS = 10.0


def _num(v):
    return float(v) if v is not None else None


def _val(v):
    return v.value if hasattr(v, "value") else v


def serialize_resume(r: Resume) -> dict:
    return {
        "id": r.id,
        "requirement_id": r.requirement_id,
        "candidate_id": r.candidate_id,
        "candidate_name": r.candidate_name,
        "email": r.email,
        "phone": r.phone,
        "source_portal": r.source_portal,
        "applicant_experience": r.applicant_experience,
        "application_details": r.application_details,
        "resume_file_url": r.resume_file_url,
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "ats_score": _num(r.ats_score),
        "ats_score_breakdown": r.ats_score_breakdown,
        "ats_status": _val(r.ats_status),
        "screened_by": r.screened_by,
        "ai_interview_status": _val(r.ai_interview_status),
        "ai_interview_scheduled_at": r.ai_interview_scheduled_at.isoformat() if r.ai_interview_scheduled_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        # Filled by enrich_resumes_with_ai when listing / detail-enriching
        "ai_overall_score_percent": None,
        "ai_interview_result": None,
        "ai_report_link": None,
        "ai_interview_record_id": None,
        "profile_id": None,
        "ai_invite_token": None,
        "ai_invite_url": None,
        "ai_access_key": None,
    }


def enrich_resumes_with_ai(db: Session, rows: list[Resume]) -> list[dict]:
    """Attach latest AI L1 score, report link, and invite share details from ai_interview_links."""
    import os

    from auth_db import get_schedule_by_token
    from models import AiInterviewLink, Candidate
    from services.ai_interview_bridge import _legacy_db_target

    data = [serialize_resume(r) for r in rows]
    ids = [r.id for r in rows]
    if not ids:
        return data

    links = db.execute(
        select(AiInterviewLink)
        .where(AiInterviewLink.resume_id.in_(ids))
        .order_by(AiInterviewLink.created_at.desc(), AiInterviewLink.id.desc())
    ).scalars().all()
    latest: dict[int, AiInterviewLink] = {}
    for link in links:
        if link.resume_id is not None and link.resume_id not in latest:
            latest[link.resume_id] = link

    cand_ids = {link.candidate_id for link in latest.values() if link.candidate_id}
    emails_by_cand: dict[int, str] = {}
    if cand_ids:
        for c in db.execute(select(Candidate).where(Candidate.id.in_(cand_ids))).scalars().all():
            if c.email:
                emails_by_cand[c.id] = c.email.strip().lower()

    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    access_by_token: dict[str, str] = {}
    for link in latest.values():
        token = (link.invite_token or "").strip()
        if not token or token in access_by_token:
            continue
        try:
            sched = get_schedule_by_token(_legacy_db_target(), token) or {}
            access_by_token[token] = str(sched.get("access_key") or "")
        except Exception:
            access_by_token[token] = ""

    for d, r in zip(data, rows):
        link = latest.get(r.id)
        if link is None:
            continue
        d["ai_overall_score_percent"] = (
            float(link.overall_score_percent) if link.overall_score_percent is not None else None
        )
        d["ai_interview_result"] = link.result
        d["ai_interview_record_id"] = link.interview_record_id
        d["profile_id"] = link.profile_id
        email = (r.email or "").strip().lower() or emails_by_cand.get(link.candidate_id, "")
        d["ai_report_link"] = (
            f"/admin?view=candidateReport&cid={email}&iid={link.interview_record_id}"
            if link.interview_record_id and email
            else None
        )
        token = (link.invite_token or "").strip()
        if token:
            d["ai_invite_token"] = token
            d["ai_invite_url"] = f"{base}/?invite={token}" if base else f"/?invite={token}"
            d["ai_access_key"] = access_by_token.get(token) or None
        if link.created_at and not d.get("ai_interview_scheduled_at"):
            d["ai_interview_scheduled_at"] = link.created_at.isoformat()
    return data


def extract_resume_text(resume_file_url: str) -> str:
    """Locate the stored file behind /api/crm-files/... and extract plain text.

    Supports .pdf (pypdf), .docx (python-docx, imported lazily) and .txt.
    Raises HTTPException 422 on missing file, unsupported type or empty/failed
    extraction (e.g. scanned image PDFs with no text layer).
    """
    rel = resume_file_url or ""
    if rel.startswith(CRM_FILES_PREFIX):
        rel = rel[len(CRM_FILES_PREFIX):]
    path = resolve_crm_file(rel)
    if path is None:
        raise HTTPException(status_code=422, detail="Resume file not found on server; cannot scan")

    ext = path.suffix.lower()
    text = ""
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif ext == ".docx":
            import docx  # lazy: python-docx may not be installed at module-import time
            document = docx.Document(str(path))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.extend(cell.text for cell in row.cells)
            text = "\n".join(parts)
        elif ext == ".txt":
            text = path.read_text(encoding="utf-8", errors="ignore")
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported resume file type '{ext}'. Supported: .pdf, .docx, .txt",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to extract text from resume: {exc}")

    text = (text or "").strip()
    if not text:
        raise HTTPException(
            status_code=422,
            detail="No text could be extracted from the resume (empty or image-only file)",
        )
    return text


def _word_match(term: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None


def _detect_experience_years(text: str) -> float | None:
    found = [float(m.group(1)) for m in EXPERIENCE_RE.finditer(text)]
    return max(found) if found else None


def run_ats_scan(db: Session, resume: Resume, requirement: Requirement, user_id: int) -> dict:
    """Score a resume against its requirement; mutates the resume row (caller commits).

    Honest scoring (see services/ats_scoring.py):
      1. Gate — the file must parse to text (422) AND look like a resume (422).
      2. Fail loud — a requirement with no required skills is a 400 config error.
      3. The score is renormalised over configured criteria only, so an empty
         criterion never awards free points, and 100 needs every required skill.
      4. When an RMG JD is present (text and/or rmg_jd attachment), JD keyword
         overlap is included in the score and breakdown.
    """
    from models import RequirementAttachment
    from services.ats_scoring import AtsConfigError, looks_like_resume, score_resume_against_requirement

    text = extract_resume_text(resume.resume_file_url)  # raises 422 on empty / image-only
    is_resume, signals = looks_like_resume(text)
    if not is_resume:
        raise HTTPException(
            status_code=422,
            detail="Document does not appear to be a resume (no contact details or résumé sections found).",
        )

    skill_rows = db.execute(
        select(RequirementSkill, Skill.name)
        .join(Skill, Skill.id == RequirementSkill.skill_id)
        .where(RequirementSkill.requirement_id == requirement.id)
    ).all()
    mandatory = [name for rs, name in skill_rows if rs.is_mandatory]
    optional = [name for rs, name in skill_rows if not rs.is_mandatory]

    city = None
    if requirement.location_id:
        loc = db.get(Location, requirement.location_id)
        city = loc.city if loc else None

    jd_parts: list[str] = []
    if (requirement.rmg_jd_text or "").strip():
        jd_parts.append(requirement.rmg_jd_text.strip())
    jd_atts = db.execute(
        select(RequirementAttachment).where(
            RequirementAttachment.requirement_id == requirement.id,
            RequirementAttachment.kind == "rmg_jd",
        )
    ).scalars().all()
    for att in jd_atts:
        try:
            jd_parts.append(extract_resume_text(att.file_url))
        except HTTPException:
            continue
    jd_text = "\n\n".join(jd_parts).strip() or None

    try:
        result = score_resume_against_requirement(
            text, mandatory, optional,
            _num(requirement.experience_min), _num(requirement.experience_max), city,
            jd_text=jd_text,
        )
    except AtsConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    total = result["ats_score"]
    breakdown = result["breakdown"]
    breakdown["parse_confidence"] = "high" if signals["word_count"] >= 120 else "low"
    if jd_text:
        breakdown["jd_text_preview"] = jd_text[:500]

    resume.ats_score = total
    resume.ats_score_breakdown = breakdown
    resume.ats_status = AtsStatus.SCORED
    resume.screened_by = user_id
    return {"ats_score": total, "breakdown": breakdown}
