"""Who may record which interview round.

The technical ladder and the customer's own round are different people's
judgements. Sales recording an L2 result would be inventing an engineering
opinion; RMG recording customer feedback would be inventing the client's. A
single WRITE_ROLES tuple could not express that, which is why the customer
round could not be recorded at all — it was neither an allowed value nor
writable by the role that owns it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.interview_rounds import (  # noqa: E402
    ROUND_VALUES, ROUND_WRITE_ROLES, WRITE_ROLES, ensure_may_write_round,
    roles_for_round, rounds_writable_by,
)


class FakeUser:
    def __init__(self, roles, is_admin=False):
        self.id = 1
        self.roles = list(roles)
        self.is_admin = is_admin


RMG = FakeUser(["RMG"])
SALES = FakeUser(["Sales"])
SALES_HEAD = FakeUser(["Sales_Head"])
TA = FakeUser(["TA"])
ADMIN = FakeUser([], is_admin=True)


def allowed(user, kind) -> bool:
    try:
        ensure_may_write_round(user, kind)
        return True
    except HTTPException:
        return False


# ------------------------------------------------------- the customer round

def test_customer_interview_is_a_recordable_round():
    """It was in the data model and in imported history, but not in the API."""
    assert "Customer_Interview" in ROUND_VALUES


def test_sales_owns_the_customer_round():
    assert allowed(SALES, "Customer_Interview")
    assert allowed(SALES_HEAD, "Customer_Interview")


def test_rmg_cannot_record_customer_feedback():
    """RMG writing the client's verdict would be inventing it."""
    assert not allowed(RMG, "Customer_Interview")


# ------------------------------------------------------ the technical ladder

@pytest.mark.parametrize("kind", ["L1_Interview", "L2_F2F", "L3_Interview", "L4_Interview"])
def test_rmg_owns_every_technical_round(kind):
    assert allowed(RMG, kind)


@pytest.mark.parametrize("kind", ["L1_Interview", "L2_F2F", "L3_Interview", "L4_Interview"])
def test_sales_cannot_record_technical_rounds(kind):
    """Sales must not gain the technical ladder by gaining the customer round."""
    assert not allowed(SALES, kind)
    assert not allowed(SALES_HEAD, kind)


# ------------------------------------------------------------------- others

def test_ta_records_no_rounds():
    """TA schedules the AI interview; the humans record the human rounds."""
    assert rounds_writable_by(TA) == []


def test_admin_may_record_anything():
    assert rounds_writable_by(ADMIN) == list(ROUND_VALUES)
    assert allowed(ADMIN, "Customer_Interview")
    assert allowed(ADMIN, "L2_F2F")


def test_unknown_round_is_rejected_not_silently_allowed():
    with pytest.raises(HTTPException) as exc:
        ensure_may_write_round(SALES, "Made_Up_Round")
    assert exc.value.status_code == 400


def test_empty_kind_is_rejected():
    for value in ("", None, "   "):
        with pytest.raises(HTTPException):
            ensure_may_write_round(SALES, value)


# ------------------------------------------------- the maps stay consistent

def test_every_round_has_an_owner():
    """A round nobody can write is a dead value in a dropdown."""
    for kind in ROUND_VALUES:
        assert roles_for_round(kind), f"{kind} has no owning role"


def test_coarse_gate_is_the_union_of_the_per_kind_roles():
    """WRITE_ROLES gates the endpoint; the per-kind map decides. If the coarse
    gate were narrower, an owner would be turned away before the real check."""
    union = {role for roles in ROUND_WRITE_ROLES.values() for role in roles}
    assert set(WRITE_ROLES) == union


def test_writable_list_matches_the_enforcement():
    """What the form offers must equal what the save accepts."""
    for user in (RMG, SALES, SALES_HEAD, TA, ADMIN):
        for kind in ROUND_VALUES:
            assert (kind in rounds_writable_by(user)) == allowed(user, kind), (user.roles, kind)
