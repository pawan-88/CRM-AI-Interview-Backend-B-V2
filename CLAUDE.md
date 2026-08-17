# CLAUDE.md — Karnex backend (AI-Interview-Model-B-V2)

Working notes for AI assistants. Written 12 Aug 2026 from a full read of the tree.
Companion file: `F:\AI-Interview-Model-F-V2\CLAUDE.md` (frontend).

---

## 1. What this repo is

One FastAPI application that is really **two products bolted together**:

| Half | Style | Storage | Entry |
| --- | --- | --- | --- |
| **Interview platform** (legacy, came first) | flat `@app.*` routes in `main.py`, raw SQL, in-process session dicts | `auth_db.py` — dual-dialect SQLite **or** Postgres, no ORM, no Alembic | `backend/main.py` (~7,900 lines) |
| **Karnex CRM/ERP** (added later) | `APIRouter` modules, SQLAlchemy 2.0, Alembic, RBAC dependencies | `crm_db.py` — **Postgres only**, 503 when unconfigured | `backend/routers/crm/__init__.py::register_crm_routers(app)` |

They meet at `backend/services/ai_interview_bridge.py` (CRM schedules an AI L1 interview) and at `registration_data`, the legacy users table that CRM models FK into via `models/base.py::USERS_TABLE`.

Serving the UI is also this app's job: `/admin` mounts the built React dashboard and `/` mounts the vanilla candidate UI, both from the sibling **F-V2** repo resolved by `paths._resolve_frontend_dir()` (`FRONTEND_DIR` env → `<repo>/frontend` → `../AI-Interview-Model-F-V2/frontend`).

---

## 2. Orientation map

```
backend/
  main.py            app assembly + ALL auth + ~70 interview/HR endpoints  ← 316 KB, start here for interview work
  ai.py              model-facing engine: generation, evaluation, TTS, transcription, scoring guards (3,520 lines)
  auth_db.py         legacy raw-SQL store (registration_data, interview_records, job_templates, …)
  crm_db.py          SQLAlchemy engine/session for the CRM (Postgres only)
  crm_deps.py        ★ the RBAC core — every CRM route's Depends() lives here
  models/            22 modules, SQLAlchemy 2.0, ~52 tables
  schemas/           Pydantic; schemas/common.py::envelope() is the response contract
  routers/crm/       38 modules, ~271 endpoints
  routers/admin.py   prompt logs + AI usage (legacy `hr` JWT role, not CRM RBAC)
  routers/question_bank.py
  services/          business logic — timesheets, leave credit, finance, tax, notify, scheduler, outbox
  ai_help/           Ask AI knowledge base (Python TypedDicts, not YAML)
  alembic/versions/  68 revisions, head = 0068; CRM tables only
  tests/             95 files, ~326 tests, in-memory SQLite — no live DB needed
  scripts/           DB-touching operational + QA CLIs
  tools/             Zoho/NEXUS CSV/NDJSON importers (dry-run by default)
services/            strangler-proxy stubs (see §9) — inert
k8s/, Dockerfile, docker-compose*.yml, render.yaml
```

---

## 3. Auth and RBAC — read this before touching any endpoint

**Two parallel auth implementations. Do not mix them.**

- Legacy interview endpoints: `main._require_user(request, allowed_roles)` returns a `(payload, error_response)` **tuple**, called imperatively inside the handler. Roles are only `hr` / `candidate`.
- CRM endpoints: `crm_deps.get_current_user` as a real `Depends()`. Roles are the 8 CRM roles.

Both HS256, both funnel to `auth_secret.auth_secret()` which reads **`AUTH_SECRET`** and refuses weak/short values — the app will not start in production without a real one.

**CRM roles** (`models/rbac.py::RoleName`): `CEO, Admin, Sales, Sales_Head, RMG, TA, HR, Finance`. `CurrentUser.is_admin` is true for **Admin or CEO**; Admin/CEO bypass every gate.

**The gate ladder in `crm_deps.py`** — pick the narrowest that fits:

| Dependency | Meaning |
| --- | --- |
| `get_crm_db` | session; raises **503** when Postgres is unconfigured |
| `any_crm_role` | must hold ≥1 CRM role |
| `role_required(*roles)` | role check; always adds `{Admin, CEO}`. No args = admin-only (aliased `admin_only`) |
| `require_access(tab, mode="view"\|"edit"\|"create", field=)` | Access-Template check (ladder) |
| `gated_read(tab, *roles)` / `gated_write(tab, *roles, field=)` / `gated_create(tab, *roles)` | **the normal choice** — see precedence below |
| `gated_write_action(action, tab, *defaults)` | role list resolved **per request** from the `action_permissions` table (admin-editable, 60 s cache); a template grant of edit+ on the tab also satisfies it |
| `page_params` | `page≥1`, `limit` clamped 1..100, `sort_dir` |

⚠️ **Precedence redesigned Aug 2026 — templates are AUTHORITATIVE** (`crm_deps._gate`):
1. Admin/CEO → allowed, always.
2. User **has** a template/override → the template **alone** decides; roles are not consulted. This can grant beyond the role (Sales + template create on projects → Sales creates projects) and restrict below it (Sales_Head whose template omits projects loses it).
3. No template → the endpoint's role list, exactly as before templates existed.

Modes are a **ladder**: `view < edit < create` (`access_registry.mode_satisfies`). `gated_create` goes on the POST that brings a record into existence; the create endpoints of customers/opportunities/projects/candidates/profiles/requirements/employees/holidays use it. Self-service tabs (timesheets, leave-applications) deliberately stay at edit level so a view-only grant doesn't break an employee filing their own sheet. Pinned by `tests/test_access_template_authority.py`.

**Field-level enforcement** (`access_templates.reject_view_only_fields`): update endpoints for profiles/candidates/employees/opportunities map payload keys → registry field keys and 403 when a templated user's save touches a view-only field (error names the fields). A field grant wins over the tab mode in both directions — it can lock a field on an edit tab or unlock one on a view tab. Frontend twins: the Profile Commercials block, Candidate edit modal and Employee CTC disable locked inputs AND strip them from the payload (an unchanged echo of a locked field still counts as touching it). Opportunity type-specific details are governed by the single `details` key.

**Access Templates** (`services/access_templates.py` + `access_registry.py`): 21 grantable tabs × per-tab field catalogues (all tabs, `FIELDS_BY_TAB`) × tab modes `view|edit|create` (fields stop at `edit` — creation is record-level). `effective_access(db, user_id, roles)` → Admin/CEO get `{full: True}`; otherwise the assigned template is the base and the per-user `UserProfile.tab_access` override wins per key. `visible_tabs is None` means "unrestricted role defaults". ⚠️ An **inactive but still-assigned** template restricts to zero tabs — `scripts/diagnose_access.py <user> --tab <tab>` explains any user's effective access. Frontend mirror: `useAccess.ts::canAct/useCanAct` — pages pass `useHasRole(...)` as the untemplated fallback.

⚠️ **`enforce_roles()` / `main._enforce_crm_roles()` fails open by design** on 18 legacy endpoints: no bearer, no CRM DB, or zero CRM roles → the check no-ops. Only a user who *has* roles and matches none gets a 403.

---

## 4. Conventions you must follow

1. **Every CRM response goes through `schemas/common.py::envelope()`** → `{success, data, message, errors, meta?}`. The frontend's `crm/api.ts` unwraps exactly this and throws when `success === false` even on a 200.
2. **Route declaration order is load-bearing.** Literal paths must precede parametric ones. Existing traps: `/purchase-orders/reports/expiry` before `/{po_id}`; `/all-employees` and `/employees/{pe_id}` before `/{project_id}`; `/all-branches` before `/{customer_id}`; `invoices/tax-generator` before `invoices/{id}`; every `timesheets` literal before `GET /{timesheet_id}`.
3. **New CRM router → add it to `_MODULES` in `routers/crm/__init__.py`.** Registration is per-module via importlib specifically so one bad import doesn't 404 all ~266 endpoints — which has happened twice. `tests/test_crm_router_registry.py` guards this.
4. **New API prefix → three places:** `routers/crm/__init__.py`, the frontend's `vite.config.ts::API_PROXY_PREFIXES`, and `F-V2/scripts/vercel-build.mjs::apiPrefixes` (which regenerates `vercel.json`). Miss the third and it works locally and 404s on Vercel.
5. **Rejection reasons are ≥10 chars** (`schemas/common.py::RejectIn.validated_reason`) across opportunities, requirements, timesheets, leave.
6. **Deletes are usually 409-with-a-count, not cascade.** See `services/crm_delete.py`. Where destruction is allowed it is explicit (`?force=true`, often Admin/CEO only).
7. **Soft-delete for calendar-ish data**: holidays and leave policies deactivate (`is_active=False`), never `DELETE`.
8. **Money and hours are computed server-side.** Client-supplied `balance`, `amount`, `days` are recomputed or ignored. `employee_leave_balances.balance` is always `accrued + carry_forward − consumed`.
9. `models/` uses `pg_enum()` — native Postgres enums storing enum **values**. Tests shim `JSONB/ARRAY/UUID/INET` onto SQLite via `@compiles`.
10. `masters.py` deliberately omits `from __future__ import annotations` — FastAPI needs runtime annotations for its closure-scoped models.

---

## 5. Domain state machines (the parts that bite)

**Opportunity** — `OppType ∈ T&M | Work_Package | Fixed_Price | Retainer`. `PipelineStage ∈ New, Active, On_Hold, Closed_Won, Closed_Lost, Closed_Partial, Rejected, Archived` with `STAGE_TRANSITIONS` in `services/opportunities.py`; `Archived` is terminal and needs Sales_Head/Admin.
Approval: Sales-created → `Pending_Sales_Head_Approval`; Sales_Head/Admin-created → auto-approved. Approval **spawns exactly one Requirement** (idempotent) pre-stamped into `Pending_Engineering_Review`. `PUT` **merges** partial `details` JSONB and never nulls unsent keys; optimistic concurrency via `version` → 409.

**Requirement** — `Draft → Pending_Sales_Head_Approval → Pending_Engineering_Review → Open_For_Sourcing → Posted_On_Portals → In_Progress → Fulfilled`, plus `Sales_Head_Rejected`, `Engineering_Rejected`, `Closed`, `Cancelled`.
Engineering-approve **requires a JD** (`rmg_jd_text` or an `rmg_jd` attachment). First job posting auto-advances `Open_For_Sourcing → Posted_On_Portals`. Visibility: TA sees only sourcing-onward statuses and `ensure_visible` raises **404, not 403**, so existence never leaks.

**Candidate profile** — 20 `PipelineStatus` values. Forward: `Sourcing → Technical_Screening → RMG_Review → Sales_Screening → Customer_Screening → Customer_Interview → L1_Feedback → (L2_Feedback) → Shortlisted → Customer_Approval → Preboarding → Joined`.
`STAGE_AUTHORITY` decides who may move **out of** a stage: TA owns Sourcing/Technical_Screening, RMG owns RMG_Review, Sales owns the Screening pair, Sales+Sales_Head own the interview stages, and **Customer_Approval is Sales_Head only** — deliberate separation of duties. Entering `Customer_Approval` requires an existing offer.

**Timesheet** — `Draft | Submitted | Approved | Rejected` (UI labels them "Pending for Submission" / "Pending for Approval"). Approve/reject is `RMG, Sales` (+admin) — **HR is deliberately excluded from deciding** even though HR is on the notification list. This contradicts `KARNEX_CRM_USER_GUIDE.md`; the code is right.
Ledger effects apply as **deltas at both submit and approve**, so the pair never double-applies; reject/delete run `reverse_timesheet_ledger_effects`.
Billing precedence (bill XOR credit): `week_off_billable`/`holidays_billable` > `comp_off_billable` > comp-off credit. Any billing path means zero comp-off earned that day. Unworked week-off/holiday days bill a full `min_hours_full_day` when their flag is on (calendar-month model). Comp-off balance/limit fields in the Customer form apply only when `comp_off_billable` is **off** (credit mode) — the UI disables and nulls them when it is on.
Policy inheritance everywhere: **project override → branch → customer default → built-in default**, resolved field-by-field.
**LOP reduces Monthly bills** (13 Aug 2026, `timesheet_invoice_preview`): the leave-billable Monthly path used to charge the full month regardless of LOP. Now each LOP day (over-balance leave, explicit LOP type, Absent, unworked half-days — the same `total_loss_of_pay_days` figure) deducts `rate × lop / working_days_in_window`, and the line's `qty` becomes `amount / rate` so Qty × Rate always reconciles. Hourly/Daily and the non-leave-billable Monthly ratio already zeroed LOP days — no deduction there or it would double-count. Pinned by `test_midmonth_timesheet.py::test_lop_reduces_a_monthly_invoice`.

**Leave** — accrual is a **job, not a trigger**: `services/project_employee_leave_credit.py`. `run_pe_leave_credit()` credits only the `as_of` month and never backfills, and `apply_year_end_carry()` acts only on 31 December.
Since Aug 2026 the scheduler runs it daily (`JOBS["pe_leave_credit"]` → `scheduler.run_pe_leave_credit_job`) and repairs itself: `missing_credit_periods()` finds closed months with no `pe_credit:` ledger row and replays them **at month end**, which also replays a missed 31 Dec carry. Replay is safe because credits are keyed `pe_credit:{pe}:{type}:{YYYY-MM}`. The `leave.credit_repaired` email fires only when a repair actually **moved balances** — a month where nothing was accruable looks identical to a month that never ran.
Deliberate repairs beyond `scheduler.pe_leave_credit_lookback` (12 months) go through `scripts/run_pe_leave_credit.py --from YYYY-MM [--to] [--all-periods] [--dry-run]`.

**Invoice/PO** — invoice cannot exceed PO balance (400). `GET /{id}/po-options` lists candidates; with multiple live POs and no `po_id` the generate call 400s rather than silently picking. Invoice is dated the generation day; `due_date` parsed from PO `payment_terms` ("Net 30 Days" → 30).
`POST /purchase-orders/{id}/renew` raises the next PO in a series, inheriting customer/branches/contact/type/tax/terms and linking via `renewed_from_po_id` (migration 0068). Three deliberate non-behaviours: the old PO is **not** touched (invoices may be in flight), unspent balance does **not** carry over, and allocations are **not** copied. `po_number` is required — it is the customer's reference, so generating one would invent a document.

---

## 6. Interview engine essentials

- Sessions are a **plain in-process dict** (`session.py`), as is proctor state. **`UVICORN_WORKERS` must stay 1.** The Redis-backed session store in the docstring is not implemented.
- Two entry points: `POST /setup` (HR direct) and `POST /candidate/invite/{token}/login` (session key `inv:{token}`, device-bound via the `x-device-id` header).
- `GET /next` → `candidate/service.py::next_question_payload`. **The server owns the clock and the count**; the warm-up question ("Please introduce yourself.") is index 0 and is invisible to the progress UI and to scoring.
- `POST /answer` is idempotent per turn index; skips can be promoted to answers when VAD evidence shows the candidate spoke (**409 `speech_blocked`** otherwise).
- Transcription is **not** Whisper-the-model: `OPENAI_TRANSCRIBE_MODEL`, default `gpt-4o-mini-transcribe`. TTS: `gpt-4o-mini-tts` / voice `nova`, with an in-process LRU prewarm cache.
- Scoring runs deterministic guards in `ai.py` **before** any model call (echo detection, keyword-only, relevance, technical depth), then three separate rubrics. `merge_per_question_eval_into_report` **overwrites `overall_score`** with the mean of evaluable per-question scores; the skill model's number survives only as `skill_model_overall_score`.
- `POST /submit`: candidates get a fast fallback report (`report_status="ready_pending_ai"`) upgraded by a BackgroundTask; HR gets the synchronous path. A startup recovery worker finalizes stale sessions as `recovered`.
- **Dead code warning**: `backend/prompts/interview/*` and `validators/interview/validate_question_objects` have no production callers (tests only). The live question prompt is `services/interview/question_service._single_template_prompt` — a **user message with no system message**.

**Ask AI** (`ai_help/`) is a separate read-only CRM feature. Knowledge base is `ai_help/entries.py` (18 `HelpEntry` TypedDicts) + `business_rules.py`; `all_entries()` is `lru_cache`d so **KB edits need a process restart**.

`enable_tools` is now real (Aug 2026). `ai_help/tools.py` holds 10 **SELECT-only** query tools; `assist.py::_run_tool_rounds` gives the model up to `_MAX_TOOL_ROUNDS = 3` lookups and then drops the tool list so the last call must answer. Rules that must survive any edit to that file:

- Permission filtering happens in `routers/crm/ai_assist.py`, where the current user is known. A tool the caller may not run is **never offered** — and `run_tool` re-checks at execution, so the offer list is not the security boundary on its own.
- `FINANCE_ROLES` there is identical to the set in `crm_data.py` on purpose. Two different answers to "may this person see revenue" is a bug waiting to happen.
- Tools **never raise**. Errors come back as `{"error": ...}` so the model can retry or apologise usefully instead of the whole assist call 502-ing.
- Rows are clamped (`MAX_ROWS = 25`) and payloads capped (`_MAX_RESULT_CHARS = 4000`).
- `tracked_chat_completion` gained `tools` / `tool_choice` passthrough so tool calls stay inside the existing logging and retry path.

---

## 7. Running it

```bash
# Windows, the normal path (runs alembic upgrade head, then uvicorn on :2020)
start_app.bat                      # --http | --https | --no-browser
rebuild_all.bat                    # builds the F-V2 dashboard first, then start_app

# Docker
docker compose up -d --build       # monolith + postgres on :2020
docker compose --profile cache up -d   # + redis

# Migrations (CRM tables only; legacy tables are excluded in alembic/env.py)
cd backend && python -m alembic upgrade head && python -m alembic current   # head = 0079
# 0077: customer_rate_cards.branch_id — rate cards are BRANCH-wise (NULL =
# customer-wide fallback; uniqueness + overlap checks scoped per branch).
# 0079: customer_rate_cards.effective_from — VERSIONED slab ladders. A new
# ladder (entered whole via Build Ladder, Add-band removed) supersedes the old
# one from its date; overlap + uniqueness scoped per (branch, version). The
# Opportunity form prices from the version current TODAY (NULL = since
# forever); future ladders wait, expired ones stay as history.
# 0078: billable_leaves_per_year on customer_billing_policies AND
# customer_branches (branch wins) — the "APTIV rule": paid leaves the customer
# bills even when leave is not billable. Resolved via branch_policy
# _EFFECTIVE_FIELDS, prefilled+locked into the Opportunity form (paid_leaves
# detail key, in the T&M parity set), and ctcSlab.calculateBillingBases adds
# min(paid, leave-deducted) back: 365−104−24−10 = 227, +18 paid = 245 days.
# WEEKEND WORK COVERS LOP (the "Harman rule", timesheet_summary): in comp-off
# CREDIT mode a worked week-off/holiday day first makes up an LOP day —
# summary/preview report NET LOP (lop_covered_days exposed), the Monthly
# invoice bills the full month, and the covering fraction earns NO comp-off
# (accrue_comp_off reads the summary's net figure). Billed modes untouched.
# Pinned by test_midmonth_timesheet.py::test_weekend_work_covers_lop and
# test_branch_policy_uc.py::test_billable_leaves_per_year_resolves….
# 0076: customer_rate_cards — per-customer experience-band pricing (1–2 yrs …)
# with five NULLABLE rate columns (hourly/daily/weekly/monthly/yearly; blank =
# "not quoted", never zero). API /api/rate-cards (routers/crm/rate_cards.py),
# gated Sales/Sales_Head (+Admin/CEO) via the "rate-cards" registry tab; bands
# may not overlap. Feeds the Opportunity form's CTC Slab rate auto-fill.
# 0069: projects.opportunity_id is now NULLABLE — projects need no sales
# opportunity (the wizard no longer asks). All branch/serializer paths are null-safe.
# 0070: pre-ladder template grants "edit" → "create" (the old editor's max mode
# was labelled "Insert / Edit" and creation endpoints enforced only edit, so
# "edit" templates could create; the ladder would have silently demoted them).
# Per-user legacy overrides likewise resolve as "create" in effective_access.
# 0071: notification_routes gains subject_template/body_template — per-event
# email WORDING is admin data now, applied in email_outbox.queue_email via
# _apply_event_template (tokens {subject} {body} {recipient} {company}; plain
# replace, never str.format — a typo'd token renders literally, never drops mail).
# 0072: week_off_days CSV (0=Mon..6=Sun) on customer_billing_policies /
# customer_branches / projects; resolved like every other policy field and
# honoured by classify_calendar_day (parse_week_off_days: any bad token
# invalidates the level entirely so it falls through the chain).
# 0073: timesheets.period_start_date/period_end_date — per-sheet window
# override (PATCH /{id}/period); NULL = derived from PE onboarding/exit.
# 0074: invoice_lines.qty Numeric(10,2)→(12,4). A Monthly qty is the billed
# FRACTION of the month; at 2dp, qty×rate drifted up to 1% of a month from the
# amount. The engine now REDEFINES amount = qty(4dp)×rate so stored line, PDF
# and GST base reconcile exactly (max drift rate×0.00005). Both the preview
# route AND generate-invoice pass lines=[] to karnex_gst_tax_and_grand so GST
# is always computed on the exact sub-total, never re-derived from qty×rate.
# Related fixes (13 Aug 2026): _is_comp_off_work_day treats day_type as
# AUTHORITATIVE (a Working Saturday bills, never credits comp-off — the old
# weekday>=5 fallback did both) and honours policy.week_off_days for legacy
# rows with no day_type. Entry-date validation in upsert_entries uses
# sheet_period_bounds (the PATCHed window), not pe_period_bounds. Tax-invoice
# column labels (UNIT_LABELS by PE billing_unit) are exposed via
# invoice_unit_labels() on GET /api/invoices/{id} (qty_label/rate_label) so
# the on-screen View matches the PDF, retroactively for old invoices.
# 0075: timesheets.approved_figures JSONB — invoice figures FROZEN at
# approval (line items + sub-total + summary, json-sanitised default=str).
# Every read recomputes billables from the CURRENT policy, so approval used
# to freeze nothing. Now: approve snapshots (after consume_timesheet_leaves,
# so the paid-vs-LOP split is final); _apply_frozen_figures() swaps them into
# invoice-preview AND generate-invoice, exposing figures_drifted /
# live_sub_total / frozen_at when a live recompute disagrees (billed = frozen,
# drift is logged INVOICE_FIGURES_DRIFTED). Reject clears the snapshot — and
# reject now also accepts APPROVED sheets (until an invoice exists → 409) as
# the correction path. Legacy pre-0075 approvals have NULL → live figures.
# Comp-off billed work now ADDS to a Monthly bill (13 Aug 2026): billed
# week-off/holiday day-fractions are priced at (rate × presence)/working_days
# on top of the flat month (Hourly/Daily always carried them in-quantity),
# and are EXCLUDED from the non-leave-billable ratio numerator (they used to
# double-pay on partial months and vanish on full ones via the min-1 cap).
# Also settings-driven now (services/org_settings.KEYS → DB row → env → default):
# TDS rate (finance.tds_rate_percent), the whole invoice seller/bank block
# (invoice.*, consumed by company_invoice_config), and uitext.* copy overrides
# served by GET /api/ui-text (status tooltips + empty-state lessons).

# Tests — run from backend/, no live DB needed
cd backend && python -m pytest -q
pip install python-multipart httpx  # one-time, needed by the TestClient suites
```

There is **no `pytest.ini` / `pyproject.toml` / `conftest.py`** anywhere. Tests must run from `backend/` or imports fail; CI works around this with `python -m pytest backend/tests` from the root.

~3–6 known failures out of ~326, documented in `CLEANUP_REPORT.md`: `test_boundary_question_finalize`, `test_password_security` ×2, `test_rmg_timesheet_reports`, `test_timesheet_entry_grid` ×2.

---

## 8. Environment

Non-negotiable in production: **`AUTH_SECRET`** (≥32 bytes, app refuses to boot without it), `AUTH_DB_URL` or the `DB_*` parts, `CORS_ALLOW_ORIGINS`, `PUBLIC_BASE_URL` (invite links are built from it), `KARNEX_ENV=production`.

OpenAI keys are **per-purpose** with fallback to the master key — `openai_client.py`, purposes `default, tts, question, eval, transcribe, ats`: `OPENAI_API_KEY`, `OPENAI_TTS_API_KEY`, `OPENAI_QUESTION_API_KEY`, `OPENAI_EVAL_API_KEY`, `OPENAI_TRANSCRIBE_API_KEY`, `OPENAI_ATS_API_KEY`. The literal `your_key_here` counts as unset.

Other clusters: `SMTP_*` + `EMAIL_*` (outbox worker), `SCHEDULER_*`, `INTERVIEW_*` (behaviour flags, incl. `INTERVIEW_SAFE_MODE` which disables all OpenAI calls), `CRM_AI_L1_*`, `TDS_RATE_PERCENT` (default 10), `RATE_LIMIT_ENABLED`, `PROMPT_LOG_*`.

⚠️ `.env.example` ships `AUTH_SECRET=change_this_jwt_secret` — 23 bytes, so a copy-pasted example **will not boot**. That is intentional.

⚠️ Rate limits are **off by default** outside production, and `rate_limit.limit()` binds the limiter at *decoration* time — if `setup_rate_limit(app)` hasn't run first, every `@_rl.limit(...)` is a silent no-op.

---

## 9. Known state, debt and traps

- **`services/` (repo root) is scaffolding.** 142 lines total; `karnex_proxy/app_factory.py` builds a catch-all httpx proxy to the monolith. Zero domain logic, no DB, no auth. CI builds the images; production runs the monolith only. RabbitMQ is provisioned in the overlay and used by nothing. `k8s/` has manifests for 2 of 4 services and no kustomization. Treat all of it as aspirational.
- **Port mismatch**: base compose publishes the monolith on 2020; the microservices overlay and nginx upstreams assume `ai-interview:8010`.
- **Two security-headers middlewares** are both registered in `main.py` (lines ~2531 and ~2678) with conflicting CSP values.
- **Alembic revisions 0026 and 0027 do not exist** — `0028` points back at `0025`. Intentional-looking, but don't "fix" it.
- `main.py` is 316 KB and mixes app assembly, auth, HR endpoints, proctoring, ATS and static-build orchestration.
- Four deliberate duplications (see `FEATURE_INVENTORY.md`): two ATS engines, two customer/opportunity stores (`/masters/*` aliases still read by the old template UI), two role systems, two schema mechanisms.
- `POST /api/admin/nexus/seed` and `/api/admin/nexus/leave-credit` in `users_admin.py` are labelled TEMPORARY test support and are live behind Admin/CEO.
- `finance.py` exposes invoice update via `@router.api_route(methods=["PUT","PATCH"])` — a grep for `@router.put` misses it.
- **ISSUE-1 closed (12 Aug 2026)**: `compute_billables()` now honours `week_off_billable` fully — worked weekend hours bill as normal time, and an **unworked** week-off day bills a full day, symmetric with `holidays_billable` on unworked holidays. Calendar-month billing (both flags on → 31/31 days) is pinned by `tests/test_weekoff_billable_day.py`. The client mirror in `Timesheets.tsx::computeBillables` matches.
- Not implemented despite being in the runbook: AES-256-GCM at rest for candidate PII, data-retention / candidate-delete, TLS termination (HSTS middleware exists and activates under TLS).
- `_to_delete/` is a gitignored quarantine bin for stale git locks — safe to delete. `newfiles/` is a redundant source copy of already-applied email-outbox files.
- Working tree state (12 Aug 2026): branch **`wip/pipeline-users-admin`**, ~61 files genuinely changed, 30 untracked including migrations **0064–0067**, the payroll/scheduler/outbox/action-permission services, and the root `karnex_*.py` self-applying patch scripts. Nothing is merged to `main`.

---

## 10. Docs worth reading before big changes

| File | Why |
| --- | --- |
| `KARNEX_CRM_README.md` | bootstrap + env; **stale** on table/migration counts |
| `KARNEX_CRM_USER_GUIDE.md` | role walkthrough; **diverges from code** on HR timesheet approval, CEO, and Customer_Approval authority |
| `FEATURE_INVENTORY.md` | 271 endpoints / ~52 tables inventory + the four duplications + cleanup candidates |
| `TIMESHEET_LOGIC_TEST_REPORT.md` | ISSUE-1..5, still partly open |
| `LEAVE_SCENARIO_TEST_REPORT.md` | the monthly-credit-job operational risk, in detail |
| `QA_LEAVE_SYSTEM_TEST_REPORT.md` | live-DB leave verification + teardown SQL |
| `TEST-CASES.md` / `TEST-RESULTS.md` | the Project-Employee UC-01..12 acceptance spec and the 3 real bugs it found |
| `ACCESS_TEMPLATES_HANDOFF.md` | what's done vs what still needs `Depends(require_access(...))` attached |
| `PRODUCTION_RUNBOOK.md` | deploy steps + the outstanding hardening list (migration numbers are stale) |
| `NEXUS_VS_KARNEX_GAP_ANALYSIS.md` | the 8 prioritised product gaps vs the Zoho app being replaced |
