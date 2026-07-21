"""Notification creation helpers (bell icon feed). Caller commits."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Notification, Role, UserRole


def notify_user(db: Session, user_id: int, title: str, message: str = "", link: str = "") -> None:
    db.add(Notification(user_id=user_id, title=title, message=message or None, link=link or None))


def notify_role(db: Session, role_name: str, title: str, message: str = "", link: str = "",
                exclude_user_id: int | None = None) -> int:
    """Notify every user holding a CRM role. Returns count notified."""
    user_ids = db.execute(
        select(UserRole.user_id).join(Role, Role.id == UserRole.role_id).where(Role.name == role_name)
    ).scalars().all()
    count = 0
    for uid in set(user_ids):
        if exclude_user_id is not None and uid == exclude_user_id:
            continue
        notify_user(db, uid, title, message, link)
        count += 1
    return count
