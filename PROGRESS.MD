# Progress Tracker

> Reference: See TECH_SPEC.md for full requirements, schema, API spec, and design details.
> Tooling: This is a **uv project**. Use `uv add` for deps, `uv run` to execute scripts.

## Current Status

**Active Phase**: Phase 12 - COMPLETE
**Last Updated**: 2026-02-12

---

## Phase Completion

| Phase | Description | Status | Notes |
|-------|-------------|--------|-------|
| 1 | Project Setup and Database | COMPLETE | deps installed, database.py with all 5 tables |
| 2 | Pydantic Models | COMPLETE | models.py |
| 3 | Task Execution | COMPLETE | tasks.py |
| 4 | Database Helper Functions | COMPLETE | executor.py — 15 CRUD functions |
| 5 | Execution Loop | COMPLETE | execute_step, execute_run, start_run_thread in executor.py |
| 6 | Crash Recovery | COMPLETE | recover_interrupted_runs() + started_at preservation |
| 7 | API Routes | COMPLETE | 8 endpoints in main.py, CORS, startup event, 29 API tests |
| 8 | Frontend | COMPLETE | index.html, run.html, style.css — vanilla JS, polling, order demo |
| 9 | Documentation | NOT STARTED | README.md, DECISIONS.md |
| 10 | Dependency-Ordered Execution | COMPLETE | topological_sort, relaxed validation, cycle detection, 10 new tests |
| 11 | File Upload | COMPLETE | Upload .json file to populate textarea on dashboard |
| 12 | Action Dispatch & Business Logic | COMPLETE | actions.py, order_id on runs, dispatch in execute_step, 21 new tests |

---

## Files Created/Modified

| File | Purpose | Status |
|------|---------|--------|
| pyproject.toml | Project config, dependencies | DONE - FastAPI/uvicorn/pydantic + pytest/httpx (dev) |
| main.py | FastAPI app + routes | DONE - 8 routes, CORS, startup event, DB dependency, optional order_id on run creation |
| database.py | DB connection handling + schema init | DONE - order_id column on runs table |
| models.py | Pydantic request/response models | DONE - StartRunRequest, order_id on RunSummaryResponse/RunDetailResponse |
| tasks.py | Simulated task executor | DONE |
| executor.py | CRUD helpers + execution loop + recovery + topo sort | DONE - create_run accepts order_id, execute_step dispatches actions, get_all_runs/get_run_detail return order_id |
| actions.py | ACTION_REGISTRY + action functions + dispatch_action | DONE - validate_order, charge_payment, ship_order, send_notification |
| static/index.html | Dashboard frontend | DONE - workflow submit, auto-refreshing runs table (orders demo commented out) |
| static/run.html | Run detail frontend | DONE - real-time step visualization with polling |
| static/style.css | Frontend styling | DONE - status colors, clean minimal layout |
| DECISIONS.md | Tech choices and trade-offs | NOT CREATED |
| test_executor.py | Pytest tests for executor.py + actions.py | DONE - 64 tests (48 existing + 16 Phase 12) |
| test_api.py | API route tests — lifecycle, validation, failures, durability, action dispatch | DONE - 36 tests (31 existing + 5 Phase 12) |
| README.md | Setup instructions | EXISTS - empty |
| examples/order-processing.json | Sample order-processing workflow (validate→charge→ship) | DONE |
| examples/simple-pipeline.json | Sample ETL pipeline (fetch→transform→load) | DONE |
| examples/diamond-workflow.json | Sample diamond dependency graph | DONE |

---

## Detailed Phase Progress

### Phase 1: Project Setup and Database
- [x] uv project initialized (pyproject.toml, .python-version, .gitignore exist)
- [x] `uv add fastapi uvicorn pydantic`
- [x] Create database.py:
  - [x] `get_connection()` - returns sqlite3.Connection with row_factory
  - [x] `init_db()` - creates all 5 tables (workflows, runs, steps, step_results, orders)
  - [x] DB file location: `workflow.db` in project root
- [x] Verify: all 5 tables created and queryable

### Phase 2: Pydantic Models
- [x] WorkflowStepConfig (action, duration_seconds, fail_probability, max_retries)
- [x] WorkflowStep (id, type as free-form string, config, depends_on)
- [x] CreateWorkflowRequest (name, steps) with validation
- [x] WorkflowSummaryResponse (id, name, created_at — for list endpoint)
- [x] WorkflowDetailResponse (id, name, definition, created_at — for detail endpoint)
- [x] RunSummaryResponse (id, workflow_id, workflow_name, order_id, status, started_at, completed_at — for list endpoint)
- [x] RunDetailResponse (id, workflow_id, workflow_name, order_id, status, started_at, completed_at, steps — for detail endpoint)
- [x] StepStateResponse (id, step_id, step_index, status, retry_count, max_retries, started_at, completed_at, error_message)
- [x] CreateOrderRequest (amount only), OrderResponse
- [x] StartRunRequest (optional order_id) — added Phase 12

### Phase 3: Task Execution
- [x] `execute_task(config)` - sleep for duration, random failure based on fail_probability
- [x] Returns result dict on success, raises TaskExecutionError on failure

### Phase 4: Database Helper Functions
- [x] create_workflow(conn, name, definition_json) -> workflow_id
- [x] create_run(conn, workflow_id, order_id=None) -> run_id
- [x] create_steps(conn, run_id, steps_definition) -> list of step records
- [x] get_all_workflows(conn) -> list of workflow summary records
- [x] get_workflow(conn, workflow_id) -> full workflow record or None
- [x] get_all_runs(conn) -> list of run records with workflow_name + order_id (JOIN on workflows)
- [x] get_run_detail(conn, run_id) -> run record with workflow_name + order_id (JOIN) or None
- [x] get_steps_for_run(conn, run_id) -> ordered by step_index
- [x] update_step_status(conn, step_id, status, **kwargs)
- [x] insert_step_result(conn, idempotency_key, step_id, result_data)
- [x] check_step_result(conn, idempotency_key) -> result or None
- [x] get_running_runs(conn) -> list of run records (for crash recovery)
- [x] Order CRUD helpers (create_order, get_order)
- [x] update_run_status(conn, run_id, status, **kwargs) — bonus, needed by Phase 5

### Phase 5: Execution Loop
- [x] `execute_step(conn, step_row, step_config, order_id=None)` - single step with idempotency + action dispatch
- [x] `execute_run(run_id)` - main loop running in background thread (own DB connection)
- [x] `start_run_thread(run_id)` - daemon thread wrapper
- [x] Correct commit ordering: idem key + status "running" -> check result -> work -> atomic commit (result + action + completion)
- [x] Retry logic: increment count, fresh idem key, status back to "pending"
- [x] Crash recovery: skips completed steps, re-executes pending/running steps

### Phase 6: Crash Recovery
- [x] `recover_interrupted_runs()` - called at startup before accepting requests
- [x] Spawns background threads for each "running" run
- [x] Same execution loop handles skipping completed steps
- [x] Fixed `execute_run` to preserve `started_at` on recovery (skip status update if already "running")

### Phase 7: API Routes
- [x] POST /workflows (201) -> WorkflowDetailResponse
- [x] POST /workflows/{workflow_id}/runs (202) -> RunDetailResponse (accepts optional order_id body)
- [x] GET /workflows (200) -> list[WorkflowSummaryResponse]
- [x] GET /workflows/{workflow_id} (200) -> WorkflowDetailResponse
- [x] GET /runs (200) -> list[RunSummaryResponse] (with workflow_name + order_id via JOIN)
- [x] GET /runs/{run_id} (200) -> RunDetailResponse (with steps)
- [x] POST /orders (201) -> OrderResponse
- [x] GET /orders/{order_id} (200) -> OrderResponse
- [x] CORS middleware for frontend
- [x] Startup event calls init_db() + recover_interrupted_runs()
- [x] DB connection dependency injection (get_db with Depends)
- [x] 404 handling for nonexistent resources
- [x] Static file mount prep for Phase 8
- [x] API tests: 29 tests (lifecycle, validation, failures, durability)

### Phase 8: Frontend
- [x] index.html - workflow submission form + runs list table + order demo section
- [x] run.html - real-time run detail with step visualization (icons, retry info, errors)
- [x] style.css - status colors, clean minimal layout
- [x] Polling logic (2s on dashboard, 1.5s on run detail, stops when run completes/fails)
- [x] Static file serving from FastAPI (conditional mount already in main.py)

### Phase 9: Documentation
- [ ] README.md - setup, run, test instructions
- [ ] DECISIONS.md - tech choices, trade-offs, future improvements

### Phase 10: Dependency-Ordered Execution
- [x] Add `topological_sort(steps)` function in executor.py
  - [x] Kahn's algorithm (BFS-based) for deterministic ordering
  - [x] Cycle detection — raises ValueError on circular dependencies
  - [x] Stable: preserves original array order when dependencies allow
- [x] Update `create_steps()` to topo-sort before assigning `step_index`
- [x] Relax `CreateWorkflowRequest.validate_steps()` — allow `depends_on` to reference any step ID (not just earlier ones)
- [x] Keep cycle detection in validation (reject at API level, not just at run creation)
- [x] Tests for topo sort: diamond dependencies, out-of-order input, cycles, no dependencies
- [x] Verify: existing tests still pass (execution loop + crash recovery unchanged)
- [x] Comment out orders demo UI from frontend (API routes still available)

### Phase 11: File Upload
- [x] Add "Upload .json" file input to dashboard (index.html)
- [x] Read file contents via FileReader API, populate textarea
- [x] Auto-format the loaded JSON in the textarea
- **Verify**: upload a .json file, confirm textarea is populated, click Start Workflow to execute

### Phase 12: Action Dispatch & Business Logic
- [x] Create `actions.py` module
  - [x] ACTION_REGISTRY dict mapping action strings to functions
  - [x] `validate_order(conn, order_id)` — pending -> validated
  - [x] `charge_payment(conn, order_id)` — validated -> charged
  - [x] `ship_order(conn, order_id)` — charged -> shipped
  - [x] `send_notification(conn, order_id)` — logs, no status change
  - [x] `dispatch_action(conn, action, order_id)` — registry lookup + call
- [x] Schema change
  - [x] Add `order_id TEXT` column to runs table in `database.py` init_db()
  - [x] Update `create_run(conn, workflow_id, order_id=None)` to accept and store order_id
- [x] API changes
  - [x] Accept optional `order_id` in POST /workflows/{workflow_id}/runs request body (StartRunRequest)
  - [x] Add `order_id` to RunSummaryResponse and RunDetailResponse models
  - [x] Update SQL in get_all_runs() and get_run_detail() to SELECT order_id
- [x] Execution changes
  - [x] Load order_id from run record at start of execute_run()
  - [x] Pass order_id to execute_step()
  - [x] After successful task, call dispatch_action(conn, action, order_id) inside atomic block
  - [x] Action failure rolls back entire atomic block, treated as step failure with retry support
- [x] Tests
  - [x] Unit tests for each action function (happy path + wrong status)
  - [x] E2E test: 3-step workflow with order_id, order ends at "shipped"
  - [x] Edge case: run without order_id (actions are no-ops)
  - [x] Edge case: unknown action string (no-op, step still succeeds)
  - [x] Edge case: action function failure (step fails, transaction rolls back)
- [x] Example workflow JSON files in `examples/`
- **Verify**: create order, run 3-step workflow with order_id, confirm order status is "shipped" at end — YES (manual curl test + 100 automated tests)

---

## Verification Checkpoints

| Checkpoint | Phase | Passed |
|------------|-------|--------|
| All 5 tables created in SQLite | 1 | YES |
| Models validate/reject correctly | 2 | YES |
| Task executor sleeps, succeeds, fails as configured | 3 | YES |
| CRUD round-trip works | 4 | YES |
| 3-step workflow completes sequentially | 5 | YES |
| Crash recovery resumes without re-executing completed steps | 6 | YES |
| All API endpoints respond correctly via curl | 7 | YES |
| Frontend shows real-time step updates in browser | 8 | PENDING USER VERIFICATION |
| README instructions work from scratch | 9 | NO |
| Out-of-order depends_on produces correct step_index; cycles rejected | 10 | YES |
| Upload .json file populates textarea, workflow submits successfully | 11 | PENDING USER VERIFICATION |
| Order progresses pending->validated->charged->shipped during run with order_id | 12 | YES |

---

## Issues / Decisions Log

_Track any issues encountered or decisions made during implementation._

- **Branching strategy**: Each phase lives on its own branch (e.g., `phase-1/setup-and-database`), merged to main on completion.
- **`type` field**: Free-form string on WorkflowStep, not validated to "task". Extensible for future step types.
- **List vs detail responses**: GET /workflows list excludes `definition`; GET /runs list excludes `steps` but includes `workflow_name` via JOIN. Detail endpoints return full data.
- **CreateOrderRequest**: Only `amount` field. Status defaults to "pending", timestamps auto-generated.
- **CRUD return types**: executor.py returns sqlite3.Row objects (no Pydantic imports). SQL aliases match Pydantic field names for easy `Model(**dict(row))` in API layer.
- **Commit policy**: Standalone creates (create_workflow, create_run, create_steps, create_order) commit. Updates used by execution loop (update_step_status, insert_step_result, update_run_status) do NOT commit — caller controls transactions for atomicity.
- **update_step_status / update_run_status**: Use **kwargs with allowlist for flexible field updates.
- **Idempotency key reuse**: `execute_step` reuses existing idempotency key on crash recovery instead of always generating a new one. Without this, the idempotency check was dead code.
- **DB dependency injection**: `get_db()` generator with `Depends()` gives each request its own connection, closed in `finally`.
- **Step row mapping**: `_step_to_response()` explicitly maps fields to `StepStateResponse` because `SELECT *` returns extra columns (`run_id`, `idempotency_key`, `created_at`) not in the model. Other models use `**dict(row)` where SQL aliases match exactly.
- **`on_event("startup")`**: Used despite deprecation warning because the project is sync-only (no async/await). Works fine.
- **Dependency-ordered execution (Phase 10)**: Topo sort at run creation time (Option 1). `step_index` reflects dependency order, execution loop unchanged. Validation relaxed to allow any-order `depends_on` references; cycle detection added instead.
- **Action dispatch design (Phase 12)**: Actions execute inside the existing atomic commit block (step_result + step completion + order mutation). No separate transaction needed.
- **Action failure handling (Phase 12)**: If dispatch_action raises, the entire atomic block (result + completion + action) is rolled back. The step is treated as failed with retry support — same retry logic as task execution failures.
- **No fulfill_order action (Phase 12)**: `ship_order` transitions directly from "charged" to "shipped". 3 steps instead of 4 keeps the sample workflow simpler.
- **Optional order_id (Phase 12)**: `order_id` on runs is nullable. Runs without an order_id still execute normally — action dispatch is silently skipped.
- **send_notification behavior (Phase 12)**: Included in ACTION_REGISTRY but performs no status transition. Demonstrates that not all actions need to mutate order state.
