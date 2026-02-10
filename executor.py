import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

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
    for index, step in enumerate(steps_definition):
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
