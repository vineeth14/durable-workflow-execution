"""Extensive pytest tests for executor.py CRUD functions.

Simulates full workflow lifecycles, retries, idempotency/crash recovery,
concurrent runs, partial failures, and edge cases.

Usage: uv run pytest test_executor.py -v
"""

import json
import os
import uuid
from unittest.mock import patch

import pytest

import database
from database import get_connection, init_db
from executor import (
    _now,
    check_step_result,
    create_order,
    create_run,
    create_steps,
    create_workflow,
    execute_run,
    execute_step,
    get_all_runs,
    get_all_workflows,
    get_order,
    get_run_detail,
    get_running_runs,
    get_steps_for_run,
    get_workflow,
    insert_step_result,
    recover_interrupted_runs,
    start_run_thread,
    update_run_status,
    update_step_status,
)
from models import CreateWorkflowRequest, WorkflowStep, WorkflowStepConfig
from tasks import TaskExecutionError, execute_task

TEST_DB = "/tmp/test_executor.db"


@pytest.fixture(autouse=True)
def setup_db():
    """Use a temp DB for every test session."""
    database.DB_PATH = database.Path(TEST_DB)
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


@pytest.fixture
def conn():
    c = get_connection()
    yield c
    c.close()


@pytest.fixture
def sample_workflow():
    """A 3-step order-processing workflow request."""
    return CreateWorkflowRequest(
        name="order-processing",
        steps=[
            WorkflowStep(
                id="validate",
                type="task",
                config=WorkflowStepConfig(
                    action="validate_order", duration_seconds=0.01, fail_probability=0.0
                ),
                depends_on=[],
            ),
            WorkflowStep(
                id="charge",
                type="task",
                config=WorkflowStepConfig(
                    action="charge_payment",
                    duration_seconds=0.01,
                    fail_probability=0.0,
                    max_retries=2,
                ),
                depends_on=["validate"],
            ),
            WorkflowStep(
                id="ship",
                type="task",
                config=WorkflowStepConfig(
                    action="ship_order", duration_seconds=0.01, fail_probability=0.0
                ),
                depends_on=["charge"],
            ),
        ],
    )


def _create_full_run(conn, wf_request):
    """Helper: create workflow + run + steps, return (wf_id, run_id, step_rows)."""
    definition_json = json.dumps(wf_request.model_dump())
    wf_id = create_workflow(conn, wf_request.name, definition_json)
    run_id = create_run(conn, wf_id)
    step_rows = create_steps(conn, run_id, wf_request.steps)
    return wf_id, run_id, step_rows


# ==================================================================
# Test 1: Full happy-path workflow simulation
# ==================================================================
class TestHappyPath:
    def test_full_execution(self, conn, sample_workflow):
        wf_id, run_id, step_rows = _create_full_run(conn, sample_workflow)

        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        for step_row in step_rows:
            sid = step_row["id"]
            idem_key = str(uuid.uuid4())

            update_step_status(conn, sid, "running", started_at=_now(), idempotency_key=idem_key)
            conn.commit()

            # Fresh execution — no existing result
            assert check_step_result(conn, idem_key) is None

            config = sample_workflow.steps[step_row["step_index"]].config
            result = execute_task(config)

            insert_step_result(conn, idem_key, sid, result)
            update_step_status(conn, sid, "completed", completed_at=_now())
            conn.commit()

        update_run_status(conn, run_id, "completed", completed_at=_now())
        conn.commit()

        run = get_run_detail(conn, run_id)
        steps = get_steps_for_run(conn, run_id)

        assert run["status"] == "completed"
        assert all(s["status"] == "completed" for s in steps)
        assert [s["step_id"] for s in steps] == ["validate", "charge", "ship"]
        assert all(s["started_at"] is not None for s in steps)
        assert all(s["completed_at"] is not None for s in steps)
        assert all(s["error_message"] is None for s in steps)


# ==================================================================
# Test 2: Retry simulation (guaranteed failure, exhaust retries)
# ==================================================================
class TestRetry:
    def test_exhaust_retries(self, conn):
        wf = CreateWorkflowRequest(
            name="retry-test",
            steps=[
                WorkflowStep(
                    id="flaky-step",
                    type="task",
                    config=WorkflowStepConfig(
                        action="flaky_operation",
                        duration_seconds=0.01,
                        fail_probability=1.0,
                        max_retries=3,
                    ),
                ),
            ],
        )
        wf_id, run_id, step_rows = _create_full_run(conn, wf)

        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        sid = step_rows[0]["id"]
        max_retries = step_rows[0]["max_retries"]
        retry_count = 0

        for attempt in range(max_retries + 1):
            idem_key = str(uuid.uuid4())
            update_step_status(conn, sid, "running", started_at=_now(), idempotency_key=idem_key)
            conn.commit()

            try:
                execute_task(wf.steps[0].config)
                insert_step_result(conn, idem_key, sid, {"status": "success"})
                update_step_status(conn, sid, "completed", completed_at=_now())
                conn.commit()
                break
            except TaskExecutionError as e:
                if retry_count < max_retries:
                    retry_count += 1
                    update_step_status(conn, sid, "pending", retry_count=retry_count)
                    conn.commit()
                else:
                    update_step_status(
                        conn, sid, "failed", completed_at=_now(), error_message=str(e)
                    )
                    conn.commit()

        failed_step = get_steps_for_run(conn, run_id)[0]
        assert failed_step["status"] == "failed"
        assert failed_step["retry_count"] == 3
        assert failed_step["error_message"] is not None
        assert "failed" in failed_step["error_message"]

        update_run_status(conn, run_id, "failed", completed_at=_now())
        conn.commit()
        assert get_run_detail(conn, run_id)["status"] == "failed"


# ==================================================================
# Test 3: Idempotency — simulate crash recovery
# ==================================================================
class TestIdempotency:
    def test_crash_recovery_skips_completed_work(self, conn):
        """Result committed but step status still 'running' (crash before status update).
        Recovery should detect the existing result and skip re-execution."""
        wf = CreateWorkflowRequest(
            name="idempotency-test",
            steps=[
                WorkflowStep(
                    id="step-a",
                    type="task",
                    config=WorkflowStepConfig(
                        action="do_something", duration_seconds=0.01, fail_probability=0.0
                    ),
                ),
            ],
        )
        wf_id, run_id, step_rows = _create_full_run(conn, wf)
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        sid = step_rows[0]["id"]
        idem_key = str(uuid.uuid4())

        # Step starts, task executes, result committed, then "crash"
        update_step_status(conn, sid, "running", started_at=_now(), idempotency_key=idem_key)
        conn.commit()

        result = execute_task(wf.steps[0].config)
        insert_step_result(conn, idem_key, sid, result)
        conn.commit()  # Result committed but step still "running"

        step_state = get_steps_for_run(conn, run_id)[0]
        assert step_state["status"] == "running"
        assert check_step_result(conn, idem_key) is not None

        # Recovery: detect existing result, skip execution
        existing_result = check_step_result(conn, idem_key)
        assert existing_result is not None
        update_step_status(conn, sid, "completed", completed_at=_now())
        conn.commit()

        recovered = get_steps_for_run(conn, run_id)[0]
        assert recovered["status"] == "completed"

        # No duplicate results
        count = conn.execute(
            "SELECT COUNT(*) FROM step_results WHERE step_id = ?", (sid,)
        ).fetchone()[0]
        assert count == 1


# ==================================================================
# Test 4: Multiple concurrent runs on same workflow
# ==================================================================
class TestConcurrentRuns:
    def test_multiple_runs_same_workflow(self, conn, sample_workflow):
        wf_id, _, _ = _create_full_run(conn, sample_workflow)

        run_a_id = create_run(conn, wf_id)
        run_b_id = create_run(conn, wf_id)
        steps_a = create_steps(conn, run_a_id, sample_workflow.steps)
        steps_b = create_steps(conn, run_b_id, sample_workflow.steps)

        assert run_a_id != run_b_id
        assert len(steps_a) == 3
        assert len(steps_b) == 3
        assert steps_a[0]["id"] != steps_b[0]["id"]
        assert steps_a[0]["step_id"] == steps_b[0]["step_id"] == "validate"

    def test_running_runs_tracking(self, conn, sample_workflow):
        wf_id, _, _ = _create_full_run(conn, sample_workflow)

        run_a_id = create_run(conn, wf_id)
        run_b_id = create_run(conn, wf_id)

        update_run_status(conn, run_a_id, "running", started_at=_now())
        update_run_status(conn, run_b_id, "running", started_at=_now())
        conn.commit()

        assert len(get_running_runs(conn)) == 2

        update_run_status(conn, run_a_id, "completed", completed_at=_now())
        conn.commit()

        running = get_running_runs(conn)
        assert len(running) == 1
        assert running[0]["id"] == run_b_id


# ==================================================================
# Test 5: Partial workflow failure (middle step fails)
# ==================================================================
class TestPartialFailure:
    def test_middle_step_fails_rest_pending(self, conn):
        wf = CreateWorkflowRequest(
            name="partial-failure",
            steps=[
                WorkflowStep(
                    id="step1",
                    type="task",
                    config=WorkflowStepConfig(
                        action="always_pass", duration_seconds=0.01, fail_probability=0.0
                    ),
                ),
                WorkflowStep(
                    id="step2",
                    type="task",
                    config=WorkflowStepConfig(
                        action="always_fail",
                        duration_seconds=0.01,
                        fail_probability=1.0,
                        max_retries=0,
                    ),
                    depends_on=["step1"],
                ),
                WorkflowStep(
                    id="step3",
                    type="task",
                    config=WorkflowStepConfig(
                        action="never_reached", duration_seconds=0.01, fail_probability=0.0
                    ),
                    depends_on=["step2"],
                ),
            ],
        )
        wf_id, run_id, steps = _create_full_run(conn, wf)
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        # Step 1 succeeds
        idem1 = str(uuid.uuid4())
        update_step_status(
            conn, steps[0]["id"], "running", started_at=_now(), idempotency_key=idem1
        )
        conn.commit()
        r1 = execute_task(wf.steps[0].config)
        insert_step_result(conn, idem1, steps[0]["id"], r1)
        update_step_status(conn, steps[0]["id"], "completed", completed_at=_now())
        conn.commit()

        # Step 2 fails
        idem2 = str(uuid.uuid4())
        update_step_status(
            conn, steps[1]["id"], "running", started_at=_now(), idempotency_key=idem2
        )
        conn.commit()
        try:
            execute_task(wf.steps[1].config)
        except TaskExecutionError as e:
            update_step_status(
                conn, steps[1]["id"], "failed", completed_at=_now(), error_message=str(e)
            )
            conn.commit()

        update_run_status(conn, run_id, "failed", completed_at=_now())
        conn.commit()

        final_steps = get_steps_for_run(conn, run_id)
        assert final_steps[0]["status"] == "completed"
        assert final_steps[1]["status"] == "failed"
        assert final_steps[2]["status"] == "pending"
        assert get_run_detail(conn, run_id)["status"] == "failed"


# ==================================================================
# Test 6: Workflow definition round-trip
# ==================================================================
class TestDefinitionRoundtrip:
    def test_json_with_special_chars(self, conn):
        complex_def = {
            "name": "complex-workflow",
            "steps": [
                {
                    "id": 'step-with-"quotes"',
                    "type": "task",
                    "config": {
                        "action": "test action with special chars: <>&",
                        "duration_seconds": 0.5,
                        "fail_probability": 0.0,
                        "max_retries": 0,
                    },
                    "depends_on": [],
                }
            ],
        }
        def_json = json.dumps(complex_def)
        wf_id = create_workflow(conn, "complex-workflow", def_json)
        retrieved = get_workflow(conn, wf_id)

        assert retrieved["definition"] == def_json
        parsed = json.loads(retrieved["definition"])
        assert parsed == complex_def
        assert '"quotes"' in parsed["steps"][0]["id"]

    def test_summary_excludes_definition(self, conn):
        wf_id = create_workflow(conn, "test", '{"steps": []}')
        rows = get_all_workflows(conn)
        assert len(rows) >= 1
        assert "definition" not in rows[0].keys()


# ==================================================================
# Test 7: Order lifecycle
# ==================================================================
class TestOrderLifecycle:
    def test_create_and_get(self, conn):
        order_id = create_order(conn, 99.99)
        order = get_order(conn, order_id)

        assert order["status"] == "pending"
        assert order["amount"] == 99.99
        assert order["created_at"] == order["updated_at"]

    def test_status_progression(self, conn):
        order_id = create_order(conn, 49.99)

        for new_status in ["validated", "charged", "shipped"]:
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, _now(), order_id),
            )
            conn.commit()

        final = get_order(conn, order_id)
        assert final["status"] == "shipped"
        assert final["updated_at"] != final["created_at"]


# ==================================================================
# Test 8: Edge cases
# ==================================================================
class TestEdgeCases:
    def test_nonexistent_workflow(self, conn):
        assert get_workflow(conn, "fake-id") is None

    def test_nonexistent_run(self, conn):
        assert get_run_detail(conn, "fake-id") is None

    def test_nonexistent_order(self, conn):
        assert get_order(conn, "fake-id") is None

    def test_steps_for_nonexistent_run(self, conn):
        assert get_steps_for_run(conn, "fake-id") == []

    def test_no_running_runs_initially(self, conn):
        assert get_running_runs(conn) == []

    def test_null_result_data(self, conn):
        # Need a step to reference
        wf = CreateWorkflowRequest(
            name="null-test",
            steps=[
                WorkflowStep(
                    id="s",
                    type="task",
                    config=WorkflowStepConfig(action="x", duration_seconds=0.01),
                )
            ],
        )
        _, run_id, steps = _create_full_run(conn, wf)
        idem = str(uuid.uuid4())
        insert_step_result(conn, idem, steps[0]["id"], None)
        conn.commit()

        result = check_step_result(conn, idem)
        assert result is not None
        assert result["result_data"] is None

    def test_single_step_workflow(self, conn):
        wf = CreateWorkflowRequest(
            name="single-step",
            steps=[
                WorkflowStep(
                    id="only",
                    type="task",
                    config=WorkflowStepConfig(action="solo", duration_seconds=0.01),
                )
            ],
        )
        _, run_id, steps = _create_full_run(conn, wf)
        assert len(steps) == 1
        assert steps[0]["step_index"] == 0

    def test_invalid_step_kwargs_rejected(self, conn):
        with pytest.raises(ValueError, match="Unknown field"):
            update_step_status(conn, "fake", "running", bogus_field="nope")

    def test_invalid_run_kwargs_rejected(self, conn):
        with pytest.raises(ValueError, match="Unknown field"):
            update_run_status(conn, "fake", "running", bogus="nope")

    def test_check_nonexistent_idempotency_key(self, conn):
        assert check_step_result(conn, "nonexistent-key") is None


# ==================================================================
# Test 9: execute_step — direct unit tests
# ==================================================================
class TestExecuteStep:
    def test_happy_path_completion(self, conn, sample_workflow):
        _, run_id, step_rows = _create_full_run(conn, sample_workflow)
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        config = sample_workflow.steps[0].config
        outcome = execute_step(conn, step_rows[0], config)

        assert outcome == "completed"
        step = get_steps_for_run(conn, run_id)[0]
        assert step["status"] == "completed"
        assert step["started_at"] is not None
        assert step["completed_at"] is not None
        assert step["idempotency_key"] is not None
        # Result was inserted
        assert check_step_result(conn, step["idempotency_key"]) is not None

    def test_idempotency_skip_on_crash_recovery(self, conn):
        """Crash recovery: step is 'running' with idem key and result already exists.
        execute_step should reuse the idem key, find the result, and skip execution."""
        wf = CreateWorkflowRequest(
            name="idem-test",
            steps=[
                WorkflowStep(
                    id="s1",
                    type="task",
                    config=WorkflowStepConfig(
                        action="x", duration_seconds=5.0, fail_probability=1.0,
                    ),
                )
            ],
        )
        _, run_id, step_rows = _create_full_run(conn, wf)
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        # Simulate crash: step has idem key + result committed, but status still "running"
        idem_key = str(uuid.uuid4())
        update_step_status(conn, step_rows[0]["id"], "running", idempotency_key=idem_key)
        insert_step_result(conn, idem_key, step_rows[0]["id"], {"status": "success"})
        conn.commit()

        # Re-fetch the step (now has idem_key set)
        step = get_steps_for_run(conn, run_id)[0]
        assert step["idempotency_key"] == idem_key

        # execute_step should reuse the existing idem key, find the result, and skip.
        # If it re-executed, it would take 5s and always fail — so this proves it skipped.
        outcome = execute_step(conn, step, wf.steps[0].config)
        assert outcome == "completed"

        final = get_steps_for_run(conn, run_id)[0]
        assert final["status"] == "completed"
        assert final["idempotency_key"] == idem_key  # key was reused, not replaced

    def test_running_step_no_result_reexecutes(self, conn):
        """Crash recovery: step is 'running' with idem key but NO result (crash before commit).
        Should re-execute the step."""
        wf = CreateWorkflowRequest(
            name="reexec-test",
            steps=[
                WorkflowStep(
                    id="s1",
                    type="task",
                    config=WorkflowStepConfig(action="x", duration_seconds=0.01),
                )
            ],
        )
        _, run_id, step_rows = _create_full_run(conn, wf)
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        # Simulate crash: step has idem key but no result (work was rolled back)
        idem_key = str(uuid.uuid4())
        update_step_status(
            conn, step_rows[0]["id"], "running",
            idempotency_key=idem_key, started_at=_now(),
        )
        conn.commit()
        assert check_step_result(conn, idem_key) is None

        step = get_steps_for_run(conn, run_id)[0]
        outcome = execute_step(conn, step, wf.steps[0].config)

        assert outcome == "completed"
        final = get_steps_for_run(conn, run_id)[0]
        assert final["status"] == "completed"
        # Result now exists for the reused idem key
        assert check_step_result(conn, idem_key) is not None

    def test_retry_on_failure(self, conn):
        wf = CreateWorkflowRequest(
            name="retry-step-test",
            steps=[
                WorkflowStep(
                    id="flaky",
                    type="task",
                    config=WorkflowStepConfig(
                        action="flaky", duration_seconds=0.01,
                        fail_probability=1.0, max_retries=2,
                    ),
                )
            ],
        )
        _, run_id, step_rows = _create_full_run(conn, wf)
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        outcome = execute_step(conn, step_rows[0], wf.steps[0].config)
        assert outcome == "retry"

        step = get_steps_for_run(conn, run_id)[0]
        assert step["status"] == "pending"
        assert step["retry_count"] == 1
        # New idem key was generated for next attempt
        assert step["idempotency_key"] is not None

    def test_exhausted_retries(self, conn):
        wf = CreateWorkflowRequest(
            name="exhaust-test",
            steps=[
                WorkflowStep(
                    id="doomed",
                    type="task",
                    config=WorkflowStepConfig(
                        action="doomed", duration_seconds=0.01,
                        fail_probability=1.0, max_retries=0,
                    ),
                )
            ],
        )
        _, run_id, step_rows = _create_full_run(conn, wf)
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        outcome = execute_step(conn, step_rows[0], wf.steps[0].config)
        assert outcome == "failed"

        step = get_steps_for_run(conn, run_id)[0]
        assert step["status"] == "failed"
        assert step["error_message"] is not None


# ==================================================================
# Test 10: execute_run — end-to-end execution loop
# ==================================================================
class TestExecuteRun:
    def test_three_step_happy_path(self, conn, sample_workflow):
        """3-step workflow completes in order — Phase 5 verification checkpoint."""
        wf_id, run_id, _ = _create_full_run(conn, sample_workflow)

        execute_run(run_id)

        run = get_run_detail(conn, run_id)
        assert run["status"] == "completed"
        assert run["started_at"] is not None
        assert run["completed_at"] is not None

        steps = get_steps_for_run(conn, run_id)
        assert len(steps) == 3
        assert all(s["status"] == "completed" for s in steps)
        assert [s["step_id"] for s in steps] == ["validate", "charge", "ship"]
        # Steps completed in order (each started_at >= previous completed_at)
        for i in range(1, len(steps)):
            assert steps[i]["started_at"] >= steps[i - 1]["completed_at"]

    def test_middle_step_permanent_failure(self, conn):
        """Middle step fails permanently → run "failed", later steps still "pending"."""
        wf = CreateWorkflowRequest(
            name="fail-middle",
            steps=[
                WorkflowStep(
                    id="s1", type="task",
                    config=WorkflowStepConfig(action="pass", duration_seconds=0.01),
                ),
                WorkflowStep(
                    id="s2", type="task",
                    config=WorkflowStepConfig(
                        action="fail", duration_seconds=0.01,
                        fail_probability=1.0, max_retries=0,
                    ),
                    depends_on=["s1"],
                ),
                WorkflowStep(
                    id="s3", type="task",
                    config=WorkflowStepConfig(action="never", duration_seconds=0.01),
                    depends_on=["s2"],
                ),
            ],
        )
        _, run_id, _ = _create_full_run(conn, wf)

        execute_run(run_id)

        run = get_run_detail(conn, run_id)
        assert run["status"] == "failed"

        steps = get_steps_for_run(conn, run_id)
        assert steps[0]["status"] == "completed"
        assert steps[1]["status"] == "failed"
        assert steps[2]["status"] == "pending"

    def test_retry_then_succeed_via_mock(self, conn):
        """Step fails twice then succeeds on third attempt. Verifies retry loop."""
        wf = CreateWorkflowRequest(
            name="retry-succeed",
            steps=[
                WorkflowStep(
                    id="flaky", type="task",
                    config=WorkflowStepConfig(
                        action="flaky", duration_seconds=0.01,
                        fail_probability=0.0, max_retries=3,
                    ),
                ),
            ],
        )
        _, run_id, _ = _create_full_run(conn, wf)

        call_count = 0

        def mock_execute_task(config):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise TaskExecutionError("simulated failure")
            return {"status": "success", "action": config.action}

        with patch("executor.execute_task", side_effect=mock_execute_task):
            execute_run(run_id)

        run = get_run_detail(conn, run_id)
        assert run["status"] == "completed"

        step = get_steps_for_run(conn, run_id)[0]
        assert step["status"] == "completed"
        assert step["retry_count"] == 2  # failed twice before succeeding
        assert call_count == 3

    def test_retry_exhaustion_via_execute_run(self, conn):
        """Step with max_retries=2 and fail_probability=1.0: 3 total attempts, then run fails."""
        wf = CreateWorkflowRequest(
            name="exhaust-run",
            steps=[
                WorkflowStep(
                    id="doomed", type="task",
                    config=WorkflowStepConfig(
                        action="doomed", duration_seconds=0.01,
                        fail_probability=1.0, max_retries=2,
                    ),
                ),
            ],
        )
        _, run_id, _ = _create_full_run(conn, wf)

        execute_run(run_id)

        run = get_run_detail(conn, run_id)
        assert run["status"] == "failed"

        step = get_steps_for_run(conn, run_id)[0]
        assert step["status"] == "failed"
        assert step["retry_count"] == 2
        assert step["error_message"] is not None

    def test_crash_recovery_skips_completed_steps(self, conn, sample_workflow):
        """Pre-complete first step, then run. First step should be skipped."""
        _, run_id, step_rows = _create_full_run(conn, sample_workflow)

        # Manually complete the first step (simulating partial execution before crash)
        idem_key = str(uuid.uuid4())
        update_step_status(
            conn, step_rows[0]["id"], "completed",
            idempotency_key=idem_key, started_at=_now(), completed_at=_now(),
        )
        insert_step_result(conn, idem_key, step_rows[0]["id"], {"status": "success"})
        conn.commit()

        execute_run(run_id)

        run = get_run_detail(conn, run_id)
        assert run["status"] == "completed"

        steps = get_steps_for_run(conn, run_id)
        assert all(s["status"] == "completed" for s in steps)
        # First step retry_count should still be 0 (wasn't re-executed)
        assert steps[0]["retry_count"] == 0

    def test_crash_recovery_idempotency_running_step_with_result(self, conn):
        """Crash scenario: step is 'running', has idem key, result exists.
        execute_run should detect result via idempotency and skip re-execution."""
        wf = CreateWorkflowRequest(
            name="idem-run-test",
            steps=[
                WorkflowStep(
                    id="s1", type="task",
                    config=WorkflowStepConfig(
                        action="x", duration_seconds=5.0, fail_probability=1.0,
                    ),
                ),
            ],
        )
        _, run_id, step_rows = _create_full_run(conn, wf)

        # Simulate crash: step "running" with idem key + result committed
        idem_key = str(uuid.uuid4())
        update_step_status(
            conn, step_rows[0]["id"], "running",
            idempotency_key=idem_key, started_at=_now(),
        )
        insert_step_result(conn, idem_key, step_rows[0]["id"], {"status": "success"})
        conn.commit()

        # If idempotency fails, this would take 5s and always fail.
        execute_run(run_id)

        run = get_run_detail(conn, run_id)
        assert run["status"] == "completed"

        step = get_steps_for_run(conn, run_id)[0]
        assert step["status"] == "completed"
        assert step["idempotency_key"] == idem_key  # key reused

    def test_execute_run_nonexistent_run(self, conn):
        """execute_run with bad run_id should return gracefully (no crash)."""
        execute_run("nonexistent-id")
        # No exception raised — just logged and returned

    def test_start_run_thread(self, conn, sample_workflow):
        """start_run_thread spawns a daemon thread that completes the run."""
        _, run_id, _ = _create_full_run(conn, sample_workflow)

        thread = start_run_thread(run_id)
        thread.join(timeout=10)

        assert not thread.is_alive()

        run = get_run_detail(conn, run_id)
        assert run["status"] == "completed"


# ==================================================================
# Test 11: recover_interrupted_runs — crash recovery (Phase 6)
# ==================================================================
class TestCrashRecovery:
    def test_recover_no_interrupted_runs(self, conn):
        """No running runs → returns empty list, no threads spawned."""
        threads = recover_interrupted_runs()
        assert threads == []

    def test_recover_single_interrupted_run(self, conn, sample_workflow):
        """One 'running' run is found and resumed to completion."""
        _, run_id, _ = _create_full_run(conn, sample_workflow)

        # Simulate crash: run is "running", all steps still "pending"
        update_run_status(conn, run_id, "running", started_at=_now())
        conn.commit()

        threads = recover_interrupted_runs()
        assert len(threads) == 1
        threads[0].join(timeout=10)

        run = get_run_detail(conn, run_id)
        assert run["status"] == "completed"
        assert all(s["status"] == "completed" for s in get_steps_for_run(conn, run_id))

    def test_recover_multiple_interrupted_runs(self, conn, sample_workflow):
        """Two 'running' runs are both resumed."""
        wf_id = create_workflow(conn, sample_workflow.name, json.dumps(sample_workflow.model_dump()))

        run_a_id = create_run(conn, wf_id)
        create_steps(conn, run_a_id, sample_workflow.steps)
        update_run_status(conn, run_a_id, "running", started_at=_now())

        run_b_id = create_run(conn, wf_id)
        create_steps(conn, run_b_id, sample_workflow.steps)
        update_run_status(conn, run_b_id, "running", started_at=_now())
        conn.commit()

        threads = recover_interrupted_runs()
        assert len(threads) == 2
        for t in threads:
            t.join(timeout=10)

        assert get_run_detail(conn, run_a_id)["status"] == "completed"
        assert get_run_detail(conn, run_b_id)["status"] == "completed"

    def test_recover_ignores_completed_and_pending(self, conn, sample_workflow):
        """Only 'running' runs are recovered — completed and pending are ignored."""
        wf_id = create_workflow(conn, sample_workflow.name, json.dumps(sample_workflow.model_dump()))

        # Completed run
        run_done_id = create_run(conn, wf_id)
        update_run_status(conn, run_done_id, "completed", completed_at=_now())
        conn.commit()

        # Pending run
        run_pending_id = create_run(conn, wf_id)

        # Running run (the only one that should be recovered)
        run_active_id = create_run(conn, wf_id)
        create_steps(conn, run_active_id, sample_workflow.steps)
        update_run_status(conn, run_active_id, "running", started_at=_now())
        conn.commit()

        threads = recover_interrupted_runs()
        assert len(threads) == 1
        threads[0].join(timeout=10)

        assert get_run_detail(conn, run_active_id)["status"] == "completed"
        assert get_run_detail(conn, run_done_id)["status"] == "completed"
        assert get_run_detail(conn, run_pending_id)["status"] == "pending"

    def test_started_at_preserved_on_recovery(self, conn, sample_workflow):
        """Recovery should NOT overwrite the original started_at timestamp."""
        _, run_id, _ = _create_full_run(conn, sample_workflow)

        original_started_at = "2025-01-01T00:00:00+00:00"
        update_run_status(conn, run_id, "running", started_at=original_started_at)
        conn.commit()

        threads = recover_interrupted_runs()
        assert len(threads) == 1
        threads[0].join(timeout=10)

        run = get_run_detail(conn, run_id)
        assert run["status"] == "completed"
        assert run["started_at"] == original_started_at
