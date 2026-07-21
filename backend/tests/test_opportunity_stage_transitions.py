"""Pipeline stage machine + Sales-role stage-transition API coverage.

Covers Closed_Partial transitions and confirms Sales (not only Sales_Head) can
move opportunities to Closed_Won / Closed_Lost / On_Hold / Closed_Partial.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _j(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(e, c, **k):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(64)"


import importlib

for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave",
    "timesheets", "finance", "hr", "candidates", "masters", "requirements",
    "profiles", "resumes", "ai_links", "scheduling", "project_employee",
    "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models.customers import Customer  # noqa: E402
from models.opportunities import Opportunity, OppType, PipelineStage  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.opportunities as opp_router  # noqa: E402
from services.opportunities import STAGE_TRANSITIONS, validate_stage_transition  # noqa: E402


# ---------------------------------------------------------------------------
# Unit: state machine
# ---------------------------------------------------------------------------

def test_closed_partial_reachable_from_active_and_on_hold():
    assert "Closed_Partial" in STAGE_TRANSITIONS["Active"]
    assert "Closed_Partial" in STAGE_TRANSITIONS["On_Hold"]
    assert STAGE_TRANSITIONS["Closed_Partial"] == ["Archived"]


def test_validate_active_to_closed_partial():
    validate_stage_transition(PipelineStage.ACTIVE, "Closed_Partial")


def test_validate_on_hold_to_closed_partial():
    validate_stage_transition(PipelineStage.ON_HOLD, "Closed_Partial")


def test_validate_closed_partial_to_archived():
    validate_stage_transition(PipelineStage.CLOSED_PARTIAL, "Archived")


def test_illegal_closed_partial_to_active_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_stage_transition(PipelineStage.CLOSED_PARTIAL, "Active")
    assert exc.value.status_code == 400


def test_illegal_new_to_closed_partial_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_stage_transition(PipelineStage.NEW, "Closed_Partial")
    assert exc.value.status_code == 400


def test_closed_won_lost_still_reachable_from_active():
    validate_stage_transition(PipelineStage.ACTIVE, "Closed_Won")
    validate_stage_transition(PipelineStage.ACTIVE, "Closed_Lost")
    validate_stage_transition(PipelineStage.ACTIVE, "On_Hold")


# ---------------------------------------------------------------------------
# API: Sales can POST outcome transitions
# ---------------------------------------------------------------------------

@pytest.fixture()
def sales_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    session.execute(users_table_stub.insert().values(id=10))
    cust = Customer(name="Acme")
    session.add(cust)
    session.flush()
    opp = Opportunity(
        opp_id="OPP-2026-900",
        title="Partial close target",
        customer_id=cust.id,
        opp_type=OppType.T_AND_M,
        rfi_value=Decimal("1000"),
        rfi_received_date=date(2026, 1, 1),
        pipeline_stage=PipelineStage.ACTIVE,
        created_by=10,
    )
    session.add(opp)
    session.commit()

    app = FastAPI()
    app.include_router(opp_router.router)

    def _db():
        yield session

    def _user():
        return crm_deps.CurrentUser(id=10, username="sales", roles={"Sales"})

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = _user

    client = TestClient(app)
    client._session = session
    client._opp_id = opp.id
    try:
        yield client
    finally:
        session.close()


@pytest.mark.parametrize(
    "new_stage",
    ["Closed_Won", "Closed_Lost", "On_Hold", "Closed_Partial"],
)
def test_sales_can_transition_to_outcomes(sales_client, new_stage):
    session = sales_client._session
    opp = session.get(Opportunity, sales_client._opp_id)
    opp.pipeline_stage = PipelineStage.ACTIVE
    session.commit()

    r = sales_client.post(
        f"/api/opportunities/{sales_client._opp_id}/stage-transition",
        json={"new_stage": new_stage, "comment": f"Sales sets {new_stage}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["pipeline_stage"] == new_stage


def test_sales_cannot_archive(sales_client):
    session = sales_client._session
    opp = session.get(Opportunity, sales_client._opp_id)
    opp.pipeline_stage = PipelineStage.CLOSED_PARTIAL
    session.commit()

    r = sales_client.post(
        f"/api/opportunities/{sales_client._opp_id}/stage-transition",
        json={"new_stage": "Archived", "comment": "try archive"},
    )
    assert r.status_code == 403
