import json
import logging
import sqlite3
import threading
import uuid
from collections import deque
from datetime import datetime, timezone

from models import WorkflowStepConfig
from tasks import TaskExecutionError, execute_task

logger = logging.getLogger(__name__)


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Workflow CRUD
# ---------------------------------------------------------------------------


def create_workflow(conn: sqlite3.Connection, name: str, definition_json: str) -> str:
    """Insert a new workflow record. Commits. Returns the workflow UUID."""
    workflow_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO workflows (id, name, definition, created_at) VALUES (?, ?, ?, ?)",
        (workflow_id, name, definition_json, now),
    )
    conn.commit()
    return workflow_id


def get_all_workflows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all workflows (summary: no definition). Newest first."""
    return conn.execute(
        "SELECT id, name, created_at FROM workflows ORDER BY created_at DESC"
    ).fetchall()


def get_workflow(conn: sqlite3.Connection, workflow_id: str) -> sqlite3.Row | None:
    """Return a single workflow with full definition, or None."""
    return conn.execute(
        "SELECT id, name, definition, created_at FROM workflows WHERE id = ?",
        (workflow_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Run CRUD
# ---------------------------------------------------------------------------


def create_run(conn: sqlite3.Connection, workflow_id: str) -> str:
    """Insert a new run in 'pending' status. Commits. Returns the run UUID."""
    run_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO runs (id, workflow_id, status, started_at, completed_at, created_at) "
        "VALUES (?, ?, 'pending', NULL, NULL, ?)",
        (run_id, workflow_id, now),
    )
    conn.commit()
    return run_id


def get_all_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all runs with workflow_name (via JOIN). Newest first."""
    return conn.execute(
        "SELECT r.id, r.workflow_id, w.name AS workflow_name, r.status, "
        "r.started_at, r.completed_at "
        "FROM runs r JOIN workflows w ON r.workflow_id = w.id "
        "ORDER BY r.created_at DESC"
    ).fetchall()


def get_run_detail(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """Return a single run with workflow_name, or None. Steps fetched separately."""
    return conn.execute(
        "SELECT r.id, r.workflow_id, w.name AS workflow_name, r.status, "
        "r.started_at, r.completed_at "
        "FROM runs r JOIN workflows w ON r.workflow_id = w.id "
        "WHERE r.id = ?",
        (run_id,),
    ).fetchone()


def get_running_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all runs with status='running' (for crash recovery)."""
    return conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall()


def update_run_status(conn: sqlite3.Connection, run_id: str, status: str, **kwargs) -> None:
    """Update a run's status and optional fields. Does NOT commit."""
    allowed = {"started_at", "completed_at"}

    set_clauses = ["status = ?"]
    params: list = [status]

    for field, value in kwargs.items():
        if field not in allowed:
            raise ValueError(f"Unknown field: {field}")
        set_clauses.append(f"{field} = ?")
        params.append(value)

    params.append(run_id)
    conn.execute(
        f"UPDATE runs SET {', '.join(set_clauses)} WHERE id = ?",
        params,
    )


# ---------------------------------------------------------------------------
# Step CRUD
# ---------------------------------------------------------------------------


def create_steps(conn: sqlite3.Connection, run_id: str, steps_definition: list) -> list[sqlite3.Row]:
    """Create step rows from workflow step definitions. Commits.

    steps_definition: list of objects with .id and .config.max_retries attributes
    (e.g. WorkflowStep Pydantic models).

    Returns the created step rows ordered by step_index.
    """
    now = _now()
    sorted_steps = topological_sort(steps_definition)
    for index, step in enumerate(sorted_steps):
        step_uuid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO steps "
            "(id, run_id, step_id, step_index, status, idempotency_key, "
            "retry_count, max_retries, started_at, completed_at, error_message, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', NULL, 0, ?, NULL, NULL, NULL, ?)",
            (step_uuid, run_id, step.id, index, step.config.max_retries, now),
        )
    conn.commit()
    return conn.execute(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY step_index",
        (run_id,),
    ).fetchall()


def get_steps_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Return all steps for a run, ordered by step_index."""
    return conn.execute(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY step_index",
        (run_id,),
    ).fetchall()


def update_step_status(conn: sqlite3.Connection, step_id: str, status: str, **kwargs) -> None:
    """Update a step's status and optional fields. Does NOT commit.

    Allowed kwargs: started_at, completed_at, error_message, idempotency_key, retry_count
    """
    allowed = {"started_at", "completed_at", "error_message", "idempotency_key", "retry_count"}

    set_clauses = ["status = ?"]
    params: list = [status]

    for field, value in kwargs.items():
        if field not in allowed:
            raise ValueError(f"Unknown field: {field}")
        set_clauses.append(f"{field} = ?")
        params.append(value)

    params.append(step_id)
    conn.execute(
        f"UPDATE steps SET {', '.join(set_clauses)} WHERE id = ?",
        params,
    )


# ---------------------------------------------------------------------------
# Step Results (idempotency)
# ---------------------------------------------------------------------------


def insert_step_result(
    conn: sqlite3.Connection,
    idempotency_key: str,
    step_id: str,
    result_data: dict | None,
) -> None:
    """Insert a step result record. Does NOT commit."""
    serialized = json.dumps(result_data) if result_data is not None else None
    conn.execute(
        "INSERT INTO step_results (idempotency_key, step_id, result_data, created_at) "
        "VALUES (?, ?, ?, ?)",
        (idempotency_key, step_id, serialized, _now()),
    )


def check_step_result(conn: sqlite3.Connection, idempotency_key: str) -> sqlite3.Row | None:
    """Check if a step result exists for the given idempotency key."""
    return conn.execute(
        "SELECT * FROM step_results WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Step Ordering (Dependency Graph)
# ---------------------------------------------------------------------------


def topological_sort(steps: list) -> list:
    """Return steps in dependency order using Kahn's algorithm (BFS).

    Guarantees:
    - Every step appears after all of its dependencies.
    - Stable: when multiple steps are ready, they appear in their
      original array order.
    - Raises ValueError on circular dependencies.

    steps: list of objects with .id (str) and .depends_on (list[str]).
    Returns: new list in topologically sorted order.
    """
    id_to_step = {}
    in_degree = {}
    dependents: dict[str, list[str]] = {}
    original_index = {}

    for idx, step in enumerate(steps):
        id_to_step[step.id] = step
        in_degree[step.id] = len(step.depends_on)
        dependents[step.id] = []
        original_index[step.id] = idx

    for step in steps:
        for dep in step.depends_on:
            dependents[dep].append(step.id)

    # Seed queue with zero-dependency steps in original order
    queue = deque(step.id for step in steps if in_degree[step.id] == 0)

    result: list = []
    while queue:
        step_id = queue.popleft()
        result.append(id_to_step[step_id])

        # Collect newly-ready dependents, sort by original index for stability
        newly_ready = []
        for dependent_id in dependents[step_id]:
            in_degree[dependent_id] -= 1
            if in_degree[dependent_id] == 0:
                newly_ready.append(dependent_id)
        newly_ready.sort(key=lambda sid: original_index[sid])
        queue.extend(newly_ready)

    if len(result) != len(steps):
        raise ValueError("Circular dependency detected among workflow steps")

    return result


# ---------------------------------------------------------------------------
# Order CRUD (demo)
# ---------------------------------------------------------------------------


def create_order(conn: sqlite3.Connection, amount: float) -> str:
    """Create a demo order in 'pending' status. Commits. Returns the order UUID."""
    order_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO orders (id, status, amount, created_at, updated_at) "
        "VALUES (?, 'pending', ?, ?, ?)",
        (order_id, amount, now, now),
    )
    conn.commit()
    return order_id


def get_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    """Return a single order by ID, or None."""
    return conn.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Execution Loop (Phase 5)
# ---------------------------------------------------------------------------


def execute_step(
    conn: sqlite3.Connection, step_row: sqlite3.Row, step_config: WorkflowStepConfig
) -> str:
    """Execute a single step with idempotency and retry support.

    Returns "completed", "retry", or "failed".
    """
    step_id = step_row["id"]
    retry_count = step_row["retry_count"]
    max_retries = step_row["max_retries"]

    # 1. Reuse existing idempotency key (crash recovery) or generate a new one
    idem_key = step_row["idempotency_key"] or str(uuid.uuid4())
    update_step_status(
        conn, step_id, "running",
        idempotency_key=idem_key, started_at=_now(),
    )
    conn.commit()

    # 2. Check for existing result (idempotency — crash recovery case)
    existing = check_step_result(conn, idem_key)
    if existing is not None:
        logger.info("Step %s: found existing result for idem key %s, skipping", step_id, idem_key)
        update_step_status(conn, step_id, "completed", completed_at=_now())
        conn.commit()
        return "completed"

    # 3. Execute the task
    try:
        result = execute_task(step_config)
    except TaskExecutionError as e:
        if retry_count < max_retries:
            # Retry: increment count, new idem key, back to pending
            new_idem_key = str(uuid.uuid4())
            update_step_status(
                conn, step_id, "pending",
                retry_count=retry_count + 1, idempotency_key=new_idem_key,
            )
            conn.commit()
            logger.info(
                "Step %s: attempt %d/%d failed, will retry",
                step_id, retry_count + 1, max_retries + 1,
            )
            return "retry"
        else:
            # Exhausted: mark failed
            update_step_status(
                conn, step_id, "failed",
                completed_at=_now(), error_message=str(e),
            )
            conn.commit()
            logger.warning("Step %s: permanently failed after %d attempts", step_id, retry_count + 1)
            return "failed"

    # 4. Success: atomic commit — insert result + mark completed
    insert_step_result(conn, idem_key, step_id, result)
    update_step_status(conn, step_id, "completed", completed_at=_now())
    conn.commit()
    logger.info("Step %s: completed successfully", step_id)
    return "completed"


def execute_run(run_id: str) -> None:
    """Execute all steps in a run sequentially. Designed for background threads.

    Opens its own DB connection for thread safety.
    """
    from database import get_connection

    conn = get_connection()
    try:
        # Load run and workflow definition
        run = get_run_detail(conn, run_id)
        if run is None:
            logger.error("Run %s not found", run_id)
            return

        workflow = get_workflow(conn, run["workflow_id"])
        if workflow is None:
            logger.error("Workflow %s not found for run %s", run["workflow_id"], run_id)
            return

        definition = json.loads(workflow["definition"])
        step_configs_by_id = {s["id"]: s["config"] for s in definition["steps"]}
        logger.info("Run %s: starting execution (workflow='%s', steps=%d)",
                     run_id, run["workflow_name"], len(step_configs_by_id))

        # Mark run as running (skip if already running — crash recovery case)
        if run["status"] != "running":
            update_run_status(conn, run_id, "running", started_at=_now())
            conn.commit()

        # Process steps sequentially
        steps = get_steps_for_run(conn, run_id)
        run_failed = False

        for step_row in steps:
            # Skip completed steps (crash recovery)
            if step_row["status"] == "completed":
                logger.info("Step %s (%s): already completed, skipping", step_row["id"], step_row["step_id"])
                continue

            # Build config from workflow definition (lookup by step ID, not array position)
            config_dict = step_configs_by_id[step_row["step_id"]]
            step_config = WorkflowStepConfig(**config_dict)

            # Retry loop for this step
            try:
                while True:
                    # Re-fetch step to get current retry_count (may have been incremented)
                    current_step = get_steps_for_run(conn, run_id)
                    current = next(s for s in current_step if s["id"] == step_row["id"])

                    outcome = execute_step(conn, current, step_config)

                    if outcome == "completed":
                        break
                    elif outcome == "retry":
                        continue
                    else:  # "failed"
                        run_failed = True
                        break
            except Exception:
                logger.exception(
                    "Run %s: unexpected error executing step '%s' (index=%d, id=%s)",
                    run_id, step_row["step_id"], step_row["step_index"], step_row["id"],
                )
                raise

            if run_failed:
                break

        # Set final run status
        final_status = "failed" if run_failed else "completed"
        update_run_status(conn, run_id, final_status, completed_at=_now())
        conn.commit()
        logger.info("Run %s: finished with status '%s'", run_id, final_status)

    except Exception:
        logger.exception("Run %s: unexpected error during execution", run_id)
        try:
            update_run_status(conn, run_id, "failed", completed_at=_now())
            conn.commit()
        except Exception:
            logger.exception("Run %s: failed to update run status after error", run_id)
    finally:
        conn.close()


def start_run_thread(run_id: str) -> threading.Thread:
    """Spawn a daemon thread to execute a run in the background."""
    thread = threading.Thread(target=execute_run, args=(run_id,), daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Crash Recovery (Phase 6)
# ---------------------------------------------------------------------------


def recover_interrupted_runs() -> list[threading.Thread]:
    """Find all runs with status='running' and resume them in background threads.

    Called at startup before accepting HTTP requests.
    Returns list of spawned threads (useful for testing).
    """
    from database import get_connection

    conn = get_connection()
    try:
        running = get_running_runs(conn)
        if not running:
            logger.info("Recovery: no interrupted runs found")
            return []

        logger.info("Recovery: found %d interrupted run(s)", len(running))
        threads = []
        for run in running:
            run_id = run["id"]
            logger.info("Recovery: resuming run %s", run_id)
            threads.append(start_run_thread(run_id))
        return threads
    finally:
        conn.close()
