# Progress Tracker

> Reference: See TECH_SPEC.md for full requirements, schema, API spec, and design details.
> Tooling: This is a **uv project**. Use `uv add` for deps, `uv run` to execute scripts.

## Current Status

**Active Phase**: Phase 1 - Project Setup and Database
**Last Updated**: 2026-02-09

---

## Phase Completion

| Phase | Description | Status | Notes |
|-------|-------------|--------|-------|
| 1 | Project Setup and Database | IN PROGRESS | uv project scaffolded, need deps + database.py |
| 2 | Pydantic Models | NOT STARTED | models.py |
| 3 | Task Execution | NOT STARTED | tasks.py |
| 4 | Database Helper Functions | NOT STARTED | CRUD in executor.py |
| 5 | Execution Loop | NOT STARTED | Step execution + run loop in executor.py |
| 6 | Crash Recovery | NOT STARTED | Startup recovery routine |
| 7 | API Routes | NOT STARTED | FastAPI routes in main.py |
| 8 | Frontend | NOT STARTED | index.html, run.html, style.css |
| 9 | Documentation | NOT STARTED | README.md, DECISIONS.md |

---

## Files Created/Modified

| File | Purpose | Status |
|------|---------|--------|
| pyproject.toml | Project config, dependencies | EXISTS - needs FastAPI/uvicorn/pydantic deps |
| main.py | FastAPI app + routes | EXISTS - placeholder only |
| database.py | DB connection handling + schema init | NOT CREATED |
| models.py | Pydantic request/response models | NOT CREATED |
| tasks.py | Simulated task executor | NOT CREATED |
| executor.py | CRUD helpers + execution loop + recovery | NOT CREATED |
| static/index.html | Dashboard frontend | NOT CREATED |
| static/run.html | Run detail frontend | NOT CREATED |
| static/style.css | Frontend styling | NOT CREATED |
| DECISIONS.md | Tech choices and trade-offs | NOT CREATED |
| README.md | Setup instructions | EXISTS - empty |

---

## Detailed Phase Progress

### Phase 1: Project Setup and Database
- [x] uv project initialized (pyproject.toml, .python-version, .gitignore exist)
- [ ] `uv add fastapi uvicorn pydantic`
- [ ] Create database.py:
  - [ ] `get_connection()` - returns sqlite3.Connection with row_factory
  - [ ] `init_db()` - creates all 5 tables (workflows, runs, steps, step_results, orders)
  - [ ] DB file location (e.g., `workflow.db` in project root)
- [ ] Verify: `uv run python -c "from database import init_db; init_db()"`, confirm 5 tables

### Phase 2: Pydantic Models
- [ ] WorkflowStepConfig (action, duration_seconds, fail_probability, max_retries)
- [ ] WorkflowStep (id, type, config, depends_on)
- [ ] CreateWorkflowRequest (name, steps) with validation
- [ ] WorkflowResponse, RunResponse, RunDetailResponse, StepStateResponse
- [ ] CreateOrderRequest, OrderResponse

### Phase 3: Task Execution
- [ ] `execute_task(config)` - sleep for duration, random failure based on fail_probability
- [ ] Returns result dict on success, raises exception on failure

### Phase 4: Database Helper Functions
- [ ] create_workflow(conn, name, definition_json) -> workflow_id
- [ ] create_run(conn, workflow_id) -> run_id
- [ ] create_steps(conn, run_id, steps_definition) -> list of step records
- [ ] get_steps_for_run(conn, run_id) -> ordered by step_index
- [ ] update_step_status(conn, step_id, status, ...)
- [ ] insert_step_result(conn, idempotency_key, step_id, result_data)
- [ ] check_step_result(conn, idempotency_key) -> result or None
- [ ] get_running_runs(conn) -> list of run records
- [ ] Order CRUD helpers

### Phase 5: Execution Loop
- [ ] `execute_run(run_id)` - main loop running in background thread
- [ ] `execute_step(conn, step)` - single step with idempotency
- [ ] Correct commit ordering: idem key -> status "running" -> check result -> work -> atomic commit
- [ ] Retry logic: increment count, fresh idem key, status back to "pending"

### Phase 6: Crash Recovery
- [ ] `recover_interrupted_runs()` - called at startup before accepting requests
- [ ] Spawns background threads for each "running" run
- [ ] Same execution loop handles skipping completed steps

### Phase 7: API Routes
- [ ] POST /workflows (201)
- [ ] POST /workflows/{workflow_id}/runs (202)
- [ ] GET /workflows (200)
- [ ] GET /workflows/{workflow_id} (200)
- [ ] GET /runs (200)
- [ ] GET /runs/{run_id} (200)
- [ ] POST /orders (201)
- [ ] GET /orders/{order_id} (200)
- [ ] CORS middleware for frontend
- [ ] Startup event calls init_db() + recover_interrupted_runs()

### Phase 8: Frontend
- [ ] index.html - workflow submission form + runs list table
- [ ] run.html - real-time run detail with step visualization
- [ ] style.css - status colors, clean layout
- [ ] Polling logic (1-2s interval, stops when run completes/fails)
- [ ] Static file serving from FastAPI

### Phase 9: Documentation
- [ ] README.md - setup, run, test instructions
- [ ] DECISIONS.md - tech choices, trade-offs, future improvements

---

## Verification Checkpoints

| Checkpoint | Phase | Passed |
|------------|-------|--------|
| All 5 tables created in SQLite | 1 | NO |
| Models validate/reject correctly | 2 | NO |
| Task executor sleeps, succeeds, fails as configured | 3 | NO |
| CRUD round-trip works | 4 | NO |
| 3-step workflow completes sequentially | 5 | NO |
| Crash recovery resumes without re-executing completed steps | 6 | NO |
| All API endpoints respond correctly via curl | 7 | NO |
| Frontend shows real-time step updates in browser | 8 | NO |
| README instructions work from scratch | 9 | NO |

---

## Issues / Decisions Log

_Track any issues encountered or decisions made during implementation._

(none yet)
