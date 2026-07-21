"""Automated candidate pipeline tables: interview slots, slot bookings, outreach log.

InterviewSlot   — TA-published interview windows on a requirement (capacity-based).
SlotBooking     — one per resume: an opaque-token public booking link the candidate
                  uses to pick a slot; once confirmed it stores the AI-interview
                  invite token/url created for that candidate.
CandidateOutreach — free-form contact log (Call / Email / WhatsApp / LinkedIn / ...).
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK


class InterviewSlot(Base):
    __tablename__ = "interview_slots"
    id = sa.Column(sa.Integer, primary_key=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    slot_at = sa.Column(sa.DateTime(timezone=True), nullable=False)
    capacity = sa.Column(sa.Integer, nullable=False, server_default="1")
    booked_count = sa.Column(sa.Integer, nullable=False, server_default="0")
    created_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    requirement = relationship("Requirement")


class SlotBooking(Base):
    __tablename__ = "slot_bookings"
    id = sa.Column(sa.Integer, primary_key=True)
    # Opaque HMAC-style token (secrets.token_urlsafe) used in the public /book/{token} URL.
    token = sa.Column(sa.String(64), nullable=False, unique=True)
    resume_id = sa.Column(sa.Integer, sa.ForeignKey("resumes.id"), nullable=False, unique=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=True)
    slot_id = sa.Column(sa.Integer, sa.ForeignKey("interview_slots.id"), nullable=True)
    # Pending / Confirmed / Expired / Cancelled
    status = sa.Column(sa.String(16), nullable=False, server_default="Pending")
    # Set once the AI interview session is created on confirmation.
    invite_token = sa.Column(sa.String(64), nullable=True)
    invite_url = sa.Column(sa.String(512), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    confirmed_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    resume = relationship("Resume")
    slot = relationship("InterviewSlot")


class CandidateOutreach(Base):
    __tablename__ = "candidate_outreach"
    id = sa.Column(sa.Integer, primary_key=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=False, index=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=True)
    # Call / Email / WhatsApp / LinkedIn / Other
    channel = sa.Column(sa.String(16), nullable=False)
    note = sa.Column(sa.Text, nullable=False)
    outcome = sa.Column(sa.String(120), nullable=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    candidate = relationship("Candidate")
