"""Project-Employee module — models are defined in models/projects.py.

Leave/rate child tables (migration 0023):
  - ProjectEmployeeLeaveDetail
  - ProjectEmployeeRate

Also: LeaveApplication.project_employee_id, Timesheet.project_employee_id.
Calculation rules live in services/project_employee_billing.py.
"""
from __future__ import annotations

from models.projects import ProjectEmployee, ProjectEmployeeLeaveDetail, ProjectEmployeeRate

__all__ = ["ProjectEmployee", "ProjectEmployeeLeaveDetail", "ProjectEmployeeRate"]
