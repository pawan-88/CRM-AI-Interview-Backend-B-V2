"""Ask AI — KB loader + assist endpoint (auth, rate-limit wiring, no mutations).

Run:  python -m pytest tests/test_ai_assist.py -q
"""
from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _j(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(e, c, **k):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(64)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "project_employee", "user_profiles", "template_requests",
    "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.ai_assist as ai_router  # noqa: E402
from ai_help.loader import (  # noqa: E402
    all_entries,
    build_help_context,
    get_entry,
    normalize_tab_key,
    resolve_navigate_to,
)
from ai_help import assist as assist_mod  # noqa: E402


REQUIRED_TABS = {
    "dashboard", "customers", "branch", "opportunities", "candidates",
    "template-requests", "profiles", "projects", "project-employees",
    "my-leave", "leave-applications", "holidays", "timesheets",
    "pos", "invoices", "tds", "employees", "reports",
}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(ai_router.router)

    def _db():
        try:
            yield db
        finally:
            pass

    def _user():
        return crm_deps.CurrentUser(
            id=1, username="hr_test", full_name="Test HR",
            email="hr@test.local", roles={"HR", "Admin"},
        )

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = _user
    # any_crm_role depends on get_current_user — override is enough
    return TestClient(app)


# -------------------- KB --------------------

def test_kb_seeds_every_required_tab():
    keys = set(all_entries().keys())
    missing = REQUIRED_TABS - keys
    assert not missing, f"Missing KB tabs: {missing}"


def test_normalize_paths():
    assert normalize_tab_key("") == "dashboard"
    assert normalize_tab_key("timesheets/12") == "timesheets"
    assert normalize_tab_key("branch-policy/3") == "branch"
    assert normalize_tab_key("purchase-orders") == "pos"


def test_timesheets_kb_has_rules_and_prompts():
    e = get_entry("timesheets")
    assert e["title"] == "Timesheets"
    assert len(e["suggested_prompts"]) >= 3
    assert "billable_days" in (e.get("rule_ids") or [])
    assert "effective_date" in (e.get("rule_ids") or [])
    ctx = build_help_context("timesheets")
    assert ctx["tab_key"] == "timesheets"
    assert any("Effective Date" in p or "leave" in p.lower() for p in ctx["suggested_prompts"])


def test_resolve_navigate_to():
    assert resolve_navigate_to("projects") == "projects"
    assert resolve_navigate_to("Purchase Orders") == "pos"
    assert resolve_navigate_to("dashboard") == ""
    assert resolve_navigate_to("not-a-real-page") is None


# -------------------- API --------------------

def test_help_context_ok(client):
    r = client.get("/api/ai/help-context", params={"route": "timesheets"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["tab_key"] == "timesheets"
    assert body["data"]["suggested_prompts"]
    assert body["data"]["read_only"] is True


def test_assist_requires_auth(db):
    app = FastAPI()
    app.include_router(ai_router.router)

    def _db():
        yield db

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    bare = TestClient(app)
    r = bare.post("/api/ai/assist", json={"message": "hi", "route": "dashboard"})
    assert r.status_code == 401


def test_assist_no_mutation_and_navigate(client, monkeypatch):
    calls = {"n": 0}

    def _fake_run(*, message, tab_key, history=None):
        calls["n"] += 1
        return {
            "reply": "Open **Projects** from the sidebar to manage engagements.",
            "navigate_to": "projects",
            "tab_key": "timesheets",
            "tab_title": "Timesheets",
            "suggested_prompts": ["What is Effective Date?"],
            "actions": [{
                "type": "navigate",
                "path": "projects",
                "label": "Go to Projects",
                "requires_confirmation": True,
                "enabled": True,
            }],
            "tools_used": [],
            "tool_results": [],
            "read_only": True,
        }

    monkeypatch.setattr(ai_router, "run_assist", _fake_run)

    r = client.post(
        "/api/ai/assist",
        json={
            "message": "Go to Projects",
            "route": "timesheets",
            "history": [],
            "enable_tools": True,  # phase-2 flag ignored
        },
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["read_only"] is True
    assert data["navigate_to"] == "projects"
    assert data["tools_used"] == []
    assert calls["n"] == 1


def test_assist_fallback_without_openai(client, monkeypatch):
    monkeypatch.setattr(assist_mod, "_assist_llm_purpose", lambda: None)
    monkeypatch.setattr(ai_router, "run_assist", assist_mod.run_assist)

    r = client.post(
        "/api/ai/assist",
        json={"message": "How do I mark leave / Comp-Off?", "tab_key": "timesheets"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["read_only"] is True
    assert "Timesheets" in data["reply"] or "timesheet" in data["reply"].lower()
    assert data["tab_key"] == "timesheets"


def test_assist_empty_message_422(client):
    r = client.post("/api/ai/assist", json={"message": "", "route": "dashboard"})
    assert r.status_code == 422
