"""Pydantic schemas for HR/leave modules: holidays, customer leave policies,
leave applications (migration 0021)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, field_validator

HOLIDAY_TYPES = ("National", "Regional", "Customer")
HOLIDAY_OBSERVANCE = ("Mandatory", "Optional")
LEAVE_CREDIT_TYPES = (
    # Legacy / existing values (keep for already-saved rows)
    "Monthly", "Quarterly", "Yearly", "One_Time", "Annually",
    # Project Leave Billing Policy UI values (shared with customer form)
    "Credit Week-Off/Holiday",
    # TODO(source-system): replace these placeholders with the exact Leave Credit
    # Type option list from the source CRM once the product owner supplies it.
    "Credit Balance Every Month",
    "Carry Forward Every Month",
)
LEAVE_CREDIT_TIMINGS = ("Start_Of_Period", "End_Of_Period", "Start_of_Month")
# TODO(source-system): extend Leave Expire / Prorate / Is Max Limit option sets
# when the source CRM option lists are provided.
LEAVE_EXPIRE_UNITS = ("Days",)  # spec §5 "Leave_Expire" dropdown (extensible)
LEAVE_PERIOD_TYPES = ("Full_Day", "Half_Day", "Multi_Day")
COMP_OFF_TYPES = ("Earned", "Consumed")
LEAVE_APP_STATUSES = ("Pending", "Approved", "Rejected", "Cancelled")


def _required_str(v: str) -> str:
    s = (v or "").strip()
    if not s:
        raise ValueError("must not be empty")
    return s


def _one_of(allowed: tuple, label: str):
    def check(v):
        if v is None:
            return v
        if v not in allowed:
            raise ValueError(f"{label} must be one of: {', '.join(allowed)}")
        return v
    return check


# ---------------------------------------------------------------- holiday names
class HolidayNameCreate(BaseModel):
    name: str
    is_active: bool = True

    _name = field_validator("name")(_required_str)


# ---------------------------------------------------------------- holidays
class HolidayCreate(BaseModel):
    name: str | None = None
    holiday_name_id: int | None = None
    holiday_date: date
    holiday_type: str = "National"
    observance: str = "Mandatory"
    customer_id: int | None = None
    branch_id: int | None = None
    is_active: bool = True

    _type = field_validator("holiday_type")(_one_of(HOLIDAY_TYPES, "holiday_type"))
    _observance = field_validator("observance")(_one_of(HOLIDAY_OBSERVANCE, "observance"))


class HolidayUpdate(BaseModel):
    name: str | None = None
    holiday_name_id: int | None = None
    holiday_date: date | None = None
    holiday_type: str | None = None
    observance: str | None = None
    customer_id: int | None = None
    branch_id: int | None = None
    is_active: bool | None = None

    _type = field_validator("holiday_type")(_one_of(HOLIDAY_TYPES, "holiday_type"))
    _observance = field_validator("observance")(_one_of(HOLIDAY_OBSERVANCE, "observance"))


class BranchHolidayCreate(BaseModel):
    holiday_name_id: int | None = None
    name: str | None = None
    holiday_date: date
    observance: str = "Mandatory"
    holiday_type: str = "Customer"

    _observance = field_validator("observance")(_one_of(HOLIDAY_OBSERVANCE, "observance"))
    _type = field_validator("holiday_type")(_one_of(HOLIDAY_TYPES, "holiday_type"))


class BranchHolidayUpdate(BaseModel):
    holiday_name_id: int | None = None
    name: str | None = None
    holiday_date: date | None = None
    observance: str | None = None
    holiday_type: str | None = None
    is_active: bool | None = None

    _observance = field_validator("observance")(_one_of(HOLIDAY_OBSERVANCE, "observance"))
    _type = field_validator("holiday_type")(_one_of(HOLIDAY_TYPES, "holiday_type"))


# ---------------------------------------------------------------- customer leave policies
class LeaveCreditConceptIn(BaseModel):
    from_date: date | None = None
    end_date: date | None = None
    balance: Decimal = Decimal("0")
    is_active: bool = True


class CustomerLeavePolicyCreate(BaseModel):
    customer_id: int
    branch_id: int | None = None
    leave_type_id: int
    leave_credit_type: str = "Credit Balance Every Month"
    leave_expire: str | None = None
    is_max_limit: bool = False
    max_limit: Decimal | None = None
    prorate_balance_credit: bool = False
    leave_credit_balance: Decimal = Decimal("0")
    initial_credit_balance: Decimal = Decimal("0")
    maximum_carry_forward: Decimal | None = None
    leave_credit_timing: str = "Start_Of_Period"
    effective_date: date | None = None
    is_billable: bool | None = None
    is_active: bool = True
    concepts: list[LeaveCreditConceptIn] = []

    _credit_type = field_validator("leave_credit_type")(
        _one_of(LEAVE_CREDIT_TYPES, "leave_credit_type"))
    _timing = field_validator("leave_credit_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_credit_timing"))


class BranchLeavePolicyCreate(BaseModel):
    """Nested under /branches/{id}/leave-policies — customer_id/branch_id derived from path."""
    leave_type_id: int
    leave_credit_type: str = "Credit Balance Every Month"
    leave_expire: str | None = None
    is_max_limit: bool = False
    max_limit: Decimal | None = None
    prorate_balance_credit: bool = False
    leave_credit_balance: Decimal = Decimal("0")
    initial_credit_balance: Decimal = Decimal("0")
    maximum_carry_forward: Decimal | None = None
    leave_credit_timing: str = "Start_Of_Period"
    effective_date: date | None = None
    is_billable: bool | None = True
    is_active: bool = True

    _credit_type = field_validator("leave_credit_type")(
        _one_of(LEAVE_CREDIT_TYPES, "leave_credit_type"))
    _timing = field_validator("leave_credit_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_credit_timing"))


class CustomerLeavePolicyUpdate(BaseModel):
    branch_id: int | None = None
    leave_type_id: int | None = None
    leave_credit_type: str | None = None
    leave_expire: str | None = None
    is_max_limit: bool | None = None
    max_limit: Decimal | None = None
    prorate_balance_credit: bool | None = None
    leave_credit_balance: Decimal | None = None
    initial_credit_balance: Decimal | None = None
    maximum_carry_forward: Decimal | None = None
    leave_credit_timing: str | None = None
    effective_date: date | None = None
    is_billable: bool | None = None
    is_active: bool | None = None

    _credit_type = field_validator("leave_credit_type")(
        _one_of(LEAVE_CREDIT_TYPES, "leave_credit_type"))
    _timing = field_validator("leave_credit_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_credit_timing"))


# ---------------------------------------------------------------- leave applications
class LeaveApplicationCreate(BaseModel):
    employee_id: int | None = None   # HR/Admin may file for any employee; others self only
    project_id: int | None = None
    project_employee_id: int | None = None  # PE-scoped leave (preferred when deployed)
    leave_type_id: int
    leave_period_type: str = "Full_Day"
    from_date: date
    to_date: date | None = None      # defaults to from_date for single-day requests
    comp_off_type: str | None = None
    reason: str | None = None

    _period = field_validator("leave_period_type")(
        _one_of(LEAVE_PERIOD_TYPES, "leave_period_type"))
    _comp_off = field_validator("comp_off_type")(_one_of(COMP_OFF_TYPES, "comp_off_type"))


class LeaveApplicationUpdate(BaseModel):
    project_id: int | None = None
    project_employee_id: int | None = None
    leave_type_id: int | None = None
    leave_period_type: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    comp_off_type: str | None = None
    reason: str | None = None

    _period = field_validator("leave_period_type")(
        _one_of(LEAVE_PERIOD_TYPES, "leave_period_type"))
    _comp_off = field_validator("comp_off_type")(_one_of(COMP_OFF_TYPES, "comp_off_type"))
