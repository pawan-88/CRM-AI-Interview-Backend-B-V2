"""Candidate profiles (candidate x opportunity): pipeline, evaluations, offers, activity log.

Pipeline transitions are validated server-side in services/candidate_profiles.py
(transition map + per-stage role authority). Reads: any CRM role.
"""
from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, any_crm_role, get_crm_db, page_params, role_required
from models import (
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, Customer,
    InterviewEvent, OfferHistory, OfferStatus, Opportunity, PipelineStatus,
)
from pydantic import BaseModel, Field

from schemas.candidate_profiles import (
    OfferCreate, OfferUpdate, ProfileCreate, ProfileUpdate, SkillEvaluationItem,
)
from schemas.common import StatusTransitionIn, envelope
from services.candidate_profiles import (
    REJECTED_BUCKET, user_may_transition_from, backfill_profile_commercials, compute_hike_percent, enrich_profiles_list,
    get_profile_or_404, interview_event_to_dict, interview_events_for_profile,
    offer_to_dict, perform_transition, profile_detail, profile_to_dict, upsert_skill_evaluations,
    visible_statuses_for,
)
from services.candidates import candidate_search_clause
from services.crm_common import log_activity, paginate
from services.interview_rounds import (
    WRITE_ROLES as INTERVIEW_ROUND_WRITE_ROLES, ensure_may_write_round, get_round_or_404,
    options as interview_round_options_data, round_label, validate_round,
)

router = APIRouter(prefix="/api/candidate-profiles", tags=["CRM: Candidate Profiles"])

create_roles = role_required("TA", "Sales", "RMG")
evaluation_roles = role_required("RMG", "TA", "Sales")
offer_roles = role_required("Sales", "Sales_Head", "HR")
rmg_roles = role_required("RMG")
#: Interview feedback is recorded by RMG (Admin/CEO are implicit in role_required).
interview_round_roles = role_required(*INTERVIEW_ROUND_WRITE_ROLES)


def _status_val(status) -> str:
    return status.value if hasattr(status, "value") else str(status)



#: Multi-level sort: ?sort=customer:asc,pipeline_status:desc,expected_ctc:desc
#: Keys map to a SQL expression; anything not listed here cannot be ordered on
#: (the computed columns — CTC-slab budget, latest interview — have no column).
#: Latest AI L1 score for a profile, as a correlated scalar subquery.
#:
#: The score lives on ai_interview_links, one row per session, so it cannot be
#: a plain column reference — a profile can have several sessions and only the
#: most recent one is the answer. Ordering matches services.candidate_profiles
#: .latest_ai_interviews(): completed first, then newest, so a finished
#: interview always outranks a later-scheduled one that has not happened.
#:
#: This exists because the directory sorts by score by default. Without it the
#: sort key was silently dropped by _parse_sort and the table claimed an order
#: it did not have.
_LATEST_AI_SCORE = (
    select(AiInterviewLink.overall_score_percent)
    .where(AiInterviewLink.profile_id == CandidateProfile.id)
    .order_by(
        AiInterviewLink.completed_at.desc().nullslast(),
        AiInterviewLink.id.desc(),
    )
    .limit(1)
    .correlate(CandidateProfile)
    .scalar_subquery()
)

_SORTABLE = {
    "candidate_name": (Candidate.first_name, Candidate.last_name),
    "email": (Candidate.email,),
    "experience_years": (Candidate.experience_years,),
    "notice_period": (Candidate.notice_period,),
    "ai_interview": (_LATEST_AI_SCORE,),
    "opportunity": (Opportunity.title,),
    "customer": (Customer.name,),
    "pipeline_status": (CandidateProfile.pipeline_status,),
    "current_ctc": (CandidateProfile.current_ctc,),
    "expected_ctc": (CandidateProfile.expected_ctc,),
    "hike_percent": (CandidateProfile.hike_percent,),
    "ctc_approval_amount": (CandidateProfile.ctc_approval_amount,),
    "customer_submission_date": (CandidateProfile.customer_submission_date,),
    "customer_onboarding_date": (CandidateProfile.customer_onboarding_date,),
    "ta_owner_name": (CandidateProfile.ta_owner_name,),
    "applied_on": (CandidateProfile.applied_on,),
    "created_at": (CandidateProfile.created_at,),
}
#: Sort keys needing a join, so it is added once and only when used.
_SORT_NEEDS_CANDIDATE = {"candidate_name", "email", "experience_years", "notice_period"}
_SORT_NEEDS_OPPORTUNITY = {"opportunity", "customer"}
MAX_SORT_LEVELS = 4


def _parse_sort(raw: str | None) -> list[tuple[str, bool]]:
    """"customer:asc,expected_ctc:desc" -> [("customer", False), ("expected_ctc", True)].

    Unknown keys are ignored rather than rejected: a saved layout referencing a
    column that has since been removed should degrade, not 400.
    """
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, direction = part.partition(":")
        key = key.strip()
        if key in _SORTABLE and key not in seen:
            seen.add(key)
            out.append((key, direction.strip().lower() == "desc"))
        if len(out) >= MAX_SORT_LEVELS:
            break
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_profiles(pp: PageParams = Depends(page_params),
                  pipeline_status: str | None = None,
                  opportunity_id: int | None = None,
                  candidate_id: int | None = None,
                  bucket: str | None = None,
                  source: str | None = None,
                  include_hidden: bool = False,
                  sort: str | None = None,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(any_crm_role)):
    stmt = select(CandidateProfile)
    if pp.search:
        # Search candidate name / email / phone without N+1 (join once for the filter).
        # Full-name aware: "anand kumar" matches first_name + last_name together.
        stmt = stmt.join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        stmt = stmt.where(candidate_search_clause(pp.search))
    if pipeline_status:
        # Accepts one status or a comma-separated set, so a reviewer can watch
        # several stages at once ("everything waiting on me") rather than
        # paging through them one at a time.
        valid = {m.value for m in PipelineStatus}
        wanted = [s.strip() for s in pipeline_status.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in valid]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown pipeline_status {', '.join(repr(u) for u in unknown)}. "
                       f"Valid values: {', '.join(sorted(valid))}",
            )
        if wanted:
            stmt = stmt.where(
                CandidateProfile.pipeline_status.in_([PipelineStatus(s) for s in wanted])
            )
    if opportunity_id is not None:
        stmt = stmt.where(CandidateProfile.opportunity_id == opportunity_id)
    if candidate_id is not None:
        stmt = stmt.where(CandidateProfile.candidate_id == candidate_id)
    if source:
        # e.g. ?source=zoho_import to show only imported rows.
        stmt = stmt.where(CandidateProfile.source == source.strip())
    if not include_hidden:
        # is_hidden is NOT NULL with a false default, so this never drops rows
        # that predate the column.
        stmt = stmt.where(CandidateProfile.is_hidden.is_(False))
    if bucket:
        bucket = bucket.strip().lower()
        rejected_enums = [PipelineStatus(v) for v in sorted(REJECTED_BUCKET)]
        if bucket == "rejected":
            stmt = stmt.where(CandidateProfile.pipeline_status.in_(rejected_enums))
        elif bucket == "active":
            stmt = stmt.where(CandidateProfile.pipeline_status.not_in(rejected_enums))
        else:
            raise HTTPException(status_code=400, detail="bucket must be 'active' or 'rejected'")

    # Scope the list to the stages a role actually owns. Sales previously saw
    # every profile from Sourcing onward, including candidates still being
    # screened by TA and RMG — so the list was mostly other people's in-progress
    # work and the genuinely actionable rows were buried.
    visible = visible_statuses_for(user)
    if visible is not None:
        stmt = stmt.where(
            CandidateProfile.pipeline_status.in_([PipelineStatus(v) for v in sorted(visible)])
        )
    # ---- ordering -------------------------------------------------------
    levels = _parse_sort(sort)
    if levels:
        keys = {k for k, _ in levels}
        # Join only what the chosen sort keys actually need. `search` may have
        # joined Candidate already, so guard against joining it twice.
        if keys & _SORT_NEEDS_CANDIDATE and not pp.search:
            stmt = stmt.outerjoin(Candidate, Candidate.id == CandidateProfile.candidate_id)
        if keys & _SORT_NEEDS_OPPORTUNITY:
            stmt = stmt.outerjoin(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
            if "customer" in keys:
                stmt = stmt.outerjoin(Customer, Customer.id == Opportunity.customer_id)
        clauses = []
        for key, desc in levels:
            for col in _SORTABLE[key]:
                clauses.append(col.desc().nullslast() if desc else col.asc().nullslast())
        # Stable tiebreak so pagination never repeats or drops a row.
        stmt = stmt.order_by(*clauses, CandidateProfile.id.desc())
    else:
        stmt = stmt.order_by(
            CandidateProfile.id.asc() if pp.sort_dir == "asc" else CandidateProfile.id.desc()
        )
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
    # Pre-fill commercials from Candidate (TA) + opportunity CTC slab (Sales) when
    # still empty, so RMG sees them without re-keying. Only touches NULL fields.
    changed = backfill_profile_commercials(db, profile)
    # Self-heal: profiles whose AI L1 passed BEFORE the RMG hand-off flow existed
    # are still parked in Technical_Screening — advance them to RMG_Review on
    # open so the RMG decision actions appear.
    if _status_val(profile.pipeline_status) == PipelineStatus.TECHNICAL_SCREENING.value:
        from models import AiInterviewLink
        passed = db.execute(
            select(AiInterviewLink.id).where(
                AiInterviewLink.profile_id == profile.id,
                AiInterviewLink.result == "Passed",
            ).limit(1)
        ).first()
        if passed:
            profile.pipeline_status = PipelineStatus.RMG_REVIEW
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                         "STATUS_CHANGE",
                         "Technical_Screening -> RMG_Review: AI L1 already passed — "
                         "auto-forwarded for RMG review")
            changed = True
    if changed:
        db.commit()
        db.refresh(profile)
    return envelope(data=profile_detail(db, profile, user))


class L2FaceToFaceIn(BaseModel):
    """RMG schedules a face-to-face L2 round (e.g. a Teams call)."""
    scheduled_at: str | None = Field(default=None, max_length=64)   # free-form date/time
    meeting_link: str | None = Field(default=None, max_length=1024)  # Teams/Meet URL
    note: str | None = Field(default=None, max_length=1000)


@router.post("/{profile_id}/l2-face-to-face")
def schedule_l2_face_to_face(
    profile_id: int,
    payload: L2FaceToFaceIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(rmg_roles),
):
    """Record an RMG face-to-face L2 round (candidate + RMG on a Teams call).

    The profile stays in RMG_Review while the round happens; RMG decides after
    the call (submit to Sales / reject). Logs the round, notifies TA to
    coordinate, and best-effort emails the candidate the invite."""
    profile = get_profile_or_404(db, profile_id)
    if _status_val(profile.pipeline_status) != PipelineStatus.RMG_REVIEW.value:
        raise HTTPException(status_code=400,
                            detail="L2 face-to-face can be scheduled only while the profile is in RMG Review")
    when = (payload.scheduled_at or "").strip()
    link = (payload.meeting_link or "").strip()
    note = (payload.note or "").strip()

    # Structured event → powers the interview calendar + the .ics invite.
    from datetime import datetime as _dt
    from models import InterviewEvent
    event_dt = None
    if when:
        try:
            event_dt = _dt.fromisoformat(when)  # datetime-local "YYYY-MM-DDTHH:MM"
        except ValueError:
            event_dt = None
    db.add(InterviewEvent(
        profile_id=profile.id,
        candidate_id=profile.candidate_id,
        kind="L2_F2F",
        scheduled_at=event_dt,
        raw_when=when or None,
        meeting_link=link or None,
        note=note or None,
        created_by=user.id,
    ))

    parts = ["L2 face-to-face round scheduled by RMG"]
    if when:
        parts.append(f"when: {when}")
    if link:
        parts.append(f"meeting: {link}")
    if note:
        parts.append(f"note: {note}")
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "L2_FACE_TO_FACE", " — ".join(parts))

    candidate = db.get(Candidate, profile.candidate_id)
    cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
             if candidate else f"Candidate #{profile.candidate_id}")
    from services.notify import notify_role
    notify_role(db, "TA",
                f"L2 face-to-face scheduled: {cname}",
                " — ".join(parts[1:]) or "RMG will take a face-to-face L2 round.",
                f"/admin?view=crm&p=profiles/{profile.id}", exclude_user_id=user.id)

    # Best-effort email to the candidate with the call details + calendar invite.
    email_sent = False
    if candidate is not None and (candidate.email or "").strip() and "@noemail" not in candidate.email:
        try:
            from services.candidate_comms import build_ics_invite, send_candidate_email
            body = (
                f"Hello {cname},\n\n"
                "Your next interview round (L2) has been scheduled with our engineering team.\n"
                + (f"When: {when}\n" if when else "")
                + (f"Meeting link: {link}\n" if link else "")
                + (f"\n{note}\n" if note else "")
                + "\n— Karnex Recruitment Team\n"
            )
            attachments = None
            if event_dt is not None:
                ics = build_ics_invite(
                    summary=f"L2 Interview — {cname}",
                    starts_at=event_dt,
                    description=(note or "L2 interview round with the engineering team."),
                    location=link or "",
                    uid=f"karnex-l2-{profile.id}",
                )
                attachments = [("interview-invite.ics", ics.encode("utf-8"), "text/calendar")]
            res = send_candidate_email(candidate.email, "Interview invitation — L2 round", body,
                                       attachments=attachments)
            email_sent = bool(res.get("sent"))
        except Exception:
            email_sent = False

    db.commit()
    return envelope(
        data={"profile_id": profile.id, "email_sent": email_sent},
        message="L2 face-to-face recorded — TA notified"
                + (" and candidate emailed" if email_sent else ""),
    )


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
    from models import AiInterviewLink, Employee, InterviewEvent
    from services.crm_common import commit_or_conflict

    profile = get_profile_or_404(db, profile_id)
    # Cascade AI interview links owned by this profile.
    for link in db.execute(
        select(AiInterviewLink).where(AiInterviewLink.profile_id == profile.id)
    ).scalars().all():
        db.delete(link)
    # Interview rounds (L2 face-to-face, customer interviews) have a NOT NULL
    # profile_id with no DB cascade — delete them or the profile delete fails.
    for ev in db.execute(
        select(InterviewEvent).where(InterviewEvent.profile_id == profile.id)
    ).scalars().all():
        db.delete(ev)
    db.flush()
    for emp in db.execute(
        select(Employee).where(Employee.candidate_profile_id == profile.id)
    ).scalars().all():
        emp.candidate_profile_id = None
    db.delete(profile)
    commit_or_conflict(db, "Cannot delete: candidate profile is still referenced by other records.")
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


# --------------------------------------------------------------------------
# Interview rounds — RMG records feedback for every round on this application.
# Vocabulary and validation live in services/interview_rounds.py so the form's
# dropdowns and the server's checks come from one place.
# --------------------------------------------------------------------------

class InterviewRoundIn(BaseModel):
    kind: str = Field(description="Interview Round, e.g. L1_Interview")
    interview_category: str | None = None      # Internal / External
    employee_id: int | None = None             # panel member from the Employees tab
    interviewer: str | None = None             # free text for external panellists
    duration_minutes: int | None = None
    status: str | None = None
    scheduled_at: datetime | None = None       # Interview Date/Time From
    result: str | None = None
    feedback: str | None = None                # Overall Feedback
    user_role: str | None = None
    meeting_link: str | None = None
    stage: str | None = None
    mode: str | None = None


class InterviewRoundUpdate(BaseModel):
    kind: str | None = None
    interview_category: str | None = None
    employee_id: int | None = None
    interviewer: str | None = None
    duration_minutes: int | None = None
    status: str | None = None
    scheduled_at: datetime | None = None
    result: str | None = None
    feedback: str | None = None
    user_role: str | None = None
    meeting_link: str | None = None
    stage: str | None = None
    mode: str | None = None


@router.get("/{profile_id}/interview-rounds/options")
def interview_round_options(profile_id: int,
                            db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(any_crm_role)):
    """Dropdown values + the active employee list for the feedback form.

    Served from the router rather than the Employees tab so the picker does not
    depend on that tab's access template.
    """
    get_profile_or_404(db, profile_id)
    # Pass the user so the round list is narrowed to what they may actually
    # save — offering a choice the save would reject is a trap, not a form.
    return envelope(data=interview_round_options_data(db, user))


@router.get("/{profile_id}/interview-rounds")
def list_interview_rounds(profile_id: int,
                          db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(any_crm_role)):
    profile = get_profile_or_404(db, profile_id)
    return envelope(data=interview_events_for_profile(db, profile.id))


@router.post("/{profile_id}/interview-rounds")
def create_interview_round(profile_id: int, payload: InterviewRoundIn,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(interview_round_roles)):
    profile = get_profile_or_404(db, profile_id)
    values = validate_round(db, payload)
    # The endpoint gate only says "may write SOME round". Which round is decided
    # here: RMG owns the technical ladder, Sales owns the customer conversation.
    ensure_may_write_round(user, values.get("kind"))
    event = InterviewEvent(profile_id=profile.id, candidate_id=profile.candidate_id,
                           created_by=user.id, **values)
    db.add(event)
    db.flush()
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "INTERVIEW_ROUND_ADDED",
                 f"{round_label(event.kind)} recorded"
                 f"{f' — {event.result}' if event.result else ''}"
                 f"{f' (interviewer: {event.interviewer})' if event.interviewer else ''}")
    db.commit()
    db.refresh(event)
    return envelope(data=interview_event_to_dict(event), message="Interview feedback saved")


@router.put("/{profile_id}/interview-rounds/{event_id}")
def update_interview_round(profile_id: int, event_id: int, payload: InterviewRoundUpdate,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(interview_round_roles)):
    profile = get_profile_or_404(db, profile_id)
    event = get_round_or_404(db, profile.id, event_id)
    values = validate_round(db, payload, partial=True)
    # Check the round as it stands AND as it would become, so an edit cannot be
    # used to convert someone else's round into one you own, or yours into theirs.
    ensure_may_write_round(user, event.kind)
    if values.get("kind") and values["kind"] != event.kind:
        ensure_may_write_round(user, values["kind"])
    for field, value in values.items():
        setattr(event, field, value)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "INTERVIEW_ROUND_UPDATED",
                 f"{round_label(event.kind)} updated: {', '.join(sorted(values)) or 'none'}")
    db.commit()
    db.refresh(event)
    return envelope(data=interview_event_to_dict(event), message="Interview feedback updated")


@router.delete("/{profile_id}/interview-rounds/{event_id}")
def delete_interview_round(profile_id: int, event_id: int,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(interview_round_roles)):
    profile = get_profile_or_404(db, profile_id)
    event = get_round_or_404(db, profile.id, event_id)
    ensure_may_write_round(user, event.kind)
    label = round_label(event.kind)
    db.delete(event)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "INTERVIEW_ROUND_DELETED", f"{label} removed")
    db.commit()
    return envelope(data={"id": event_id}, message="Interview round removed")


# --------------------------------------------------------------------------
# Workflow actions surfaced as buttons on the Candidate Profiles page.
# Each one does the real pipeline work rather than just stamping a field.
# --------------------------------------------------------------------------

class SubmitToCustomerIn(BaseModel):
    #: Defaults to today when omitted.
    submitted_on: date | None = None
    comment: str | None = None


@router.post("/{profile_id}/submit-to-customer")
def submit_to_customer(profile_id: int, payload: SubmitToCustomerIn | None = None,
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(role_required("Sales", "Sales_Head"))):
    """Record that this candidate was submitted to the customer.

    Stamps `customer_submission_date` and, when the profile is at Sales Screening
    AND this user has authority over that stage, advances it to Customer
    Screening — the move this action represents. Authority is checked BEFORE
    anything is written, so the date and the stage always agree.
    """
    body = payload or SubmitToCustomerIn()
    profile = get_profile_or_404(db, profile_id)
    when = body.submitted_on or date.today()

    if profile.customer_submission_date and not body.submitted_on:
        raise HTTPException(
            status_code=409,
            detail=f"Already submitted to the customer on {profile.customer_submission_date}. "
                   f"Send submitted_on to change the date.",
        )

    at_sales_screening = (
        _status_val(profile.pipeline_status) == PipelineStatus.SALES_SCREENING.value
    )
    move = at_sales_screening and user_may_transition_from(profile.pipeline_status, user)

    profile.customer_submission_date = when
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "SUBMITTED_TO_CUSTOMER",
                 (body.comment or "").strip() or f"Submitted to the customer on {when}")
    if move:
        perform_transition(db, profile, PipelineStatus.CUSTOMER_SCREENING.value,
                           f"Submitted to the customer on {when}", user)

    db.commit()
    db.refresh(profile)
    return envelope(
        data=profile_detail(db, profile, user),
        message=f"Submitted to the customer on {when}"
                + (" — moved to Customer Screening" if move else ""),
    )
