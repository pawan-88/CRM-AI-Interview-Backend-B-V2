"""Candidate profiles (candidate x opportunity), skill evaluations, offers, activity log."""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin, USERS_FK, pg_enum


class PipelineStatus(str, enum.Enum):
    SOURCING = "Sourcing"
    TECHNICAL_SCREENING = "Technical_Screening"
    RMG_REVIEW = "RMG_Review"
    SALES_SCREENING = "Sales_Screening"
    CUSTOMER_SCREENING = "Customer_Screening"
    CUSTOMER_INTERVIEW = "Customer_Interview"
    # The customer's OWN two rounds, recorded by Sales as each verdict lands.
    # Distinct from L1_Interview / L2_F2F, which are RMG's technical rounds
    # much earlier in the pipeline — the customer runs its own ladder after we
    # submit, and the pipeline had no way to show which of those rounds a
    # candidate was waiting on.
    L1_FEEDBACK = "L1_Feedback"
    L2_FEEDBACK = "L2_Feedback"
    SHORTLISTED = "Shortlisted"
    CUSTOMER_APPROVAL = "Customer_Approval"
    PREBOARDING = "Preboarding"
    JOINED = "Joined"
    SALES_REJECTED = "Sales_Rejected"
    RMG_REJECTED = "RMG_Rejected"
    CUSTOMER_REJECTED = "Customer_Rejected"
    SELF_WITHDRAWN = "Self_Withdrawn"
    REJECTED = "Rejected"


class OfferStatus(str, enum.Enum):
    PENDING = "Pending"
    ACCEPTED = "Accepted"
    EXPIRED = "Expired"
    REJECTED = "Rejected"


class CandidateProfile(Base, TimestampMixin):
    __tablename__ = "candidate_profiles"
    id = sa.Column(sa.Integer, primary_key=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=False, index=True)
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=False, index=True)
    current_ctc = sa.Column(sa.Numeric(14, 2), nullable=True)
    expected_ctc = sa.Column(sa.Numeric(14, 2), nullable=True)
    hike_percent = sa.Column(sa.Numeric(6, 2), nullable=True)
    pipeline_status = sa.Column(pg_enum(PipelineStatus, "profile_pipeline_status"), nullable=False,
                                server_default=PipelineStatus.SOURCING.value, index=True)
    commercial_approved = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    ctc_approval_amount = sa.Column(sa.Numeric(14, 2), nullable=True)
    # Durable link back to the Zoho Candidate Profile record — makes re-imports
    # idempotent without depending on (candidate, opportunity).
    zoho_profile_id = sa.Column(sa.String(32), nullable=True)
    # TA who owns this application (Zoho "TA Person"). ta_owner_id is set when the
    # name matches a CRM user; ta_owner_name always keeps the original text.
    ta_owner_name = sa.Column(sa.String(120), nullable=True)
    ta_owner_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True, index=True)
    # When the candidate actually applied (Zoho "Added Time"); created_at is the row's
    # insert time, which for imported rows is the import run, not the application date.
    applied_on = sa.Column(sa.DateTime(timezone=True), nullable=True)

    # --- provenance + visibility (migration 0059) ---------------------------
    #: 'app' for rows created in the CRM, 'zoho_import' for imported ones.
    source = sa.Column(sa.String(32), nullable=True, index=True)
    #: Hide a profile from the lists without deleting it (nothing is ever purged).
    is_hidden = sa.Column(sa.Boolean, nullable=False, server_default=sa.false(), index=True)

    # --- workflow dates -----------------------------------------------------
    sales_submission_date = sa.Column(sa.Date, nullable=True)
    technical_submission_date = sa.Column(sa.Date, nullable=True)
    #: Stamped when the profile is submitted to the customer.
    customer_submission_date = sa.Column(sa.Date, nullable=True)
    customer_onboarding_date = sa.Column(sa.Date, nullable=True)

    # --- approvals / commercials -------------------------------------------
    commercial_approval_status = sa.Column(sa.String(120), nullable=True)
    approved_ctc = sa.Column(sa.Numeric(14, 2), nullable=True)
    offer_letter_reference = sa.Column(sa.String(255), nullable=True)

    # --- documents ----------------------------------------------------------
    #: The resume attached to THIS application (may differ from the candidate's CV).
    resume_url = sa.Column(sa.String(1024), nullable=True)
    cv_original_filename = sa.Column(sa.String(255), nullable=True)
    resignation_certificate_url = sa.Column(sa.String(1024), nullable=True)

    # --- Zoho attributes ----------------------------------------------------
    stage = sa.Column(sa.String(60), nullable=True)              # RMG / Sales / HR
    candidate_pre_status = sa.Column(sa.String(120), nullable=True)
    employee_ref = sa.Column(sa.String(255), nullable=True)
    created_by_name = sa.Column(sa.String(120), nullable=True)
    user_role = sa.Column(sa.String(40), nullable=True)
    comments_text = sa.Column(sa.Text, nullable=True)
    is_archive_ta = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    is_archive_rmg = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    is_archive_sales = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    is_archive_hr = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())

    __table_args__ = (sa.UniqueConstraint("candidate_id", "opportunity_id", name="uq_profile_candidate_opp"),)

    candidate = relationship("Candidate", back_populates="profiles")
    opportunity = relationship("Opportunity")
    skill_evaluations = relationship("SkillEvaluation", back_populates="profile",
                                     cascade="all, delete-orphan")
    offers = relationship("OfferHistory", back_populates="profile", cascade="all, delete-orphan",
                          order_by="OfferHistory.offer_date")
    activity_log = relationship("CandidateProfileActivityLog", back_populates="profile",
                                cascade="all, delete-orphan",
                                order_by="CandidateProfileActivityLog.timestamp")


class SkillEvaluation(Base):
    __tablename__ = "skill_evaluations"
    id = sa.Column(sa.Integer, primary_key=True)
    profile_id = sa.Column(sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False, index=True)
    skill_id = sa.Column(sa.Integer, sa.ForeignKey("skills.id"), nullable=False)
    required_level = sa.Column(sa.Integer, nullable=True)   # 1..5
    self_rated = sa.Column(sa.Integer, nullable=True)       # 1..5
    reviewer_rated = sa.Column(sa.Integer, nullable=True)   # 1..5 (AI interview writes here)
    __table_args__ = (sa.UniqueConstraint("profile_id", "skill_id", name="uq_profile_skill_eval"),)

    profile = relationship("CandidateProfile", back_populates="skill_evaluations")


class OfferHistory(Base):
    __tablename__ = "offer_history"
    id = sa.Column(sa.Integer, primary_key=True)
    profile_id = sa.Column(sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False, index=True)
    offer_date = sa.Column(sa.Date, nullable=False)
    ctc = sa.Column(sa.Numeric(14, 2), nullable=False)
    joining_date = sa.Column(sa.Date, nullable=True)
    offer_letter_url = sa.Column(sa.String(1024), nullable=True)
    acceptance_date = sa.Column(sa.Date, nullable=True)
    expiry_date = sa.Column(sa.Date, nullable=True)
    status = sa.Column(pg_enum(OfferStatus, "offer_status"), nullable=False,
                       server_default=OfferStatus.PENDING.value)

    profile = relationship("CandidateProfile", back_populates="offers")


class CandidateProfileActivityLog(Base):
    __tablename__ = "candidate_profile_activity_log"
    id = sa.Column(sa.Integer, primary_key=True)
    profile_id = sa.Column(sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False, index=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    action_type = sa.Column(sa.String(64), nullable=False)
    comment = sa.Column(sa.Text, nullable=True)
    timestamp = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    profile = relationship("CandidateProfile", back_populates="activity_log")
