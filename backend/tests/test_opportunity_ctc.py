"""Candidate CTC Slab formula and validation tests."""
import pytest
from pydantic import ValidationError

from schemas.opportunities import OpportunityCtcSlabIn
from services.opportunity_ctc import (
    calculate_billing_bases,
    derive_ctc_row,
    normalize_tm_billing_details,
)


BASE = {
    "hours_per_day": 8,
    "holidays": 10,
    "weekoff": 104,
    "leave": 24,
    "holidays_billable": False,
    "weekoff_billable": False,
    "leave_billable": False,
}


def test_billing_bases_subtract_only_non_billable_values():
    days, hours = calculate_billing_bases(BASE)
    assert float(days) == 227
    assert float(hours) == 1816

    days, hours = calculate_billing_bases({**BASE, "holidays_billable": True})
    assert float(days) == 237
    assert float(hours) == 1896


@pytest.mark.parametrize(
    ("billing_type", "rate", "annual", "monthly"),
    [
        ("Per Hour", 100, 181600, 15133.33),
        ("Per Day", 100, 22700, 1891.67),
        ("Per Month", 10000, 120000, 10000),
        ("Per Year", 120000, 120000, 10000),
    ],
)
def test_all_billing_type_revenue_branches(billing_type, rate, annual, monthly):
    row = derive_ctc_row(
        {"rate": rate, "management_cost_pct": 30, "hike_pct": 10},
        opportunity_type="T&M",
        details={**BASE, "billing_type": billing_type},
    )
    assert row["revenue_annual"] == annual
    assert row["revenue_monthly"] == monthly
    assert row["engineering_budget"] == round(annual * 0.70, 2)
    assert row["approved_ctc_lac"] == round(annual * 0.70 / 1.10, 2)


def test_per_month_is_not_prorated_by_billing_days():
    row = derive_ctc_row(
        {"rate": 15000},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Month", "holidays": 300},
    )
    assert row["revenue_annual"] == 180000
    assert row["revenue_monthly"] == 15000


def test_blank_and_zero_edges_do_not_produce_nan_or_divide_by_zero():
    missing = derive_ctc_row(
        {}, opportunity_type="T&M", details={**BASE, "billing_type": "Per Hour"},
    )
    assert missing["revenue_annual"] is None
    assert missing["approved_ctc_lac"] is None

    no_hours = derive_ctc_row(
        {"rate": 100},
        opportunity_type="T&M",
        details={**BASE, "hours_per_day": "", "billing_type": "Per Hour"},
    )
    assert no_hours["revenue_annual"] is None

    zero = derive_ctc_row(
        {"rate": 0, "management_cost_pct": "", "hike_pct": ""},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Year"},
    )
    assert zero["revenue_annual"] == 0
    assert zero["approved_ctc_lac"] == 0

    guarded = derive_ctc_row(
        {"rate": 100000, "hike_pct": -100},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Year"},
    )
    assert guarded["approved_ctc_lac"] is None


@pytest.mark.parametrize(
    ("opportunity_type", "rate", "details", "annual"),
    [
        ("Work_Package", 120000, {}, 120000),
        ("Fixed_Price", 60000, {"project_duration_months": 6}, 120000),
        ("Retainer", 10000, {}, 120000),
    ],
)
def test_non_tm_types_ignore_hidden_billing_hours(opportunity_type, rate, details, annual):
    row = derive_ctc_row(
        {"rate": rate},
        opportunity_type=opportunity_type,
        details={
            **details,
            "billing_type": "Per Hour",
            "hours_per_day": 999,
            "actual_billing_hours": 999999,
        },
    )
    assert row["revenue_annual"] == annual
    assert row["revenue_monthly"] == 10000


def test_fixed_price_blank_and_zero_duration_edges():
    blank = derive_ctc_row(
        {"rate": 120000},
        opportunity_type="Fixed_Price",
        details={"project_duration_months": ""},
    )
    assert blank["revenue_annual"] == 120000

    zero = derive_ctc_row(
        {"rate": 120000},
        opportunity_type="Fixed_Price",
        details={"project_duration_months": 0},
    )
    assert zero["revenue_annual"] is None
    assert zero["approved_ctc_lac"] is None


def test_opportunity_type_switch_recalculates_instead_of_reusing_stale_annual():
    tm = derive_ctc_row(
        {"rate": 100},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Hour"},
    )
    switched = derive_ctc_row(
        tm,
        opportunity_type="Work_Package",
        details={"actual_billing_hours": 1816},
    )
    assert tm["revenue_annual"] == 181600
    assert switched["revenue_annual"] == 100


def test_normalize_tm_details_applies_form_defaults():
    details = normalize_tm_billing_details({"billing_type": "Per Hour"})
    assert details["actual_billing_days"] == 227
    assert details["actual_billing_hours"] == 1816


def test_exp_max_is_midpoint_of_exp_min_and_target_exp():
    row = derive_ctc_row(
        {"exp_min": 3, "target_exp": 5, "rate": 400},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Hour"},
    )
    assert row["exp_max"] == 4

    non_tm = derive_ctc_row(
        {"exp_min": 2, "target_exp": 6, "rate": 120000},
        opportunity_type="Work_Package",
        details={},
    )
    assert non_tm["exp_max"] == 4

    blank = derive_ctc_row(
        {"exp_min": 3, "target_exp": "", "exp_max": 9, "rate": 400},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Hour"},
    )
    assert blank["exp_max"] is None


def test_exp_min_cannot_exceed_exp_max():
    with pytest.raises(ValidationError, match="Exp Min"):
        OpportunityCtcSlabIn(exp_min=5, exp_max=3)
