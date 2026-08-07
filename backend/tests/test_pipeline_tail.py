"""The end of the pipeline: customer interview → offer → approval → preboarding.

Two rules matter here and both are easy to get wrong:

  * The person who proposes offer terms must not be the person who approves
    them. Sales attaches the offer; Sales Head signs it off.
  * Customer Approval means "these are the terms" — entering it without an
    offer on record asks Sales Head to approve nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import PipelineStatus as PS  # noqa: E402
from services.candidate_profiles import (  # noqa: E402
    ENTRY_REQUIREMENTS, STAGE_AUTHORITY, _ARRIVAL_NOTIFY_ROLE, _CUSTOMER_VERDICT,
    allowed_next_statuses, visible_statuses_for,
)


class FakeUser:
    def __init__(self, roles, is_admin=False):
        self.id = 1
        self.roles = list(roles)
        self.is_admin = is_admin


# ------------------------------------------------------- the approval gate

def test_sales_cannot_approve_its_own_offer():
    """Sales puts the profile INTO Customer Approval; only Sales Head takes it out."""
    assert STAGE_AUTHORITY[PS.CUSTOMER_APPROVAL.value] == {"Sales_Head"}


def test_sales_still_owns_the_stages_before_approval():
    """Narrowing the approval must not strip Sales of its own pipeline."""
    assert "Sales" in STAGE_AUTHORITY[PS.SALES_SCREENING.value]
    assert "Sales" in STAGE_AUTHORITY[PS.CUSTOMER_SCREENING.value]
    assert "Sales" in STAGE_AUTHORITY[PS.SHORTLISTED.value]


def test_approval_leads_to_preboarding():
    assert PS.PREBOARDING.value in allowed_next_statuses(PS.CUSTOMER_APPROVAL.value)


def test_hr_takes_over_at_preboarding():
    assert "HR" in STAGE_AUTHORITY[PS.PREBOARDING.value]
    assert PS.JOINED.value in allowed_next_statuses(PS.PREBOARDING.value)


def test_sales_head_is_told_an_offer_is_waiting():
    """The approver has to know there is something to approve."""
    assert _ARRIVAL_NOTIFY_ROLE[PS.CUSTOMER_APPROVAL.value] == "Sales_Head"


def test_hr_is_told_when_preboarding_starts():
    assert _ARRIVAL_NOTIFY_ROLE[PS.PREBOARDING.value] == "HR"


# ------------------------------------------------------ the offer precondition

def test_customer_approval_requires_an_offer():
    assert PS.CUSTOMER_APPROVAL.value in ENTRY_REQUIREMENTS


def test_the_precondition_explains_itself():
    """An error that only says "not allowed" makes the user guess."""
    reason = ENTRY_REQUIREMENTS[PS.CUSTOMER_APPROVAL.value]
    assert "offer" in reason.lower()
    assert "Offers tab" in reason


def test_no_other_stage_has_a_hidden_precondition():
    """Preconditions are invisible until they fire; keep the set deliberate."""
    assert set(ENTRY_REQUIREMENTS) == {PS.CUSTOMER_APPROVAL.value}


# ------------------------------------------------ customer feedback as a round

def test_shortlisting_records_a_hire_verdict():
    assert _CUSTOMER_VERDICT[PS.SHORTLISTED.value] == "Hire"


def test_customer_rejection_records_a_no_hire_verdict():
    assert _CUSTOMER_VERDICT[PS.CUSTOMER_REJECTED.value] == "No Hire"


def test_a_bounce_back_is_not_a_verdict():
    """Returning to Customer Screening is a reschedule, not the client's answer."""
    assert PS.CUSTOMER_SCREENING.value not in _CUSTOMER_VERDICT


def test_every_verdict_is_a_real_result_value():
    from services.interview_rounds import RESULTS
    for verdict in _CUSTOMER_VERDICT.values():
        assert verdict in RESULTS, verdict


# ------------------------------------------------------- Sales visibility holds

def test_sales_still_sees_the_whole_tail_it_works():
    visible = visible_statuses_for(FakeUser(["Sales"]))
    for stage in (PS.SALES_SCREENING, PS.CUSTOMER_SCREENING, PS.CUSTOMER_INTERVIEW,
                  PS.SHORTLISTED, PS.CUSTOMER_APPROVAL, PS.PREBOARDING, PS.JOINED):
        assert stage.value in visible, stage.value


def test_sales_head_can_see_what_it_must_approve():
    """Losing sight of Customer Approval would make the gate unusable."""
    visible = visible_statuses_for(FakeUser(["Sales_Head"]))
    assert PS.CUSTOMER_APPROVAL.value in visible
