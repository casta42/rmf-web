import asyncio
import json
import time
import unittest

from api_server import own_poses
from api_server.models import FleetLog, FleetLogUpdate, FleetState, FleetStateUpdate
from api_server.rmf_io.events import fleet_events
from api_server.routes import fleets as fleets_route
from api_server.test import AppFixture, make_fleet_log, make_fleet_state


class TestFleetsRoute(AppFixture):
    def test_fleet_states(self):
        fleet_state = make_fleet_state()

        with self.client.websocket_connect("/_internal") as ws, self.subscribe_sio(
            f"/fleets/{fleet_state.name}/state"
        ) as sub:
            ws.send_text(
                FleetStateUpdate(
                    type="fleet_state_update", data=fleet_state
                ).model_dump_json()
            )

            msg = FleetState(**next(sub))
            self.assertEqual(fleet_state.name, msg.name)

            # get fleet state
            resp = self.client.get(f"/fleets/{fleet_state.name}/state")
            self.assertEqual(200, resp.status_code)
            state = resp.json()
            self.assertEqual(fleet_state.name, state["name"])

            # query fleets
            resp = self.client.get(f"/fleets?fleet_name={fleet_state.name}")
            self.assertEqual(200, resp.status_code)
            resp_json = resp.json()
            self.assertEqual(1, len(resp_json))
            self.assertEqual(fleet_state.name, resp_json[0]["name"])

    def test_fleet_logs(self):
        fleet_log = make_fleet_log()

        with self.client.websocket_connect("/_internal") as ws, self.subscribe_sio(
            f"/fleets/{fleet_log.name}/log"
        ) as sub:
            fleet_events.fleet_logs.on_next(fleet_log)

            ws.send_text(
                FleetLogUpdate(
                    type="fleet_log_update", data=fleet_log
                ).model_dump_json()
            )

            msg = FleetLog(**next(sub))
            self.assertEqual(fleet_log.name, msg.name)

            # Since there are no sample fleet logs, we cannot check the log contents
            resp = self.client.get(f"/fleets/{fleet_log.name}/log")
            self.assertEqual(200, resp.status_code)
            self.assertEqual(fleet_log.name, resp.json()["name"])


# ----------------------------------------------------------------------
# F-469 (G ruling 2026-10-03): GET /fleets/position_freshness serves the
# pose a surface should DRAW. For a robot whose pose the adapter withholds
# from RMF, with a fresh own pose, that is the robot's own — not stale,
# marked, with the reason — while the row's own `stale` and
# `position_is_stale` go on answering "is the FLEET STATE's pose
# current?", which is what every guard pairs with `robot.location`.
# ----------------------------------------------------------------------
FLEET = "gentle_fleet"
GHOST = (37.806, 10.3)  # what the fleet state still showed (f1-n70)
BODY = (36.86, 10.32)  # where gentle_bot_5 stood


class _T:
    def __init__(self, t):
        self.sec = int(t)
        self.nanosec = int((t - int(t)) * 1e9)


class _Loc:
    def __init__(self, t, x, y):
        self.t = _T(t)
        self.x, self.y, self.yaw, self.level_name = x, y, 0.0, "L1"


class _Robot:
    def __init__(self, name, t, x, y):
        self.name = name
        self.location = _Loc(t, x, y)


class _Msg:
    def __init__(self, robots):
        self.name = FLEET
        self.robots = [_Robot(n, t, x, y) for n, (t, x, y) in robots.items()]


class TestPositionFreshnessServesTheRobotsOwnPose(unittest.TestCase):
    def setUp(self):
        fleets_route._reset_freshness_for_test()

    def tearDown(self):
        fleets_route._reset_freshness_for_test()

    def _fleet_state(self, frozen=("gentle_bot_5",), everyone_frozen=False):
        """60 s of healthy history, then 30 s with `frozen` robots' stamps
        stopped — the F-268 shape — ending NOW on this process's clock."""
        start = time.monotonic() - 90.0
        for k in range(900):
            stamp = 500.0 + k * 0.1
            held = 500.0 + 60.0 if k >= 600 else stamp
            robots = {
                "gentle_bot_5": (
                    held if "gentle_bot_5" in frozen or everyone_frozen else stamp,
                    GHOST[0],
                    GHOST[1],
                ),
                "gentle_bot_1": (held if everyone_frozen else stamp, 1.0, 1.0),
                "gentle_bot_2": (held if everyone_frozen else stamp, 5.0, 1.0),
            }
            fleets_route.on_fleet_positions(_Msg(robots), now=start + k * 0.1)

    def _own(self, **robots):
        entries = {}
        for name, (placement, stale, age) in robots.items():
            entries[name] = {
                "x": BODY[0],
                "y": BODY[1],
                "yaw": 3.1,
                "map": "L1",
                "pose_unix": time.time() - age,
                "age_s": age,
                "stale": stale,
                "placement": placement,
                "report": "lost",
            }
        own_poses.on_own_poses(
            json.dumps(
                {
                    "fleet": FLEET,
                    "unix_millis_time": round(time.time() * 1000),
                    "period_s": 0.5,
                    "robots": entries,
                }
            )
        )

    def _row(self, name="gentle_bot_5"):
        data = asyncio.run(fleets_route.get_position_freshness())
        self.assertTrue(data["available"], data)
        return next(r for r in data["robots"] if r["robot"] == name), data

    def test_a_withheld_robot_is_served_at_its_own_pose_not_stale_and_marked(self):
        self._fleet_state()
        self._own(gentle_bot_5=("withheld", False, 0.05))
        row, data = self._row()
        self.assertEqual(
            (row["position"]["x"], row["position"]["y"]), BODY, row["position"]
        )
        self.assertEqual(row["position"]["source"], "robot")
        self.assertIs(row["position"]["stale"], False)
        self.assertIs(row["placement_withheld"], True)
        self.assertEqual(
            row["position"]["reason"],
            "RMF cannot place this pose on the navigation graph; "
            "position reported by the robot",
        )
        self.assertEqual(data["own_pose_max_age_s"], 10.0)
        # the fleet state's own pose and verdict are UNCHANGED: every
        # guard and drill pairs these with robot.location from GET /fleets
        self.assertEqual((row["x"], row["y"]), GHOST)
        self.assertIs(row["stale"], True)
        self.assertTrue(fleets_route.position_is_stale(FLEET, "gentle_bot_5"))
        # and a robot that is simply fine is drawn where the fleet state
        # says, current, unmarked
        other, _ = self._row("gentle_bot_1")
        self.assertEqual(other["position"]["source"], "fleet_state")
        self.assertIs(other["position"]["stale"], False)
        self.assertIs(other["placement_withheld"], False)
        self.assertEqual((other["position"]["x"], other["position"]["y"]), (1.0, 1.0))

    def test_without_a_usable_own_pose_todays_stale_stays(self):
        """KNOWN GOOD the other way: each of these is the POSITION STALE of
        before, on the fleet state's last accepted pose."""
        cases = {
            "no own feed": None,
            "RMF is being told the pose (placed)": ("placed", False, 0.05),
            "the robot interface calls its odometry stale": ("withheld", True, 4.0),
            "the own pose is older than the display bar": ("withheld", False, 11.0),
        }
        for case, own in cases.items():
            fleets_route._reset_freshness_for_test()
            self._fleet_state()
            if own is not None:
                self._own(gentle_bot_5=own)
            row, _ = self._row()
            self.assertEqual(row["position"]["source"], "fleet_state", case)
            self.assertIs(row["position"]["stale"], True, case)
            self.assertIs(row["placement_withheld"], False, case)
            self.assertEqual((row["position"]["x"], row["position"]["y"]), GHOST, case)
            self.assertIn("behind the newest", row["position"]["reason"], case)
            self.assertIs(row["stale"], True, case)

    def test_a_frozen_fleet_state_is_not_covered_by_own_poses(self):
        self._fleet_state(everyone_frozen=True)
        self._own(gentle_bot_5=("withheld", False, 0.05))
        row, data = self._row()
        self.assertTrue(data["feed_frozen"])
        self.assertEqual(row["position"]["source"], "fleet_state")
        self.assertIs(row["position"]["stale"], True)
        self.assertIs(row["placement_withheld"], False)

    def test_the_ros_callback_survives_anything(self):
        for raw in ("not json", "[]", "{}", json.dumps({"fleet": FLEET})):
            own_poses.on_own_poses(raw)
        self._fleet_state()
        row, _ = self._row()
        self.assertEqual(row["position"]["source"], "fleet_state")
