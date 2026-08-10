"""Pydantic schemas for candidate profiles (candidate x opportunity), evaluations, offers."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class ProfileCreate(BaseModel):
    candidate_id: int
    opportunity_id: int
    current_ctc: float | None = None
    expected_ctc: float | None = None
    commercial_approved: bool | None = None
    ctc_approval_amount: float | None = None


class ProfileUpdate(BaseModel):
    current_ctc: float | None = None
    expected_ctc: float | None = None
    commercial_approved: bool | None = None
    ctc_approval_amount: float | None = None
    # Workflow references. No automated source exists for these — they are
    # numbers issued outside the system — so they have to be typed in, and
    # were previously display-only with nothing anywhere able to set them.
    offer_letter_reference: str | None = None
    employee_ref: str | None = None
    #: Derived from the offer on reaching Pre Onboarding, but plans move.
    customer_onboarding_date: date | None = None


class SkillEvaluationItem(BaseModel):
    """Upsert item — only fields explicitly provided are overwritten."""

    skill_id: int
    required_level: int | None = Field(default=None, ge=1, le=5)
    self_rated: int | None = Field(default=None, ge=1, le=5)
    reviewer_rated: int | None = Field(default=None, ge=1, le=5)


class OfferCreate(BaseModel):
    offer_date: date
    ctc: float
    joining_date: date | None = None
    expiry_date: date | None = None
    offer_letter_url: str | None = None


class ProfileStatusTransitionIn(BaseModel):
    """Status change, optionally carrying the offer that the change requires.

    Customer Approved cannot be entered without an offer on record. Rather than
    sending the user to the Offers tab, creating one, and coming back, the
    offer travels with the move and both are written in one transaction.
    """
    new_status: str
    comment: str | None = None
    #: Only valid when new_status is Customer_Approval.
    offer: OfferCreate | None = None


class OfferUpdate(BaseModel):
    status: str | None = None  # Pending / Accepted / Expired / Rejected
    offer_date: date | None = None
    ctc: float | None = None
    joining_date: date | None = None
    expiry_date: date | None = None
    offer_letter_url: str | None = None
    acceptance_date: date | None = None
