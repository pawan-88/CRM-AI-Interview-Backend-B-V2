# 2. Module-wise Flow Diagrams

## 2.1 Backend module map

```mermaid
graph TB
    main["main.py<br/>(FastAPI app, legacy interview endpoints,<br/>startup, static hosting)"]

    subgraph routers["routers/"]
        r_admin["admin.py<br/>prompt logs, AI usage"]
        subgraph rcrm["routers/crm/ (19 modules)"]
            rc1["me · customers · opportunities"]
            rc2["requirements · resumes · candidates"]
            rc3["candidate_profiles · ai_interviews"]
            rc4["projects · timesheets · finance"]
            rc5["employees · masters · settings"]
            rc6["users_admin · notifications · files"]
            rc7["dashboards · reports"]
        end
    end

    subgraph services["services/"]
        s_bridge["ai_interview_bridge.py"]
        s_common["crm_common.py (paginate, to_dict, activity log, CSV, files)"]
        s_domain["customers · opportunities · opportunity_ctc · requirements<br/>candidates · candidate_profiles · projects<br/>project_employees · project_employee_billing<br/>project_employee_leave_credit · timesheets · finance · leave<br/>employees · masters · resumes · ats_scoring · reports · dashboards · users_admin"]
        s_tax["tax.py (GST/TDS) · invoice_pdf.py · notify.py"]
        s_openai["openai/chat.py"]
        s_interview["interview/question_service.py"]
    end

    subgraph logic["Interview logic (non-service)"]
        ai["ai.py (gen, eval, TTS, STT)"]
        ats["ats.py (ATS scoring)"]
        validators["validators/interview/*"]
        utils["utils/* (auto_advance, score_exclusion,<br/>time_warnings, speech_validation,<br/>strengths_weaknesses, warmup, uniqueness)"]
        hr["hr/* (repository)"]
    end

    subgraph dataacc["Data access"]
        authdb["auth_db.py (raw SQL)"]
        crmdb["crm_db.py"]
        models["models/* (ORM)"]
        promptlog["prompt_logger.py"]
        alembicm["alembic/versions/*"]
    end

    deps["crm_deps.py<br/>get_crm_db · get_current_user · role_required"]
    sessionm["session.py · rate_limit.py · response_cache.py"]

    main --> routers
    main --> ai
    main --> ats
    main --> authdb
    main --> sessionm
    main --> promptlog
    rcrm --> deps
    rcrm --> services
    r_admin --> promptlog
    services --> models
    s_bridge --> authdb
    s_bridge --> models
    deps --> crmdb
    crmdb --> models
    models --> alembicm
    ai --> s_openai
    s_interview --> s_openai
    ai --> validators
    ai --> utils
    main --> hr
```

## 2.2 Frontend module map

```mermaid
graph TB
    subgraph static["Static UI (frontend/js/)"]
        appjs["app.js (orchestrator: invite→device→login→screens)"]
        core["core.js (apiFetch: Bearer + x-device-id, 401 handling)"]
        state["state.js (interview state)"]
        auth["auth/* (session, sharedAuth, hrAuth, authMotion)"]
        subgraph candidate_side["Candidate interview engine"]
            candjs["candidate.js"]
            engine["interview_engine.js"]
            autoadv["interview_auto_advance.js"]
            whisper["interview_whisper_segments.js"]
            vad["vad_silero.js"]
            security["interview_security.js"]
            face["face_detection.js"]
            finalize["interview_finalization.js"]
            timew["interview_time_warnings.js"]
            devtest["device_test.js · invite_device.js"]
        end
        subgraph hr_side["HR side"]
            hrjs["hr.js (setup, templates, ATS, schedule)"]
            hrsetup["hrSetupUi.js · hrAccessDetails.js"]
            results["results.js (reports, exports)"]
        end
        scene["scene.js · avatar.js · brandLogo.js"]
    end

    subgraph react["React Admin Dashboard (admin-dashboard/src)"]
        maintsx["main.tsx (ThemeProvider, cache/version sync)"]
        apptsx["App.tsx (product shell, ?view= router)"]
        topbar["PlatformTopBar (sticky tabs · More overflow · ⌘K · account menu)"]
        pages["pages/* (HrDashboard, Templates, Candidates,<br/>CandidateReport, ATS, PromptLogs, Integrity,<br/>UpcomingInterviews, QuestionBank*)"]
        apiclient["api/client.ts (authFetch, apiGet cache)"]
        subgraph crmapp["crm/ (embedded Karnex CRM)"]
            crmroot["CrmApp.tsx (?view=crm&p=, /api/me roles;<br/>desktop sidebar: expanded ↔ icon rail → localStorage)"]
            crmnav["nav.ts (CRM_NAV shared with ⌘K palette)"]
            crmrouter["router.tsx · routes.ts (mini-router)<br/>?view=crm&amp;p=path · filters as sibling qs"]
            crmapi["api.ts (envelope: success/data/meta)"]
            crmpages["pages/* (Customers, BranchPolicy, OpportunitiesWorkspace,<br/>Opportunities, Requirements, Profiles, Projects,<br/>ProjectEmployees, ProjectEmployeeDetail,<br/>LeaveApplications, Holidays, Timesheets,<br/>Finance, Employees, Reports, UsersAdmin, Settings)"]
        end
    end

    appjs --> core
    appjs --> auth
    candjs --> core
    candjs --> state
    candjs --> engine
    candjs --> autoadv
    autoadv --> vad
    autoadv --> whisper
    candjs --> security
    security --> face
    candjs --> finalize
    hrjs --> core
    results --> core
    apptsx --> topbar
    apptsx --> pages
    apptsx --> apiclient
    apptsx --> crmroot
    topbar --> crmnav
    crmroot --> crmnav
    crmroot --> crmrouter
    crmroot --> crmapi
    crmrouter --> crmpages
    crmapi --> apiclient

    core -->|HTTP| backend["FastAPI backend"]
    apiclient -->|HTTP| backend
```

**Platform top bar:** sticky glass header in `components/platform-nav/PlatformTopBar.tsx`
(section tabs with **More** overflow on narrow widths, hamburger on mobile, **Interview Schedule**
CTA, ⌘K/Ctrl+K command palette, single account dropdown for theme / role label / profile /
logout). Destinations and RBAC unchanged from the former inline header in `App.tsx`.

**CRM mini-router filters:** `crmNavigate("timesheets?project_id=1&employee_id=2")` sets
`p=timesheets` plus sibling query keys (never baked into `p`). `TimesheetsListPage` reads
`project_id` / `employee_id` from `window.location.search` on mount. Used by Project
Employee detail → Timesheet tab → **Open timesheets**.

## 2.3 Backend request pipeline (CRM route)

```mermaid
flowchart LR
    req["HTTP request<br/>/api/..."] --> mw["Middleware<br/>(CORS, security headers,<br/>request logging, rate limit)"]
    mw --> dep1{"get_crm_db<br/>Postgres configured?"}
    dep1 -->|no| e503["503 CRM unavailable"]
    dep1 -->|yes| dep2["_decode_bearer<br/>verify JWT"]
    dep2 -->|invalid| e401["401 Unauthorized"]
    dep2 --> dep3["get_current_user<br/>load user + roles<br/>(user_roles)"]
    dep3 -->|inactive| e403a["403 deactivated"]
    dep3 --> dep4{"role_required<br/>role ∈ allowed ∪ Admin?"}
    dep4 -->|no| e403b["403 Forbidden"]
    dep4 -->|yes| handler["Router handler"]
    handler --> service["services/* domain logic"]
    service --> orm["SQLAlchemy models → Postgres"]
    orm --> env["envelope(success, data, meta)"]
    env --> resp["JSON response"]
```

## 2.4 Interview turn loop (module interaction)

```mermaid
flowchart TB
    next["GET /next → next_question()"] --> tts["POST /candidate/tts (OpenAI TTS)"]
    tts --> play["candidate.js plays audio<br/>(mic muted while AI speaks)"]
    play --> rec["Mic auto-records after audio ends"]
    rec --> vadmod["vad_silero.js detects speech"]
    vadmod --> stt["POST /candidate/transcribe (Whisper)"]
    stt --> decide{"auto-advance?<br/>(interview_auto_advance.js)"}
    decide -->|"speech complete / timeout"| answer
    decide -->|"manual Send/Skip"| answer["POST /answer → answer()"]
    answer --> valid["utils/speech_validation<br/>utils/auto_advance<br/>validators/interview"]
    valid --> evalbg["_schedule_turn_evaluation<br/>(background: OpenAI eval)"]
    answer --> nextq{"more questions?"}
    nextq -->|yes| next
    nextq -->|no| submit["POST /submit → finalize + report"]

    par["Parallel: proctoring<br/>face_detection.js + interview_security.js<br/>→ POST /interview/violation"] -.-> answer
```

## 2.5 Opportunity-type Candidate CTC Slab calculation flow

```mermaid
flowchart LR
    inputs["Opportunity Type · Rate · Duration<br/>Billing Type · Hours/Day · Leave/Holidays<br/>Mgmt Cost % · Hike %"]
    bases["Actual Billing Days<br/>365 − non-billable deductions"]
    hours["Actual Billing Hours<br/>Days × Hours/Day"]
    type{"Opportunity Type"}
    billing{"T&M Billing Type"}
    annual["Annual Revenue"]
    monthly["Monthly = Annual / 12"]
    budget["Engineering Budget<br/>Annual × (1 − Mgmt Cost %)"]
    ctc["Approved CTC<br/>Budget / (1 + Hike %)"]
    api["POST/PUT /api/opportunities<br/>server recalculates with Decimal"]
    db["opportunity_ctc_slabs"]

    inputs --> type
    type -->|"T&M"| bases --> hours --> billing
    billing -->|"Per Hour: Rate × Hours"| annual
    billing -->|"Per Day: Rate × Days"| annual
    billing -->|"Per Month: Rate × 12"| annual
    billing -->|"Per Year: Rate"| annual
    type -->|"Work Package: Rate"| annual
    type -->|"Fixed Price: Rate ÷ (Months ÷ 12)"| annual
    type -->|"Retainer: Rate × 12"| annual
    annual --> monthly
    annual --> budget --> ctc
    ctc --> api --> db
```

## 2.6 Opportunity form value-conditional fields (`showWhen`)

```mermaid
flowchart TD
  schema["FieldDef.showWhen<br/>field + equals"]
  typeGate["fieldVisible<br/>opportunity type"]
  valueGate["fieldMatchesShowWhen<br/>core + details"]
  render["SectionFields renders field"]
  strip["stripHiddenFields on submit"]
  allow["opportunity_form_schema._TM_DETAIL<br/>tm_replacement_engineer"]

  schema --> valueGate
  typeGate --> valueGate
  valueGate -->|"both pass"| render
  valueGate -->|"fail"| strip
  render --> allow
```

Example: `tm_replacement_engineer` (searchable) appears only when `tm_position_type = Replacement` (T&M). Selecting an engineer auto-fills `tm_role` from project-employee / employee `role_title`.

## 2.7 CRM wizard chrome (Opportunity + Customer)

```mermaid
flowchart LR
  top["Top bar<br/>title · Step N/M · progress · Reset/autosave"]
  rail["Left stepper rail<br/>md+ vertical / &lt;md horizontal<br/>+ circular step %"]
  card["Content card<br/>StepHeader + icon + fields"]
  foot["Sticky footer<br/>step meter LEFT · Previous+Next/Submit RIGHT"]

  top --> rail --> card --> foot
```

Shared presentational pieces live in `admin-dashboard/src/crm/components/WizardChrome.tsx`
(`WizardTopBar`, `WizardStepper`, `WizardStepHeader`, `WizardStepProgress`, `WizardFooter`)
plus `WizardAurora.tsx` (moonlit backdrop: moon glow, aurora blobs, starfield, grain — visual-only;
`useReducedMotion` → static). Opportunity, Customer, and New/Edit Employee full-screen modals share
`scopeClassName="crm-wizard wiz-noise"` + `wiz-moonlit-*` panel chrome from `wizard/premium.css`.
List tables use `DataTable.rowActions` + `components/RowActions.tsx` (Edit / Delete + `ConfirmModal`;
Delete calls `crmDelete` — 409 `detail` is shown inline; **Delete is disabled after a
dependency error** (Cancel → Close); Employee rows expose a secondary **Deactivate**
action when hard-delete is blocked. Backend cascades safe owned children on delete
(see `services/crm_delete.py`): opportunity → requirements/profiles/AI links;
candidate → profiles/AI links/bookings; invoice → unpaid TDS; PO → deletable invoices;
project/PE → draft+uninvoiced paths and deletable invoices. Hard blockers
(payments, credit notes, projects-on-opportunity, approved leave) still return
numbered 409s. Reports remain analytics-only with no row actions).

| Form | File | Steps |
|------|------|-------|
| New Opportunity | `pages/opportunity/NewOpportunityForm.tsx` | Schema-driven (~10–11); autosave draft; moonlit `WizardAurora` |
| New/Edit Customer | `components/CustomerFormModal.tsx` | 5 fixed sections; Leave & Holiday Billing merges Comp Off + Attendance Rule; moonlit `WizardAurora` |
| New/Edit Employee | `pages/Employees.tsx` (`EmployeeFormModal`) | Single-screen create/edit; same moonlit family |
| Edit Branch | `components/BranchWizardModal.tsx` | 4 steps: Branch Info → Holiday Billing Policy → Leave & Holiday Billing Policy (includes Billable Leave dialog) → Billing Properties |
| New/Edit Project | `components/EditProjectWizard.tsx` | Create: Details (Name/Opp/Customer/**Branch** SearchableSelect) → Leave & Holiday Billing Policy → Billing Properties; Branch required when customer has branches; selecting branch prefills policy (confirm on change) and POSTs/PUTs `branch_id` |

Both wizards place **Previous and Next (or Submit) together on the bottom-right**. Navigation,
per-step validation, and submit stay in each form file.

## 2.8 Timesheet Comp Off Billable (weekend / holiday work)

```mermaid
flowchart TD
  hours["Week Off / Holiday row<br/>hours_worked &gt; 0"]
  policy["effective_billing_policy<br/>_project_branch: project.branch_id → opp<br/>project → branch → customer → default<br/>comp_off_billable"]
  gate{"comp_off_billable?"}
  bill["compute_billables<br/>bill hours + days<br/>invoice qty includes Comp-off"]
  credit["comp_off_earned<br/>accrue_comp_off on approve<br/>PE leave pool when mapped<br/>else employee yearly"]
  apply["Apply-leave picker<br/>lists Comp-Off when balance &gt; 0"]

  hours --> policy --> gate
  gate -->|TRUE| bill
  gate -->|FALSE| credit --> apply
```

Mutually exclusive: never both bill and credit. Pure holiday-off (0 hours) still uses
`holidays_billable`. Week Off hours are editable in the daily grid; calendar holidays stay locked.

## 2.9 PE leave accrual start (`effective_date`)

```mermaid
flowchart TD
  map["PE map / leave sync<br/>seed_leave_details_from_customer_policy"]
  start["accrual_start = max<br/>policy.effective_date · pe.onboarding_date<br/>missing side = no bound"]
  future{"start &gt; seed_as_of?"}
  defer["Upfront One_Time/Yearly:<br/>open at initial only<br/>stash period on leave_accrual<br/>Monthly Start: like End_Of_Period"]
  seedNow["Seed opening as today<br/>onboarding-month proration if enabled"]
  job["run_pe_leave_credit / credit_one_pe_leave_row"]
  clamp["_days_present_in_month<br/>not_before=accrual_start<br/>period end before start → 0"]
  grant["Deferred upfront: grant leave_accrual<br/>in first month with month_end ≥ start<br/>source pe_credit:pe:type:YYYY-MM"]

  map --> start --> future
  future -->|yes| defer --> job
  future -->|no| seedNow
  job --> clamp
  job --> grant
```

Policy `effective_date` edits never rewrite existing PE leave rows; only new seeds and
future credit-job runs observe the new lower bound (no clawback of already-credited months).
