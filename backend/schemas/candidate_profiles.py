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


class OfferUpdate(BaseModel):
    status: str | None = None  # Pending / Accepted / Expired / Rejected
    offer_date: date | None = None
    ctc: float | None = None
    joining_date: date | None = None
    expiry_date: date | None = None
    offer_letter_url: str | None = None
    acceptance_date: date | None = None
