# Durable Workflow Execution System

A workflow engine that accepts JSON workflow definitions, executes them durably, and provides real-time visualization. If the server crashes mid-workflow, execution resumes from where it left off.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies
uv sync

# Start the server
uv run python main.py
```

Open http://localhost:8000 in your browser.

### Running Tests

```bash
uv run pytest     # 100 tests — executor, API, actions, topo sort
```

## Workflow JSON Structure

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

| Field                             | Description                                                                                                                                          |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                            | Human-readable workflow name                                                                                                                         |
| `steps[].id`                      | Unique step identifier (referenced by `depends_on`)                                                                                                  |
| `steps[].type`                    | Step type (free-form string, e.g. `"task"`)                                                                                                          |
| `steps[].config.action`           | Action to dispatch on success (e.g. `validate_order`, `charge_payment`, `ship_order`). Optional — steps without actions just run the simulated task. |
| `steps[].config.duration_seconds` | How long the simulated task takes (default: 1.0)                                                                                                     |
| `steps[].config.fail_probability` | Chance of random failure, 0.0-1.0 (default: 0.0)                                                                                                     |
| `steps[].config.max_retries`      | How many times to retry on failure (default: 0)                                                                                                      |
| `steps[].depends_on`              | Array of step IDs that must complete before this step runs                                                                                           |

## How to Use

1. **Select an example workflow** from the dropdown (or paste/upload your own JSON)
2. Choose how to run it:
   - **"Start Workflow"** — runs the steps as-is. Good for generic workflows with no business logic.
   - **"Start with Order"** — creates a demo order in the database, then runs the workflow with that order attached. Steps with `action` fields (like `validate_order`, `charge_payment`, `ship_order`) will execute business logic that mutates the order's status in real time: `pending → validated → charged → shipped`. Use this to see the action dispatch system in action.
3. You'll be redirected to the **run detail page** showing steps completing in real time (and order status if using "Start with Order")
4. Open the **Live DB Viewer** (linked from the dashboard) in a second tab to watch database state change as the workflow runs

### Crash Recovery Demo

1. Load `slow-order.json` from the examples dropdown
2. Click "Start with Order"
3. While a step is running, kill the server (Ctrl+C)
4. Restart with `uv run python main.py`
5. Refresh the run detail page — the workflow resumes from where it left off, skipping completed steps

## How It Works

The system has three components: a FastAPI backend, a SQLite database, and a vanilla JS frontend. When a user submits an order, the backend stores the definition, creates a run record with step records (ordered via topological sort of depends_on), and spawns a daemon thread to execute steps sequentially. Each step completion is written atomically — the step result, step status update, and any business logic (like order status transitions) all commit in a single database transaction. If the server crashes at any point, the startup recovery routine queries for runs still marked "running," spawns threads to resume them, and the execution loop skips already-completed steps using idempotency key checks. The frontend polls the API every 1.5 seconds to show real-time step progress.

The core durability guarantee comes from three mechanisms working together: atomic commits ensure partial state never persists, idempotency keys prevent duplicate work on recovery, and the startup recovery routine ensures no run is forgotten.

## Architecture

```
Browser (vanilla JS)          FastAPI (sync Python)          SQLite
┌─────────────────┐          ┌─────────────────────┐       ┌──────────┐
│ index.html      │─POST────▶│ POST /workflows     │──────▶│workflows │
│ (dashboard)     │─POST────▶│ POST /…/runs        │──────▶│runs      │
│                 │─GET─────▶│ GET /runs           │◀──────│steps     │
│ run.html        │─poll────▶│ GET /runs/{id}      │◀──────│step_results│
│ (run detail)    │─poll────▶│ GET /orders/{id}    │◀──────│orders    │
│ db.html         │─poll────▶│ GET /db/snapshot    │◀──────│          │
│ (DB viewer)     │          └────────┬────────────┘       └──────────┘
└─────────────────┘                   │
                              Background thread
                              per run (daemon)
```

**Request flow**: User submits workflow JSON → backend stores the workflow definition → creates a run + step records → spawns a background thread → returns immediately. The frontend polls for updates every 1.5s.

**Execution model**: One daemon thread per run, each with its own DB connection. Steps execute sequentially in dependency order (topological sort at run creation time). Each step completion is atomically committed before the next step begins.

**Durability**: SQLite is the single source of truth. On startup, the server queries for any runs left in "running" state and resumes them. Completed steps are skipped via idempotency checks. Business logic (order mutations) and step completion are committed in the same transaction — no window where one succeeds without the other.

### Key Files

| File          | Purpose                                                        |
| ------------- | -------------------------------------------------------------- |
| `main.py`     | FastAPI app, 12 API routes, startup recovery                   |
| `executor.py` | CRUD helpers, execution loop, crash recovery, topological sort |
| `actions.py`  | Action registry and order mutation functions                   |
| `database.py` | SQLite connection handling and schema creation                 |
| `models.py`   | Pydantic request/response models                               |
| `tasks.py`    | Simulated task executor (sleep + random failure)               |
| `static/`     | Frontend: dashboard, run detail, DB viewer, CSS                |
| `examples/`   | Sample workflow JSON files                                     |

### Database Schema (5 tables)

- **workflows** — stores the JSON definition
- **runs** — tracks execution status, optional order association
- **steps** — individual step state, retry count, idempotency key
- **step_results** — idempotent record of completed work
- **orders** — demo entity showing business logic integration

## Decisions, Trade-offs, and Limitations

See [DECISIONS.md](DECISIONS.md)
