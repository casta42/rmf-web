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


# G ruling 2026-10-01 item 6 (F-410/F-412 class): "A timed-out auction never
# becomes a failed mission: re-auction with backoff, and fail only after N
# attempts with the reason named." api_server/redispatch.py does the
# re-auctioning; this is the reason it names when the LAST auction also
# closes with no bid.
#
# Two different failures end up here and the operator must be told which.
# The dispatcher's own code 10 alone means NOBODY ANSWERED: with the auction
# closing early once every fleet has answered, a healthy fleet adapter
# always answers, even when it has no robot to offer — so repeated silence is
# not "the robots are busy", it is fleet coordination not running, and the
# wording sends the operator there. Any other error next to code 10 is the
# fleet adapter's own refusal: it DID answer, every time, and what it said
# (battery, an unreachable stop) is the reason — not the silence.
_NO_BID_CODE = 10


def no_bid_failure_reason(
    errors: Optional[List[Error]], auctions: int, over_s: Optional[float] = None
) -> str:
    """Operator-language reason for a mission whose last auction also got
    no bid. `auctions` is how many it had in a row, `over_s` how long that
    took from the first one opening (left out when it is not known)."""
    count = "1 auction" if auctions == 1 else f"{auctions} auctions in a row"
    if over_s is not None and over_s >= 0:
        count = f"{count} over {over_s:.0f} s"
    said = [e for e in errors or [] if e.code != _NO_BID_CODE]
    if not said:
        return (
            f"no robot answered {count} — the fleet is not answering "
            "dispatches; check that the fleet coordination service is running"
        )
    why = dispatch_failure_reason(said) or (
        "the fleet answered every time without saying why"
    )
    return f"no robot offered to take this mission at {count} — {why}"


# G ruling 2026-10-01, ruling 2 (F-435): "a mission never fails because a
# robot is busy or charging. It waits, shown to the operator as 'waiting for
# a robot' with its age". This is the reason that row (and its one alert)
# names while a mission whose last auction got no bid waits for the next.
# Unlike the failure wording above it never tells the operator to dispatch
# again: the fleet is already doing that.
def no_bid_waiting_reason(errors: Optional[List[Error]]) -> str:
    """Why a mission whose last auction got no bid is waiting, in an
    operator's words, from what the fleet answered (code 10 alone: it did
    not answer)."""
    said = [e for e in errors or [] if e.code != _NO_BID_CODE]
    if not said:
        return "no robot answered its last auction"
    details = [(e.code, e.detail or "") for e in said]
    if any(code == 9 and _LIMITED_CAPACITY in d for code, d in details):
        return "no robot can finish it on one battery charge, even starting full"
    if any(code == 9 and _LOW_BATTERY in d for code, d in details):
        return "every robot is too low on battery for it until charging finishes"
    if any(code == 9 for code, _ in details):
        return "no robot's schedule can fit it at the moment"
    if any(code == 13 for code, _ in details):
        return "fleet coordination hit an internal error on its last auction"
    return "no robot offered to take it at its last auction"
