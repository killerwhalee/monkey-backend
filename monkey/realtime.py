"""Server→client WebSocket publishers (the single sync→async seam).

Called from synchronous code (Celery tasks, ``services.submit_monkey_order``,
Celery signals). Every publish is best-effort: if the channel layer is missing
or errors, we log and move on — a WebSocket hiccup must never break a trade or a
task. Group names / payload shapes are the contract the frontend consumes.
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def _publish(group, msg_type, data):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, {"type": msg_type, "data": data})
    except Exception:  # noqa: BLE001 — never let a WS publish break the caller
        logger.warning("realtime publish to %s failed", group, exc_info=True)


def publish_order(order):
    """Push a succeeded order to the dashboard order feed (skips system monkeys)."""
    from market.serializers import OrderSerializer

    if order is None or (order.monkey_id and getattr(order.monkey, "is_system", False)):
        return
    _publish(
        "dashboard.orders",
        "order_event",
        {"event": "order.succeeded", "order": OrderSerializer(order).data},
    )


def publish_index_tick(value, recorded_at):
    from monkey.services import KST_OFFSET_SECONDS

    _publish(
        "dashboard.index",
        "index_event",
        {
            "event": "index.tick",
            "value": value,
            "time": int(recorded_at.timestamp()) + KST_OFFSET_SECONDS,
        },
    )


def publish_monkey_updated(monkey):
    """Push a monkey state change (kept lean — fired on state transitions only)."""
    if monkey is None or monkey.is_system:
        return
    _publish(
        "dashboard.monkeys",
        "monkey_event",
        {
            "event": "monkey.updated",
            "monkey": {
                "id": monkey.id,
                "state": monkey.state,
                "is_active": monkey.is_active,
            },
        },
    )


def publish_task(task_name, task_id, status, ok=None, error=None):
    """Push a manual-task lifecycle event (status: 'started' or 'finished')."""
    _publish(
        "admin.tasks",
        "task_event",
        {
            "event": f"task.{status}",
            "task": task_name,
            "id": task_id,
            "ok": ok,
            "error": error,
        },
    )
