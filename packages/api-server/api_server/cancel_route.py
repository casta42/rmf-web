"""F-285: who can cancel this task, decided from its stored state.

On the Humble pin a `cancel_task_request` on the task API topic is
answered ONLY by the fleet adapter's TaskManager — and only for a task
it holds. A task the dispatcher still has in its bidding queue (a
mission scheduled for later, FR-4, or one nobody has bid on yet) is
answered by nobody, the api-server's pseudo-service times out, and the
operator sees HTTP 500 for a cancel that may or may not have happened.
The dispatcher exposes a ROS service (`rmf_task_msgs/srv/CancelTask`,
"cancel_task") for exactly those tasks; the gateway already holds a
client for it. This module decides which door to knock on.

Pure: state in, route out. Tested both ways in test_cancel_route.py.
"""

from typing import Optional

TERMINAL = {"completed", "failed", "skipped", "canceled", "killed"}
ALREADY_CANCELED = {"canceled", "killed"}
# Dispatch statuses under which the dispatcher, not a fleet, owns the task.
DISPATCHER_HELD = {"queued", "selected"}

ROUTE_UNKNOWN = "unknown"            # no such task -> 404
ROUTE_ALREADY_CANCELED = "already"   # idempotent -> 200
ROUTE_TERMINAL = "terminal"          # completed/failed -> 409
ROUTE_DISPATCHER = "dispatcher"      # ROS service, then fleet
ROUTE_FLEET = "fleet"                # task API topic


def status_tail(value) -> Optional[str]:
    if value is None:
        return None
    return str(value).split(".")[-1].strip().lower()


def cancel_route(status_value, dispatch_status_value, assigned_to) -> str:
    """Where a cancel for a task in this state must go."""
    status = status_tail(status_value)
    if status is None:
        return ROUTE_UNKNOWN
    if status in ALREADY_CANCELED:
        return ROUTE_ALREADY_CANCELED
    if status in TERMINAL:
        return ROUTE_TERMINAL
    dispatch = status_tail(dispatch_status_value)
    if assigned_to is None and dispatch in DISPATCHER_HELD:
        return ROUTE_DISPATCHER
    return ROUTE_FLEET
