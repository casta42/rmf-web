"""F-395 (G ruling 2026-09-29, D-84): fleet state reaches the dashboards on
change, rate-capped, plus the existing periodic push — both ways.

The message shape is upstream's own (`FleetUpdateHandle::Implementation::
update_fleet_state`, rmf_fleet_adapter on the Humble pin): name, and per
robot name, status, task_id, unix_millis_time, battery (0-1), location
{map, x, y, yaw}, issues [{category, detail}] — and one test parses it with
the REAL model, so what the cadence reads is what the live path hands it.
"""

import copy
import unittest

from api_server import models as mdl
from api_server.fleet_state_cadence import (
    PERIOD_S,
    FleetStateCadence,
    signature,
)


def fleet(**robot_changes):
    data = {
        "name": "gentle_fleet",
        "robots": {
            "gentle_bot_1": {
                "name": "gentle_bot_1",
                "status": "charging",
                "task_id": "",
                "unix_millis_time": 1790681951000,
                "battery": 0.874,
                "location": {"map": "L1", "x": 1.5, "y": 1.5, "yaw": 3.14},
                "issues": [],
            },
            "gentle_bot_2": {
                "name": "gentle_bot_2",
                "status": "working",
                "task_id": "patrol.dispatch-299260",
                "unix_millis_time": 1790681951000,
                "battery": 0.61,
                "location": {"map": "L1", "x": 7.25, "y": 3.0, "yaw": 0.0},
                "issues": [],
            },
        },
    }
    robot = data["robots"]["gentle_bot_1"]
    for key, value in robot_changes.items():
        if key in ("x", "y", "yaw"):
            robot["location"][key] = value
        else:
            robot[key] = value
    return data


class TestSignatureIsWhatAnOperatorSees(unittest.TestCase):
    def test_the_adapters_stamp_is_not_state(self):
        self.assertEqual(
            signature(fleet()), signature(fleet(unix_millis_time=1790681952200))
        )

    def test_battery_as_the_dashboards_print_it(self):
        # 0.874 and 0.8749 both print 87 %
        self.assertEqual(signature(fleet()), signature(fleet(battery=0.8749)))
        # 87 % -> 86 % is a change
        self.assertNotEqual(signature(fleet()), signature(fleet(battery=0.864)))

    def test_halves_round_up_like_javascript_math_round(self):
        # 0.125 * 100 = 12.5 exactly: Math.round -> 13, Python round -> 12
        self.assertEqual(
            signature(fleet(battery=0.125)), signature(fleet(battery=0.13))
        )
        self.assertNotEqual(
            signature(fleet(battery=0.125)), signature(fleet(battery=0.12))
        )

    def test_position_to_the_centimetre_and_heading_to_a_hundredth(self):
        self.assertEqual(signature(fleet()), signature(fleet(x=1.5004)))
        self.assertNotEqual(signature(fleet()), signature(fleet(x=1.52)))
        self.assertNotEqual(signature(fleet()), signature(fleet(yaw=3.0)))

    def test_status_task_and_issues_are_state(self):
        base = signature(fleet())
        self.assertNotEqual(base, signature(fleet(status="working")))
        self.assertNotEqual(base, signature(fleet(task_id="patrol.dispatch-1")))
        self.assertNotEqual(
            base,
            signature(
                fleet(issues=[{"category": "robot_stranded_off_graph", "detail": {}}])
            ),
        )

    def test_a_robot_arriving_or_leaving_is_state(self):
        gone = fleet()
        del gone["robots"]["gentle_bot_2"]
        self.assertNotEqual(signature(fleet()), signature(gone))

    def test_a_field_it_does_not_know_counts_conservatively(self):
        self.assertNotEqual(signature(fleet()), signature(fleet(commission={"x": 1})))


class TestDecideBothWays(unittest.TestCase):
    def test_first_message_is_a_full_cycle(self):
        self.assertEqual(FleetStateCadence().decide(fleet(), 100.0), (True, True))

    def test_nothing_visible_changed_and_the_cycle_is_not_due_drops_it(self):
        c = FleetStateCadence()
        c.decide(fleet(), 100.0)
        self.assertEqual(c.decide(fleet(unix_millis_time=2), 100.2), (False, False))
        self.assertEqual(c.decide(fleet(battery=0.8741), 100.4), (False, False))

    def test_a_visible_change_goes_out_at_once_without_a_full_cycle(self):
        c = FleetStateCadence()
        c.decide(fleet(), 100.0)
        self.assertEqual(c.decide(fleet(battery=0.864), 100.2), (True, False))
        # and is not re-sent while it stays the same
        self.assertEqual(c.decide(fleet(battery=0.864), 100.4), (False, False))

    def test_the_periodic_push_survives_an_unchanging_floor(self):
        c = FleetStateCadence()
        c.decide(fleet(), 100.0)
        for t in (100.2, 100.4, 100.6, 100.8):
            self.assertEqual(c.decide(fleet(), t), (False, False))
        self.assertEqual(c.decide(fleet(), 100.0 + PERIOD_S), (True, True))

    def test_fleets_are_independent(self):
        c = FleetStateCadence()
        c.decide(fleet(), 100.0)
        other = fleet()
        other["name"] = "other_fleet"
        self.assertEqual(c.decide(other, 100.2), (True, True))

    def test_a_message_without_a_fleet_name_is_never_dropped(self):
        nameless = fleet()
        del nameless["name"]
        c = FleetStateCadence()
        self.assertEqual(c.decide(nameless, 100.0), (True, True))
        self.assertEqual(c.decide(nameless, 100.1), (True, True))


class TestTheRealModelReadsTheSameMessage(unittest.TestCase):
    """What decide() reads is the dict the live path parses (the F-343
    lesson: a guard proven on a permissive double can skip every live
    update)."""

    def test_the_upstream_shape_parses_and_round_trips(self):
        data = fleet(battery=0.864)
        state = mdl.FleetState(**copy.deepcopy(data))
        self.assertEqual(state.name, "gentle_fleet")
        robot = state.robots["gentle_bot_1"]
        self.assertAlmostEqual(robot.battery, 0.864)
        # a message rebuilt from the model is the same operator view
        rebuilt = state.model_dump(mode="json", exclude_none=True)
        self.assertEqual(signature(rebuilt), signature(data))


if __name__ == "__main__":
    unittest.main()
