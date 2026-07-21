"""Shared FastAPI dependencies for Karnex CRM routers — JWT auth + RBAC.

RBAC is enforced HERE, at the API level (never only in the UI). CRM roles
come from the user_roles table (7 roles). The legacy
registration_data.role ('hr'/'candidate') stays untouched for the interview
platform. Token decoding mirrors main.py so existing JWTs keep working.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import jwt
import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_db import CrmNotConfiguredError, get_session_factory
from models import Role, UserRole


def _auth_secret() -> str:
    # Must mirror main.py::_auth_secret exactly — including the sha256
    # normalization of short secrets — or tokens issued by /auth/login fail
    # verification here (the default fallback secret is only 21 bytes).
    raw = (os.getenv("AUTH_SECRET") or os.getenv("REPORT_CODE") or "change-me-auth-secret").strip()
    if len(raw.encode("utf-8")) >= 32:
        return raw
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _decode_bearer(request: Request) -> dict | None:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            _auth_secret(),
            algorithms=["HS256"],
            options={"verify_signature": True, "verify_exp": True, "require": ["exp"]},
        )
        return payload if isinstance(payload, dict) else None
    except jwt.PyJWTError:
        return None


def get_crm_db():
    """CRM DB session dependency; 503 with a clear message when Postgres isn't configured."""
    try:
        session: Session = get_session_factory()()
    except CrmNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        yield session
    finally:
        session.close()


@dataclass
class CurrentUser:
    id: int
    username: str
    full_name: str = ""
    email: str = ""
    roles: set[str] = field(default_factory=set)

    def has_any(self, *names: str) -> bool:
        return bool(self.roles.intersection(names))

    @property
    def is_admin(self) -> bool:
        # CEO is a super-admin: it satisfies every Admin gate.
        return "Admin" in self.roles or "CEO" in self.roles

    @property
    def is_ceo(self) -> bool:
        return "CEO" in self.roles


def get_current_user(request: Request, db: Session = Depends(get_crm_db)) -> CurrentUser:
    payload = _decode_bearer(request)
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    username = str(payload.get("sub") or "")
    row = db.execute(
        sa.text(
            "SELECT id, full_name, email, COALESCE(is_active, TRUE) AS is_active "
            "FROM registration_data WHERE username = :u"
        ),
        {"u": username},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=401, detail="Unknown user")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="User is deactivated")
    role_rows = db.execute(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == row["id"])
    ).scalars().all()
    roles = {r.value if hasattr(r, "value") else str(r) for r in role_rows}
    return CurrentUser(
        id=row["id"], username=username,
        full_name=row["full_name"] or "", email=row["email"] or "", roles=roles,
    )


def role_required(*allowed: str, allow_admin: bool = True):
    """Dependency factory: require any of the given CRM roles (Admin passes by default).

    Usage: user: CurrentUser = Depends(role_required("Sales", "Sales_Head"))
    """
    allowed_set = set(allowed) | ({"Admin", "CEO"} if allow_admin else set())

    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.roles & allowed_set:
            return user
        raise HTTPException(
            status_code=403,
            detail=f"Requires one of roles: {', '.join(sorted(allowed_set))}",
        )

    return dep


def any_crm_role(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Any authenticated user that has at least one CRM role."""
    if not user.roles:
        raise HTTPException(status_code=403, detail="No CRM role assigned to this user")
    return user


def _roles_for_username(username: str) -> set[str] | None:
    """CRM role names for a username, or None when the CRM DB isn't configured.

    Returns an empty set when the user exists but has no CRM role, and None when
    Postgres/CRM is unavailable (so callers can fall back to legacy behavior).
    """
    try:
        session: Session = get_session_factory()()
    except CrmNotConfiguredError:
        return None
    try:
        row = session.execute(
            sa.text("SELECT id FROM registration_data WHERE username = :u"),
            {"u": username},
        ).first()
        if not row:
            return set()
        role_rows = session.execute(
            select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == row[0])
        ).scalars().all()
        return {r.value if hasattr(r, "value") else str(r) for r in role_rows}
    except Exception:
        # Never let an RBAC lookup failure take down a legacy interview endpoint.
        return None
    finally:
        session.close()


def enforce_roles(request: Request, *allowed: str, allow_admin: bool = True) -> None:
    """Authorize the current bearer against CRM roles on Interview Platform routes.

    This is the backend twin of the frontend RBAC (src/lib/rbac.ts): it lets the
    same role matrix be enforced on plain FastAPI endpoints in main.py that were
    written before CRM roles existed. Call it AFTER the endpoint's existing
    `_require_user(...)` auth check.

    Fails CLOSED (403) only when the user HAS CRM roles and none of them match.
    Falls back to a no-op (legacy behavior) when:
      * the request has no decodable bearer (caller already enforced auth), or
      * the CRM DB is unconfigured, or
      * the user has no CRM role assigned yet.
    This keeps existing HR logins working during and after RBAC rollout, while
    fully restricting users who DO have a scoped role (Sales, Finance, TA, ...).
    """
    payload = _decode_bearer(request)
    if not payload:
        return
    username = str(payload.get("sub") or "")
    if not username:
        return
    roles = _roles_for_username(username)
    if roles is None:          # CRM unconfigured / lookup failed → legacy mode
        return
    if not roles:              # no CRM role assigned → legacy mode (don't lock out)
        return
    allowed_set = set(allowed) | ({"Admin", "CEO"} if allow_admin else set())
    if roles & allowed_set:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Requires one of roles: {', '.join(sorted(allowed_set))}",
    )


@dataclass
class PageParams:
    page: int
    limit: int
    search: str | None
    sort_by: str | None
    sort_dir: str

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


def page_params(page: int = 1, limit: int = 20, search: str | None = None,
                sort_by: str | None = None, sort_dir: str = "desc") -> PageParams:
    page = max(1, page)
    limit = min(max(1, limit), 100)
    sort_dir = "asc" if str(sort_dir).lower() == "asc" else "desc"
    return PageParams(page=page, limit=limit, search=(search or "").strip() or None,
                      sort_by=sort_by, sort_dir=sort_dir)


def gated_read(tab: str, *roles: str, allow_admin: bool = True):
    """Combine CRM role check (or any_crm_role when roles empty) + template view access."""
    role_dep = role_required(*roles, allow_admin=allow_admin) if roles else any_crm_role
    acc_dep = require_access(tab, mode="view")

    def dep(
        user: CurrentUser = Depends(role_dep),
        _acc: CurrentUser = Depends(acc_dep),
    ) -> CurrentUser:
        return user

    return dep


def gated_write(tab: str, *roles: str, allow_admin: bool = True, field: str | None = None):
    """Combine CRM role check + template edit access (optional field)."""
    role_dep = role_required(*roles, allow_admin=allow_admin)
    acc_dep = require_access(tab, mode="edit", field=field)

    def dep(
        user: CurrentUser = Depends(role_dep),
        _acc: CurrentUser = Depends(acc_dep),
    ) -> CurrentUser:
        return user

    return dep


def require_access(tab: str, *, mode: str = "edit", field: str | None = None):
    """Dependency factory enforcing Access-Template permissions on an endpoint.

    Admin/CEO and users with no template/override (role-based defaults) pass through.
    Otherwise the user must have the tab (and, for writes, `edit` on the tab or the
    specific field). Read endpoints should use mode="view".

        user: CurrentUser = Depends(require_access("customers", mode="edit"))
    """
    def dep(user: "CurrentUser" = Depends(get_current_user),
            db: Session = Depends(get_crm_db)) -> "CurrentUser":
        from services.access_templates import effective_access
        acc = effective_access(db, user.id, set(user.roles))
        # full (Admin/CEO) or unrestricted (role defaults) -> allowed
        if acc.get("full") or acc.get("visible_tabs") is None:
            return user
        tabs = acc.get("tabs", {})
        bare = tab  # callers pass bare registry keys; resolver normalizes stored keys
        if bare not in tabs:
            raise HTTPException(status_code=403, detail=f"You do not have access to the '{tab}' tab")
        if mode == "view":
            return user
        # edit required — field mode (if any) governs, else the tab mode
        tab_mode = tabs.get(bare)
        if field is not None:
            fmode = (acc.get("fields", {}).get(bare, {}) or {}).get(field, tab_mode)
            if fmode != "edit":
                raise HTTPException(status_code=403,
                                    detail=f"You have view-only access to '{tab}.{field}'")
        elif tab_mode != "edit":
            raise HTTPException(status_code=403, detail=f"You have view-only access to the '{tab}' tab")
        return user
    return dep
