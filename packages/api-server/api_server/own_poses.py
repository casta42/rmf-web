"""F-469 (G ruling 2026-10-03) — a robot's body is never invisible: the pose
a surface should draw, decided in ONE place.

    "When RMF won't place a robot, the sentinel and every surface use the
    robot's own reported, freshness-checked pose, and the robot shows as
    present ... never stale and never missing."

The defect: a fleet member resting a hand's width off its lane cannot be
placed by RMF, so the fleet adapter WITHHOLDS its pose from RMF (D-84) and
the fleet state keeps the last pose RMF accepted, with its old stamp. The
map then drew the robot STALE, 0.9 m from where it stood (f1-n70,
2026-10-02, gentle_bot_5, 46 s) — while its odometry was alive.

The adapter now publishes every member's own pose on `gf_own_poses`
(latched, ~2 Hz) with the age of the odometry sample, the robot
interface's verdict on it and whether RMF is being told it. This module
relays that feed and answers the one question a surface asks —
`served_position`: WHERE do I draw this robot, and is that current?

  * the pose is WITHHELD from RMF and the robot's own pose is fresh
    -> the robot's own pose, not stale, marked `placement_withheld` with
       the reason;
  * anything else (placed; no feed; an old feed; a robot the feed does not
    list; odometry the robot interface calls stale; the whole fleet state
    frozen) -> the fleet state's pose with the F-268 verdict it already
    had. Absence of evidence is not evidence (F-191): today's STALE stays.

What this does NOT change, on purpose: `routes.fleets.position_is_stale`
and the row's own `stale` still answer "is the FLEET STATE's pose
current?". Every guard in this process (the dispatch guard, the stuck
detector, the site-change snapshot) and every drill pairs that verdict
with `robot.location` from the fleet state — the last ACCEPTED pose,
which for a withheld robot is still wrong. Telling them "not stale" would
hand each of them a ghost.

The freshness judgment itself is `position_freshness.OwnPoseFeed`, the
file the sentinel referee runs byte for byte: the product and the referee
do not get two answers about the same pose.
"""

import time
from typing import Any, Dict, Optional

from .position_freshness import OWN_DISPLAY_MAX_AGE_S, OwnPoseFeed

WITHHELD_REASON = (
    "RMF cannot place this pose on the navigation graph; "
    "position reported by the robot"
)
SOURCE_FLEET_STATE = "fleet_state"
SOURCE_ROBOT = "robot"

_feed = OwnPoseFeed()


def on_own_poses(raw: Any, now: Optional[float] = None) -> None:
    """The adapter's `gf_own_poses` (ROS thread: store only, never raise).
    `now` (monotonic seconds) is a seam for tests that walk the clock."""
    try:
        _feed.on_message(raw, time.monotonic() if now is None else now)
    except Exception:  # noqa: BLE001  pylint: disable=broad-except
        # a malformed message must never take the ROS gateway down
        pass


def _reset_for_test() -> None:
    _feed.clear()


def served_position(
    fleet: Optional[str],
    robot: Optional[str],
    fleet_pose: Dict[str, Any],
    fleet_stale: bool,
    feed_frozen: bool,
    fleet_reason: Optional[str],
    wall_now: Optional[float] = None,
    mono_now: Optional[float] = None,
) -> Dict[str, Any]:
    """The pose to DRAW for `fleet/robot`, and whether it is current.

    `fleet_pose` / `fleet_stale` / `fleet_reason` are what the fleet state
    and the F-268 rule already say; they are returned unchanged unless the
    robot's own pose stands in (see the module docstring).
    """
    if not feed_frozen and fleet and robot:
        own, _why = _feed.pose(
            fleet,
            robot,
            time.time() if wall_now is None else wall_now,
            time.monotonic() if mono_now is None else mono_now,
            OWN_DISPLAY_MAX_AGE_S,
        )
        if own is not None and own.withheld:
            return {
                "x": round(own.x, 3),
                "y": round(own.y, 3),
                "yaw": round(own.yaw, 4),
                "map": own.map or fleet_pose.get("map"),
                "source": SOURCE_ROBOT,
                "stale": False,
                "placement_withheld": True,
                "age_s": round(own.age_s, 2),
                "reason": WITHHELD_REASON,
            }
    return {
        "x": fleet_pose.get("x"),
        "y": fleet_pose.get("y"),
        "yaw": fleet_pose.get("yaw"),
        "map": fleet_pose.get("map"),
        "source": SOURCE_FLEET_STATE,
        "stale": bool(fleet_stale),
        "placement_withheld": False,
        "reason": fleet_reason,
    }
