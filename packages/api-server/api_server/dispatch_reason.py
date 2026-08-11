# F-95 (E6 run 1): a dispatch failure must reach the operator with its WHY.
# The dispatcher already tells us — DispatchState.errors carries the fleet
# adapter's structured refusal (rmf_fleet_adapter FleetUpdateHandle
# make_error_str: code 9 "Not feasible" with a TaskPlanner detail string,
# code 10 "No fleet adapters offered a bid", code 13 "Internal bug") — but
# until now the alert said only "Task X failed". This module translates the
# known error shapes into operator language. The raw detail stays available
# in the task state (`dispatch.errors`) for the History drawer.
#
# The detail-substring matching is deliberate: the upstream pin has no
# machine-readable subcode for WHICH TaskPlanner failure occurred — the
# distinction only exists in the detail text (FleetUpdateHandle.cpp
# allocate_tasks). Substrings are matched against the pinned strings; an
# unrecognized detail falls back to the generic planner wording plus the
# raw detail, so a pin bump degrades to honest-but-verbose, never silent.

from typing import List, Optional

from api_server.models.rmf_api.error import Error

# Pinned upstream detail substrings (rmf_fleet_adapter FleetUpdateHandle.cpp)
_LIMITED_CAPACITY = "insufficient battery capacity"
_LOW_BATTERY = "insufficient initial battery charge"


def _reason_for_error(err: Error) -> Optional[str]:
    detail = err.detail or ""
    if err.code == 9:
        if _LIMITED_CAPACITY in detail:
            return (
                "no robot can finish this mission on one battery charge, even "
                "starting full — shorten it (fewer rounds or stops) or split "
                "it into smaller missions"
            )
        if _LOW_BATTERY in detail:
            return (
                "every robot is currently too low on battery for this mission "
                "— let charging finish, then dispatch again"
            )
        return (
            "the fleet planner could not fit this mission into any robot's "
            "schedule — check that every stop exists and is reachable"
        )
    if err.code == 10:
        return (
            "no robot answered the dispatch in time — the fleet may be busy, "
            "offline, or restarting; dispatching again usually works"
        )
    if err.code == 13:
        return (
            "fleet coordination hit an internal error on this mission — "
            "dispatch it again, and restart fleet coordination if it repeats"
        )
    return None


def dispatch_failure_reason(errors: Optional[List[Error]]) -> Optional[str]:
    """Operator-language reason for a dispatch failure, or None when the
    task state carries no dispatch errors (e.g. a task that failed during
    execution rather than at dispatch)."""
    if not errors:
        return None
    for err in errors:
        reason = _reason_for_error(err)
        if reason is not None:
            return reason
    # Unknown shape: stay honest with the raw detail rather than silent.
    detail = next((e.detail for e in errors if e.detail), None)
    return detail
