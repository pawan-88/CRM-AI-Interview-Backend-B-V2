"""Typed shape for Ask AI per-tab help KB entries."""
from __future__ import annotations

from typing import NotRequired, TypedDict


class HelpEntry(TypedDict):
    """One tab/route help article. Keys match CRM `p=` paths where possible."""

    key: str
    title: str
    purpose: str
    key_fields: list[str]
    common_tasks: list[str]
    gotchas: list[str]
    related_routes: list[str]
    suggested_prompts: list[str]
    # Optional deep-rule block IDs from business_rules.py
    rule_ids: NotRequired[list[str]]
