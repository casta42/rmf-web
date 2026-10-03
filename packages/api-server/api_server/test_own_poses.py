"""F-469 (G ruling 2026-10-03) — the pose a surface draws for a robot RMF
will not place, decided in one place and proven both ways.

The defect (f1-n70, 2026-10-02): gentle_bot_5 rested off its lane, its
pose withheld from RMF (D-84), and for 46 s the map showed it STALE at
the pose RMF last accepted — 0.9 m from where it stood, with its
odometry alive.

  FIRES  — withheld AND the robot's own pose fresh: served at the own
           pose, not stale, marked, with the reason;
  PASSES — every other case is exactly what it was (the fleet state's
           pose with the F-268 verdict it already had): a robot RMF IS
           being told about, no feed, an old feed, a robot the feed does
           not list or never heard, odometry the robot interface calls
           stale, a frozen fleet state, a malformed message.

Pure: no app, no ROS — runs on the host as well as in the image.
"""

import json

import pytest

from api_server import own_poses
from api_server.position_freshness import OWN_DISPLAY_MAX_AGE_S, STALE_FLOOR_S

WALL = 1791003213.0  # 22:53:33 on 2026-10-02, the recorded fault
MONO = 5000.0
FLEET = "gentle_fleet"
BOT = "gentle_bot_5"
# what the fleet state still showed, and where the robot stood
GHOST = {"fleet": FLEET, "robot": BOT, "x": 37.806, "y": 10.3, "yaw": 3.1, "map": "L1"}
BODY = {"x": 36.86, "y": 10.32, "yaw": 3.1, "map": "L1"}
GHOST_REASON = (
    "position stamp is 13.1 s behind the newest in the same message "
    "(threshold 10.0 s, held 3.0 s)"
)


def entry(placement="withheld", stale=False, age_s=0.05, **over):
    row = {
        **BODY,
        "pose_unix": None if age_s is None else WALL - age_s,
        "age_s": age_s,
        "stale": stale,
        "placement": placement,
        "report": "lost" if placement == "withheld" else "lane",
    }
    row.update(over)
    return row


def feed(robots, wall=WALL, mono=MONO, fleet=FLEET):
    own_poses.on_own_poses(
        json.dumps(
            {
                "fleet": fleet,
                "unix_millis_time": round(wall * 1000),
                "period_s": 0.5,
                "robots": robots,
            }
        ),
        now=mono,
    )


def served(after=0.3, fleet_stale=True, feed_frozen=False, robot=BOT):
    return own_poses.served_position(
        FLEET,
        robot,
        GHOST,
        fleet_stale,
        feed_frozen,
        GHOST_REASON,
        wall_now=WALL + after,
        mono_now=MONO + after,
    )


def as_today(position, stale=True):
    """The fleet state's pose, with the verdict it already had."""
    return position == {
        "x": 37.806,
        "y": 10.3,
        "yaw": 3.1,
        "map": "L1",
        "source": "fleet_state",
        "stale": stale,
        "placement_withheld": False,
        "reason": GHOST_REASON,
    }


@pytest.fixture(autouse=True)
def _fresh():
    own_poses._reset_for_test()
    yield
    own_poses._reset_for_test()


# ---- FIRES ---------------------------------------------------------------


def test_a_withheld_robot_with_a_fresh_own_pose_is_served_where_it_stands():
    feed({BOT: entry()})
    assert served() == {
        "x": 36.86,
        "y": 10.32,
        "yaw": 3.1,
        "map": "L1",
        "source": "robot",
        "stale": False,
        "placement_withheld": True,
        "age_s": 0.35,
        "reason": "RMF cannot place this pose on the navigation graph; "
        "position reported by the robot",
    }
    # ...from the first second of the episode, before the fleet state's
    # own pose has even been confirmed stale (13 s): the map does not
    # show the wrong place meanwhile
    assert served(fleet_stale=False)["source"] == "robot"


def test_the_display_bar_is_the_operator_facing_floor():
    """A feed that stalls for a few seconds does not flip the marker to
    STALE (the same 10 s patience a fleet-state pose gets); one that has
    stopped does."""
    assert OWN_DISPLAY_MAX_AGE_S == STALE_FLOOR_S == 10.0
    feed({BOT: entry(age_s=0.5)})
    assert served(after=9.0)["source"] == "robot"
    assert as_today(served(after=9.6))


# ---- PASSES: everything else is what it was --------------------------------


@pytest.mark.parametrize("stale", [True, False])
def test_a_robot_rmf_is_being_told_about_keeps_the_fleet_states_pose(stale):
    """`placed` and a lagging stamp is the fleet state failing to carry a
    robot — a fault the STALE marker must go on showing."""
    feed({BOT: entry(placement="placed")})
    assert as_today(served(fleet_stale=stale), stale=stale)


@pytest.mark.parametrize(
    "case, robots",
    [
        ("the feed does not list the robot", {"gentle_bot_1": entry()}),
        (
            "the robot interface never heard the robot",
            {BOT: entry(x=None, y=None, yaw=None, age_s=None, stale=None)},
        ),
        # young enough for the bar: refused on the interface's verdict alone
        (
            "the robot interface calls its odometry stale",
            {BOT: entry(stale=True, age_s=4.0)},
        ),
        ("the entry carries no pose", {BOT: entry(x=None)}),
        ("the entry is not an object", {BOT: "withheld"}),
        ("the placement is missing", {BOT: entry(placement=None)}),
    ],
)
def test_without_a_usable_own_pose_the_stale_marker_stays(case, robots):
    feed(robots)
    assert as_today(served()), case


def test_no_feed_at_all_is_todays_behaviour():
    assert as_today(served())
    assert as_today(served(fleet_stale=False), stale=False)


def test_a_feed_from_another_fleet_says_nothing_about_this_one():
    feed({BOT: entry()}, fleet="other_fleet")
    assert as_today(served())


def test_an_old_feed_is_not_fresh_by_either_clock():
    feed({BOT: entry()})
    # a latched message from an adapter that stopped publishing: old by
    # the wall clock, however recently this process received it
    assert as_today(
        own_poses.served_position(
            FLEET,
            BOT,
            GHOST,
            True,
            False,
            GHOST_REASON,
            wall_now=WALL + 60.0,
            mono_now=MONO + 0.1,
        )
    )
    # a wall clock that stepped back: old by the monotonic clock
    assert as_today(
        own_poses.served_position(
            FLEET,
            BOT,
            GHOST,
            True,
            False,
            GHOST_REASON,
            wall_now=WALL - 3600.0,
            mono_now=MONO + 60.0,
        )
    )


def test_a_frozen_fleet_state_is_not_covered():
    """Every pose frozen together is a fault about the FEED; the sentinel
    judges nobody then, and the map shows the same thing."""
    feed({BOT: entry()})
    assert as_today(served(feed_frozen=True))


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "{}",
        json.dumps({"fleet": FLEET}),
        json.dumps({"fleet": FLEET, "unix_millis_time": 1, "robots": []}),
        json.dumps({"fleet": FLEET, "robots": {BOT: {}}}),
        json.dumps({"fleet": "", "unix_millis_time": 1, "robots": {}}),
        None,
        42,
    ],
)
def test_a_malformed_message_is_ignored_and_never_raises(raw):
    feed({BOT: entry()})
    own_poses.on_own_poses(raw, now=MONO + 0.1)  # must not raise...
    assert served()["source"] == "robot"  # ...and must not erase the last good one
    own_poses._reset_for_test()
    own_poses.on_own_poses(raw, now=MONO)
    assert as_today(served())
