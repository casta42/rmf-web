from typing import List, Tuple

from fastapi import Depends, HTTPException
from reactivex import operators as rxops

from api_server.dependencies import between_query, sio_user
from api_server.fast_io import FastIORouter, SubscriptionRequest
from api_server.models import FleetLog, FleetState
from api_server.repositories import FleetRepository, fleet_repo_dep
from api_server.rmf_io import fleet_events

router = FastIORouter(tags=["Fleets"])


@router.get("", response_model=List[FleetState])
async def get_fleets(
    repo: FleetRepository = Depends(fleet_repo_dep),
):
    return await repo.get_all_fleets()


@router.get("/{name}/state", response_model=FleetState)
async def get_fleet_state(name: str, repo: FleetRepository = Depends(fleet_repo_dep)):
    """
    Available in socket.io
    """
    fleet_state = await repo.get_fleet_state(name)
    if fleet_state is None:
        raise HTTPException(status_code=404)
    return fleet_state


@router.sub("/{name}/state", response_model=FleetState)
async def sub_fleet_state(req: SubscriptionRequest, name: str):
    user = sio_user(req)
    repo = FleetRepository(user)
    obs = fleet_events.fleet_states.pipe(rxops.filter(lambda x: x.name == name))
    fleet_state = await repo.get_fleet_state(name)
    if fleet_state:
        return obs.pipe(rxops.start_with(fleet_state))
    return obs


@router.get("/{name}/log", response_model=FleetLog)
async def get_fleet_log(
    name: str,
    repo: FleetRepository = Depends(fleet_repo_dep),
    between: Tuple[int, int] = Depends(between_query),
):
    """
    Available in socket.io
    """
    fleet_log = await repo.get_fleet_log(name, between)
    if fleet_log is None:
        raise HTTPException(status_code=404)
    return fleet_log


@router.sub("/{name}/log", response_model=FleetLog)
async def sub_fleet_log(_req: SubscriptionRequest, name: str):
    return fleet_events.fleet_logs.pipe(rxops.filter(lambda x: x.name == name))


# ----------------------------------------------------------------------
# F-268 — IS THIS POSE CURRENT?
#
# `/fleet_states` republishes a robot's LAST ACCEPTED pose forever. When
# RMF refuses an update (an off-graph robot: "has diverged from its
# navigation graph") the position field keeps arriving and stops being
# true, and the staleness is stated in the same message — `location.t`,
# per robot. The dashboard drew those poses as current for as long as
# this existed, so an operator watching the map saw a robot standing
# somewhere it was not.
#
# The RMF API fleet state cannot answer this: its `unix_millis_time` is
# the FLEET's publish time and is identical for every robot in the
# message. Only the ROS `location.t` is per-robot, so this reads it at
# the source and serves the verdict beside it.
#
# The rule itself lives in `api_server.position_freshness`, which is the
# same file the sentinel referee runs (`ops/sentinel/`), byte for byte
# and asserted so by `test_position_freshness.py`. The product and the
# referee must not drift into two answers about the same pose.
# ----------------------------------------------------------------------
from typing import Any, Dict, Optional  # noqa: E402

from api_server.position_freshness import FreshnessWatch  # noqa: E402

_watch = FreshnessWatch()
_verdict: Optional[Any] = None
_poses: Dict[str, Dict[str, Any]] = {}
_at: Optional[float] = None


def on_fleet_positions(msg, now: Optional[float] = None) -> None:
    """Called from the ros gateway subscription (rmf_fleet_msgs/FleetState).

    Deliberately cheap and total: this runs in the ROS callback and must
    never raise into it. `now` (monotonic seconds) is a seam for tests
    that need to walk the clock; the callback leaves it None.
    """
    global _verdict, _at  # pylint: disable=global-statement
    import time as _t  # pylint: disable=import-outside-toplevel

    if now is None:
        now = _t.monotonic()
    stamps = {}
    for robot in msg.robots:
        key = f"{msg.name}/{robot.name}"
        stamps[key] = robot.location.t.sec + robot.location.t.nanosec * 1e-9
        _poses[key] = {
            "fleet": msg.name,
            "robot": robot.name,
            "x": round(float(robot.location.x), 3),
            "y": round(float(robot.location.y), 3),
            "yaw": round(float(robot.location.yaw), 4),
            "map": robot.location.level_name,
        }
    _verdict = _watch.sample(now, stamps)
    _at = now


def _reset_freshness_for_test() -> None:
    """Test seam: forget every verdict, as a fresh process would."""
    global _watch, _verdict, _at  # pylint: disable=global-statement
    _watch = FreshnessWatch()
    _verdict = None
    _at = None
    _poses.clear()


def position_is_stale(fleet: str, robot: str) -> bool:
    """F-268: may a product decision rest on where this robot is reported?

    True when the pose the API serves for `fleet/robot` is NOT current —
    confirmed stale, or the whole feed frozen. False when it is current,
    and ALSO false when freshness is simply unknown (no fleet state seen
    yet): a consumer with no evidence either way falls back to what it
    always did rather than inventing a fault (F-191). Callers that must
    fail closed should check `get_position_freshness()['available']`.

    This is the ONE predicate for every consumer in this process — the
    stuck detector, the dispatch guard, anything added later. Each of
    them read `robot.location` and trusted it, which on a frozen feed
    produced a phantom stuck robot and a phantom obstruction in turn.
    """
    if _verdict is None:
        return False
    if _verdict.feed_frozen:
        return True
    return f"{fleet}/{robot}" in _verdict.stale_keys


@router.get("/position_freshness")
async def get_position_freshness() -> Dict[str, Any]:
    """Per-robot position freshness (F-268).

    `stale: true` means the pose this API is serving for that robot is
    NOT current — it is where the robot was when RMF last accepted an
    update. The map must draw it as unlocated rather than as a robot
    standing there, and nothing may be judged from it.

    `feed_frozen: true` means the whole fleet state has stopped advancing
    its clock while still arriving: every pose is frozen together, so
    none of them is current and none of them looks wrong.

    Never guesses. Before the publish rate has been observed, or with no
    fleet state seen at all, it reports what it does not know instead of
    reporting everything fresh (F-191).
    """
    import time as _t  # pylint: disable=import-outside-toplevel

    if _verdict is None or _at is None:
        return {
            "available": False,
            "reason": "no fleet state has been received yet — position "
            "freshness is unknown, which is not the same as fresh",
            "robots": [],
        }
    age = _t.monotonic() - _at
    if age > 10.0:
        return {
            "available": False,
            "reason": f"the fleet state stopped arriving {age:.0f}s ago — "
            "freshness is unknown for every robot",
            "age_s": round(age, 1),
            "robots": [],
        }
    rows = []
    for entry in _verdict.robots:
        pose = _poses.get(entry.key, {})
        rows.append(
            {
                "fleet": pose.get("fleet"),
                "robot": pose.get("robot"),
                "key": entry.key,
                "x": pose.get("x"),
                "y": pose.get("y"),
                "map": pose.get("map"),
                "lag_s": round(entry.lag_s, 2),
                # `stale` is the OPERATOR-facing fault: confirmed, slow to
                # trip, and what the map draws as unlocated. `judgeable`
                # is the much tighter physics bar the referee abstains on
                # — reported for diagnosis, never for display, because a
                # marker that flickered every time a pose was 0.3 s late
                # would teach an operator to ignore it.
                "stale": bool(entry.stale) or bool(_verdict.feed_frozen),
                "judgeable": bool(entry.judgeable) and not _verdict.feed_frozen,
                "threshold_s": (
                    None if entry.threshold_s is None else round(entry.threshold_s, 2)
                ),
                "reason": entry.reason,
            }
        )
    return {
        "available": True,
        "age_s": round(age, 2),
        "feed_frozen": bool(_verdict.feed_frozen),
        "feed_frozen_s": (
            None
            if _verdict.feed_frozen_s is None
            else round(_verdict.feed_frozen_s, 1)
        ),
        "publish_period_s": _verdict.period_s,
        "threshold_s": _verdict.threshold_s,
        "judge_bar_s": _verdict.judge_bar_s,
        "rule": _watch.config(),
        "robots": rows,
    }
