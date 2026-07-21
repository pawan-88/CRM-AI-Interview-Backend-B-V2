# Dead Code Cleanup Report

**Branch:** `chore/dead-code-cleanup` (both repos)  
**Date:** 2026-07-21  
**Scope:** `AI-Interview-Model-B-V2/backend`, `AI-Interview-Model-F-V2/frontend/admin-dashboard`

---

## Phase 1 — Detection Summary

Tools run: **knip**, **ts-prune**, **depcheck** (frontend); **ruff F401/F841**, **vulture (≥80%)**, **deptry** (backend).

### Baseline gates (before cleanup)

| Gate | Result |
|------|--------|
| Frontend `typecheck` | ✅ pass |
| Frontend `lint` | ⚠️ 76 errors / 26 warnings (pre-existing a11y + hooks) |
| Frontend `build` | ✅ pass (chunk warnings only) |
| Backend `import main` | ✅ pass |
| Backend `ruff F401,F841` | ❌ 60 issues |
| Backend `pytest -q` | ⚠️ 323 pass / 3 fail (pre-existing: boundary label, password bcrypt) |
| Backend `alembic current` | ✅ `0035 (head)` |
| Backend `compileall` | ✅ pass |

### Frontend — Confirmed unused (removed in Phase 2)

| Item | Evidence | Action |
|------|----------|--------|
| `src/crm/pages/ProjectHistory.tsx` | Not in `routes.ts` lazy map; `ProjectOverviewTab` has zero importers (`Projects.tsx` uses inline `OverviewTab`) | **Deleted** |
| `scripts/_repro_phone_hops.cjs` | One-off phone repro script; zero references | **Deleted** |
| `@testing-library/user-event` | In `package.json` only; no test imports | **Removed from devDependencies** |
| Duplicate `export default` on 6 CRM pages | `routes.ts` lazy-loads named exports only | **Removed default exports** |
| `useCrmPathActive` import in `CrmApp.tsx` | ESLint unused-var | **Removed import** |

### Frontend — Uncertain (kept)

| Item | Reason |
|------|--------|
| `scripts/check-contrast.mjs` | Documented WCAG verifier (`DESIGN-DECISIONS.md`, token READMEs) |
| `autoprefixer`, `postcss`, `tailwindcss` | depcheck false positive — used by PostCSS/Tailwind build chain |
| `@vitest/coverage-v8` | Unlisted but referenced in `vitest.config.ts` — add to devDeps if coverage runs in CI |
| 33 knip “unused exports” (e.g. `crmUrl`, `canEditTab`, API re-exports) | Public module surface / future hooks; ts-prune marks many as “used in module” |
| `src/api/index.ts` re-exports (getSchedules, deleteSchedule, …) | HR dashboard API surface; may be used by lazy pages or future features |
| `Tilt3D`, `PressableScale`, motion/RBAC helpers | Component library exports; safe to trim later with explicit audit |

### Backend — Confirmed unused (removed in Phase 2)

| Item | Evidence | Action |
|------|----------|--------|
| 50× unused imports (F401) | ruff across `main.py`, tests, utils, `auth_db.py`, … | **Auto-removed** |
| 10× unused locals (F841) | `ql`, `jd_lower`, dead `coach` assignments, test fixtures | **Removed** |
| `tests/test_submit_background_finalize.py` | Patched obsolete `main.upsert_hr_record` (no longer imported) | **Updated to patch `_persist_hr_record_mirror`** |

### Backend — Uncertain (kept)

| Item | Reason |
|------|--------|
| Alembic migration unused imports (`0002_*.py`) | User rule: do not modify migration files |
| Pydantic validator `cls` params | Required by Pydantic `@field_validator` signature |
| `alembic/env.py` `compare_to`, `reflected` | Alembic hook signature |
| `auth_db.py` `exc_val`, `exc_tb` | Exception context manager protocol |
| `uvicorn`, `python-multipart`, `tzdata`, `supabase`, `pytest` (deptry DEP002) | Runtime / deploy / test deps not imported in app code |
| `slowapi` (deptry DEP001) | Used in `rate_limit.py` but **missing from requirements.txt** — fix separately (add dep), do not remove |
| `generate_questions_with_model` in `ai.py` | Used by `question_service.py`; only unused re-import in `main.py` (removed) |

---

## Phase 2 — Removals (committed)

### Batch A — Backend unused imports/locals
- ruff `--fix` on all files except `alembic/versions/*`
- Files: `main.py`, `ai.py`, `ats.py`, `auth_db.py`, `prompt_builder.py`, utils, tests

### Batch B — Backend test fix
- `test_submit_background_finalize.py`: monkeypatch `_persist_hr_record_mirror` instead of removed `upsert_hr_record` re-export

### Batch C — Frontend dead files
- Delete `ProjectHistory.tsx`, `_repro_phone_hops.cjs`

### Batch D — Frontend deps & exports
- Remove `@testing-library/user-event`
- Remove duplicate default exports from 6 CRM pages
- Remove unused `useCrmPathActive` import

---

## Phase 3 — Optimization (conservative)

### Frontend bundle (after cleanup)

| Chunk | Size (minified) |
|-------|-----------------|
| `index-BSqv-sPT.js` (main) | 494.94 kB |
| `vendor-pdf-CzyCalQe.js` | 582.67 kB |
| `vendor-charts-FjJQI1N2.js` | 399.22 kB |
| `CustomerFormModal-*.js` | 147.26 kB |
| `Employees-*.js` | 66.36 kB |

**Already optimized:**
- CRM routes lazy-loaded via `React.lazy` in `routes.ts` and `App.tsx`
- PDF/charts/motion in separate `manualChunks` (`vite.config.ts`)
- PDF worker excluded from module preload

**Not changed (would need behavior review):**
- `CustomerFormModal` (147 kB) still pulled with Customers/Opportunities — could lazy-load modal only
- Pre-existing lint/a11y debt (76 errors) — out of scope for dead-code pass

### Backend queries / indexes
- No new indexes added — no hot-path N+1 identified with zero-risk grep during this pass
- Recommend separate perf audit on timesheet list + employee project-history endpoints

---

## Phase 4 — Deploy-safety (after cleanup)

| Gate | Result |
|------|--------|
| Frontend `typecheck` | ✅ |
| Frontend `lint` | ⚠️ unchanged pre-existing errors |
| Frontend `build` | ✅ zero errors |
| Backend `ruff F401,F841` | ✅ all passed |
| Backend `import main` | ✅ |
| Backend `pytest -q` | ⚠️ 322 pass / 3 fail (same pre-existing: boundary label, password bcrypt×2) |
| Backend `compileall` | ✅ |
| Backend `alembic current` | ✅ head |

### Size delta
- **Frontend:** main bundle unchanged (~495 kB) — removed source not in production graph
- **Backend:** ~29 unused import lines + 10 dead locals removed; no functional API changes
- **Deleted files:** `ProjectHistory.tsx` (~24 KB source), `_repro_phone_hops.cjs` (~2 KB)

---

## Items for manual review

1. **Add `slowapi` to `requirements.txt`** — imported by `rate_limit.py`, deptry flags missing
2. **Fix 3 pre-existing pytest failures** before merge (boundary metadata label, bcrypt hashing config)
3. **Frontend lint debt** — 76 a11y/hooks errors; separate cleanup PR recommended
4. **`@vitest/coverage-v8`** — add to devDependencies or remove from vitest config
5. **Knip unused exports** — trim in a follow-up with per-export grep if desired

---

_Last updated: Phase 4 complete on `chore/dead-code-cleanup`._
