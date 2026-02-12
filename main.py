import json
import logging
import sqlite3
from pathlib import Path
from typing import Generator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import get_connection, init_db
from executor import (
    create_order,
    create_run,
    create_steps,
    create_workflow,
    get_all_runs,
    get_all_workflows,
    get_order,
    get_run_detail,
    get_steps_for_run,
    get_workflow,
    recover_interrupted_runs,
    start_run_thread,
)
from models import (
    CreateOrderRequest,
    CreateWorkflowRequest,
    OrderResponse,
    RunDetailResponse,
    RunSummaryResponse,
    StartRunRequest,
    StepStateResponse,
    WorkflowDetailResponse,
    WorkflowStep,
    WorkflowSummaryResponse,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Durable Workflow Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()
    recover_interrupted_runs()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step_to_response(row: sqlite3.Row) -> StepStateResponse:
    return StepStateResponse(
        id=row["id"],
        step_id=row["step_id"],
        step_index=row["step_index"],
        status=row["status"],
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_message=row["error_message"],
    )


# ---------------------------------------------------------------------------
# Workflow routes
# ---------------------------------------------------------------------------


@app.post("/workflows", response_model=WorkflowDetailResponse, status_code=201)
def create_workflow_route(
    body: CreateWorkflowRequest,
    conn: sqlite3.Connection = Depends(get_db),
):
    workflow_id = create_workflow(conn, body.name, body.model_dump_json())
    row = get_workflow(conn, workflow_id)
    return WorkflowDetailResponse(
        id=row["id"],
        name=row["name"],
        definition=json.loads(row["definition"]),
        created_at=row["created_at"],
    )


@app.get("/workflows", response_model=list[WorkflowSummaryResponse])
def list_workflows(conn: sqlite3.Connection = Depends(get_db)):
    rows = get_all_workflows(conn)
    return [WorkflowSummaryResponse(**dict(r)) for r in rows]


@app.get("/workflows/{workflow_id}", response_model=WorkflowDetailResponse)
def get_workflow_route(
    workflow_id: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    row = get_workflow(conn, workflow_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowDetailResponse(
        id=row["id"],
        name=row["name"],
        definition=json.loads(row["definition"]),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Run routes
# ---------------------------------------------------------------------------


@app.post(
    "/workflows/{workflow_id}/runs",
    response_model=RunDetailResponse,
    status_code=202,
)
def create_run_route(
    workflow_id: str,
    body: StartRunRequest | None = None,
    conn: sqlite3.Connection = Depends(get_db),
):
    workflow_row = get_workflow(conn, workflow_id)
    if workflow_row is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    definition = json.loads(workflow_row["definition"])
    steps = [WorkflowStep(**s) for s in definition["steps"]]

    order_id = body.order_id if body else None
    run_id = create_run(conn, workflow_id, order_id=order_id)
    step_rows = create_steps(conn, run_id, steps)
    start_run_thread(run_id)

    run_row = get_run_detail(conn, run_id)
    return RunDetailResponse(
        **dict(run_row),
        steps=[_step_to_response(s) for s in step_rows],
    )


@app.get("/runs", response_model=list[RunSummaryResponse])
def list_runs(conn: sqlite3.Connection = Depends(get_db)):
    rows = get_all_runs(conn)
    return [RunSummaryResponse(**dict(r)) for r in rows]


@app.get("/runs/{run_id}", response_model=RunDetailResponse)
def get_run_route(
    run_id: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    run_row = get_run_detail(conn, run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    step_rows = get_steps_for_run(conn, run_id)
    return RunDetailResponse(
        **dict(run_row),
        steps=[_step_to_response(s) for s in step_rows],
    )


# ---------------------------------------------------------------------------
# Order routes
# ---------------------------------------------------------------------------


@app.post("/orders", response_model=OrderResponse, status_code=201)
def create_order_route(
    body: CreateOrderRequest,
    conn: sqlite3.Connection = Depends(get_db),
):
    order_id = create_order(conn, body.amount)
    row = get_order(conn, order_id)
    return OrderResponse(**dict(row))


@app.get("/orders/{order_id}", response_model=OrderResponse)
def get_order_route(
    order_id: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    row = get_order(conn, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderResponse(**dict(row))


# ---------------------------------------------------------------------------
# DB Snapshot (live viewer)
# ---------------------------------------------------------------------------


@app.get("/db/snapshot")
def db_snapshot(conn: sqlite3.Connection = Depends(get_db)):
    tables = ["workflows", "runs", "steps", "step_results", "orders"]
    result = {}
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 20"  # noqa: S608
        ).fetchall()
        result[table] = {
            "count": count,
            "rows": [dict(r) for r in rows],
        }
    return result


# ---------------------------------------------------------------------------
# Static files (Phase 8 prep)
# ---------------------------------------------------------------------------

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
