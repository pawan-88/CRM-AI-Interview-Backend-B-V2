"""Read-only aggregation queries behind /api/dashboard/* endpoints.

Everything here is SELECT-only (counts / sums / group-bys) — no commits.
Postgres-only features (date_trunc, FILTER aggregates) are OK per project rules.
"""
from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    AiInterviewStatus,
    AtsStatus,
    CandidateProfile,
    Customer,
    CustomerStatus,
    Employee,
    Invoice,
    Opportunity,
    PaymentStatus,
    PipelineStage,
    PipelineStatus,
    POStatus,
    Project,
    ProjectStatus,
    PurchaseOrder,
    Requirement,
    RequirementStatus,
    Resume,
    TdsRecord,
    TdsStatus,
)

# Requirements considered "open positions" for sourcing dashboards.
OPEN_SOURCING_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)

# Requirements that can no longer contribute open positions.
TERMINAL_REQ_STATUSES = (
    RequirementStatus.FULFILLED,
    RequirementStatus.CLOSED,
    RequirementStatus.CANCELLED,
)


def _ev(value):
    """Enum -> spec string; anything else passes through."""
    return value.value if hasattr(value, "value") else value


def _num(value) -> float:
    """Decimal/None -> float for JSON."""
    return float(value) if value is not None else 0.0


def _usernames(db: Session, user_ids) -> dict[int, str]:
    """id -> username map from the legacy registration_data table (raw SQL —
    the ORM stub for that table only carries the id column)."""
    ids = sorted({i for i in user_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(
        sa.text("SELECT id, username FROM registration_data WHERE id IN :ids")
        .bindparams(sa.bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    return {row[0]: row[1] for row in rows}


def _quarter_label(dt) -> str:
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def _last_quarter_labels(n: int = 4) -> list[str]:
    """Labels for the last n quarters (oldest first), including the current one."""
    today = date.today()
    year, quarter = today.year, (today.month - 1) // 3 + 1
    labels: list[str] = []
    for _ in range(n):
        labels.append(f"{year}-Q{quarter}")
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
    return list(reversed(labels))


# ---------------------------------------------------------------------------
# Executive (Sales_Head)
# ---------------------------------------------------------------------------

def executive_dashboard(db: Session) -> dict:
    headcount = db.execute(
        select(func.count(Employee.id)).where(Employee.is_active.is_(True))
    ).scalar() or 0
    customer_count = db.execute(
        select(func.count(Customer.id)).where(Customer.status == CustomerStatus.ACTIVE)
    ).scalar() or 0
    project_count = db.execute(
        select(func.count(Project.id)).where(Project.status == ProjectStatus.ACTIVE)
    ).scalar() or 0

    opp_counts = {
        _ev(stage): count
        for stage, count in db.execute(
            select(Opportunity.pipeline_stage, func.count(Opportunity.id))
            .group_by(Opportunity.pipeline_stage)
        ).all()
    }
    opportunity_funnel = [
        {"stage": stage.value, "count": opp_counts.get(stage.value, 0)}
        for stage in PipelineStage
    ]

    req_counts = {
        _ev(status): count
        for status, count in db.execute(
            select(Requirement.status, func.count(Requirement.id)).group_by(Requirement.status)
        ).all()
    }
    requirement_funnel = [
        {"status": status.value, "count": req_counts.get(status.value, 0)}
        for status in RequirementStatus
    ]

    # Quarterly matrix: profiles that reached Joined (bucketed by when they were
    # last updated, i.e. moved to Joined) x invoiced revenue by invoice date.
    joined_q = func.date_trunc("quarter", CandidateProfile.updated_at).label("q")
    joined_map = {
        _quarter_label(bucket): count
        for bucket, count in db.execute(
            select(joined_q, func.count(CandidateProfile.id))
            .where(CandidateProfile.pipeline_status == PipelineStatus.JOINED)
            .group_by(joined_q)
        ).all()
    }
    revenue_q = func.date_trunc("quarter", Invoice.invoice_date).label("q")
    revenue_map = {
        _quarter_label(bucket): _num(total)
        for bucket, total in db.execute(
            select(revenue_q, func.sum(Invoice.grand_total)).group_by(revenue_q)
        ).all()
    }
    quarterly_matrix = [
        {
            "quarter": label,
            "joined_count": joined_map.get(label, 0),
            "revenue": revenue_map.get(label, 0.0),
        }
        for label in _last_quarter_labels(4)
    ]

    return {
        "headcount": headcount,
        "customer_count": customer_count,
        "project_count": project_count,
        "opportunity_funnel": opportunity_funnel,
        "quarterly_matrix": quarterly_matrix,
        "requirement_funnel": requirement_funnel,
    }


# ---------------------------------------------------------------------------
# RMG
# ---------------------------------------------------------------------------

def rmg_dashboard(db: Session) -> dict:
    req_rows = db.execute(
        select(
            Requirement.id,
            Requirement.req_number,
            Requirement.title,
            Requirement.no_of_positions,
            Requirement.opportunity_id,
            Customer.name,
        )
        .join(Customer, Customer.id == Requirement.customer_id)
        .where(Requirement.status.in_(OPEN_SOURCING_STATUSES))
        .order_by(Requirement.req_number)
    ).all()

    req_ids = [row[0] for row in req_rows]
    opp_ids = {row[4] for row in req_rows}

    resume_counts: dict[int, int] = {}
    if req_ids:
        resume_counts = {
            req_id: count
            for req_id, count in db.execute(
                select(Resume.requirement_id, func.count(Resume.id))
                .where(Resume.requirement_id.in_(req_ids))
                .group_by(Resume.requirement_id)
            ).all()
        }

    profiles_by_opp: dict[int, dict[str, int]] = {}
    if opp_ids:
        for opp_id, status, count in db.execute(
            select(
                CandidateProfile.opportunity_id,
                CandidateProfile.pipeline_status,
                func.count(CandidateProfile.id),
            )
            .where(CandidateProfile.opportunity_id.in_(opp_ids))
            .group_by(CandidateProfile.opportunity_id, CandidateProfile.pipeline_status)
        ).all():
            profiles_by_opp.setdefault(opp_id, {})[_ev(status)] = count

    pipeline = [
        {
            "req_number": req_number,
            "title": title,
            "customer": customer_name,
            "no_of_positions": no_of_positions,
            "resumes_count": resume_counts.get(req_id, 0),
            "profiles_by_status": profiles_by_opp.get(opportunity_id, {}),
        }
        for req_id, req_number, title, no_of_positions, opportunity_id, customer_name in req_rows
    ]

    pending_engineering_reviews = db.execute(
        select(func.count(Requirement.id))
        .where(Requirement.status == RequirementStatus.PENDING_ENGINEERING_REVIEW)
    ).scalar() or 0

    return {
        "pipeline": pipeline,
        "totals": {"pending_engineering_reviews": pending_engineering_reviews},
    }


# ---------------------------------------------------------------------------
# TA
# ---------------------------------------------------------------------------

def ta_dashboard(db: Session) -> dict:
    open_requirements_count = db.execute(
        select(func.count(Requirement.id)).where(Requirement.status.in_(OPEN_SOURCING_STATUSES))
    ).scalar() or 0
    resumes_pending_scan = db.execute(
        select(func.count(Resume.id)).where(Resume.ats_status == AtsStatus.PENDING_SCAN)
    ).scalar() or 0

    queue_rows = db.execute(
        select(
            Resume.id,
            Resume.candidate_name,
            Requirement.req_number,
            Resume.ai_interview_scheduled_at,
        )
        .join(Requirement, Requirement.id == Resume.requirement_id)
        .where(Resume.ai_interview_status == AiInterviewStatus.SCHEDULED)
        .order_by(Resume.ai_interview_scheduled_at)
    ).all()
    interview_pending_queue = [
        {
            "id": resume_id,
            "candidate_name": candidate_name,
            "requirement": req_number,
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        }
        for resume_id, candidate_name, req_number, scheduled_at in queue_rows
    ]

    # Recruiter productivity, keyed by resumes.screened_by (Postgres FILTER aggregates).
    prod_rows = db.execute(
        select(
            Resume.screened_by,
            func.count(Resume.id).filter(
                sa.cast(Resume.created_at, sa.Date) == func.current_date()
            ),
            func.count(Resume.id).filter(
                Resume.created_at >= func.date_trunc("week", func.now())
            ),
            func.count(Resume.id),
            func.count(Resume.id).filter(Resume.ats_status == AtsStatus.SHORTLISTED),
        )
        .where(Resume.screened_by.is_not(None))
        .group_by(Resume.screened_by)
    ).all()
    usernames = _usernames(db, (row[0] for row in prod_rows))
    recruiter_productivity = [
        {
            "user": usernames.get(user_id, f"user:{user_id}"),
            "resumes_screened_today": today_count,
            "this_week": week_count,
            "total": total_count,
            "shortlisted_total": shortlisted_count,
        }
        for user_id, today_count, week_count, total_count, shortlisted_count in prod_rows
    ]
    recruiter_productivity.sort(key=lambda r: r["total"], reverse=True)

    return {
        "open_requirements_count": open_requirements_count,
        "resumes_pending_scan": resumes_pending_scan,
        "interview_pending_queue": interview_pending_queue,
        "recruiter_productivity": recruiter_productivity,
    }


# ---------------------------------------------------------------------------
# Finance
# ---------------------------------------------------------------------------

def finance_dashboard(db: Session) -> dict:
    out_count, out_amount = db.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.balance_amount), 0))
        .where(Invoice.payment_status != PaymentStatus.PAID)
    ).one()
    tds_count, tds_amount = db.execute(
        select(func.count(TdsRecord.id), func.coalesce(func.sum(TdsRecord.tds_balance), 0))
        .where(TdsRecord.tds_status != TdsStatus.PAID)
    ).one()

    po_rows = db.execute(
        select(
            PurchaseOrder.po_number,
            PurchaseOrder.total_value,
            PurchaseOrder.consumed_value,
            PurchaseOrder.balance_value,
        )
        .where(PurchaseOrder.status == POStatus.ACTIVE)
        .order_by(PurchaseOrder.total_value.desc())
        .limit(20)
    ).all()
    po_consumption = []
    for po_number, total_value, consumed_value, balance_value in po_rows:
        total = _num(total_value)
        consumed = _num(consumed_value)
        po_consumption.append({
            "po_number": po_number,
            "total_value": total,
            "consumed_value": consumed,
            "balance_value": _num(balance_value),
            "pct_consumed": round(consumed / total * 100, 1) if total else 0.0,
        })

    return {
        "outstanding_invoices": {"count": out_count, "amount": _num(out_amount)},
        "tds_pending": {"count": tds_count, "amount": _num(tds_amount)},
        "po_consumption": po_consumption,
    }


# ---------------------------------------------------------------------------
# Requirements funnel / stage timing
# ---------------------------------------------------------------------------

def requirements_dashboard(db: Session) -> dict:
    counts = {
        _ev(status): count
        for status, count in db.execute(
            select(Requirement.status, func.count(Requirement.id)).group_by(Requirement.status)
        ).all()
    }
    funnel = [
        {"status": status.value, "count": counts.get(status.value, 0)}
        for status in RequirementStatus
    ]

    def _avg_days(delta_expr, *conditions) -> float | None:
        value = db.execute(
            select(func.avg(sa.extract("epoch", delta_expr) / 86400.0)).where(*conditions)
        ).scalar()
        return round(float(value), 1) if value is not None else None

    avg_days_in_stage = {
        "submission_to_sales_head_approval": _avg_days(
            Requirement.sales_head_approved_at - Requirement.created_at,
            Requirement.sales_head_approved_at.is_not(None),
        ),
        "sales_head_to_engineering": _avg_days(
            Requirement.engineering_reviewed_at - Requirement.sales_head_approved_at,
            Requirement.sales_head_approved_at.is_not(None),
            Requirement.engineering_reviewed_at.is_not(None),
        ),
    }

    open_positions = db.execute(
        select(func.coalesce(func.sum(Requirement.no_of_positions), 0))
        .where(Requirement.status.not_in(TERMINAL_REQ_STATUSES))
    ).scalar() or 0
    filled_positions = db.execute(
        select(func.coalesce(func.sum(Requirement.no_of_positions), 0))
        .where(Requirement.status == RequirementStatus.FULFILLED)
    ).scalar() or 0

    return {
        "funnel": funnel,
        "avg_days_in_stage": avg_days_in_stage,
        "positions": {"open": int(open_positions), "filled": int(filled_positions)},
    }
