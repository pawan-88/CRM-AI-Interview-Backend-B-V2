"""Pydantic schemas for the Project module (team, communication matrix)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from models import BillingFrequency, BillingUnit, CommEntryType, ProjectStatus, WorkMode


class ProjectCreate(BaseModel):
    opportunity_id: int
    customer_id: int
    name: str = Field(min_length=1, max_length=255)
    billing_cycle_start_day: int = Field(default=1, ge=1, le=31)
    billing_cycle_end_day: int = Field(default=31, ge=1, le=31)
    billing_frequency: BillingFrequency = BillingFrequency.MONTHLY
    max_billable_hours_day: Decimal | None = Field(default=None, ge=0, le=24)
    max_billable_hours_month: Decimal | None = Field(default=None, ge=0)
    max_billable_days_month: int | None = Field(default=None, ge=0, le=31)
    no_billing_period_days: int | None = Field(default=None, ge=0)
    status: ProjectStatus = ProjectStatus.ACTIVE


class ProjectUpdate(BaseModel):
    opportunity_id: int | None = None
    customer_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    billing_cycle_start_day: int | None = Field(default=None, ge=1, le=31)
    billing_cycle_end_day: int | None = Field(default=None, ge=1, le=31)
    billing_frequency: BillingFrequency | None = None
    max_billable_hours_day: Decimal | None = Field(default=None, ge=0, le=24)
    max_billable_hours_month: Decimal | None = Field(default=None, ge=0)
    max_billable_days_month: int | None = Field(default=None, ge=0, le=31)
    no_billing_period_days: int | None = Field(default=None, ge=0)
    status: ProjectStatus | None = None


class ProjectEmployeeIn(BaseModel):
    employee_id: int
    onboarding_date: date | None = None
    experience_years: Decimal | None = Field(default=None, ge=0, le=99)
    project_experience_years: Decimal | None = Field(default=None, ge=0, le=99)
    work_mode: WorkMode | None = None
    billing_rate: Decimal = Field(ge=0)
    billing_unit: BillingUnit = BillingUnit.MONTHLY
    is_exit: bool = False
    exit_date: date | None = None
    billing_date: date | None = None  # first billable date


class ProjectEmployeeUpdate(BaseModel):
    onboarding_date: date | None = None
    experience_years: Decimal | None = Field(default=None, ge=0, le=99)
    project_experience_years: Decimal | None = Field(default=None, ge=0, le=99)
    work_mode: WorkMode | None = None
    billing_rate: Decimal | None = Field(default=None, ge=0)
    billing_unit: BillingUnit | None = None
    is_active: bool | None = None
    is_exit: bool | None = None
    exit_date: date | None = None
    billing_date: date | None = None  # first billable date


class CommMatrixIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    role: str | None = Field(default=None, max_length=120)
    responsible_person: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    type: CommEntryType = CommEntryType.CUSTOMER


class ProjectEmployeeLeaveDetailUpdate(BaseModel):
    initial_balance: Decimal | None = Field(default=None, ge=0)
    opening_balance: Decimal | None = Field(default=None, ge=0)
    leave_accrual: Decimal | None = Field(default=None, ge=0)
    leave_consumed: Decimal | None = Field(default=None, ge=0)
    leave_balance: Decimal | None = Field(default=None, ge=0)


class ProjectEmployeeRateIn(BaseModel):
    effective_from: date
    rate: Decimal = Field(ge=0)
    billing_unit: BillingUnit | None = None
    is_current_rate: bool = True


class ProjectEmployeeRateUpdate(BaseModel):
    effective_from: date | None = None
    rate: Decimal | None = Field(default=None, ge=0)
    billing_unit: BillingUnit | None = None
    is_current_rate: bool | None = None
