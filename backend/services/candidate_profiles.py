"""Candidate profile pipeline: server-side transition map, role authority, serializers.

The transition map and stage-authority map live HERE (server side) — the UI only
renders what GET /api/candidate-profiles/{id} returns in allowed_next_statuses.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser
from models import (
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, InterviewEvent,
    OfferHistory, Opportunity, OpportunityCtcSlab, PipelineStatus, Requirement, Skill,
    SkillEvaluation,
)
from services.crm_common import log_activity

logger = logging.getLogger("karnex.crm.profiles")

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
    # The customer's own ladder: interview happens, then its first round's
    # feedback lands, then its second's, then they shortlist.
    PS.CUSTOMER_INTERVIEW.value: [PS.L1_FEEDBACK.value],
    # Not every customer runs two rounds, so L1 feedback may go straight to a
    # shortlist. Forcing a fictional L2 stage would make the pipeline lie.
    PS.L1_FEEDBACK.value: [PS.L2_FEEDBACK.value, PS.SHORTLISTED.value],
    PS.L2_FEEDBACK.value: [PS.SHORTLISTED.value],
    PS.SHORTLISTED.value: [PS.CUSTOMER_APPROVAL.value],
    PS.CUSTOMER_APPROVAL.value: [PS.PREBOARDING.value],
    PS.PREBOARDING.value: [PS.JOINED.value],
}

#: Allowed BACKWARD moves (send a profile back a stage for another look).
_BACKWARD: dict[str, list[str]] = {
    # Customer Screening can bounce the profile back to the Sales team.
    PS.CUSTOMER_SCREENING.value: [PS.SALES_SCREENING.value],
    # Customer Interviewing can send the profile back to Customer Screening
    # (e.g. interview postponed / another shortlist round needed).
    PS.CUSTOMER_INTERVIEW.value: [PS.CUSTOMER_SCREENING.value],
    # A customer round can be re-run — bounce back to the interview stage
    # rather than stranding the profile on a feedback it has superseded.
    PS.L1_FEEDBACK.value: [PS.CUSTOMER_INTERVIEW.value],
    PS.L2_FEEDBACK.value: [PS.L1_FEEDBACK.value],
}

#: Stage-specific rejection moves.
_STAGE_REJECTIONS: dict[str, list[str]] = {
    PS.RMG_REVIEW.value: [PS.RMG_REJECTED.value],
    PS.SALES_SCREENING.value: [PS.SALES_REJECTED.value],
    # At Customer Screening the drop can be either side: the customer says no
    # (Customer_Rejected) or Sales pulls the submission (Sales_Rejected).
    PS.CUSTOMER_SCREENING.value: [PS.CUSTOMER_REJECTED.value, PS.SALES_REJECTED.value],
    PS.CUSTOMER_INTERVIEW.value: [PS.CUSTOMER_REJECTED.value],
    # Most customer rejections land here — a candidate is usually dropped
    # because of what a round's feedback said.
    PS.L1_FEEDBACK.value: [PS.CUSTOMER_REJECTED.value],
    PS.L2_FEEDBACK.value: [PS.CUSTOMER_REJECTED.value],
    PS.SHORTLISTED.value: [PS.CUSTOMER_REJECTED.value],
    PS.CUSTOMER_APPROVAL.value: [PS.CUSTOMER_REJECTED.value],
}

#: Every non-terminal stage can also end in generic withdrawal/rejection —
#: except stages listed here, whose dropdown is kept to its specific set.
_ALWAYS: list[str] = [PS.SELF_WITHDRAWN.value, PS.REJECTED.value]
_NO_GENERIC: set[str] = {PS.CUSTOMER_SCREENING.value}

#: Full transition map: stage -> ordered list of allowed next statuses.
TRANSITION_MAP: dict[str, list[str]] = {
    stage: _FORWARD[stage]
    + _BACKWARD.get(stage, [])
    + _STAGE_REJECTIONS.get(stage, [])
    + ([] if stage in _NO_GENERIC else _ALWAYS)
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
    # Sales owns the customer relationship, so Sales records what the customer
    # said at each of its rounds.
    PS.L1_FEEDBACK.value: {"Sales", "Sales_Head"},
    PS.L2_FEEDBACK.value: {"Sales", "Sales_Head"},
    PS.SHORTLISTED.value: {"Sales", "Sales_Head"},
    # Sales Head ONLY. Sales puts a candidate into Customer Approval by
    # attaching the offer (rate, joining date); the person who proposes terms
    # should not also be the person who signs them off. Sales Head reviews,
    # edits if needed, and approves — which moves the profile to Preboarding
    # for HR. Sales retaining authority here made the approval a formality
    # they could grant themselves.
    PS.CUSTOMER_APPROVAL.value: {"Sales_Head"},
    PS.PREBOARDING.value: {"HR", "Sales_Head"},
}

#: Stages a profile may not ENTER without meeting a precondition.
#: Enforced in perform_transition, with the reason surfaced to the user.
#:
#: Customer Approval means "these are the terms we are asking the customer to
#: approve". Without an offer on record there are no terms, and Sales Head
#: would be approving an empty proposal.
ENTRY_REQUIREMENTS: dict[str, str] = {
    PS.CUSTOMER_APPROVAL.value: (
        "Record the offer first — the candidate's rate and joining date are what "
        "Sales Head is being asked to approve. Add it on the Offers tab."
    ),
}

#: The 5 rejection/withdrawal states (used by the ?bucket=rejected list filter).
REJECTED_BUCKET: set[str] = {
    PS.SALES_REJECTED.value, PS.RMG_REJECTED.value, PS.CUSTOMER_REJECTED.value,
    PS.SELF_WITHDRAWN.value, PS.REJECTED.value,
}

#: Which pipeline stages each role should SEE in the profiles list.
#:
#: Distinct from STAGE_AUTHORITY, which is about who may *move* a profile. This
#: is about whose work it is to look at. Sales previously saw every profile from
#: Sourcing onward — candidates TA was still sourcing and RMG was still
#: screening — so the list was mostly other people's in-progress work.
#:
#: Sales sees candidates from the moment RMG hands them over. By then the
#: candidate has cleared AI L1 and any L2 round RMG asked for, which is exactly
#: the "L1 and L2 done" list. Their own rejections stay visible so a Sales
#: rejection does not vanish from the person who made it.
_SALES_VISIBLE: set[str] = {
    PS.SALES_SCREENING.value,
    PS.CUSTOMER_SCREENING.value,
    PS.CUSTOMER_INTERVIEW.value,
    PS.L1_FEEDBACK.value,
    PS.L2_FEEDBACK.value,
    PS.SHORTLISTED.value,
    PS.CUSTOMER_APPROVAL.value,
    PS.PREBOARDING.value,
    PS.JOINED.value,
    PS.SALES_REJECTED.value,
    PS.CUSTOMER_REJECTED.value,
    PS.SELF_WITHDRAWN.value,
}

#: Roles whose profile list is scoped. Any role absent from this map sees
#: everything — TA and RMG work across the early stages and need the full view,
#: and Admin/CEO/HR/Finance are unrestricted by design.
PROFILE_VISIBILITY: dict[str, set[str]] = {
    "Sales": _SALES_VISIBLE,
    "Sales_Head": _SALES_VISIBLE,
}


def visible_statuses_for(user: CurrentUser) -> set[str] | None:
    """Pipeline statuses this user may see, or None for "everything".

    A user with several roles sees the union of their roles' scopes, and any
    unscoped role (TA, RMG, HR, Finance) lifts the restriction entirely — a
    Sales person who is also RMG must not lose their RMG view.
    """
    if getattr(user, "is_admin", False):
        return None
    roles = list(getattr(user, "roles", []) or [])
    if not roles:
        return None
    allowed: set[str] = set()
    for role in roles:
        if role not in PROFILE_VISIBILITY:
            return None  # an unscoped role — full visibility
        allowed |= PROFILE_VISIBILITY[role]
    return allowed or None


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


def backfill_profile_commercials(db: Session, profile: CandidateProfile) -> bool:
    """Pre-fill a profile's commercials from their real sources when still empty:

      * Current CTC / Expected CTC  ← the Candidate (captured by TA when adding).
      * CTC Approval Amount         ← the linked opportunity's Candidate CTC Slab
                                      (set by Sales — the approved budget).
      * Hike %                      ← derived from Current → Expected.

    Only NULL fields are filled, so RMG's own edits are never overwritten.
    Idempotent; returns True when something changed (caller commits)."""
    changed = False

    if (profile.current_ctc is None or profile.expected_ctc is None) and profile.candidate_id:
        cand = db.get(Candidate, profile.candidate_id)
        if cand is not None:
            # Second-chance source: the candidate's latest resume application
            # details ("12 LPA" etc, from the apply link / TA upload form).
            # Heal the CANDIDATE record too so every page and role sees it.
            if cand.current_ctc is None or cand.expected_ctc is None:
                from models import Resume
                from services.slot_booking import _ctc_from_str
                resume = db.execute(
                    select(Resume)
                    .where(Resume.candidate_id == cand.id, Resume.application_details.isnot(None))
                    .order_by(Resume.id.desc())
                ).scalars().first()
                d = (resume.application_details or {}) if resume is not None else {}
                if cand.current_ctc is None:
                    parsed = _ctc_from_str(d.get("current_ctc"))
                    if parsed is not None:
                        cand.current_ctc = parsed
                        changed = True
                if cand.expected_ctc is None:
                    parsed = _ctc_from_str(d.get("expected_ctc"))
                    if parsed is not None:
                        cand.expected_ctc = parsed
                        changed = True
            if profile.current_ctc is None and cand.current_ctc is not None:
                profile.current_ctc = cand.current_ctc
                changed = True
            if profile.expected_ctc is None and cand.expected_ctc is not None:
                profile.expected_ctc = cand.expected_ctc
                changed = True

    if profile.ctc_approval_amount is None and profile.opportunity_id:
        slabs = db.execute(
            select(OpportunityCtcSlab)
            .where(OpportunityCtcSlab.opportunity_id == profile.opportunity_id)
            .order_by(OpportunityCtcSlab.position)
        ).scalars().all()
        # Pick the band that matches THIS candidate's experience. Taking the first
        # row gave every candidate on an opportunity the junior-most budget.
        cand = db.get(Candidate, profile.candidate_id)
        slab = select_ctc_slab(slabs, getattr(cand, "experience_years", None) if cand else None)
        if slab is None and slabs:
            slab = slabs[0]  # no experience recorded — fall back to the first band
        if slab is not None and slab.approved_ctc_lac is not None:
            profile.ctc_approval_amount = slab.approved_ctc_lac
            changed = True

    if (profile.hike_percent is None
            and profile.current_ctc is not None and profile.expected_ctc is not None):
        hp = compute_hike_percent(profile.current_ctc, profile.expected_ctc)
        if hp is not None:
            profile.hike_percent = hp
            changed = True

    return changed


#: Who takes over when a profile ARRIVES at each stage. Used to tell the next
#: role there is work waiting for them.
#:
#: This was the missing link in the TA -> RMG -> Sales handoff: RMG was notified
#: when an AI interview passed, but when RMG then forwarded the candidate,
#: nothing told Sales. Sales had to notice by browsing the profiles list, which
#: is how handoffs quietly stall.
_ARRIVAL_NOTIFY_ROLE: dict[str, str] = {
    PS.RMG_REVIEW.value: "RMG",
    PS.SALES_SCREENING.value: "Sales",
    PS.CUSTOMER_SCREENING.value: "Sales",
    PS.CUSTOMER_INTERVIEW.value: "Sales",
    PS.L1_FEEDBACK.value: "Sales",
    PS.L2_FEEDBACK.value: "Sales",
    # Customer Approval is now Sales Head's decision, so it is Sales Head who
    # needs telling that an offer is waiting on them.
    PS.CUSTOMER_APPROVAL.value: "Sales_Head",
    PS.PREBOARDING.value: "HR",
}

#: Human wording for the arrival message, so each role is told what to DO rather
#: than just that a status changed.
_ARRIVAL_ACTION: dict[str, str] = {
    PS.RMG_REVIEW.value: "Review the interview report and decide: request an L2 round, "
                         "or submit to the Sales team.",
    PS.SALES_SCREENING.value: "RMG has cleared this candidate. Review and submit to the customer.",
    PS.CUSTOMER_SCREENING.value: "Submitted to the customer — track the response.",
    PS.CUSTOMER_INTERVIEW.value: "A customer interview is due for this candidate.",
    PS.L1_FEEDBACK.value: "The customer's first-round feedback is in. Record it, then move "
                          "to their second round or straight to a shortlist.",
    PS.L2_FEEDBACK.value: "The customer's second-round feedback is in. Record it, then "
                          "shortlist or reject.",
    PS.CUSTOMER_APPROVAL.value: "Offer terms are ready for your approval. Review the rate "
                                "and joining date, edit if needed, then approve to move "
                                "this candidate into preboarding.",
    PS.PREBOARDING.value: "Approved by Sales Head — start preboarding for this candidate.",
}


def _notify_stage_owner(db: Session, profile: CandidateProfile, previous: str,
                        new_status: str, comment: str, user: CurrentUser) -> None:
    """Tell whoever owns the new stage that a candidate has arrived there.

    Best-effort: a notification failure must never roll back a legitimate status
    change. The candidate has moved either way, and losing the transition would
    be far worse than losing the alert.
    """
    role = _ARRIVAL_NOTIFY_ROLE.get(new_status)
    if not role:
        return
    try:
        from services.notify import notify_role

        name = _candidate_display_name(db, profile)
        notify_role(
            db, role,
            f"{new_status.replace('_', ' ')}: {name}",
            f"{_ARRIVAL_ACTION.get(new_status, 'This candidate needs your attention.')} "
            f"(moved from {previous.replace('_', ' ')} — “{comment}”)",
            f"/admin?view=crm&p=profiles/{profile.id}",
            # Don't notify the person who just made the change.
            exclude_user_id=user.id,
            event="candidate.stage_arrival",
        )
    except Exception:  # pragma: no cover — never break a transition
        logger.warning("Could not notify %s about profile %s", role, profile.id, exc_info=True)


def _candidate_display_name(db: Session, profile: CandidateProfile) -> str:
    try:
        candidate = db.get(Candidate, profile.candidate_id)
        if candidate is None:
            return f"Candidate #{profile.candidate_id}"
        name = " ".join(
            p for p in (candidate.first_name, candidate.last_name) if p
        ).strip()
        return name or f"Candidate #{candidate.id}"
    except Exception:
        return f"Profile #{profile.id}"


def _check_entry_requirement(db: Session, profile: CandidateProfile, new_status: str) -> None:
    """Block entry to a stage whose precondition is unmet, with the reason."""
    reason = ENTRY_REQUIREMENTS.get(new_status)
    if not reason:
        return
    if new_status == PS.CUSTOMER_APPROVAL.value:
        has_offer = db.execute(
            select(OfferHistory.id).where(OfferHistory.profile_id == profile.id).limit(1)
        ).first()
        if not has_offer:
            raise HTTPException(status_code=400, detail=reason)


#: Destination status -> the customer's verdict, in the Interview_Result
#: vocabulary. Only outcomes that genuinely express a decision map; a bounce
#: back to an earlier stage is a reschedule, not a verdict.
_CUSTOMER_VERDICT: dict[str, str] = {
    PS.SHORTLISTED.value: "Hire",
    PS.CUSTOMER_APPROVAL.value: "Hire",
    PS.CUSTOMER_REJECTED.value: "No Hire",
}

#: Arriving at one of these means that customer round's feedback is in, so the
#: text typed on the transition IS that round's feedback. Stored on the round's
#: `stage` column so the customer's first and second rounds stay distinct
#: without inventing new interview kinds.
_FEEDBACK_STAGE_ROUND: dict[str, str] = {
    PS.L1_FEEDBACK.value: "L1",
    PS.L2_FEEDBACK.value: "L2",
}


def _record_customer_round_from_transition(db: Session, profile: CandidateProfile,
                                           previous: str, new_status: str,
                                           feedback: str, user: CurrentUser) -> None:
    """Persist customer feedback typed on a transition as an interview round.

    Two shapes of the same idea:

      * ARRIVING at L1/L2 Feedback — that round's verdict is in, and the text
        is its feedback. Recorded against `stage` L1 or L2.
      * LEAVING the customer's ladder with a decision (shortlist / approve /
        reject) — the text is the closing verdict, applied to the most recent
        customer round.

    Without this the Interviews tab showed RMG's technical rounds and then
    stopped, while the customer's actual words lived only in the activity log.

    Best-effort: bookkeeping must never roll back a legitimate status change.
    """
    try:
        arriving_round = _FEEDBACK_STAGE_ROUND.get(new_status)
        if arriving_round:
            _upsert_customer_round(db, profile, user, stage=arriving_round,
                                   feedback=feedback, verdict=None)
            return

        # Closing decision — only meaningful if the customer actually saw them.
        if previous not in _CUSTOMER_LADDER:
            return
        verdict = _CUSTOMER_VERDICT.get(new_status)
        if not verdict:
            return
        _upsert_customer_round(db, profile, user,
                               stage=_FEEDBACK_STAGE_ROUND.get(previous),
                               feedback=feedback, verdict=verdict)
    except Exception:  # pragma: no cover — never break a transition
        logger.warning("Could not record customer round for profile %s", profile.id, exc_info=True)


#: Stages at which the customer has the candidate in front of them.
_CUSTOMER_LADDER = {
    PS.CUSTOMER_INTERVIEW.value,
    PS.L1_FEEDBACK.value,
    PS.L2_FEEDBACK.value,
}


def _upsert_customer_round(db: Session, profile: CandidateProfile, user: CurrentUser, *,
                           stage: str | None, feedback: str, verdict: str | None) -> None:
    """Create or update the customer round for this stage.

    Matched on (profile, kind, stage) so the customer's L1 and L2 are separate
    rows, and so recording a verdict after the feedback updates the same round
    rather than creating a second one.
    """
    query = (
        select(InterviewEvent)
        .where(InterviewEvent.profile_id == profile.id,
               InterviewEvent.kind == "Customer_Interview")
        .order_by(InterviewEvent.id.desc())
        .limit(1)
    )
    if stage:
        query = (
            select(InterviewEvent)
            .where(InterviewEvent.profile_id == profile.id,
                   InterviewEvent.kind == "Customer_Interview",
                   InterviewEvent.stage == stage)
            .order_by(InterviewEvent.id.desc())
            .limit(1)
        )
    existing = db.execute(query).scalar_one_or_none()

    if existing is not None:
        if verdict:
            existing.result = existing.result or verdict
        existing.status = existing.status or "Completed"
        # Append rather than overwrite — Sales may have written the round up
        # in detail already, and a one-line decision note should not replace it.
        if feedback and feedback not in (existing.feedback or ""):
            existing.feedback = (
                f"{existing.feedback}\n\n{feedback}".strip() if existing.feedback else feedback
            )
        return

    db.add(InterviewEvent(
        profile_id=profile.id,
        candidate_id=profile.candidate_id,
        created_by=user.id,
        kind="Customer_Interview",
        stage=stage,
        interview_category="External",
        status="Completed",
        result=verdict,
        feedback=feedback,
        user_role="Customer",
    ))


#: Minimum length when a note IS required.
MIN_COMMENT_LENGTH = 5


def comment_required_for(current: str, new_status: str) -> bool:
    """Does this transition need a written note?

    Requiring one on EVERY move made the field noise on routine progress —
    "moving to Customer Screening" adds nothing the status change does not
    already say, so people type "ok" to get past it, and the habit devalues
    the notes that matter.

    A note is required where it carries information nothing else records:

      * rejections and withdrawals — why someone was dropped is not
        recoverable from the status alone
      * backward moves — going back is an exception and needs explaining
      * the customer's feedback stages — the note IS the feedback, and it is
        saved as that round's interview record
    """
    if new_status in REJECTED_BUCKET:
        return True
    if new_status in _FEEDBACK_STAGE_ROUND:
        return True
    if new_status in _BACKWARD.get(current, []):
        return True
    # Closing the customer's ladder: the note is the customer's verdict.
    if current in _CUSTOMER_LADDER and new_status in _CUSTOMER_VERDICT:
        return True
    return False


#: Workflow dates the pipeline already knows and should stamp for itself.
#: Each is "the day this profile was handed to that team", so the day of the
#: move is the correct value — asking a human to retype it invites drift.
_STAGE_DATE_STAMPS: dict[str, str] = {
    PS.TECHNICAL_SCREENING.value: "technical_submission_date",
    PS.SALES_SCREENING.value: "sales_submission_date",
    PS.CUSTOMER_SCREENING.value: "customer_submission_date",
}


def _stamp_workflow_dates(db: Session, profile: CandidateProfile, new_status: str) -> None:
    """Fill the workflow dates this transition establishes.

    Only ever fills a blank. Stepping back and forward again must not overwrite
    the first submission date — that original is what turnaround time is
    measured from, and silently resetting it would flatter the numbers.
    """
    field = _STAGE_DATE_STAMPS.get(new_status)
    if field and getattr(profile, field, None) is None:
        setattr(profile, field, date.today())

    # Onboarding is a planned future date, not the date of this move, so it is
    # taken from the offer rather than stamped as today.
    if new_status == PS.PREBOARDING.value and profile.customer_onboarding_date is None:
        joining = db.execute(
            select(OfferHistory.joining_date)
            .where(OfferHistory.profile_id == profile.id,
                   OfferHistory.joining_date.isnot(None))
            .order_by(OfferHistory.offer_date.desc(), OfferHistory.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if joining is not None:
            profile.customer_onboarding_date = joining


def perform_transition(db: Session, profile: CandidateProfile, new_status: str,
                       comment: str | None, user: CurrentUser) -> str:
    """Validate + apply one pipeline transition. Caller commits. Returns the old status."""
    clean_comment = (comment or "").strip()
    current = _status_value(profile.pipeline_status)

    if comment_required_for(current, new_status) and len(clean_comment) < MIN_COMMENT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This move needs a note of at least {MIN_COMMENT_LENGTH} characters — "
                "it is the only record of why."
            ),
        )
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

    _check_entry_requirement(db, profile, new_status)

    profile.pipeline_status = PS(new_status)
    _stamp_workflow_dates(db, profile, new_status)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "STATUS_CHANGE", f"{current} -> {new_status}: {clean_comment}")

    # A customer decision typed here is real interview feedback. Record it as a
    # round so it sits with the others on the Interviews tab rather than being
    # findable only by scrolling the activity log.
    _record_customer_round_from_transition(db, profile, current, new_status, clean_comment, user)

    _notify_stage_owner(db, profile, current, new_status, clean_comment, user)

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


def select_ctc_slab(slabs: list, experience_years):
    """The Candidate CTC Slab band that covers this much experience.

    Opportunities define bands like 5-6 / 7-9 / 10+ years, each with its own
    approved CTC budget. A candidate's budget is the band their experience falls
    into — not simply the first row, which is what the old backfill used.

    Bounds are inclusive. A missing exp_min/exp_max makes that end open, so a
    final "10+" row with no max still catches a 20-year candidate. When the
    experience sits outside every band the nearest edge band is used, so a
    budget is still shown rather than a blank.
    """
    if not slabs:
        return None
    if experience_years is None:
        return None
    try:
        exp = Decimal(str(experience_years))
    except (ArithmeticError, ValueError, TypeError):
        return None

    def lo(s):
        return Decimal(str(s.exp_min)) if s.exp_min is not None else Decimal("-999999")

    def hi(s):
        return Decimal(str(s.exp_max)) if s.exp_max is not None else Decimal("999999")

    for slab in slabs:
        if lo(slab) <= exp <= hi(slab):
            return slab
    # Experience fell in a gap between bands (e.g. 6.8 with bands 5-6 and 7-9),
    # or above the top band. Round DOWN to the highest band the candidate has
    # cleared — they do not earn the 7-9 budget until they actually have 7 years.
    at_or_below = [s for s in slabs if lo(s) <= exp]
    if at_or_below:
        return max(at_or_below, key=lo)
    # Below every band — use the lowest rather than showing nothing.
    return min(slabs, key=lo)


def approved_ctc_budgets(db: Session, profiles: list[CandidateProfile],
                         candidates: dict[int, Candidate]) -> dict[int, dict]:
    """profile.id -> {"approved_ctc_budget", "ctc_slab_band"} for a page of rows.

    Every slab for the page's opportunities is fetched in ONE query — doing it
    per row would be an N+1 on the busiest list in the CRM.
    """
    if not profiles:
        return {}
    opp_ids = {p.opportunity_id for p in profiles if p.opportunity_id}
    if not opp_ids:
        return {}
    slabs_by_opp: dict[int, list] = {}
    for slab in db.execute(
        select(OpportunityCtcSlab)
        .where(OpportunityCtcSlab.opportunity_id.in_(opp_ids))
        .order_by(OpportunityCtcSlab.opportunity_id, OpportunityCtcSlab.position)
    ).scalars().all():
        slabs_by_opp.setdefault(slab.opportunity_id, []).append(slab)

    out: dict[int, dict] = {}
    for profile in profiles:
        cand = candidates.get(profile.candidate_id)
        slab = select_ctc_slab(slabs_by_opp.get(profile.opportunity_id, []),
                               getattr(cand, "experience_years", None) if cand else None)
        if slab is None:
            continue
        band = None
        if slab.exp_min is not None or slab.exp_max is not None:
            low = f"{_num(slab.exp_min):g}" if slab.exp_min is not None else "0"
            band = f"{low}+" if slab.exp_max is None else f"{low}-{_num(slab.exp_max):g}"
        out[profile.id] = {
            "approved_ctc_budget": _num(slab.approved_ctc_lac),
            "ctc_slab_band": band,
        }
    return out


#: Shape returned for a profile with no AI interview, so every row has the same
#: keys and the frontend never has to distinguish "absent" from "not scheduled".
_EMPTY_AI_STATUS: dict = {
    "ai_interview_status": None,
    "ai_interview_result": None,
    "ai_overall_score_percent": None,
    "ai_hr_decision": None,
    "ai_hr_decision_label": None,
    "ai_effective_result": None,
    "ai_is_overridden": False,
    "ai_interview_completed_at": None,
    "ai_report_link": None,
}


def notice_periods_from_applications(db: Session, candidate_ids: set[int]) -> dict[int, str]:
    """Notice period as captured on the application form, per candidate.

    There are two places this value can live:

      * `candidates.notice_period` — the master record, what this list reads
      * `resumes.application_details["notice_period"]` — what the apply form
        writes, and what the Opportunity > Resumes tab displays

    Until recently the apply form never copied its answer onto the candidate,
    so the master field is NULL for everyone who applied online — which is most
    people. That is why the Notice Period column read "—" while the very same
    candidate showed "Notice 30 days" one screen away.

    The forward path is fixed and `scripts/backfill_notice_period.py` repairs
    history, but neither helps a list rendered before the backfill is run. So
    the serialiser falls back to the application answer.

    One query for the whole page, newest application first.
    """
    if not candidate_ids:
        return {}
    from models import Resume

    rows = db.execute(
        select(Resume.candidate_id, Resume.application_details)
        .where(Resume.candidate_id.in_(candidate_ids),
               Resume.application_details.isnot(None))
        .order_by(Resume.candidate_id.asc(), Resume.id.desc())
    ).all()

    out: dict[int, str] = {}
    for candidate_id, details in rows:
        if candidate_id in out or not isinstance(details, dict):
            continue  # first row per candidate is the newest, per the ordering
        value = str(details.get("notice_period") or "").strip()
        if value:
            out[candidate_id] = value[:60]
    return out


def latest_ai_interviews(db: Session, profiles: list[CandidateProfile]) -> dict[int, dict]:
    """Most recent AI L1 outcome per profile, in one query.

    `effective_result` surfaces a recruiter's override when they disagreed with
    the AI's score-threshold verdict, so this list agrees with the candidate
    profile page and the Resumes tab rather than showing a stale "Failed".
    """
    from models import AiInterviewLink
    from models.ai_links import hr_decision_label

    profile_ids = [p.id for p in profiles if p.id]
    if not profile_ids:
        return {}

    rows = db.execute(
        select(AiInterviewLink, Candidate.email)
        .join(Candidate, Candidate.id == AiInterviewLink.candidate_id, isouter=True)
        .where(AiInterviewLink.profile_id.in_(profile_ids))
        # Completed first, then newest — so a finished interview always wins over
        # a later-scheduled one that has not happened yet.
        .order_by(
            AiInterviewLink.profile_id.asc(),
            AiInterviewLink.completed_at.desc().nullslast(),
            AiInterviewLink.id.desc(),
        )
    ).all()

    out: dict[int, dict] = {}
    for link, email in rows:
        if link.profile_id in out:
            continue  # first row per profile is the one we want, per the ordering
        clean_email = (email or "").strip().lower()
        out[link.profile_id] = {
            "ai_interview_status": link.result,
            "ai_interview_result": link.result,
            "ai_overall_score_percent": (
                float(link.overall_score_percent)
                if link.overall_score_percent is not None else None
            ),
            "ai_hr_decision": link.hr_decision,
            "ai_hr_decision_label": hr_decision_label(link.hr_decision),
            "ai_effective_result": link.effective_result,
            "ai_is_overridden": bool(link.hr_decision) and link.effective_result != link.result,
            "ai_interview_completed_at": (
                link.completed_at.isoformat() if link.completed_at else None
            ),
            "ai_report_link": (
                f"/admin?view=candidateReport&cid={clean_email}&iid={link.interview_record_id}"
                if link.interview_record_id and clean_email else None
            ),
        }
    return out


def latest_interviews(db: Session, profiles: list[CandidateProfile]) -> dict[int, dict]:
    """profile.id -> the most recent interview round, for the list columns.

    "Most recent" = latest scheduled_at, falling back to the newest row when a
    round has no date. One query for the whole page, not one per row.
    """
    if not profiles:
        return {}
    ids = [p.id for p in profiles]
    rows = db.execute(
        select(InterviewEvent)
        .where(InterviewEvent.profile_id.in_(ids))
        .order_by(InterviewEvent.profile_id,
                  InterviewEvent.scheduled_at.asc().nullsfirst(),
                  InterviewEvent.id.asc())
    ).scalars().all()
    latest: dict[int, InterviewEvent] = {}
    for ev in rows:
        latest[ev.profile_id] = ev  # ordered ascending, so the last wins
    return {
        pid: {
            "interview_round": ev.kind,
            "interview_status": getattr(ev, "status", None),
            "interview_datetime": _dt(ev.scheduled_at) or getattr(ev, "raw_when", None),
            "interview_result": getattr(ev, "result", None),
            "interview_count": sum(1 for r in rows if r.profile_id == pid),
        }
        for pid, ev in latest.items()
    }


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
        # When the candidate actually applied (Zoho). Falls back to the row's
        # insert time so imported and app-created profiles sort together.
        "applied_on": _dt(getattr(profile, "applied_on", None)) or _dt(profile.created_at),
        "ta_owner_name": getattr(profile, "ta_owner_name", None),
        "ta_owner_id": getattr(profile, "ta_owner_id", None),
        # --- provenance + workflow fields (migration 0059) ------------------
        "source": getattr(profile, "source", None),
        "is_hidden": bool(getattr(profile, "is_hidden", False)),
        "sales_submission_date": _dt(getattr(profile, "sales_submission_date", None)),
        "technical_submission_date": _dt(getattr(profile, "technical_submission_date", None)),
        "customer_submission_date": _dt(getattr(profile, "customer_submission_date", None)),
        "customer_onboarding_date": _dt(getattr(profile, "customer_onboarding_date", None)),
        "commercial_approval_status": getattr(profile, "commercial_approval_status", None),
        "approved_ctc": _num(getattr(profile, "approved_ctc", None)),
        "offer_letter_reference": getattr(profile, "offer_letter_reference", None),
        "resume_url": getattr(profile, "resume_url", None),
        "cv_original_filename": getattr(profile, "cv_original_filename", None),
        "resignation_certificate_url": getattr(profile, "resignation_certificate_url", None),
        "stage": getattr(profile, "stage", None),
        "candidate_pre_status": getattr(profile, "candidate_pre_status", None),
        "employee_ref": getattr(profile, "employee_ref", None),
        "created_by_name": getattr(profile, "created_by_name", None),
        "user_role": getattr(profile, "user_role", None),
        "comments_text": getattr(profile, "comments_text", None),
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
        data["customer_id"] = opportunity.customer_id
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
    # Customer names in one query — the grouped view and the Customer column both
    # need them, and a lookup per row would be an N+1 on the busiest list.
    customer_ids = {o.customer_id for o in opportunities.values() if o.customer_id}
    customers = {
        cid: name
        for cid, name in db.execute(
            select(Customer.id, Customer.name).where(Customer.id.in_(customer_ids))
        ).all()
    } if customer_ids else {}
    budgets = approved_ctc_budgets(db, profiles, candidates)
    interviews = latest_interviews(db, profiles)
    ai_status = latest_ai_interviews(db, profiles)
    # Fallback for candidates whose notice period only exists on their
    # application — see notice_periods_from_applications for why.
    applied_notice = notice_periods_from_applications(db, cand_ids)
    out = []
    for p in profiles:
        data = profile_to_dict(
            p,
            candidate=candidates.get(p.candidate_id),
            opportunity=opportunities.get(p.opportunity_id),
        )
        # Approved CTC budget for this candidate's experience band, from the
        # opportunity's Candidate CTC Slab.
        data.update(budgets.get(p.id, {"approved_ctc_budget": None, "ctc_slab_band": None}))
        # Latest interview round / status / date, for the list columns.
        data.update(interviews.get(p.id, {
            "interview_round": None, "interview_status": None,
            "interview_datetime": None, "interview_result": None, "interview_count": 0,
        }))
        # AI L1 outcome. Previously absent from this endpoint entirely, which is
        # why neither the profiles list nor the Opportunity applicants table
        # showed an AI interview status at all.
        data.update(ai_status.get(p.id, _EMPTY_AI_STATUS))
        # The application's own resume, else fall back to the candidate's CV.
        cand = candidates.get(p.candidate_id)
        data["resume_url"] = getattr(p, "resume_url", None) or (cand.cv_url if cand else None)
        # Resignation certificate lives on the CANDIDATE (a person resigns once),
        # so every application they make shows the same proof. A profile-level
        # override still wins if one was imported.
        data["resignation_certificate_url"] = (
            getattr(p, "resignation_certificate_url", None)
            or (getattr(cand, "resignation_certificate_url", None) if cand else None)
        )
        opp = opportunities.get(p.opportunity_id)
        data["customer_name"] = customers.get(opp.customer_id) if opp else None
        # The candidate record wins when it has a value — a recruiter who typed
        # a notice period by hand is more current than an old application.
        if not (data.get("notice_period") or "").strip():
            data["notice_period"] = applied_notice.get(p.candidate_id)
        data["resignation_status"] = bool(getattr(cand, "resignation_status", False)) if cand else False
        data["last_working_day"] = _dt(getattr(cand, "last_working_day", None)) if cand else None
        out.append(data)
    return out


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

    budget = approved_ctc_budgets(db, [profile], {candidate.id: candidate} if candidate else {})
    data.update(budget.get(profile.id, {"approved_ctc_budget": None, "ctc_slab_band": None}))

    data["offers"] = [offer_to_dict(o) for o in profile.offers]
    data["interview_events"] = interview_events_for_profile(db, profile.id)
    data["allowed_next_statuses"] = allowed_next_statuses_for_user(profile.pipeline_status, user)
    return data


#: Display order for interview rounds — L1 before L2 before customer rounds.
_ROUND_ORDER = {
    "L1_Interview": 1, "L2_F2F": 2, "L3_Interview": 3, "L4_Interview": 4,
    "HR_Interview": 5, "Customer_Interview": 6, "Other": 9,
}


def interview_event_to_dict(event: InterviewEvent) -> dict:
    """One interview round, as the Interviews tab renders it."""
    return {
        "id": event.id,
        "kind": event.kind,
        "scheduled_at": _dt(event.scheduled_at),
        "raw_when": event.raw_when,
        "meeting_link": event.meeting_link,
        "stage": getattr(event, "stage", None),
        "mode": getattr(event, "mode", None),
        "status": getattr(event, "status", None),
        "result": getattr(event, "result", None),
        "interviewer": getattr(event, "interviewer", None),
        "feedback": getattr(event, "feedback", None),
        "interview_category": getattr(event, "interview_category", None),
        "duration_minutes": getattr(event, "duration_minutes", None),
        "user_role": getattr(event, "user_role", None),
        "employee_id": getattr(event, "employee_id", None),
        "note": event.note,
        "created_at": _dt(event.created_at),
    }


def interview_events_for_profile(db: Session, profile_id: int) -> list[dict]:
    """Every human interview round recorded against a profile — L1/L2/L3/L4,
    customer and HR rounds, whether entered in the app or imported from Zoho.

    Ordered oldest-first by the scheduled time so the profile reads as a history;
    rounds with no date (imported without one) sort last, by round type.
    """
    rows = db.execute(
        select(InterviewEvent)
        .where(InterviewEvent.profile_id == profile_id)
        .order_by(InterviewEvent.scheduled_at.asc().nullslast(), InterviewEvent.id.asc())
    ).scalars().all()
    out = [interview_event_to_dict(e) for e in rows]
    out.sort(key=lambda r: (r["scheduled_at"] is None,
                            r["scheduled_at"] or "",
                            _ROUND_ORDER.get(r["kind"], 9)))
    return out
