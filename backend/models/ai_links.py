"""Link table joining AI-interview sessions (legacy interview_schedule /
interview_records, joined by invite_token) to CRM entities. Keeps the legacy
tables untouched while scoping results to candidate + opportunity."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK


class AiInterviewLink(Base):
    __tablename__ = "ai_interview_links"
    id = sa.Column(sa.Integer, primary_key=True)
    invite_token = sa.Column(sa.String(64), nullable=False, unique=True)  # join key to legacy tables
    schedule_id = sa.Column(sa.String(64), nullable=True)                 # interview_schedule.id (uuid)
    interview_record_id = sa.Column(sa.String(64), nullable=True)         # interview_records.id (set on completion)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=False, index=True)
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=False, index=True)
    profile_id = sa.Column(sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False, index=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=True)
    resume_id = sa.Column(sa.Integer, sa.ForeignKey("resumes.id"), nullable=True)
    scheduled_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    level = sa.Column(sa.String(8), nullable=False, server_default="L1")
    overall_score_percent = sa.Column(sa.Numeric(5, 2), nullable=True)
    result = sa.Column(sa.String(16), nullable=False, server_default="Pending")  # Pending/Passed/Failed
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    completed_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    profile = relationship("CandidateProfile")
    candidate = relationship("Candidate")
