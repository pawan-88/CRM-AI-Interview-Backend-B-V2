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
        pages["pages/* (HrDashboard, Templates, Candidates,<br/>CandidateReport, ATS, PromptLogs, Integrity,<br/>UpcomingInterviews, QuestionBank*)"]
        apiclient["api/client.ts (authFetch, apiGet cache)"]
        subgraph crmapp["crm/ (embedded Karnex CRM)"]
            crmroot["CrmApp.tsx (?view=crm&p=, /api/me roles)"]
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
    apptsx --> pages
    apptsx --> apiclient
    apptsx --> crmroot
    crmroot --> crmrouter
    crmroot --> crmapi
    crmrouter --> crmpages
    crmapi --> apiclient

    core -->|HTTP| backend["FastAPI backend"]
    apiclient -->|HTTP| backend
```

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
