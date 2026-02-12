# Durable Workflow Execution System - Technical Specification

## Executive Summary

A durable workflow execution system that accepts JSON workflow definitions, executes them reliably, and provides real-time visualization. The core guarantee is **durability**: if the server crashes mid-workflow and restarts, execution continues from where it left off without duplicating work or corrupting data.

Priorities: explainability and correctness over feature richness.

---

## System Architecture

### Components

| Component | Role |
|-----------|------|
| **FastAPI Backend** | REST API, state coordination, step execution in background threads. Synchronous Python (no async/await). |
| **SQLite** | Single source of truth. Workflows, runs, steps, idempotency records. Zero-setup, file-based. |
| **Frontend** | Minimal HTML/CSS/JS (no build step). Workflow submission + real-time run visualization via REST polling. |

### Data Flow

1. User pastes workflow JSON in frontend, clicks submit
2. Frontend POSTs to backend
3. Backend stores workflow definition, creates run + step records, spawns background thread, returns run ID immediately
4. Frontend polls run status endpoint every 1-2 seconds
5. On server restart, recovery routine finds incomplete runs and resumes them

---

## Database Schema

### `workflows`

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| name | TEXT NOT NULL | Human-readable name from JSON |
| definition | TEXT NOT NULL | Complete JSON definition as string |
| created_at | TEXT NOT NULL | ISO 8601 |

### `runs`

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| workflow_id | TEXT NOT NULL | FK -> workflows.id |
| status | TEXT NOT NULL | "pending" / "running" / "completed" / "failed" |
| started_at | TEXT | Nullable, set when execution begins |
| completed_at | TEXT | Nullable, set when execution finishes |
| created_at | TEXT NOT NULL | ISO 8601 |
| order_id | TEXT | Nullable FK -> orders.id. Associates run with an order for action dispatch. |

### `steps`

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| run_id | TEXT NOT NULL | FK -> runs.id |
| step_id | TEXT NOT NULL | Human-readable string from workflow JSON (NOT a UUID) |
| step_index | INTEGER NOT NULL | Position in execution order (0, 1, 2, ...) |
| status | TEXT NOT NULL | "pending" / "running" / "completed" / "failed" |
| idempotency_key | TEXT | Nullable UUID, regenerated on each retry |
| retry_count | INTEGER NOT NULL DEFAULT 0 | |
| max_retries | INTEGER NOT NULL DEFAULT 0 | Copied from step config |
| started_at | TEXT | Nullable |
| completed_at | TEXT | Nullable |
| error_message | TEXT | Nullable, populated on failure |
| created_at | TEXT NOT NULL | ISO 8601 |

### `step_results`

| Column | Type | Notes |
|--------|------|-------|
| idempotency_key | TEXT PK | Lookup key for idempotency checks |
| step_id | TEXT NOT NULL | FK -> steps.id (for debugging) |
| result_data | TEXT | Nullable JSON blob |
| created_at | TEXT NOT NULL | ISO 8601 |

### `orders` (demo)

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| status | TEXT NOT NULL | "pending" / "validated" / "charged" / "shipped" |
| amount | REAL NOT NULL | Dollars |
| created_at | TEXT NOT NULL | |
| updated_at | TEXT NOT NULL | |

---

## Entity Relationships

- **Workflow** (1) -> (many) **Runs**
- **Run** (1) -> (many) **Steps**
- **Step** (1) -> (0..many) **Step Results** (one per execution attempt via idempotency_key)
- **Run** (many) -> (0..1) **Order** (optional association via `order_id`)

UUID hierarchy: `workflow_id` -> `run_id` -> step `id` -> `idempotency_key`

The `step_id` field is the human-readable name from JSON (e.g., "validate"), NOT a UUID.

---

## State Machines

### Step States

```
pending -> running -> completed
                  \-> failed (retries exhausted)
                  \-> pending (retry, increment retry_count)
```

On server restart: "running" steps are re-executed; idempotency check prevents duplicate work.

### Run States

```
pending -> running -> completed (all steps succeeded)
                  \-> failed (any step permanently failed)
```

On server restart: "running" runs are candidates for recovery.

---

## Execution Model

### Key Design Decisions

- **Sequential execution, dependency-ordered** - steps run one at a time, but `step_index` is assigned via topological sort of `depends_on` at run creation time. JSON array order doesn't need to match dependency order.
- **One background thread per run** - daemon threads, own DB connection each.
- **Immediate retries** - no exponential backoff.

### Execution Loop

1. Open new DB connection (thread safety)
2. Mark run as "running", commit
3. Query all steps ordered by step_index
4. For each step:
   - If "completed": skip
   - If "pending" or "running": execute
   - On success: continue to next
   - On failure + retries remain: re-attempt same step
   - On failure + retries exhausted: break (workflow failed)
5. Set final run status ("completed" or "failed")
6. Close DB connection

### Step Execution Logic

1. Generate new UUID as idempotency_key, save to step record, commit
2. Mark step as "running" (set status + started_at), commit
3. Check step_results for existing result with this idempotency_key
   - If found: mark step "completed", return success (skip work)
4. Execute simulated task (sleep + random failure)
5. On success: **single transaction** -> insert step_result + mark step "completed" + dispatch action (if applicable), commit
6. On failure + retries remain: increment retry_count, set status to "pending", generate NEW idempotency_key, commit
7. On failure + retries exhausted: mark step "failed" with error_message, commit

---

## Durability Guarantees

### What We Guarantee

- After each step completes, completion is durably recorded before the next step begins
- Crash at any point leaves DB in consistent, recoverable state
- Business logic writes and completion records are committed atomically
- Completed steps are never re-executed on recovery
- Idempotency prevents duplicate work for steps that completed before crash

### What We Don't Guarantee

- No sub-step durability (long step that crashes loses all progress)
- No special handling for external side effects (API calls may be duplicated)
- No distributed execution (SQLite is single-node)

---

## Crash Recovery

### Startup Recovery

Before accepting HTTP requests:
1. Query all runs where status = "running"
2. For each, spawn background thread to resume execution
3. Resume logic = same as fresh execution (skip completed steps, re-execute rest)

### Recovery Scenarios

| Scenario | State After Crash | Recovery Behavior |
|----------|-------------------|-------------------|
| Before any step starts | Run "running", all steps "pending" | Execute from step 0 |
| After step 1 completes, before step 2 starts | Step 1 "completed", rest "pending" | Skip step 1, execute rest |
| During step 2, before task finishes | Step 2 "running", no result record | Re-execute step 2 |
| During step 2, after task but before commit | Step 2 "running", no result (rolled back) | Re-execute step 2 |
| After step 2 transaction commits | Step 2 "completed" or "running" with result | Skip step 2 via idempotency |

---

## Simulated Tasks

### Config Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| duration_seconds | float | 1.0 | Sleep duration |
| fail_probability | float | 0.0 | Chance of random failure (0.0-1.0) |
| action | string | - | Key into ACTION_REGISTRY. If matched and run has an `order_id`, the registered function executes inside the atomic commit block. |

### Action Dispatch (Phase 12)

#### ACTION_REGISTRY

| Action Key | Function | Order Status Transition | Description |
|------------|----------|------------------------|-------------|
| validate_order | validate_order(conn, order_id) | pending -> validated | Checks order exists, verifies amount > 0 |
| charge_payment | charge_payment(conn, order_id) | validated -> charged | Simulates payment processing |
| ship_order | ship_order(conn, order_id) | charged -> shipped | Marks order as shipped (final state) |
| send_notification | send_notification(conn, order_id) | (no transition) | Logs notification, no status change |

#### Order Status Flow

```
pending -> validated -> charged -> shipped
```

#### Dispatch Rules

1. After task execution succeeds, check if the step's `action` exists in ACTION_REGISTRY
2. If found AND the run has an `order_id`: call the registered function with `(conn, order_id)` inside the existing atomic transaction
3. If action not found in registry: no-op (step still succeeds)
4. If run has no `order_id`: no-op (step still succeeds)
5. If action function raises an exception: the entire atomic commit (step_result + step completion + action) rolls back, and the step is treated as failed

#### Durability Guarantees

The action mutation happens **inside the same atomic transaction** as the step result insert and step completion update. This means:
- Either all three succeed (result recorded + step completed + order mutated) or none do
- On crash recovery, a step without a result record will re-execute, which will re-run the action
- No window exists where a step is marked complete but the order mutation is lost

---

## API Specification

### Endpoints

| Method | Path | Description | Status |
|--------|------|-------------|--------|
| POST | `/workflows` | Create workflow definition | 201 |
| POST | `/workflows/{workflow_id}/runs` | Start a run (optional order_id in body) | 202 |
| GET | `/workflows` | List all workflows | 200 |
| GET | `/workflows/{workflow_id}` | Get workflow with full JSON | 200 |
| GET | `/runs` | List all runs | 200 |
| GET | `/runs/{run_id}` | Get run detail with step states | 200 |
| POST | `/orders` | Create test order (demo) | 201 |
| GET | `/orders/{order_id}` | Get order state (demo) | 200 |

### POST /workflows - Request

```json
{
  "name": "order-processing",
  "steps": [
    {
      "id": "validate",
      "type": "task",
      "config": {
        "action": "validate_order",
        "duration_seconds": 2,
        "fail_probability": 0.0,
        "max_retries": 0
      },
      "depends_on": []
    }
  ]
}
```

### POST /workflows/{workflow_id}/runs - Request (Optional)

```json
{
  "order_id": "order-uuid"
}
```

> If `order_id` is provided, the run is associated with that order. Action functions in the ACTION_REGISTRY will receive the order and can mutate its state inside the atomic commit block. If omitted, the run executes normally without action dispatch.

### Validation Rules

- Each step must have a unique `id`
- `type` is a free-form string (e.g., `"task"`). Not validated against a fixed set — extensible for future step types.
- `depends_on` may only reference step IDs that appear earlier in the array
- `fail_probability` must be between 0.0 and 1.0

### GET /workflows - Response

```json
[
  {
    "id": "wf-uuid",
    "name": "order-processing",
    "created_at": "2024-01-01T00:00:00"
  }
]
```

> List endpoint excludes `definition` for lighter responses. Use GET /workflows/{id} for full JSON.

### GET /workflows/{workflow_id} - Response

```json
{
  "id": "wf-uuid",
  "name": "order-processing",
  "definition": { "...full workflow JSON..." },
  "created_at": "2024-01-01T00:00:00"
}
```

### GET /runs - Response

```json
[
  {
    "id": "run-uuid",
    "workflow_id": "wf-uuid",
    "workflow_name": "order-processing",
    "order_id": "order-uuid-or-null",
    "status": "running",
    "started_at": "2024-01-01T00:00:00",
    "completed_at": null
  }
]
```

> List endpoint excludes `steps`. Includes `workflow_name` (via JOIN) for frontend dashboard table.

### GET /runs/{run_id} - Response

```json
{
  "id": "run-uuid",
  "workflow_id": "wf-uuid",
  "workflow_name": "order-processing",
  "order_id": "order-uuid-or-null",
  "status": "running",
  "started_at": "2024-01-01T00:00:00",
  "completed_at": null,
  "steps": [
    {
      "id": "step-uuid",
      "step_id": "validate",
      "step_index": 0,
      "status": "completed",
      "retry_count": 0,
      "max_retries": 0,
      "started_at": "...",
      "completed_at": "...",
      "error_message": null
    }
  ]
}
```

### POST /orders - Request

```json
{
  "amount": 49.99
}
```

### GET /orders/{order_id} - Response

```json
{
  "id": "order-uuid",
  "status": "pending",
  "amount": 49.99,
  "created_at": "2024-01-01T00:00:00",
  "updated_at": "2024-01-01T00:00:00"
}
```

---

## Frontend Specification

### Dashboard (index.html)

- Textarea for pasting workflow JSON (with placeholder example)
- File upload: "Upload .json" button reads a `.json` file and populates the textarea (user still clicks "Start Workflow" to submit)
- "Start Workflow" button + validation feedback
- On submit: POST /workflows -> POST /workflows/{id}/runs -> redirect to run detail
- Runs list table: workflow name, status (color-coded), started time, duration
- Click row -> navigate to run detail

### Run Detail (run.html?id=...)

- Header: workflow name, run status (colored), start/end time, duration
- Steps list (vertical, ordered by step_index):
  - Step name + status indicator (◯ pending, ⟳ running, ✓ completed, ✗ failed)
  - Timing info
  - Retry info ("Attempt 2 of 3") when retry_count > 0
  - Error message if failed
- Polls GET /runs/{run_id} every 1-2s while status is "running"; stops when done

### Status Colors

| Status | Color |
|--------|-------|
| pending | grey (#888) |
| running | blue (#2196F3) |
| completed | green (#4CAF50) |
| failed | red (#f44336) |

---

## Implementation Phases

### Phase 1: Project Setup and Database
- [x] Set up uv project
- [ ] Add FastAPI, uvicorn, pydantic dependencies
- [ ] Implement `database.py` with connection handling and schema creation
- **Verify**: call `init_db()`, query `sqlite_master`, confirm all 5 tables exist

### Phase 2: Pydantic Models
- [ ] Implement request/response models in `models.py`
- **Verify**: create instances with valid/invalid data, check serialization

### Phase 3: Task Execution
- [ ] Implement simulated task executor in `tasks.py`
- **Verify**: test sleep duration, fail_probability=0 always succeeds, fail_probability=1 always fails

### Phase 4: Database Helper Functions
- [ ] Implement CRUD functions in `executor.py`
- **Verify**: create workflow -> create run -> create steps -> update statuses -> query back

### Phase 5: Execution Loop
- [ ] Implement step execution logic and run execution loop in `executor.py`
- **Verify**: 3-step workflow completes in order, retry logic works with fail_probability

### Phase 6: Crash Recovery
- [ ] Implement startup recovery routine
- **Verify**: start run with long step, kill process mid-step, restart, observe resume without re-executing completed steps

### Phase 7: API Routes
- [ ] Implement all FastAPI routes in `main.py`
- **Verify**: curl/httpie test each endpoint, end-to-end workflow creation and execution

### Phase 8: Frontend
- [ ] Implement HTML pages and JavaScript
- **Verify**: browser test - submit workflow, watch real-time step updates

### Phase 9: Documentation
- [ ] README with setup instructions
- [ ] DECISIONS.md with tech choices and trade-offs

### Phase 10: Dependency-Ordered Execution
- [x] Topological sort function (Kahn's algorithm)
- [x] Update create_steps to sort before assigning step_index
- [x] Relax validation to allow any-order depends_on references
- [x] Cycle detection at validation and run creation
- [x] Tests for topo sort, diamond deps, out-of-order input, cycles

### Phase 11: File Upload
- [x] Add file input to dashboard
- [x] FileReader API to populate textarea
- [x] Auto-format loaded JSON
- [x] Improve validation error display

### Phase 12: Action Dispatch & Business Logic
- [ ] Create `actions.py` module with ACTION_REGISTRY and action functions
- [ ] Add `order_id` column to runs table schema in `database.py`
- [ ] Update `init_db()` to include order_id column
- [ ] Update `create_run()` to accept optional order_id parameter
- [ ] Update API: accept optional order_id in POST /workflows/{id}/runs request body
- [ ] Update response models to include order_id field
- [ ] Update SQL queries (get_all_runs, get_run_detail) to return order_id
- [ ] Update `execute_step()` to dispatch action after task success
- [ ] Load order_id from run record in execution loop
- [ ] Unit tests for each action function
- [ ] E2E test: order progresses pending->validated->charged->shipped
- [ ] Edge case tests: no order_id, unknown action, action failure
- **Verify**: create order, run 3-step workflow with order_id, confirm order status is "shipped" at end

---

## Testing Scenarios

| Scenario | Setup | Expected Result |
|----------|-------|-----------------|
| **Happy Path** | 3 steps, no failures | All steps complete in order, run "completed" |
| **Retry Success** | fail_probability=0.5, max_retries=3 | Step fails then succeeds, retry_count reflected in UI |
| **Permanent Failure** | fail_probability=1.0, max_retries=2 | 3 attempts, step "failed", run "failed", subsequent steps "pending" |
| **Crash Mid-Step** | Long middle step, kill server | Run resumes, completed steps skipped, interrupted step re-executes |
| **Crash Between Steps** | Kill between steps | Resumes from next pending step |
| **Multiple Concurrent Runs** | Several runs at once | All complete independently |
| **Idempotency** | Crash after commit, before thread continues | Recovery detects existing result, skips re-execution |

---

## Sample Workflow JSON

```json
{
  "name": "order-processing",
  "steps": [
    {
      "id": "validate",
      "type": "task",
      "config": {
        "action": "validate_order",
        "duration_seconds": 2,
        "fail_probability": 0.0,
        "max_retries": 0
      },
      "depends_on": []
    },
    {
      "id": "charge",
      "type": "task",
      "config": {
        "action": "charge_payment",
        "duration_seconds": 3,
        "fail_probability": 0.3,
        "max_retries": 2
      },
      "depends_on": ["validate"]
    },
    {
      "id": "ship",
      "type": "task",
      "config": {
        "action": "ship_order",
        "duration_seconds": 2,
        "fail_probability": 0.0,
        "max_retries": 0
      },
      "depends_on": ["charge"]
    }
  ]
}
```

~7 seconds total if no retries needed. Charge step (30% failure, 2 retries) will usually succeed eventually.

---

## Dependency-Ordered Execution (Phase 10)

### Overview

Steps are ordered by their `depends_on` graph rather than JSON array position. A `topological_sort(steps)` function computes the execution order at run creation time and assigns `step_index` accordingly. The execution loop remains unchanged — it still processes steps sequentially by `step_index`.

### Design

- **Topological sort at run creation**: `create_steps()` calls `topological_sort()` before assigning `step_index` values.
- **Stable sort**: If the input array already respects dependencies, the original order is preserved.
- **Cycle detection**: Validation rejects workflows with circular dependencies (raises `ValueError`).
- **No execution loop changes**: The executor still iterates `step_index` 0, 1, 2, ... — it just trusts that the index reflects a valid dependency order.
- **No crash recovery changes**: Recovery still skips completed steps and re-executes pending/running ones in `step_index` order, which is guaranteed to be a valid topological order.

### Validation Changes

- `CreateWorkflowRequest.validate_steps()`: Relaxed — `depends_on` may reference any step ID in the workflow, not just earlier ones in the array. Cycle detection replaces the forward-reference check.

---

## Known Limitations

- Step-level durability only (no sub-step checkpointing)
- SQLite single-writer limits concurrent write throughput
- Sequential execution only (no parallel steps, but dependency-ordered)
- No exponential backoff on retries
- External API calls may be duplicated on recovery
- No authentication or multi-tenancy

## Future Improvements

- Postgres for concurrency + distributed deployment
- Parallel execution of independent steps (concurrent threads within a run)
- Configurable retry policies with backoff
- Sub-step checkpointing
- External idempotency keys for payment APIs
- Workflow versioning
- WebSocket/SSE instead of polling
- Auth + per-user isolation
