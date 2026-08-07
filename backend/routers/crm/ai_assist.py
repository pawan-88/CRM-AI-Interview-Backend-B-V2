"""Ask AI — READ-ONLY in-app help assistant (CRM).

POST /api/ai/assist — grounded reply (+ optional navigate_to hint)
GET  /api/ai/help-context — tab title + suggested prompts (no LLM)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from crm_deps import CurrentUser, any_crm_role, get_crm_db
from schemas.common import envelope
import rate_limit as _rl

from ai_help.assist import run_assist
from ai_help.loader import build_help_context

logger = logging.getLogger("karnex.ai_assist")

router = APIRouter(prefix="/api/ai", tags=["CRM: Ask AI"])


class AssistHistoryTurn(BaseModel):
    role: str = Field(..., description="user | assistant")
    content: str = Field(..., max_length=4000)


class AssistRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    # Accept either name for the current CRM tab/path
    route: str | None = Field(default=None, max_length=120)
    tab_key: str | None = Field(default=None, max_length=120)
    history: list[AssistHistoryTurn] = Field(default_factory=list, max_length=16)
    # ---- Phase-2 seams (ignored in MVP; schema reserved) ----
    enable_tools: bool = False
    tool_whitelist: list[str] | None = None
    confirm_actions: bool = True  # future: actions[] only after user confirm


@router.get("/help-context")
def help_context(
    route: str | None = Query(default=None),
    tab_key: str | None = Query(default=None),
    user: CurrentUser = Depends(any_crm_role),
):
    """Return title + suggested prompts for the current tab (no LLM call)."""
    _ = user
    ctx = build_help_context(tab_key or route)
    return envelope(
        data={
            "tab_key": ctx["tab_key"],
            "title": ctx["title"],
            "purpose": ctx["purpose"],
            "suggested_prompts": ctx["suggested_prompts"],
            "related_routes": ctx["related_routes"],
            "read_only": True,
        },
        message="Help context",
    )


@router.post("/assist")
@_rl.limit("20/minute")
def assist(
    request: Request,  # required by slowapi when limiter active
    payload: AssistRequest,
    user: CurrentUser = Depends(any_crm_role),
    db: Session = Depends(get_crm_db),
):
    """Read-only help reply grounded in the per-tab KB + a live CRM data snapshot."""
    _ = request  # slowapi key_func may use request
    # Explicitly ignore phase-2 tool flags in MVP
    if payload.enable_tools or payload.tool_whitelist:
        logger.info("ai_assist.tools_ignored", extra={"user_id": user.id})

    tab = payload.tab_key or payload.route
    history = [{"role": t.role, "content": t.content} for t in payload.history]
    # Live CRM copilot: read-only, role-aware snapshot so the assistant can
    # answer questions about the user's actual data ("top candidates for
    # REQ-2026-016", "which POs expire this quarter", ...). Best-effort.
    data_context = None
    try:
        from ai_help.crm_data import build_data_context
        data_context = build_data_context(
            db, set(user.roles), bool(getattr(user, "is_admin", False)), payload.message,
        )
    except Exception:
        logger.warning("ai_assist.data_context_failed", exc_info=True)
    try:
        data = run_assist(message=payload.message, tab_key=tab, history=history,
                          data_context=data_context)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("ai_assist.failed")
        raise HTTPException(
            status_code=502,
            detail="Ask AI is temporarily unavailable. Please try again.",
        ) from exc

    return envelope(data=data, message="Assist reply")
