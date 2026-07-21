"""Current-user endpoint for the CRM UI (role-based navigation)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, get_current_user
from schemas.common import envelope
from services import users_admin as svc

router = APIRouter(prefix="/api/me", tags=["CRM: Me"])


def _legacy_ui_tab_keys(visible: list[str] | None) -> list[str] | None:
    """Map resolver bare keys → UI keys (`crm:` / `iv:`) expected by the admin shell.

    `effective_access` normalizes to bare registry keys for API guards; the React
    shell still looks up `crm:<path>` / `iv:<view>` in `me.tab_access`.
    """
    if visible is None:
        return None
    out: list[str] = []
    for k in visible:
        key = str(k or "").strip()
        if not key:
            continue
        if key.startswith("crm:") or key.startswith("iv:"):
            out.append(key)
        else:
            out.append(f"crm:{key}")
    return out


@router.get("")
def me(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_crm_db)):
    try:
        tab_access = svc.get_tab_access(db, user.id)
        field_access = svc.get_field_access(db, user.id)
    except Exception:
        tab_access = None  # never block login on the tab-access lookup
        field_access = None
    try:
        from services.access_templates import effective_access
        access = effective_access(db, user.id, set(user.roles))
        if access.get("visible_tabs") is not None:
            # Keep access.tabs bare for guards; expose crm:/iv: keys for the UI allow-list.
            tab_access = _legacy_ui_tab_keys(access["visible_tabs"])
    except Exception:
        access = {"full": user.is_admin, "template_id": None, "tabs": {}, "fields": {},
                  "visible_tabs": None, "source": "role_default"}
    return envelope(data={
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "roles": sorted(user.roles),
        "is_superadmin": user.is_admin,   # Admin or CEO
        "is_ceo": user.is_ceo,
        # None => full role-based access; list => only these tab keys are visible.
        "tab_access": tab_access,
        # { "<tab_key>": ["field", ...] } — tabs absent = all fields allowed.
        "field_access": field_access,
        # NEW: mode-aware effective access {full, template_id, tabs:{tab:mode}, fields:{tab:{field:mode}}, source}
        "access": access,
    })


@router.get("/project-leave")
def my_project_leave(user: CurrentUser = Depends(get_current_user),
                     db: Session = Depends(get_crm_db)):
    """Consolidated 'My Leave' across every project the current user is mapped to."""
    from services.timesheets import employee_for_user
    from services.project_employees import employee_project_leave
    emp = employee_for_user(db, user.id)
    if emp is None:
        return envelope(data={"employee_id": None, "total_leave_balance": 0, "projects": []})
    return envelope(data=employee_project_leave(db, emp.id))
