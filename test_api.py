"""API route tests for main.py.

Tests expected responses, validation errors, 404s, run lifecycle,
retry/failure behavior, and crash recovery through the HTTP layer.

Usage: uv run pytest test_api.py -v
"""

import json
import os
import time
import uuid
from unittest.mock import patch

import pytest

import database
from database import get_connection, init_db
from executor import (
    _now,
    get_run_detail,
    get_steps_for_run,
    insert_step_result,
    update_run_status,
    update_step_status,
)
from tasks import TaskExecutionError

TEST_DB = "/tmp/test_api.db"

SAMPLE_WORKFLOW = {
    "name": "order-processing",
    "steps": [
        {
            "id": "validate",
            "type": "task",
            "config": {
                "action": "validate_order",
                "duration_seconds": 0.01,
                "fail_probability": 0.0,
                "max_retries": 0,
            },
            "depends_on": [],
        },
        {
            "id": "charge",
            "type": "task",
            "config": {
                "action": "charge_payment",
                "duration_seconds": 0.01,
                "fail_probability": 0.0,
                "max_retries": 2,
            },
            "depends_on": ["validate"],
        },
        {
            "id": "ship",
            "type": "task",
            "config": {
                "action": "ship_order",
                "duration_seconds": 0.01,
                "fail_probability": 0.0,
                "max_retries": 0,
            },
            "depends_on": ["charge"],
        },
    ],
}


@pytest.fixture(autouse=True)
def setup_db():
    """Redirect all DB access to a temp file, fresh per test."""
    database.DB_PATH = database.Path(TEST_DB)
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ==================================================================
# Happy path: workflow CRUD
# ==================================================================
class TestWorkflowRoutes:
    def test_create_workflow(self, client):
        resp = client.post("/workflows", json=SAMPLE_WORKFLOW)
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "order-processing"
        assert body["id"]  # UUID present
        assert body["created_at"]
        # definition round-trips as parsed JSON, not a string
        assert body["definition"]["name"] == "order-processing"
        assert len(body["definition"]["steps"]) == 3

    def test_list_workflows(self, client):
        client.post("/workflows", json=SAMPLE_WORKFLOW)
        client.post("/workflows", json={**SAMPLE_WORKFLOW, "name": "second-wf"})

        resp = client.get("/workflows")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        # List response should NOT include definition
        for item in items:
            assert "definition" not in item
            assert "id" in item
            assert "name" in item
            assert "created_at" in item

    def test_get_workflow_detail(self, client):
        create_resp = client.post("/workflows", json=SAMPLE_WORKFLOW)
        wf_id = create_resp.json()["id"]

        resp = client.get(f"/workflows/{wf_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == wf_id
        assert body["definition"]["steps"][0]["id"] == "validate"

    def test_get_workflow_not_found(self, client):
        resp = client.get("/workflows/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Workflow not found"


# ==================================================================
# Happy path: run lifecycle
# ==================================================================
class TestRunLifecycle:
    def _create_workflow(self, client):
        return client.post("/workflows", json=SAMPLE_WORKFLOW).json()["id"]

    def test_create_run_returns_202_with_pending_steps(self, client):
        wf_id = self._create_workflow(client)
        resp = client.post(f"/workflows/{wf_id}/runs")

        assert resp.status_code == 202
        body = resp.json()
        assert body["workflow_id"] == wf_id
        assert body["workflow_name"] == "order-processing"
        assert len(body["steps"]) == 3
        assert all(s["status"] == "pending" for s in body["steps"])
        assert [s["step_id"] for s in body["steps"]] == ["validate", "charge", "ship"]
        assert [s["step_index"] for s in body["steps"]] == [0, 1, 2]

    def test_run_completes_all_steps(self, client):
        wf_id = self._create_workflow(client)
        run_id = client.post(f"/workflows/{wf_id}/runs").json()["id"]

        # Poll until completed (steps are 0.01s each, should finish fast)
        for _ in range(50):
            resp = client.get(f"/runs/{run_id}")
            if resp.json()["status"] == "completed":
                break
            time.sleep(0.1)

        body = resp.json()
        assert body["status"] == "completed"
        assert body["started_at"] is not None
        assert body["completed_at"] is not None
        assert all(s["status"] == "completed" for s in body["steps"])
        # Steps completed in order
        for i in range(1, len(body["steps"])):
            assert body["steps"][i]["started_at"] >= body["steps"][i - 1]["completed_at"]

    def test_list_runs_includes_workflow_name(self, client):
        wf_id = self._create_workflow(client)
        client.post(f"/workflows/{wf_id}/runs")

        resp = client.get("/runs")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["workflow_name"] == "order-processing"
        # List should NOT include steps
        assert "steps" not in items[0]

    def test_create_run_workflow_not_found(self, client):
        resp = client.post("/workflows/nonexistent/runs")
        assert resp.status_code == 404

    def test_get_run_not_found(self, client):
        resp = client.get("/runs/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Run not found"

    def test_multiple_runs_same_workflow(self, client):
        wf_id = self._create_workflow(client)
        run_a = client.post(f"/workflows/{wf_id}/runs").json()
        run_b = client.post(f"/workflows/{wf_id}/runs").json()

        assert run_a["id"] != run_b["id"]
        assert run_a["workflow_id"] == run_b["workflow_id"] == wf_id

        # Wait for both to complete
        for run_id in [run_a["id"], run_b["id"]]:
            for _ in range(50):
                if client.get(f"/runs/{run_id}").json()["status"] == "completed":
                    break
                time.sleep(0.1)

        runs = client.get("/runs").json()
        assert all(r["status"] == "completed" for r in runs)


# ==================================================================
# Validation errors (422)
# ==================================================================
class TestValidation:
    def test_empty_steps(self, client):
        resp = client.post("/workflows", json={"name": "empty", "steps": []})
        assert resp.status_code == 422

    def test_missing_name(self, client):
        resp = client.post("/workflows", json={"steps": [{"id": "s", "type": "task", "config": {"action": "x"}}]})
        assert resp.status_code == 422

    def test_duplicate_step_ids(self, client):
        resp = client.post("/workflows", json={
            "name": "dup",
            "steps": [
                {"id": "same", "type": "task", "config": {"action": "a"}},
                {"id": "same", "type": "task", "config": {"action": "b"}},
            ],
        })
        assert resp.status_code == 422

    def test_invalid_depends_on(self, client):
        resp = client.post("/workflows", json={
            "name": "bad-dep",
            "steps": [
                {"id": "s1", "type": "task", "config": {"action": "a"}, "depends_on": ["nonexistent"]},
            ],
        })
        assert resp.status_code == 422

    def test_fail_probability_out_of_range(self, client):
        resp = client.post("/workflows", json={
            "name": "bad-prob",
            "steps": [
                {"id": "s1", "type": "task", "config": {"action": "a", "fail_probability": 1.5}},
            ],
        })
        assert resp.status_code == 422

    def test_forward_reference_accepted(self, client):
        """depends_on can reference a step defined later in the array."""
        resp = client.post("/workflows", json={
            "name": "forward-ref",
            "steps": [
                {"id": "B", "type": "task", "config": {"action": "b"}, "depends_on": ["A"]},
                {"id": "A", "type": "task", "config": {"action": "a"}, "depends_on": []},
            ],
        })
        assert resp.status_code == 201

    def test_cycle_rejected(self, client):
        """Circular dependencies return 422."""
        resp = client.post("/workflows", json={
            "name": "cycle",
            "steps": [
                {"id": "A", "type": "task", "config": {"action": "a"}, "depends_on": ["B"]},
                {"id": "B", "type": "task", "config": {"action": "b"}, "depends_on": ["A"]},
            ],
        })
        assert resp.status_code == 422

    def test_order_negative_amount(self, client):
        resp = client.post("/orders", json={"amount": -10})
        assert resp.status_code == 422

    def test_order_zero_amount(self, client):
        resp = client.post("/orders", json={"amount": 0})
        assert resp.status_code == 422


# ==================================================================
# Order routes
# ==================================================================
class TestOrderRoutes:
    def test_create_and_get_order(self, client):
        resp = client.post("/orders", json={"amount": 49.99})
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "pending"
        assert body["amount"] == 49.99
        assert body["created_at"] == body["updated_at"]

        get_resp = client.get(f"/orders/{body['id']}")
        assert get_resp.status_code == 200
        assert get_resp.json() == body

    def test_order_not_found(self, client):
        resp = client.get("/orders/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Order not found"


# ==================================================================
# Failure handling
# ==================================================================
class TestFailures:
    def test_step_permanent_failure_fails_run(self, client):
        """A step with fail_probability=1.0 and no retries should fail the run."""
        wf = {
            "name": "fail-test",
            "steps": [
                {"id": "ok", "type": "task", "config": {"action": "pass", "duration_seconds": 0.01}},
                {
                    "id": "doomed", "type": "task",
                    "config": {"action": "fail", "duration_seconds": 0.01, "fail_probability": 1.0, "max_retries": 0},
                    "depends_on": ["ok"],
                },
                {
                    "id": "skipped", "type": "task",
                    "config": {"action": "never", "duration_seconds": 0.01},
                    "depends_on": ["doomed"],
                },
            ],
        }
        wf_id = client.post("/workflows", json=wf).json()["id"]
        run_id = client.post(f"/workflows/{wf_id}/runs").json()["id"]

        for _ in range(50):
            body = client.get(f"/runs/{run_id}").json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)

        assert body["status"] == "failed"
        steps = body["steps"]
        assert steps[0]["status"] == "completed"
        assert steps[1]["status"] == "failed"
        assert steps[1]["error_message"] is not None
        assert steps[2]["status"] == "pending"  # never reached

    def test_retry_then_succeed(self, client):
        """A flaky step should retry and eventually succeed."""
        wf = {
            "name": "retry-test",
            "steps": [
                {
                    "id": "flaky", "type": "task",
                    "config": {"action": "flaky", "duration_seconds": 0.01, "fail_probability": 0.0, "max_retries": 3},
                },
            ],
        }
        wf_id = client.post("/workflows", json=wf).json()["id"]

        call_count = 0

        def mock_task(config):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise TaskExecutionError("flaky failure")
            return {"status": "success", "action": config.action}

        with patch("executor.execute_task", side_effect=mock_task):
            run_id = client.post(f"/workflows/{wf_id}/runs").json()["id"]
            for _ in range(50):
                body = client.get(f"/runs/{run_id}").json()
                if body["status"] in ("completed", "failed"):
                    break
                time.sleep(0.1)

        assert body["status"] == "completed"
        assert body["steps"][0]["retry_count"] == 2
        assert call_count == 3

    def test_retry_exhaustion(self, client):
        """A step that always fails should exhaust retries and fail the run."""
        wf = {
            "name": "exhaust-test",
            "steps": [
                {
                    "id": "doomed", "type": "task",
                    "config": {"action": "doomed", "duration_seconds": 0.01, "fail_probability": 1.0, "max_retries": 2},
                },
            ],
        }
        wf_id = client.post("/workflows", json=wf).json()["id"]
        run_id = client.post(f"/workflows/{wf_id}/runs").json()["id"]

        for _ in range(50):
            body = client.get(f"/runs/{run_id}").json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)

        assert body["status"] == "failed"
        step = body["steps"][0]
        assert step["status"] == "failed"
        assert step["retry_count"] == 2
        assert step["error_message"] is not None


# ==================================================================
# Durability / crash recovery
# ==================================================================
class TestDurability:
    def _create_workflow_and_run(self, client):
        """Create a workflow and manually set up a run (no background thread)."""
        wf_id = client.post("/workflows", json=SAMPLE_WORKFLOW).json()["id"]
        conn = get_connection()
        try:
            from executor import create_run, create_steps
            from models import WorkflowStep

            wf_row = client.get(f"/workflows/{wf_id}").json()
            steps = [WorkflowStep(**s) for s in wf_row["definition"]["steps"]]
            run_id = create_run(conn, wf_id)
            create_steps(conn, run_id, steps)
            return wf_id, run_id
        finally:
            conn.close()

    def test_recovery_resumes_running_run(self, client):
        """Simulate crash: run is 'running', all steps 'pending'.
        Recovery should resume and complete it."""
        wf_id, run_id = self._create_workflow_and_run(client)

        conn = get_connection()
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()
        conn.close()

        # Trigger recovery (simulates server restart)
        from executor import recover_interrupted_runs
        threads = recover_interrupted_runs()
        for t in threads:
            t.join(timeout=10)

        body = client.get(f"/runs/{run_id}").json()
        assert body["status"] == "completed"
        assert all(s["status"] == "completed" for s in body["steps"])

    def test_recovery_skips_completed_steps(self, client):
        """Simulate crash after first step completes.
        Recovery should skip completed step and finish the rest."""
        wf_id, run_id = self._create_workflow_and_run(client)

        conn = get_connection()
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        # Manually complete the first step
        steps = get_steps_for_run(conn, run_id)
        idem_key = str(uuid.uuid4())
        update_step_status(
            conn, steps[0]["id"], "completed",
            idempotency_key=idem_key, started_at=_now(), completed_at=_now(),
        )
        insert_step_result(conn, idem_key, steps[0]["id"], {"status": "success"})
        conn.commit()
        conn.close()

        from executor import recover_interrupted_runs
        threads = recover_interrupted_runs()
        for t in threads:
            t.join(timeout=10)

        body = client.get(f"/runs/{run_id}").json()
        assert body["status"] == "completed"
        assert all(s["status"] == "completed" for s in body["steps"])
        # First step was not re-executed (retry_count still 0)
        assert body["steps"][0]["retry_count"] == 0

    def test_recovery_idempotency_with_committed_result(self, client):
        """Simulate crash: step is 'running' with idem key and result already committed.
        Recovery should detect the result and skip re-execution."""
        wf = {
            "name": "idem-test",
            "steps": [
                {
                    "id": "slow", "type": "task",
                    "config": {"action": "slow", "duration_seconds": 5.0, "fail_probability": 1.0},
                },
            ],
        }
        wf_id = client.post("/workflows", json=wf).json()["id"]

        conn = get_connection()
        from executor import create_run, create_steps
        from models import WorkflowStep

        steps_def = [WorkflowStep(**s) for s in wf["steps"]]
        run_id = create_run(conn, wf_id)
        step_rows = create_steps(conn, run_id, steps_def)

        # Simulate: run "running", step "running" with idem key + result committed
        update_run_status(conn, run_id, "running", started_at=_now())
        idem_key = str(uuid.uuid4())
        update_step_status(
            conn, step_rows[0]["id"], "running",
            idempotency_key=idem_key, started_at=_now(),
        )
        insert_step_result(conn, idem_key, step_rows[0]["id"], {"status": "success"})
        conn.commit()
        conn.close()

        # If idempotency fails, this would take 5s and always fail
        from executor import recover_interrupted_runs
        threads = recover_interrupted_runs()
        for t in threads:
            t.join(timeout=10)

        body = client.get(f"/runs/{run_id}").json()
        assert body["status"] == "completed"
        assert body["steps"][0]["status"] == "completed"

    def test_recovery_preserves_started_at(self, client):
        """Recovery should not overwrite the original started_at timestamp."""
        wf_id, run_id = self._create_workflow_and_run(client)

        original_started = "2025-01-01T00:00:00+00:00"
        conn = get_connection()
        update_run_status(conn, run_id, "running", started_at=original_started)
        conn.commit()
        conn.close()

        from executor import recover_interrupted_runs
        threads = recover_interrupted_runs()
        for t in threads:
            t.join(timeout=10)

        body = client.get(f"/runs/{run_id}").json()
        assert body["status"] == "completed"
        assert body["started_at"] == original_started


# ==================================================================
# Response shape checks
# ==================================================================
class TestResponseShapes:
    def test_step_response_fields(self, client):
        """Verify StepStateResponse has exactly the expected fields."""
        wf_id = client.post("/workflows", json=SAMPLE_WORKFLOW).json()["id"]
        run_id = client.post(f"/workflows/{wf_id}/runs").json()["id"]

        body = client.get(f"/runs/{run_id}").json()
        step = body["steps"][0]
        expected_keys = {
            "id", "step_id", "step_index", "status",
            "retry_count", "max_retries",
            "started_at", "completed_at", "error_message",
        }
        assert set(step.keys()) == expected_keys
        # Internal fields should NOT leak
        assert "run_id" not in step
        assert "idempotency_key" not in step
        assert "created_at" not in step

    def test_run_detail_response_fields(self, client):
        wf_id = client.post("/workflows", json=SAMPLE_WORKFLOW).json()["id"]
        run_id = client.post(f"/workflows/{wf_id}/runs").json()["id"]

        body = client.get(f"/runs/{run_id}").json()
        expected_keys = {
            "id", "workflow_id", "workflow_name", "status",
            "started_at", "completed_at", "steps",
        }
        assert set(body.keys()) == expected_keys

    def test_workflow_summary_vs_detail(self, client):
        """List excludes definition, detail includes it."""
        wf_id = client.post("/workflows", json=SAMPLE_WORKFLOW).json()["id"]

        summary = client.get("/workflows").json()[0]
        detail = client.get(f"/workflows/{wf_id}").json()

        assert "definition" not in summary
        assert "definition" in detail
        assert summary["id"] == detail["id"]
        assert summary["name"] == detail["name"]
