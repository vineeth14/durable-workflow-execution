"""Action dispatch for workflow steps.

Each action function receives a DB connection and an order_id, and mutates
the order's status inside the caller's transaction (no commit here).
"""

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_order(conn: sqlite3.Connection, order_id: str) -> None:
    """Transition order: pending -> validated. Checks amount > 0."""
    row = conn.execute("SELECT status, amount FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise ValueError(f"Order {order_id} not found")
    if row["status"] != "pending":
        raise ValueError(f"Cannot validate order in '{row['status']}' status (expected 'pending')")
    if row["amount"] <= 0:
        raise ValueError(f"Order amount must be > 0, got {row['amount']}")
    conn.execute(
        "UPDATE orders SET status = 'validated', updated_at = ? WHERE id = ?",
        (_now(), order_id),
    )


def charge_payment(conn: sqlite3.Connection, order_id: str) -> None:
    """Transition order: validated -> charged."""
    row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise ValueError(f"Order {order_id} not found")
    if row["status"] != "validated":
        raise ValueError(f"Cannot charge order in '{row['status']}' status (expected 'validated')")
    conn.execute(
        "UPDATE orders SET status = 'charged', updated_at = ? WHERE id = ?",
        (_now(), order_id),
    )


def ship_order(conn: sqlite3.Connection, order_id: str) -> None:
    """Transition order: charged -> shipped."""
    row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise ValueError(f"Order {order_id} not found")
    if row["status"] != "charged":
        raise ValueError(f"Cannot ship order in '{row['status']}' status (expected 'charged')")
    conn.execute(
        "UPDATE orders SET status = 'shipped', updated_at = ? WHERE id = ?",
        (_now(), order_id),
    )


def send_notification(conn: sqlite3.Connection, order_id: str) -> None:
    """Log a notification. No status transition."""
    logger.info("Notification sent for order %s", order_id)


ACTION_REGISTRY: dict[str, callable] = {
    "validate_order": validate_order,
    "charge_payment": charge_payment,
    "ship_order": ship_order,
    "send_notification": send_notification,
}


def dispatch_action(conn: sqlite3.Connection, action: str, order_id: str | None) -> None:
    """Look up action in registry and call it if applicable.

    No-op when:
    - order_id is None (run has no associated order)
    - action is not in ACTION_REGISTRY
    """
    if order_id is None:
        return
    func = ACTION_REGISTRY.get(action)
    if func is None:
        return
    func(conn, order_id)
