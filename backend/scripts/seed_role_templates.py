#!/usr/bin/env python3
"""Seed the six standard role Access Templates (idempotent).

Creates or UPDATES one template per CRM role — Sales, Sales Head, RMG, TA,
Finance, HR — encoding how each role actually uses this application. Admin
and CEO need no template: they bypass every gate.

The grants follow the mode ladder view < edit < create; a field entry raises
or lowers access for just that field. Every key is validated against
services.access_registry before writing, so a renamed tab or field makes this
script FAIL LOUDLY instead of seeding a template that silently grants nothing.

Usage:
  cd backend
  python scripts/seed_role_templates.py            # create/update all six
  python scripts/seed_role_templates.py --dry-run  # show what would change
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from models import AccessTemplate  # noqa: E402
from services.access_registry import FIELDS_BY_TAB, MODES, TABS  # noqa: E402

# ---------------------------------------------------------------- templates
# Philosophy per role (why, not just what):
#  * Sales own the customer relationship: they CREATE customers, slabs,
#    opportunities and candidate applications, and they co-approve timesheets.
#    They do not approve CTC (Sales Head authority) and only read finance.
#  * Sales Head adds the approval layer: requirements sign-off, projects,
#    POs and invoices creation, full commercial control.
#  * RMG run engineering review + fulfilment: requirements are theirs to
#    create/progress, they edit skill evaluations on opportunities (field
#    raise on a view tab), move candidate profiles, and approve timesheets.
#  * TA source: candidates and applications are theirs to create, requirements
#    theirs to work (postings/resumes) — CTC approval figures stay read-only.
#  * Finance own the money: POs, invoices, TDS, project commercials — and
#    only READ timesheets (they bill them, they don't decide them).
#  * HR own people: employees, holidays, leave decisions — timesheets stay
#    read-only on purpose (HR reviews, RMG/Sales decide; matches the code).
#  * EVERY role gets my-leave edit (filing one's own leave is universal) and
#    dashboard + reports view.

ROLE_TEMPLATES: dict[str, dict] = {
    "Sales — Standard": {
        "role": "Sales",
        "description": "Customer-facing sales: create customers, CTC slabs, "
                       "opportunities and applications; approve timesheets; "
                       "read-only finance. CTC approval stays with Sales Head.",
        "tabs": {
            "dashboard": "view", "customers": "create", "rate-cards": "create",
            "opportunities": "create", "candidates": "create", "profiles": "create",
            "projects": "view", "project-employees": "view",
            "branch-policy": "view", "timesheets": "edit",
            "my-leave": "edit", "holidays": "view",
            "pos": "view", "invoices": "view", "reports": "view",
        },
        "fields": {
            # Approving CTC is Sales Head authority — Sales sees, never sets.
            "profiles": {"approved_ctc": "view"},
        },
    },
    "Sales Head — Standard": {
        "role": "Sales_Head",
        "description": "Everything Sales has, plus the approval layer: "
                       "requirements sign-off, project/PO/invoice creation "
                       "and full commercial control.",
        "tabs": {
            "dashboard": "view", "customers": "create", "rate-cards": "create",
            "opportunities": "create", "candidates": "create", "profiles": "create",
            "projects": "create", "project-employees": "edit",
            "branch-policy": "edit", "timesheets": "edit",
            "my-leave": "edit", "holidays": "view",
            "pos": "create", "invoices": "create", "tds": "view",
            "reports": "view",
        },
        "fields": {},
    },
    "RMG — Standard": {
        "role": "RMG",
        "description": "Engineering review & fulfilment: create/progress "
                       "requirements, edit skill evaluations, move candidate "
                       "profiles, approve timesheets.",
        "tabs": {
            "dashboard": "view", "opportunities": "view",
            "candidates": "view",
            "template-requests": "edit", "profiles": "edit",
            "projects": "view", "project-employees": "view",
            "timesheets": "edit", "my-leave": "edit",
            "holidays": "view", "reports": "view",
        },
        "fields": {
            # Skill evaluation is RMG's call even though the opportunity
            # itself is read-only for them — a field RAISE on a view tab.
            "opportunities": {"skills": "edit"},
        },
    },
    "TA — Standard": {
        "role": "TA",
        "description": "Sourcing: create candidates and applications, work "
                       "requirements (postings, resumes, slots). CTC approval "
                       "figures are read-only.",
        "tabs": {
            "dashboard": "view", "customers": "view", "opportunities": "view",
            "candidates": "create",
            "template-requests": "edit", "profiles": "create",
            "my-leave": "edit", "reports": "view",
        },
        "fields": {
            "profiles": {"approved_ctc": "view", "hike_percent": "view"},
        },
    },
    "Finance — Standard": {
        "role": "Finance",
        "description": "Money ownership: POs, invoices, TDS and project "
                       "commercials. Timesheets are read-only — Finance bills "
                       "them, RMG/Sales decide them.",
        "tabs": {
            "dashboard": "view", "projects": "create",
            "project-employees": "edit", "timesheets": "view",
            "pos": "create", "invoices": "create", "tds": "edit",
            "my-leave": "edit", "reports": "view",
        },
        "fields": {},
    },
    "HR — Standard": {
        "role": "HR",
        "description": "People ownership: employees, holidays and leave "
                       "decisions. Timesheets stay read-only by design — HR "
                       "reviews, RMG/Sales decide (matches the code).",
        "tabs": {
            "dashboard": "view", "employees": "create", "holidays": "create",
            "leave-applications": "edit", "timesheets": "view",
            "project-employees": "edit", "candidates": "view",
            "branch-policy": "view", "my-leave": "edit", "reports": "view",
        },
        "fields": {},
    },
}


def _validate() -> None:
    problems: list[str] = []
    for name, spec in ROLE_TEMPLATES.items():
        for tab, mode in spec["tabs"].items():
            if tab not in TABS:
                problems.append(f"{name}: unknown tab '{tab}'")
            if mode not in MODES:
                problems.append(f"{name}: bad mode '{mode}' on {tab}")
        for tab, fields in spec["fields"].items():
            if tab not in spec["tabs"]:
                problems.append(f"{name}: field grant on unlisted tab '{tab}'")
            for field, fmode in fields.items():
                if field not in FIELDS_BY_TAB.get(tab, {}):
                    problems.append(f"{name}: unknown field '{tab}.{field}'")
                if fmode not in ("view", "edit"):
                    problems.append(f"{name}: bad field mode '{fmode}'")
    if problems:
        print("REFUSING to seed — registry mismatch:")
        for p in problems:
            print(f"  • {p}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    _validate()
    if args.dry_run:
        # DB-free: validation passed; show what would be written.
        for name, spec in ROLE_TEMPLATES.items():
            print(f"[dry-run] would create/update: {name} "
                  f"({len(spec['tabs'])} tabs, "
                  f"{sum(len(v) for v in spec['fields'].values())} field rules)")
        return
    Session = get_session_factory()
    with Session() as db:
        for name, spec in ROLE_TEMPLATES.items():
            existing = db.execute(
                select(AccessTemplate).where(AccessTemplate.name == name)
            ).scalars().first()
            action = "update" if existing else "create"
            if existing is None:
                existing = AccessTemplate(name=name)
                db.add(existing)
            existing.role = spec["role"]
            existing.description = spec["description"]
            existing.is_active = True
            existing.tab_access = spec["tabs"]
            existing.field_access = spec["fields"]
            print(f"{action}d: {name}  ({len(spec['tabs'])} tabs)")
        if not args.dry_run:
            db.commit()
            print("\nDone. Assign templates to users on the Users page "
                  "(or Access Templates → assign). Users must re-login "
                  "for template changes to take effect.")


if __name__ == "__main__":
    main()
