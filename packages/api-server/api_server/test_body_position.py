"""F-469, widened (G's ruling on D-89, 2026-10-03, D-90 (5); built under
D-91): "a withheld robot's body counts everywhere, so the dispatch guard
(F-34), the stuck detector and the site-change snapshot use the same
freshness-checked own pose the dashboard draws."

As first built (D-89, release f1-n72) every guard of this process paired
`position_is_stale` with the fleet state's pose. For a robot RMF cannot
place that pose is the last one RMF accepted — stale, and wrong — so each
guard treated the robot as unlocated while the operator saw it drawn where
it stood. `routes.fleets.body_position` is now the one answer, and it is
`own_poses.served_position` with the fleet state's pose and verdict.

The recorded shape (f1-n70, 2026-10-02): gentle_bot_5 at rest at
(36.86, 10.32), withheld; the fleet state still at (37.806, 10.3), 0.95 m
away, stale.

Each guard both ways, on the real models:
  FIRES   on a withheld robot whose own pose is fresh — at its BODY;
  PASSES  (exactly as before) for a robot RMF is being told about, and for
          a stale pose with no fresh report of the robot's own: no
          evidence, no objection (F-191, F-268).
The known bad is asserted too: the same inputs with no own-pose feed are
what release f1-n72 saw, and the guard does not see the robot.

The dispatch guard and the stuck detector are pure and run on the host;
the site-change snapshot reads the database (AppFixture).
"""

import json
import time
import unittest

from api_server import own_poses
from api_server.app_config import app_config
from api_server.models import FleetState, RobotState
from api_server.models.rmf_api.location_2D import Location2D
from api_server.models.rmf_api.robot_state import Status as RobotStatus
from api_server.routes import fleets as fleets_route
from api_server.routes.internal import _stuck_states, check_robot_stuck
from api_server.routes.tasks.dispatch_guard import parked_robot_near
from api_server.routes.test_internal import _freeze_robot, _Msg

FLEET = "gentle_fleet"
BOT = "gentle_bot_5"
GHOST = (37.806, 10.3)  # where the fleet state still has it (stale)
BODY = (36.86, 10.32)  # where it stands, by its own report


def own_feed(placement="withheld", age_s=0.05, stale=False, x=BODY[0], y=BODY[1]):
    """One `gf_own_poses` message, as the fleet adapter publishes it NOW."""
    wall = time.time()
    own_poses.on_own_poses(
        json.dumps(
            {
                "fleet": FLEET,
                "unix_millis_time": round(wall * 1000),
                "period_s": 0.5,
                "robots": {
                    BOT: {
                        "x": x,
                        "y": y,
                        "yaw": 3.1,
                        "map": "L1",
                        "pose_unix": wall - age_s,
                        "age_s": age_s,
                        "stale": stale,
                        "placement": placement,
                        "report": "lost" if placement == "withheld" else "lane",
                    }
                },
            }
        )
    )


def withheld_robot():
    """The fleet state's pose for BOT confirmed stale (RMF is not accepting
    its reports), others current — and the robot's own report fresh."""
    _freeze_robot(FLEET, BOT)
    own_feed()


def live_fleet_state():
    """A current fleet-state feed for BOT (RMF is being told its pose)."""
    fleets_route._reset_freshness_for_test()
    for k in range(900):
        stamps = {BOT: 500.0 + k * 0.1, "other_a": 500.0 + k * 0.1}
        fleets_route.on_fleet_positions(_Msg(FLEET, stamps), now=1000.0 + k * 0.1)


def robot(x, y, status=RobotStatus.idle, task_id=""):
    return RobotState(
        name=BOT,
        status=status,
        task_id=task_id,
        location=Location2D(map="L1", x=x, y=y, yaw=0),
        battery=0.6,
    )


def fleet(x, y, **kw):
    return [FleetState(name=FLEET, robots={BOT: robot(x, y, **kw)})]


class _Clean(unittest.TestCase):
    def setUp(self):
        fleets_route._reset_freshness_for_test()
        _stuck_states.clear()

    def tearDown(self):
        fleets_route._reset_freshness_for_test()
        _stuck_states.clear()


class TestBodyPosition(_Clean):
    def test_a_withheld_robot_is_where_its_own_fresh_report_says(self):
        withheld_robot()
        body = fleets_route.body_position(FLEET, BOT, robot(*GHOST).location)
        self.assertEqual((BODY[0], BODY[1]), (body["x"], body["y"]))
        self.assertEqual("robot", body["source"])
        self.assertTrue(body["placement_withheld"])
        self.assertEqual("L1", body["map"])

    def test_it_is_the_pose_the_map_draws(self):
        """One answer: the guards' pose is the `position` served on
        /fleets/position_freshness, not a second computation."""
        withheld_robot()
        body = fleets_route.body_position(FLEET, BOT, robot(*GHOST).location)
        drawn = own_poses.served_position(
            FLEET, BOT, {"x": GHOST[0], "y": GHOST[1], "map": "L1"}, True, False, None
        )
        for key in ("x", "y", "source", "stale", "placement_withheld"):
            self.assertEqual(drawn[key], body[key], key)

    def test_a_stale_pose_with_no_report_of_the_robots_own_is_unlocated(self):
        """PASSES as before (and the known bad of f1-n72 for a withheld
        robot: this is all the guards had)."""
        _freeze_robot(FLEET, BOT)
        self.assertIsNone(
            fleets_route.body_position(FLEET, BOT, robot(*GHOST).location)
        )

    def test_an_old_or_stale_report_of_the_robots_own_does_not_stand_in(self):
        _freeze_robot(FLEET, BOT)
        own_feed(age_s=own_poses.OWN_DISPLAY_MAX_AGE_S + 5.0)
        self.assertIsNone(
            fleets_route.body_position(FLEET, BOT, robot(*GHOST).location)
        )
        own_feed(stale=True)
        self.assertIsNone(
            fleets_route.body_position(FLEET, BOT, robot(*GHOST).location)
        )

    def test_a_robot_rmf_is_told_about_is_where_the_fleet_state_says(self):
        live_fleet_state()
        own_feed(placement="placed")
        body = fleets_route.body_position(FLEET, BOT, robot(9.0, 3.5).location)
        self.assertEqual(
            (9.0, 3.5, "fleet_state"), (body["x"], body["y"], body["source"])
        )
        self.assertFalse(body["placement_withheld"])

    def test_with_no_freshness_report_at_all_it_is_the_fleet_states_pose(self):
        """A fresh process: freshness unknown is not stale (F-191)."""
        body = fleets_route.body_position(FLEET, BOT, robot(9.0, 3.5).location)
        self.assertEqual((9.0, 3.5), (body["x"], body["y"]))

    def test_a_dict_location_and_no_location(self):
        body = fleets_route.body_position(FLEET, BOT, {"x": 9.0, "y": 3.5})
        self.assertEqual((9.0, 3.5), (body["x"], body["y"]))
        self.assertIsNone(fleets_route.body_position(FLEET, BOT, None))
        self.assertIsNone(fleets_route.body_position(FLEET, BOT, {}))
        # no fleet-state pose at all, but the robot reports its own while
        # withheld: that is where it is
        withheld_robot()
        body = fleets_route.body_position(FLEET, BOT, None)
        self.assertEqual((BODY[0], BODY[1]), (body["x"], body["y"]))


class TestDispatchGuardCountsAWithheldBody(_Clean):
    """F-34: a patrol whose final destination is occupied by a parked
    robot is refused at dispatch."""

    def test_known_bad_f1_n72_the_guard_did_not_see_the_robot(self):
        """Stale fleet pose, no own report: nothing at the body, and (as
        F-268 wants) nothing at the ghost either."""
        _freeze_robot(FLEET, BOT)
        self.assertIsNone(parked_robot_near(fleet(*GHOST), *BODY))
        self.assertIsNone(parked_robot_near(fleet(*GHOST), *GHOST))

    def test_a_destination_under_a_withheld_robot_is_occupied(self):
        withheld_robot()
        self.assertEqual(f"{FLEET}/{BOT}", parked_robot_near(fleet(*GHOST), *BODY))

    def test_the_place_it_left_is_free(self):
        """The fleet state's pose is where it WAS: 0.95 m from the body,
        outside the guard's 0.6 m."""
        withheld_robot()
        self.assertIsNone(parked_robot_near(fleet(*GHOST), *GHOST))

    def test_a_withheld_robot_on_a_mission_does_not_occupy(self):
        withheld_robot()
        self.assertIsNone(
            parked_robot_near(
                fleet(*GHOST, status=RobotStatus.working, task_id="patrol.dispatch-7"),
                *BODY,
            )
        )

    def test_a_placed_robot_occupies_where_the_fleet_state_says(self):
        live_fleet_state()
        own_feed(placement="placed", x=1.0, y=1.0)  # ignored: RMF is told
        self.assertEqual(f"{FLEET}/{BOT}", parked_robot_near(fleet(9.0, 3.5), 9.0, 3.5))
        self.assertIsNone(parked_robot_near(fleet(9.0, 3.5), 1.0, 1.0))

    def test_it_is_not_in_its_own_way(self):
        withheld_robot()
        self.assertIsNone(
            parked_robot_near(fleet(*GHOST), *BODY, exclude=f"{FLEET}/{BOT}")
        )


class TestStuckDetectorJudgesAWithheldBody(_Clean):
    """FR-17: a robot executing a task that has not moved for the stuck
    timeout pages an operator."""

    ROBOT_ID = f"{FLEET}/{BOT}"

    def setUp(self):
        super().setUp()
        self.timeout = round(app_config.stuck_timeout * 1000)

    def working(self, x=GHOST[0], y=GHOST[1]):
        return robot(x, y, status=RobotStatus.working, task_id="patrol.dispatch-7")

    def test_known_bad_f1_n72_a_withheld_robot_was_never_judged(self):
        _freeze_robot(FLEET, BOT)
        self.assertEqual(
            (None, None), check_robot_stuck(self.ROBOT_ID, self.working(), 0)
        )
        self.assertEqual(
            (None, None),
            check_robot_stuck(self.ROBOT_ID, self.working(), self.timeout * 3),
        )
        self.assertNotIn(self.ROBOT_ID, _stuck_states)

    def test_a_withheld_robot_standing_through_the_timeout_pages(self):
        withheld_robot()
        self.assertEqual(
            (None, None), check_robot_stuck(self.ROBOT_ID, self.working(), 0)
        )
        state = _stuck_states[self.ROBOT_ID]
        self.assertEqual((BODY[0], BODY[1]), (state.x, state.y))  # judged at its body
        own_feed()
        new_id, resolved = check_robot_stuck(
            self.ROBOT_ID, self.working(), self.timeout
        )
        self.assertIsNotNone(new_id)
        self.assertIsNone(resolved)

    def test_a_withheld_robot_that_is_moving_does_not_page(self):
        withheld_robot()
        check_robot_stuck(self.ROBOT_ID, self.working(), 0)
        own_feed(x=BODY[0] + 0.5)  # driven back toward its lane
        self.assertEqual(
            (None, None), check_robot_stuck(self.ROBOT_ID, self.working(), self.timeout)
        )

    def test_a_placed_robot_is_judged_on_the_fleet_state_as_before(self):
        live_fleet_state()
        own_feed(placement="placed")
        check_robot_stuck(self.ROBOT_ID, self.working(9.0, 3.5), 0)
        new_id, _ = check_robot_stuck(
            self.ROBOT_ID, self.working(9.0, 3.5), self.timeout
        )
        self.assertIsNotNone(new_id)

    def test_no_pose_at_all_is_not_an_episode(self):
        none = RobotState(
            name=BOT,
            status=RobotStatus.working,
            task_id="t",
            location=None,
            battery=0.6,
        )
        self.assertEqual((None, None), check_robot_stuck(self.ROBOT_ID, none, 0))
        self.assertNotIn(self.ROBOT_ID, _stuck_states)


class TestGuardsAskTheOneAnswer(unittest.TestCase):
    """The class of defect, not the instance: no guard of this process may
    pair `position_is_stale` with the fleet state's pose again."""

    def test_no_guard_reads_position_is_stale_beside_a_pose(self):
        import inspect

        from api_server.routes import internal, site_config
        from api_server.routes.tasks import dispatch_guard

        for module, function in (
            (dispatch_guard, dispatch_guard.parked_robot_near),
            (internal, internal.check_robot_stuck),
            (site_config, site_config.robot_positions),
        ):
            source = inspect.getsource(function)
            code = "\n".join(
                line for line in source.splitlines() if not line.strip().startswith("#")
            )
            self.assertIn("body_position(", code, function.__name__)
            self.assertNotIn("position_is_stale", code, function.__name__)


if __name__ == "__main__":
    unittest.main()
