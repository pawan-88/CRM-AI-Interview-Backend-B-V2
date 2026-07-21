"""Serve CRM-uploaded files (resumes, CVs, certificates, invoices, timesheet attachments)."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from crm_deps import CurrentUser, any_crm_role
from services.crm_common import resolve_crm_file

router = APIRouter(prefix="/api/crm-files", tags=["CRM: Files"])

# Ensure common office types are registered (stdlib misses some on Windows).
mimetypes.add_type("application/pdf", ".pdf")
mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx",
)
mimetypes.add_type("application/msword", ".doc")


@router.get("/{rel_path:path}")
def get_crm_file(rel_path: str, user: CurrentUser = Depends(any_crm_role)):
    path = resolve_crm_file(rel_path)
    if path is None:
        raise HTTPException(status_code=404, detail="File not found")
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        filename=Path(path.name).name,
        content_disposition_type="inline",
    )
