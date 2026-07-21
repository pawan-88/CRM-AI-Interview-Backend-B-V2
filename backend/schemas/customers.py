"""Pydantic schemas for the Customer module (branches, billing policy, documents, contacts)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from models import CustomerStatus


class _CustomerAddress(BaseModel):
    address_line_1: str | None = Field(default=None, max_length=255)
    address_line_2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, max_length=120)


class CustomerCreate(_CustomerAddress):
    name: str = Field(min_length=1, max_length=255)
    legal_entity_name: str | None = Field(default=None, max_length=255)
    customer_type: str | None = Field(default=None, max_length=60)
    status: CustomerStatus = CustomerStatus.ACTIVE


class CustomerUpdate(_CustomerAddress):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    legal_entity_name: str | None = Field(default=None, max_length=255)
    customer_type: str | None = Field(default=None, max_length=60)
    status: CustomerStatus | None = None


class _BranchBillingFields(BaseModel):
    """Branch-level leave/holiday + billing policy (all optional)."""
    branch_legal_name: str | None = Field(default=None, max_length=255)
    address_line_2: str | None = Field(default=None, max_length=255)
    country: str | None = Field(default=None, max_length=120)
    holidays_billable: bool | None = None
    weekoff_billable: bool | None = None
    leave_billable: bool | None = None
    comp_off_billable: bool | None = None
    hours_required_half_day: float | None = None
    hours_required_full_day: float | None = None
    working_hours_per_day: float | None = None
    hours_required_half_day_comp_off: float | None = None
    hours_required_full_day_comp_off: float | None = None
    billing_frequency: str | None = Field(default=None, max_length=40)
    billing_cycle_start_day: int | None = Field(default=None, ge=1, le=31)
    billing_cycle_end_day: int | None = Field(default=None, ge=1, le=31)
    is_max_billable_hours_per_day: bool | None = None
    max_billable_hours_per_day: float | None = None
    is_max_billable_hours_per_month: bool | None = None
    max_billable_hours_per_month: float | None = None
    is_max_billable_days_per_month: bool | None = None
    max_billable_days_per_month: float | None = None
    is_initial_no_billing_period: bool | None = None
    initial_no_billing_qty: int | None = None
    initial_no_billing_period: str | None = Field(default=None, max_length=40)


class BranchCreate(_BranchBillingFields):
    branch_name: str = Field(min_length=1, max_length=255)
    billing_address: str | None = None
    delivery_address: str | None = None
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=16)
    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)
    is_primary: bool = False


class BranchUpdate(_BranchBillingFields):
    branch_name: str | None = Field(default=None, min_length=1, max_length=255)
    billing_address: str | None = None
    delivery_address: str | None = None
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=16)
    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)
    is_primary: bool | None = None


class BranchBillingPolicyIn(BaseModel):
    """Partial update of a branch's billing policy (PUT …/branches/{id}/billing-policy).

    Every field is optional; only fields present in the payload are touched
    (exclude_unset). Sending an explicit null CLEARS the field so the branch
    inherits the customer-level default policy for it.
    """

    holidays_billable: bool | None = None
    weekoff_billable: bool | None = None
    leave_billable: bool | None = None
    comp_off_billable: bool | None = None
    hours_required_half_day: float | None = Field(default=None, ge=0, le=24)
    hours_required_full_day: float | None = Field(default=None, ge=0, le=24)
    working_hours_per_day: float | None = Field(default=None, ge=0, le=24)
    hours_required_half_day_comp_off: float | None = Field(default=None, ge=0, le=24)
    hours_required_full_day_comp_off: float | None = Field(default=None, ge=0, le=24)
    billing_frequency: str | None = Field(default=None, max_length=40)
    billing_cycle_start_day: int | None = Field(default=None, ge=1, le=31)
    billing_cycle_end_day: int | None = Field(default=None, ge=1, le=31)
    is_max_billable_hours_per_day: bool | None = None
    max_billable_hours_per_day: float | None = Field(default=None, ge=0)
    is_max_billable_hours_per_month: bool | None = None
    max_billable_hours_per_month: float | None = Field(default=None, ge=0)
    is_max_billable_days_per_month: bool | None = None
    max_billable_days_per_month: float | None = Field(default=None, ge=0)
    is_initial_no_billing_period: bool | None = None
    initial_no_billing_qty: int | None = Field(default=None, ge=0)
    initial_no_billing_period: str | None = Field(default=None, max_length=40)


class BillingPolicyIn(BaseModel):
    """Upsert payload — one billing policy row per customer."""

    week_off_billable: bool = False
    leave_billable: bool = False
    holidays_billable: bool = False
    min_hours_full_day: float = Field(default=8.0, ge=0, le=24)
    min_hours_half_day: float = Field(default=4.0, ge=0, le=24)
    # comp-off section
    comp_off_billable: bool = False
    comp_off_balance: float | None = None
    comp_off_balance_initial: float | None = None
    comp_off_max_limit: float | None = None
    comp_off_max_carry_forward: float | None = None
    # attendance rule
    normal_hours_per_day: float | None = Field(default=None, ge=0, le=24)
    user_role: str | None = Field(default=None, max_length=120)
    operation: str | None = Field(default=None, max_length=120)


class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    branch_id: int | None = None
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    designation: str | None = Field(default=None, max_length=120)
    is_hiring_manager: bool = False
    is_active: bool = True


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    branch_id: int | None = None
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    designation: str | None = Field(default=None, max_length=120)
    is_hiring_manager: bool | None = None
    is_active: bool | None = None
