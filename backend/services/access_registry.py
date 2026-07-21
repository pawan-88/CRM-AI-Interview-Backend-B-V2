"""Registry of grantable CRM tabs + fields, and validation for Access Templates.

This is the catalogue the template editor renders (real checkboxes) and the server
validates against, so a template can never grant an unknown tab/field/mode.

MODES: "view" (read-only) | "edit" (insert/update).
Tab keys mirror the CRM router nav; field keys mirror each page's primary form fields.
Extend freely — add tabs/fields here and they light up in the editor automatically.
"""
from __future__ import annotations

MODES = ("view", "edit")

# ------------------------------------------------------------------ tabs
# key -> human label. Keys match the CRM query-param routes (?view=crm&p=<key>).
TABS: dict[str, str] = {
    "dashboard": "Dashboard",
    "customers": "Customers",
    "opportunities": "Opportunities",
    "requirements": "Requirements",
    "candidates": "Candidates",
    "template-requests": "Template Requests",
    "profiles": "Candidate Profiles",
    "projects": "Projects",
    "project-employees": "Project Employees",
    "branch-policy": "Branch Policy",
    "my-leave": "My Leave",
    "leave-applications": "Leave Applications",
    "holidays": "Holidays",
    "timesheets": "Timesheets",
    "pos": "Purchase Orders",
    "invoices": "Invoices",
    "tds": "TDS",
    "employees": "Employees",
    "reports": "Reports",
    "users": "Users",
    "settings": "Settings",
}

# ------------------------------------------------------------------ fields per tab
# tab_key -> { field_key: label }. A curated set of the primary form fields per tab.
FIELDS_BY_TAB: dict[str, dict[str, str]] = {
    "customers": {
        "name": "Customer Name", "legal_entity_name": "Legal Entity Name",
        "customer_type": "Customer Type", "status": "Status",
        "address": "Address", "branches": "Branches", "billing_policy": "Billing Policy",
        "contacts": "Contacts", "documents": "Documents",
    },
    "opportunities": {
        "title": "Opportunity Title", "customer_id": "Customer", "opp_type": "Type",
        "sales_stage": "Sales Stage", "rfi_value": "RFI Value",
        "customer_type": "Customer Type", "contact_email": "Contact Email",
        "hiring_manager_email": "Hiring Manager Email",
    },
    "projects": {
        "name": "Project Name", "customer_id": "Customer", "status": "Status",
        "billing_frequency": "Billing Frequency", "billing_cycle_start_day": "Cycle Start",
        "billing_cycle_end_day": "Cycle End", "max_billable_hours_day": "Max Billable Hrs/Day",
    },
    "project-employees": {
        "employee_id": "Employee", "project_id": "Project", "onboarding_date": "Onboarding Date",
        "billing_rate": "Billing Rate", "billing_unit": "Billing Unit",
        "leave_details": "Leave", "rates": "Commercial Rate", "is_exit": "Exit",
    },
    "branch-policy": {
        "identity": "Branch Identity", "holiday_years": "Holiday Years",
        "billability": "Billability Flags", "thresholds": "Hour Thresholds",
        "caps": "Billing Caps", "leave_policies": "Billable Leave Policy",
    },
    "timesheets": {
        "period": "Period", "status": "Status", "entries": "Daily Entries",
        "billable_days": "Billable Days", "approve": "Approve/Reject",
    },
    "invoices": {
        "invoice_number": "Invoice Number", "po_id": "PO", "sub_total": "Sub Total",
        "tax_amount": "Tax", "grand_total": "Grand Total", "payment_status": "Payment Status",
    },
    "pos": {
        "po_number": "PO Number", "customer_id": "Customer", "total_value": "Total Value",
        "allocations": "Project Allocations", "status": "Status",
    },
    "employees": {
        "first_name": "First Name", "last_name": "Last Name", "email": "Email",
        "department_id": "Department", "designation_id": "Designation", "current_ctc": "CTC",
    },
    "requirements": {
        "title": "Requirement Title", "customer_id": "Customer", "priority": "Priority",
        "status": "Status", "skills": "Skills", "description": "Description",
    },
    "candidates": {
        "name": "Name", "email": "Email", "phone": "Phone", "experience_years": "Experience",
        "current_ctc": "Current CTC", "expected_ctc": "Expected CTC", "cv": "Resume",
    },
    "profiles": {
        "full_name": "Full Name", "email": "Email", "phone": "Phone", "skills": "Skills",
        "experience": "Experience", "ctc": "CTC",
    },
    "template-requests": {
        "title": "Title", "requirement_id": "Requirement", "status": "Status", "template_body": "Body",
    },
    "leave-applications": {
        "employee_id": "Employee", "leave_type": "Leave Type", "dates": "Dates", "status": "Status",
    },
    "holidays": {
        "branch_id": "Branch", "year": "Year", "holiday_date": "Date", "name": "Holiday Name",
    },
    "my-leave": {
        "balance": "Leave Balance", "projects": "Project Leave",
    },
    "reports": {
        "date_range": "Date Range", "export": "Export",
    },
    "settings": {
        "company": "Company Settings", "smtp": "SMTP", "sequences": "Sequences",
    },
    "tds": {
        "tds_amount": "TDS Amount", "payment_status": "Payment Status", "payments": "Payments",
    },
    "users": {
        "roles": "Roles", "tab_access": "Tab Access", "access_template": "Access Template",
        "activation": "Activation", "portal_access": "Portal Access",
    },
}


def tab_keys() -> list[str]:
    return list(TABS.keys())


def is_valid_tab(key: str) -> bool:
    return key in TABS


def field_keys(tab: str) -> list[str]:
    return list(FIELDS_BY_TAB.get(tab, {}).keys())


def is_valid_field(tab: str, field: str) -> bool:
    return field in FIELDS_BY_TAB.get(tab, {})


def registry() -> dict:
    """Full catalogue for the template-editor UI."""
    return {
        "modes": list(MODES),
        "tabs": [
            {"key": k, "label": v, "fields": [{"key": fk, "label": fl}
                                              for fk, fl in FIELDS_BY_TAB.get(k, {}).items()]}
            for k, v in TABS.items()
        ],
    }


def validate_access(tab_access: dict | None, field_access: dict | None) -> None:
    """Raise ValueError on any unknown tab / field / mode."""
    for k, mode in (tab_access or {}).items():
        if not is_valid_tab(k):
            raise ValueError(f"Unknown tab key: {k}")
        if mode not in MODES:
            raise ValueError(f"Invalid mode {mode!r} for tab {k}")
    for tab, fields in (field_access or {}).items():
        if not is_valid_tab(tab):
            raise ValueError(f"Unknown tab key in field_access: {tab}")
        for fk, mode in (fields or {}).items():
            if not is_valid_field(tab, fk):
                raise ValueError(f"Unknown field {fk!r} for tab {tab}")
            if mode not in MODES:
                raise ValueError(f"Invalid mode {mode!r} for field {tab}.{fk}")
