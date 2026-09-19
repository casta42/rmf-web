"""F-345 (G ruling 2026-09-19): the F-338 hold is a SEQUENCING guarantee.

A robot whose charger is behind an operator's cordon runs a `gf_hold`
task so that it is never idle while the cordon stands — the fleet core's
own automatic retreat returns on a non-empty queue, and an idle robot
there would be sent into the closed lanes and abort the whole adapter.
Two operator paths would break that guarantee if the api-server let them
through as they are:

  * cancelling the hold outright leaves the robot idle below the line
    with the cordon still standing — the crash window — so it is refused
    with the way out named;
  * sending the robot a trip it can reach is the way out, and it must be
    SEQUENCED: the trip is queued first (the queue is non-empty), the
    hold is cancelled second, and the trip activates at once. Never the
    other order.

Pure rules here, proven both ways in test_hold_guard.py; the routes do
the I/O.
"""

from typing import Any, Dict, List, Optional

HOLD_TASK_PREFIX = "f338-hold-"   # the adapter's HOLD_TAG plus its separator
RELEASED_FOR_LABEL = "gf:hold-released-for"


def is_hold_task(task_id: Optional[str]) -> bool:
    return bool(task_id) and str(task_id).startswith(HOLD_TASK_PREFIX)


def cancel_refusal(task_id: Optional[str]) -> Optional[str]:
    """Why a direct cancel of `task_id` is refused, or None to allow it."""
    if not is_hold_task(task_id):
        return None
    return (
        f"[{task_id}] is the hold that keeps this robot out of the closed "
        "lanes around its charger; cancelling it would leave the robot "
        "idle there, and an idle robot with no route to its charger is sent "
        "into the cordon by the fleet core (F-338/F-345). Reopen the lanes, "
        "or send the robot somewhere it can reach — the fleet releases the "
        "hold for that trip on its own."
    )


def current_task_of(fleet_state: Optional[Dict[str, Any]], robot: str) -> str:
    """The robot's current task id from a stored fleet state, '' if none."""
    if not isinstance(fleet_state, dict):
        return ""
    robots = fleet_state.get("robots") or {}
    entry = robots.get(robot) if isinstance(robots, dict) else None
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("task_id") or "")


def hold_release(current_task: str, queued_task: str) -> Optional[Dict[str, Any]]:
    """The cancel request that releases a held robot's hold AFTER
    `queued_task` has been accepted by the fleet, or None when there is
    no hold to release. `queued_task` must be a real id: releasing a
    hold for a trip that was not queued is the crash window."""
    if not is_hold_task(current_task) or not queued_task:
        return None
    return {
        "type": "cancel_task_request",
        "task_id": current_task,
        "labels": [f"{RELEASED_FOR_LABEL}={queued_task}",
                   "released so an operator's trip can run; the hold is "
                   "re-issued when the trip ends if the charger is still "
                   "unreachable (F-345)"],
    }


def wait_for_confirmation_plan(intended: List[int], confirmed: List[int]) -> List[int]:
    """The lanes the fleet has not yet confirmed closed — empty means the
    closure is in force and the cut sweep may run."""
    return sorted(set(intended) - set(confirmed))
