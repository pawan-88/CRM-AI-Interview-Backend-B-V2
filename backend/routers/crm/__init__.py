"""Karnex CRM router registry — main.py calls register_crm_routers(app) once."""
from __future__ import annotations


def register_crm_routers(app) -> None:
    from routers.crm import (
        access_templates, ai_assist, ai_interviews, apply, calendar, candidate_profiles, candidates, credit_notes, customers, dashboards,
        table_preferences,
        employees, files, finance, finance_reports, holidays, leave_applications, leave_policies, masters, me,
        me_profile, notifications, opportunities, opportunity_attachments, outreach, projects,
        reports, requirements, resumes, settings, slots, tax_invoice, template_requests, timesheets, users_admin,
    )
    for module in (
        access_templates, me, me_profile, masters, settings, users_admin, notifications, files,
        customers, opportunities, opportunity_attachments, requirements, resumes,
        candidates, candidate_profiles, ai_interviews, calendar, table_preferences, projects, timesheets,
        finance, finance_reports, credit_notes, tax_invoice, employees, dashboards, reports, apply, template_requests,
        slots, outreach, holidays, leave_policies, leave_applications, ai_assist,
    ):
        app.include_router(module.router)
    app.include_router(holidays.names_router)
    # Flat education/experience row routes for the Employees master form
    # (PUT/DELETE /api/education/{id}, /api/experience/{id} + certificate uploads).
    app.include_router(employees.subform_router)
