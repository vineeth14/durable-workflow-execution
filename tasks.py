import logging
import random
import time

from models import WorkflowStepConfig

logger = logging.getLogger(__name__)


class TaskExecutionError(Exception):
    """Raised when a simulated task fails."""

    pass


def execute_task(config: WorkflowStepConfig) -> dict:
    """Execute a simulated task: sleep for the configured duration, then
    randomly fail based on fail_probability.

    Returns a result dict on success, raises TaskExecutionError on failure.
    """
    logger.info("Executing task: %s (duration=%.1fs)", config.action, config.duration_seconds)

    time.sleep(config.duration_seconds)

    if random.random() < config.fail_probability:
        msg = f"Task '{config.action}' failed (fail_probability={config.fail_probability})"
        logger.warning(msg)
        raise TaskExecutionError(msg)

    logger.info("Task '%s' completed successfully", config.action)
    return {
        "action": config.action,
        "status": "success",
        "duration": config.duration_seconds,
    }
