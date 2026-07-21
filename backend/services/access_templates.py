"""Access Template service: CRUD, assign-to-user, and the one reusable
`effective_access` resolver used by `/api/me` and (Phase 4) server-side write guards.

Effective access = the linked template (LIVE) with the per-user override winning on
top, per tab/field. Admin/CEO always resolve to FULL (unrestricted).
"""
from __future__ import annotations


from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AccessTemplate, UserProfile
from services import access_registry
from services.users_admin import _decode_tab_access, get_field_access


# ------------------------------------------------------------------ serialize
def serialize_template(t: AccessTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "department_id": t.department_id,
        "role": t.role,
        "is_active": bool(t.is_active),
        "tab_access": t.tab_access or {},
        "field_access": t.field_access or {},
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def get_template_or_404(db: Session, template_id: int) -> AccessTemplate:
    t = db.get(AccessTemplate, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Access template not found")
    return t


def list_templates(db: Session) -> list[dict]:
    rows = db.execute(select(AccessTemplate).order_by(AccessTemplate.name)).scalars().all()
    return [serialize_template(t) for t in rows]


def _validate(tab_access, field_access) -> None:
    try:
        access_registry.validate_access(tab_access, field_access)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def create_template(db: Session, payload: dict) -> dict:
    _validate(payload.get("tab_access"), payload.get("field_access"))
    existing = db.execute(
        select(AccessTemplate).where(AccessTemplate.name == payload["name"])
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=400, detail=f"A template named {payload['name']!r} already exists")
    t = AccessTemplate(
        name=payload["name"], description=payload.get("description"),
        department_id=payload.get("department_id"), role=payload.get("role"),
        is_active=payload.get("is_active", True),
        tab_access=payload.get("tab_access") or {},
        field_access=payload.get("field_access") or {},
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return serialize_template(t)


def update_template(db: Session, template_id: int, payload: dict) -> dict:
    t = get_template_or_404(db, template_id)
    if "tab_access" in payload or "field_access" in payload:
        _validate(payload.get("tab_access", t.tab_access), payload.get("field_access", t.field_access))
    for field in ("name", "description", "department_id", "role", "is_active", "tab_access", "field_access"):
        if field in payload and payload[field] is not None:
            setattr(t, field, payload[field])
    db.commit()
    db.refresh(t)
    return serialize_template(t)


def delete_template(db: Session, template_id: int) -> dict:
    t = get_template_or_404(db, template_id)
    # unlink any users pointing at it (live link cleared, users fall back to role defaults)
    db.execute(
        UserProfile.__table__.update()
        .where(UserProfile.access_template_id == template_id)
        .values(access_template_id=None)
    )
    db.delete(t)
    db.commit()
    return {"id": template_id}


# ------------------------------------------------------------------ assign
def assign_template(db: Session, user_id: int, template_id: int | None) -> dict:
    if template_id is not None:
        get_template_or_404(db, template_id)
    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id)
    ).scalars().first()
    if profile is None:
        profile = UserProfile(user_id=user_id, access_template_id=template_id)
        db.add(profile)
    else:
        profile.access_template_id = template_id
    db.commit()
    return {"user_id": user_id, "access_template_id": template_id}


def _bare_tab_key(key: str) -> str:
    """Normalize legacy `crm:<path>` keys to bare registry tab keys."""
    k = str(key or "").strip()
    if k.startswith("crm:"):
        return k[4:] or "dashboard"
    return k or "dashboard"


def _normalize_tab_modes(raw: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        out[_bare_tab_key(k)] = v
    return out


def _normalize_field_modes(raw: dict | None) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for tab, fields in (raw or {}).items():
        bare = _bare_tab_key(tab)
        out[bare] = dict(out.get(bare, {}))
        out[bare].update({str(f): m for f, m in (fields or {}).items()})
    return out


# ------------------------------------------------------------------ resolver
def effective_access(db: Session, user_id: int, roles: set[str]) -> dict:
    """Resolve a user's effective tab/field access (modes).

    Admin/CEO -> full. Else start from the linked template (live), then let the
    per-user override win per tab/field. Returns mode maps + a legacy visible list.
    """
    if roles & {"Admin", "CEO"}:
        return {"full": True, "template_id": None, "tabs": {}, "fields": {},
                "visible_tabs": None, "source": "admin"}

    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id)
    ).scalars().first()

    tabs: dict[str, str] = {}
    fields: dict[str, dict[str, str]] = {}
    source = "role_default"

    template_id = profile.access_template_id if profile else None
    if template_id:
        t = db.get(AccessTemplate, template_id)
        if t and t.is_active:
            tabs = _normalize_tab_modes(t.tab_access or {})
            fields = _normalize_field_modes(t.field_access or {})
            source = "template"

    # per-user override (legacy list/dict) wins per tab/field, granted as "edit"
    override_tabs = _decode_tab_access(profile.tab_access) if profile else None
    if override_tabs is not None:
        for k in override_tabs:
            tabs[_bare_tab_key(k)] = "edit"
        source = "override" if source == "role_default" else "template+override"
    override_fields = get_field_access(db, user_id) if profile else None
    if override_fields:
        for tab, flist in override_fields.items():
            bare = _bare_tab_key(tab)
            fields.setdefault(bare, {})
            for f in (flist or []):
                fields[bare][f] = "edit"

    restricted = bool(template_id) or (override_tabs is not None)
    visible_tabs = sorted(tabs.keys()) if restricted else None
    return {
        "full": False,
        "template_id": template_id,
        "tabs": tabs,
        "fields": fields,
        "visible_tabs": visible_tabs,   # None = role-based defaults (no restriction)
        "source": source,
    }


def can_edit_tab(access: dict, tab: str) -> bool:
    if access.get("full"):
        return True
    if access.get("visible_tabs") is None:
        return True   # unrestricted (role defaults)
    bare = _bare_tab_key(tab)
    return access.get("tabs", {}).get(bare) == "edit"


def can_view_tab(access: dict, tab: str) -> bool:
    if access.get("full") or access.get("visible_tabs") is None:
        return True
    bare = _bare_tab_key(tab)
    return bare in access.get("tabs", {})
