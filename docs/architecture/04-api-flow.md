# 4. API Request/Response Flow Diagrams

Two API surfaces share the same host and JWT:

- **Legacy interview API** — routes defined directly in `main.py` (`@app.*`), authorized
  by the JWT `role` claim via `_require_user`.
- **Karnex CRM API** — 19 routers under `/api/*`, authorized by DB-backed RBAC via
  `role_required`, returning a uniform envelope `{success, data, message, errors, meta}`.

## 4.1 API surface map

```mermaid
graph LR
    subgraph legacy["Legacy Interview API (main.py)"]
        auth["/auth/* — register, login, me, logout"]
        setup["/setup, /extract-skills"]
        loop["/next, /answer, /submit, /report"]
        media["/candidate/tts, /candidate/transcribe, /candidate/validate-speech"]
        invite["/candidate/invite/{token}[/verify|/login]"]
        hrrec["/hr/*, /hr-records, /hr-record/{id}"]
        job["/job/config[s], /job/template/*"]
        atsapi["/ats/score[/upload], /candidates/ranked"]
        proctor["/proctor/*, /interview/violation, /interview/integrity-logs"]
        health["/health/live, /health/ready, /version, /models"]
    end

    subgraph crm["CRM API (/api/*)"]
        me["/api/me"]
        customers["/api/customers · /all-branches · /api/opportunities"]
        req["/api/requirements · attachments · /api/resumes"]
        candp["/api/candidates · /api/candidate-profiles"]
        aiint["/api/candidate-profiles/{id}/ai-interviews"]
        proj["/api/projects · /all-employees · /employees/{pe_id} · rates · /api/timesheets"]
        leave["/api/leave-applications · /api/holidays · /api/holiday-names · /api/customer-leave-policies"]
        finance["/api/purchase-orders · /api/invoices · /api/tds"]
        emp["/api/employees"]
        mast["/api/{masters} · /api/settings"]
        usersadm["/api/users (Admin)"]
        dash["/api/dashboard/* · /api/reports/*"]
        notif["/api/notifications · /api/crm-files/*"]
    end

    subgraph observ["Admin/Observability (routers/admin.py)"]
        plogs["/api/prompt-logs[/stats|/filters|/export]"]
        usage["/admin/ai/usage"]
    end
```

## 4.2 CRM request/response envelope flow

```mermaid
sequenceDiagram
    autonumber
    participant UI as React CRM (crm/api.ts)
    participant API as FastAPI /api/* router
    participant Dep as crm_deps (auth chain)
    participant Svc as services/*
    participant DB as PostgreSQL

    UI->>API: GET /api/customers?page=1 (Bearer JWT)
    API->>Dep: get_crm_db()
    alt Postgres not configured
        Dep-->>UI: 503 CRM unavailable
    end
    API->>Dep: get_current_user() + role_required(...)
    Dep->>DB: SELECT user by sub + roles (user_roles)
    alt inactive / missing role
        Dep-->>UI: 403 Forbidden
    end
    Dep-->>API: CurrentUser(id, roles)
    API->>Svc: paginate + serialize (to_dict)
    Svc->>DB: SELECT ... LIMIT/OFFSET
    DB-->>Svc: rows
    Svc-->>API: items + meta
    API-->>UI: 200 {success:true, data:[...], meta:{page,total,pages}}
    UI->>UI: unwrap envelope, render (401→clear session+reload)
```

## 4.3 Legacy interview auth flow (role-claim)

```mermaid
sequenceDiagram
    autonumber
    participant UI as Static JS (core.js apiFetch)
    participant API as FastAPI @app.* (main.py)
    participant Auth as _require_user
    participant DB as auth_db (raw SQL)

    UI->>API: POST /answer (Bearer JWT, x-device-id)
    API->>Auth: _require_user(request, {"hr","candidate"})
    Auth->>Auth: _decode_token_from_header (HS256, exp)
    alt invalid/expired
        Auth-->>UI: 401 Unauthorized
    else role not allowed
        Auth-->>UI: 403 Forbidden
    end
    Auth-->>API: (payload, None)
    API->>API: _enforce_invite_device_binding (single device)
    API->>DB: append answer, persist interview_progress
    API-->>UI: 200 next-question payload
```

## 4.4 File upload flow (multipart, e.g. resume / CV / documents)

```mermaid
flowchart LR
    up["POST multipart<br/>(e.g. /api/requirements/{id}/resumes)"] --> role{"role_required('TA')"}
    role -->|denied| f403["403"]
    role -->|ok| save["crm_common.save_upload()<br/>uuid filename under data/crm_uploads/"]
    save --> url["stored URL: /api/crm-files/&lt;rel&gt;"]
    url --> row["insert DB row (resumes/documents)"]
    row --> resp["200 envelope with file URL"]
    serve["GET /api/crm-files/{rel_path}"] --> guard["resolve_crm_file()<br/>reject path traversal"]
    guard --> file["stream file (any_crm_role)"]
```

## 4.5 Per-user tab access (Admin/CEO)

Admin/CEO assigns an explicit allow-list of UI tab keys per user via
`POST /api/users/{id}/tab-access`. Stored in `user_profiles.tab_access` (JSON text).
`NULL` → role-based sidebar defaults; non-null → only listed keys appear in the shell.

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Admin/CEO (Users page)
    participant API as POST /api/users/{id}/tab-access
    participant DB as user_profiles.tab_access
    participant Shell as App.tsx + CrmApp.tsx

    Admin->>API: {tabs: ["crm:customers","iv:ats",...]} (Bearer JWT)
    API->>DB: UPSERT tab_access JSON
    DB-->>API: saved
    API-->>Admin: 200 {success, data:{tab_access}}
    Note over Shell: On next /api/me, user receives tab_access
    Shell->>Shell: tabVisible(null) → role defaults<br/>tabVisible([...]) → Admin allow-list
```

## 4.5b Access Templates (department/role-wise modes)

Reusable templates (`access_templates`) grant per-tab and per-field **view** or **edit**
modes. Users link via `user_profiles.access_template_id` (live — edits propagate).
`services.access_templates.effective_access()` merges template + per-user override (override wins).
Admin/CEO always get `full: true`.

**Enforcement:** `crm_deps.gated_read(tab, *roles)` / `gated_write(tab, *roles)` on major CRM
routers (customers, opportunities, projects, finance, employees, timesheets, …).

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Admin/CEO
    participant TAPI as /api/access-templates/*
    participant Me as GET /api/me
    participant CRM as gated CRM endpoints

    Admin->>TAPI: POST template {tab_access, field_access}
    Admin->>TAPI: POST /assign {user_id, template_id}
    Me-->>Admin: access {tabs, fields, visible_tabs, source}
    CRM->>CRM: effective_access → 403 if view-only on write
```

## 4.6 Response caching & rate limiting (cross-cutting)

```mermaid
flowchart TB
    subgraph frontend["Frontend cache (api/client.ts)"]
        fc["apiGet: in-memory TTL cache + inflight dedupe<br/>for /hr/dashboard, /job/configs, /hr/schedules, logs"]
        inv["apiPut/Patch/Delete → invalidate cache"]
    end
    subgraph backend["Backend guards"]
        rl["rate_limit.py — per-IP windows<br/>/auth/login 10/min · /answer 60/min<br/>/candidate/tts 40/min · /transcribe 30/min"]
        rc["response_cache.py — optional OpenAI response cache<br/>(OPENAI_RESPONSE_CACHE_ENABLED)"]
    end
    fc --> backend
    inv --> fc
```

## 4.7 Project Employee ownership (delivery bridge)

```mermaid
flowchart LR
    map["POST /api/projects/{id}/employees"] --> seed["Seed leave_details from<br/>CustomerLeavePolicy copy<br/>+ pe_seed LeaveAccrualEvent<br/>+ ensure current rate row"]
    list["GET /api/projects/all-employees<br/>?project_id&customer_id&status&group_by=employee"] --> enrich["Flat rows OR nested mappings[] · leave_balance_total · po_status"]
    detail["GET /api/projects/employees/{pe_id}"] --> nested["leave_details · leave_eligibility · leave_summary<br/>credit_history · leave_applications<br/>rates · timesheet_rollups · holidays · holiday_calendar · po"]
    rates["POST/PUT .../rates"] --> sync["Sync pe.billing_rate<br/>when is_current_rate"]
    leaveApp["POST /api/leave-applications<br/>project required when active PE"] --> peBal["Debit PE leave_balance on approve"]
    credit["scripts/run_pe_leave_credit.py"] --> peBal2["Credit PE leave_details<br/>skip is_exit"]
    exitEp["POST .../employees/{pe_id}/exit"] --> stop["Stop accrual · flag open TS<br/>settlement_pending + leave balances"]
    tsLock["POST /api/timesheets/{id}/entries"] --> holLock["Client calendar holidays locked<br/>day_type · view_flag · entry_project_id<br/>billable_day display = hours÷8"]
    tsCreate["POST /api/timesheets generate_days=true"] --> autoDay["Auto-classify Working/Week_Off/Holiday<br/>default 8.50h working · 0 off-days"]
    tsSubmit["POST /api/timesheets/{id}/submit"] --> noPo["Never blocks on PO"]
    tsRpt["GET /api/timesheets/reports/{due|for-submission|approvals}"] --> rmg["RMG/HR/Finance gated_write"]
    tsAtt["POST/GET /api/timesheets/{id}/attachments<br/>DELETE /api/timesheets/attachments/{id}"] --> multi["Multi-file + legacy file_attachment_url"]
    inv["POST .../generate-invoice"] --> rateEng["timesheet_invoice_preview<br/>RateRow + split_period_by_rate"]
    inv --> poGate["assert_po_allows_new_drawdown<br/>expiry / balance 100%"]
```

## 4.7 Customer Holiday Calendar (branch-year + names master)

```mermaid
flowchart LR
    names["GET/POST /api/holiday-names<br/>HR write"] --> master["holiday_names table"]
    global["GET/POST/PUT/DELETE /api/holidays<br/>HR write · global/customer scope"]
    branch["GET/POST/PUT/DELETE<br/>/api/customers/branches/{id}/holiday-years/{year}/holidays<br/>read_branch_policy · write_branch_policy"]
    freeze["PATCH .../holiday-years/{year_id}<br/>is_freeze blocks branch date edits"]
    master --> hol["holidays · holiday_name_id · observance Mandatory|Optional<br/>optional holiday_calendar_id → branch_holiday_years"]
    branch --> hol
    global --> hol
    freeze --> bhy["branch_holiday_years"]
    hol --> ts["holidays_for_project_period<br/>Mandatory only → timesheet Holiday/0h"]
```
