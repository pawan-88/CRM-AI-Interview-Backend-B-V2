"""Shared CRM service helpers: pagination, activity logs, sequence numbers, CSV, file storage."""
from __future__ import annotations

import csv
import io
import os
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from fastapi import UploadFile
from sqlalchemy import Select, func, select
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.orm import Session

from paths import DATA_DIR  # existing helper for the repo's data/ directory

CRM_UPLOAD_DIR = Path(os.getenv("CRM_UPLOAD_DIR") or (Path(DATA_DIR) / "crm_uploads"))


def to_dict(instance) -> dict:
    """Serialize a SQLAlchemy model instance to a JSON-friendly dict.

    Only mapped columns are included; datetimes/dates become ISO strings and
    Decimals become floats so the result is directly serializable by FastAPI.
    """
    if instance is None:
        return {}
    result: dict = {}
    for column in sa_inspect(instance).mapper.column_attrs:
        value = getattr(instance, column.key)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        result[column.key] = value
    return result


def paginate(db: Session, stmt: Select, page: int, limit: int) -> tuple[list, dict]:
    total = db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar() or 0
    items = db.execute(stmt.offset((page - 1) * limit).limit(limit)).scalars().all()
    pages = (total + limit - 1) // limit if limit else 1
    return items, {"page": page, "limit": limit, "total": total, "pages": pages}


def log_activity(db: Session, log_model, fk_field: str, entity_id: int, user_id: int,
                 action_type: str, comment: str | None = None) -> None:
    """Write one row to an *_activity_log table. Caller commits."""
    db.add(log_model(**{fk_field: entity_id}, user_id=user_id,
                     action_type=action_type, comment=comment))


def next_sequence_number(db: Session, model, column, prefix: str) -> str:
    """Generate PREFIX-YYYY-NNN (e.g. REQ-2026-001), scanning existing max for this year."""
    year = datetime.now().year
    like = f"{prefix}-{year}-%"
    values = db.execute(select(column).where(column.like(like))).scalars().all()
    max_n = 0
    for v in values:
        m = re.match(rf"{re.escape(prefix)}-{year}-(\d+)$", str(v))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}-{year}-{max_n + 1:03d}"


def save_upload(file: UploadFile, subdir: str = "") -> str:
    """Persist an uploaded file under data/crm_uploads and return its serving URL."""
    url, _sha, _size = save_upload_hashed(file, subdir)
    return url


def save_upload_hashed(file: UploadFile, subdir: str = "") -> tuple[str, str, int]:
    """Persist an upload and return (serving_url, sha256_hex, size_bytes).

    The SHA-256 is over the exact stored bytes — used for integrity (verify on
    read) and to deduplicate identical files. Filenames are never trusted: the
    stored name is a random uuid + the sanitized extension only.
    """
    import hashlib

    target_dir = CRM_UPLOAD_DIR / subdir if subdir else CRM_UPLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "file").suffix[:10]
    name = f"{uuid.uuid4().hex}{ext}"
    dest = target_dir / name
    data = file.file.read()
    with dest.open("wb") as fh:
        fh.write(data)
    rel = f"{subdir}/{name}" if subdir else name
    return f"/api/crm-files/{rel}", hashlib.sha256(data).hexdigest(), len(data)


def verify_crm_file_checksum(rel_path: str, expected_sha256: str) -> bool:
    """Re-hash a stored CRM file and constant-time compare to the expected digest."""
    import hashlib
    import hmac

    path = resolve_crm_file(rel_path)
    if not path or not expected_sha256:
        return False
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return hmac.compare_digest(actual, expected_sha256.strip().lower())


def resolve_crm_file(rel_path: str) -> Path | None:
    """Map /api/crm-files/<rel> back to a real path, refusing traversal."""
    candidate = (CRM_UPLOAD_DIR / rel_path).resolve()
    try:
        candidate.relative_to(CRM_UPLOAD_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def rows_to_csv(rows: list[dict], filename: str = "report.csv"):
    """Build a text/csv response body from a list of dicts."""
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def get_app_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.execute(sa.text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).first()
    return row[0] if row else default


def commit_or_conflict(
    db: Session,
    detail: str = "Cannot delete: record is still referenced by other data. Remove dependencies first.",
) -> None:
    """Commit; on FK violation roll back and raise HTTP 409 (never bubble as 500)."""
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=detail)
