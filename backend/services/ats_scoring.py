"""Honest, deterministic ATS resume scorer (pure functions — no DB, fully testable).

Fixes the "random document scores 100%" bug in the previous scorer, which awarded
FULL points for every empty/missing criterion (empty skill pool => full 50, no
optional => full 20, no experience bound => full 15, no location => full 5). A
requirement with no configured skills therefore scored ~90-100% for *any* file.

Design:
  * Gate first — `looks_like_resume` refuses documents that aren't resumes.
  * No free points — score is renormalised over ONLY the criteria that are actually
    configured, so an empty criterion never inflates the score.
  * Fail loud — a requirement with no required skills is a configuration error,
    never a perfect score (`AtsConfigError`).
  * Evidence — every matched skill returns the text snippet proving it.
  * Honest cap — 100% is reachable only when every required skill is matched.
  * JD component — when RMG JD text is present, keyword overlap vs resume is scored
    and weights renormalize; without JD, behavior matches the skills-only scorer.
"""
from __future__ import annotations

import re

# Component weights (out of 100). Only the configured components enter the
# denominator, so missing criteria neither help nor are silently ignored.
MANDATORY_POOL = 50.0
OPTIONAL_POOL = 20.0
EXPERIENCE_POINTS = 15.0
LOCATION_POINTS = 5.0
EDUCATION_POINTS = 10.0
JD_POOL = 20.0

EDUCATION_KEYWORDS = [
    "B.E", "B.Tech", "M.Tech", "BE", "BTech", "MTech",
    "BSc", "MSc", "MCA", "Bachelor", "Master", "Diploma", "PhD",
]
EXPERIENCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs)", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d[\s().-]?){8,}\d")
RESUME_SECTION_KEYWORDS = [
    "experience", "education", "skills", "work history", "employment", "project",
    "responsibilit", "curriculum vitae", "resume", "objective", "summary",
    "internship", "certification", "university", "college", "bachelor", "master",
]
_JD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#-]{2,}")
_JD_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "onto", "over",
    "under", "about", "above", "below", "after", "before", "between", "among",
    "will", "shall", "must", "should", "would", "could", "have", "has", "had",
    "are", "was", "were", "been", "being", "their", "they", "them", "your",
    "our", "you", "who", "what", "when", "where", "which", "while", "than",
    "then", "also", "such", "other", "only", "both", "each", "few", "more",
    "most", "some", "any", "all", "not", "nor", "but", "per", "via", "using",
    "used", "use", "role", "job", "work", "team", "years", "year", "experience",
    "responsibilities", "requirements", "required", "preferred", "nice", "good",
    "strong", "ability", "able", "knowledge", "understanding", "including",
    "related", "etc", "etcetera", "candidate", "candidates", "position",
    "description", "company", "client", "project", "projects", "based",
})


class AtsConfigError(ValueError):
    """Raised when a requirement is not scorable (e.g. no required skills configured)."""


def _word_match(term: str, text: str) -> bool:
    if not term:
        return False
    return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None


def extract_jd_keywords(jd_text: str, max_n: int = 40) -> list[str]:
    """Pull meaningful tokens from an RMG job description for overlap scoring."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _JD_TOKEN_RE.findall(jd_text or ""):
        low = raw.lower()
        if low in _JD_STOPWORDS or low in seen:
            continue
        if low.isdigit():
            continue
        seen.add(low)
        out.append(raw)
        if len(out) >= max_n:
            break
    return out


def detect_experience_years(text: str) -> float | None:
    found = [float(m.group(1)) for m in EXPERIENCE_RE.finditer(text or "")]
    return max(found) if found else None


def evidence_snippet(term: str, text: str, width: int = 70) -> str | None:
    """Return a short snippet of `text` around the first occurrence of `term`."""
    m = re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE)
    if not m:
        return None
    lo = max(0, m.start() - width // 2)
    hi = min(len(text), m.end() + width // 2)
    snippet = re.sub(r"\s+", " ", text[lo:hi]).strip()
    return f"…{snippet}…" if snippet else None


def looks_like_resume(text: str) -> tuple[bool, dict]:
    """Heuristic gate: does this document look like a resume/CV?

    Requires enough length AND at least one strong signal: contact info (email/phone)
    or two or more resume section headings. Returns (is_resume, signals).
    """
    t = (text or "").strip()
    low = t.lower()
    words = t.split()
    has_email = bool(EMAIL_RE.search(t))
    has_phone = bool(PHONE_RE.search(t))
    sections = [k for k in RESUME_SECTION_KEYWORDS if k in low]
    # Length is only a floor against one-liners; the real gate is the strong signal
    # (contact details or multiple résumé sections), which a menu/article lacks.
    long_enough = len(words) >= 25
    strong_signal = has_email or has_phone or len(sections) >= 2
    is_resume = long_enough and strong_signal
    return is_resume, {
        "has_email": has_email,
        "has_phone": has_phone,
        "section_signals": sections[:10],
        "word_count": len(words),
    }


def score_resume_against_requirement(
    text: str,
    mandatory_skills: list[str],
    optional_skills: list[str] | None = None,
    exp_min: float | None = None,
    exp_max: float | None = None,
    city: str | None = None,
    jd_text: str | None = None,
) -> dict:
    """Deterministically score resume `text` against a requirement.

    Renormalises over configured criteria only. Raises AtsConfigError when there
    are no mandatory skills (a bare requirement must not yield a perfect score).
    When `jd_text` is provided, a JD-keyword overlap component is included.
    """
    text = text or ""
    mandatory = [s.strip() for s in (mandatory_skills or []) if s and s.strip()]
    optional = [s.strip() for s in (optional_skills or []) if s and s.strip()]
    if not mandatory:
        raise AtsConfigError("Requirement has no required (mandatory) skills configured; cannot score.")

    earned = 0.0
    possible = 0.0
    matched: list[str] = []
    missing: list[str] = []
    evidence: dict[str, str | None] = {}

    # --- mandatory skills (always scored; denominator >= MANDATORY_POOL) ---
    mand_ok = [(s, _word_match(s, text)) for s in mandatory]
    m_hit = sum(1 for _, ok in mand_ok if ok)
    earned += MANDATORY_POOL * m_hit / len(mandatory)
    possible += MANDATORY_POOL
    for s, ok in mand_ok:
        (matched if ok else missing).append(s)
        if ok:
            evidence[s] = evidence_snippet(s, text)

    # --- optional skills (only counted when configured) ---
    if optional:
        opt_ok = [(s, _word_match(s, text)) for s in optional]
        o_hit = sum(1 for _, ok in opt_ok if ok)
        earned += OPTIONAL_POOL * o_hit / len(optional)
        possible += OPTIONAL_POOL
        for s, ok in opt_ok:
            (matched if ok else missing).append(s)
            if ok:
                evidence[s] = evidence_snippet(s, text)

    # --- experience (only when a bound is configured) ---
    detected_years = detect_experience_years(text)
    exp_match: bool | None
    if exp_min is not None or exp_max is not None:
        possible += EXPERIENCE_POINTS
        exp_match = (
            detected_years is not None
            and (exp_min is None or detected_years >= exp_min)
            and (exp_max is None or detected_years <= exp_max)
        )
        if exp_match:
            earned += EXPERIENCE_POINTS
    else:
        exp_match = None  # not a scored criterion

    # --- location (only when configured) ---
    loc_match: bool | None
    if city:
        possible += LOCATION_POINTS
        loc_match = _word_match(city, text)
        if loc_match:
            earned += LOCATION_POINTS
    else:
        loc_match = None

    # --- education (a universal resume expectation → always in the denominator) ---
    edu_hits = [k for k in EDUCATION_KEYWORDS if _word_match(k, text)]
    possible += EDUCATION_POINTS
    if edu_hits:
        earned += EDUCATION_POINTS

    # --- RMG JD keyword overlap (only when JD text is present) ---
    jd_keywords = extract_jd_keywords(jd_text) if (jd_text or "").strip() else []
    jd_matched: list[str] = []
    jd_missing: list[str] = []
    jd_hit = 0
    if jd_keywords:
        # Prefer tokens not already counted as skill names (still score all JD tokens).
        known = {s.lower() for s in mandatory + optional}
        jd_ok = [(k, _word_match(k, text)) for k in jd_keywords]
        jd_hit = sum(1 for _, ok in jd_ok if ok)
        earned += JD_POOL * jd_hit / len(jd_keywords)
        possible += JD_POOL
        for k, ok in jd_ok:
            (jd_matched if ok else jd_missing).append(k)
            if ok and k.lower() not in known:
                evidence[f"jd:{k}"] = evidence_snippet(k, text)

    total = round(earned / possible * 100, 2) if possible else 0.0
    all_required = m_hit == len(mandatory)
    # Honest cap: only a full required-skill match may reach 100.
    if not all_required and total >= 100.0:
        total = 99.0

    # Light keyword-stuffing signal: many skills claimed but the doc lacks the
    # structure/length of a real resume.
    _is_resume, sig = looks_like_resume(text)
    stuffed = m_hit >= 3 and sig["word_count"] < 60 and len(sig["section_signals"]) < 2

    score_details: dict = {
        "earned_points": round(earned, 2),
        "possible_points": round(possible, 2),
        "mandatory_matched": m_hit,
        "mandatory_total": len(mandatory),
        "all_required_matched": all_required,
        "detected_experience_years": detected_years,
        "location_match": loc_match,
        "education_keywords_found": edu_hits,
        "jd_applied": bool(jd_keywords),
    }
    if jd_keywords:
        score_details.update({
            "jd_keywords_matched": jd_hit,
            "jd_keywords_total": len(jd_keywords),
            "jd_match_ratio": round(jd_hit / len(jd_keywords), 4),
            "jd_pool_points": JD_POOL,
        })

    breakdown: dict = {
        "skills_matched": matched,
        "skills_missing": missing,
        "evidence": evidence,
        "experience_match": exp_match,
        "keyword_stuffing_suspected": stuffed,
        "score_details": score_details,
    }
    if jd_keywords:
        breakdown["jd_keywords_matched"] = jd_matched
        breakdown["jd_keywords_missing"] = jd_missing

    return {
        "valid": True,
        "ats_score": total,
        "breakdown": breakdown,
    }
