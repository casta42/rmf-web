import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

from api_server.app_config import app_config
from api_server.models import FleetState, RobotState
from api_server.models.rmf_api.location_2D import Location2D
from api_server.models.rmf_api.robot_state import Status as RobotStatus

from .internal import (
    CHARGE_GHOST_FULL_SOC,
    CURRENT_RUN_SLACK,
    LOW_BATTERY_HYSTERESIS,
    STUCK_REARM_DISTANCE,
    _low_battery_alerted,
    _stuck_states,
    charge_ghost_stale_cutoff,
    check_low_battery,
    check_robot_stuck,
    classify_charge_ghost,
)

ROBOT_ID = "test_fleet/test_robot"


def make_robot_state(
    battery: Optional[float] = None,
    x: float = 0,
    y: float = 0,
    task_id: Optional[str] = "task_1",
    status: Optional[RobotStatus] = RobotStatus.working,
) -> RobotState:
    return RobotState(
        name="test_robot",
        status=status,
        task_id=task_id,
        location=Location2D(map="L1", x=x, y=y, yaw=0),
        battery=battery,
    )


class TestCheckLowBattery(unittest.TestCase):
    def setUp(self):
        _low_battery_alerted.clear()
        self.threshold = app_config.low_battery_threshold

    def test_alerts_once_per_episode(self):
        alert_id = check_low_battery(
            ROBOT_ID, make_robot_state(battery=self.threshold - 0.01), 1000
        )
        self.assertEqual(alert_id, "low_battery__test_fleet__test_robot__1000")
        # still low, no new alert
        self.assertIsNone(
            check_low_battery(
                ROBOT_ID, make_robot_state(battery=self.threshold - 0.02), 2000
            )
        )

    def test_rearms_after_hysteresis(self):
        check_low_battery(
            ROBOT_ID, make_robot_state(battery=self.threshold - 0.01), 1000
        )
        # above the threshold but within the hysteresis margin, stays armed off
        self.assertIsNone(
            check_low_battery(
                ROBOT_ID, make_robot_state(battery=self.threshold + 0.01), 2000
            )
        )
        self.assertIsNone(
            check_low_battery(
                ROBOT_ID, make_robot_state(battery=self.threshold - 0.01), 3000
            )
        )
        # recovers past threshold + hysteresis, the next drop alerts again
        self.assertIsNone(
            check_low_battery(
                ROBOT_ID,
                make_robot_state(
                    battery=self.threshold + LOW_BATTERY_HYSTERESIS + 0.01
                ),
                4000,
            )
        )
        alert_id = check_low_battery(
            ROBOT_ID, make_robot_state(battery=self.threshold - 0.01), 5000
        )
        self.assertEqual(alert_id, "low_battery__test_fleet__test_robot__5000")

    def test_no_alert_without_battery(self):
        self.assertIsNone(check_low_battery(ROBOT_ID, make_robot_state(), 1000))


class TestCheckRobotStuck(unittest.TestCase):
    def setUp(self):
        _stuck_states.clear()
        self.timeout_millis = round(app_config.stuck_timeout * 1000)

    def test_alerts_once_per_episode(self):
        self.assertEqual(
            check_robot_stuck(ROBOT_ID, make_robot_state(), 0), (None, None)
        )
        # stationary but the timeout has not elapsed yet
        self.assertEqual(
            check_robot_stuck(ROBOT_ID, make_robot_state(), self.timeout_millis - 1),
            (None, None),
        )
        new_id, resolved_id = check_robot_stuck(
            ROBOT_ID, make_robot_state(), self.timeout_millis
        )
        self.assertEqual(
            new_id, f"robot_stuck__test_fleet__test_robot__{self.timeout_millis}"
        )
        self.assertIsNone(resolved_id)
        # still stuck, no new alert
        self.assertEqual(
            check_robot_stuck(ROBOT_ID, make_robot_state(), self.timeout_millis * 2),
            (None, None),
        )

    def test_movement_resets_timer(self):
        self.assertEqual(
            check_robot_stuck(ROBOT_ID, make_robot_state(), 0), (None, None)
        )
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(x=1.0), self.timeout_millis - 1
            ),
            (None, None),
        )
        # only half the timeout has elapsed since the robot last moved
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID,
                make_robot_state(x=1.0),
                self.timeout_millis - 1 + self.timeout_millis // 2,
            ),
            (None, None),
        )

    def test_resolves_and_rearms_after_moving_away(self):
        check_robot_stuck(ROBOT_ID, make_robot_state(), 0)
        alert_id, _ = check_robot_stuck(
            ROBOT_ID, make_robot_state(), self.timeout_millis
        )
        self.assertIsNotNone(alert_id)
        # small movement neither resolves nor re-arms the episode
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(x=0.1), self.timeout_millis * 2
            ),
            (None, None),
        )
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(x=0.1), self.timeout_millis * 4
            ),
            (None, None),
        )
        # moving away resolves the open alert (F-29) and re-arms
        rearm_x = STUCK_REARM_DISTANCE + 0.01
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(x=rearm_x), self.timeout_millis * 4 + 1
            ),
            (None, alert_id),
        )
        # a new stuck episode alerts again
        new_id, resolved_id = check_robot_stuck(
            ROBOT_ID, make_robot_state(x=rearm_x), self.timeout_millis * 6
        )
        self.assertIsNotNone(new_id)
        self.assertIsNone(resolved_id)

    def test_resolves_when_task_ends_without_motion(self):
        # F-29: the 2026-07-20 soak storm — a patrol dispatched to the
        # robot's current waypoint runs > stuck_timeout and finishes with
        # the robot never moving; the alert must not outlive the episode.
        check_robot_stuck(ROBOT_ID, make_robot_state(), 0)
        alert_id, _ = check_robot_stuck(
            ROBOT_ID, make_robot_state(), self.timeout_millis
        )
        self.assertIsNotNone(alert_id)
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID,
                make_robot_state(task_id="", status=RobotStatus.idle),
                self.timeout_millis * 2,
            ),
            (None, alert_id),
        )
        # idle in place afterwards: no new episode, nothing to resolve
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID,
                make_robot_state(task_id="", status=RobotStatus.idle),
                self.timeout_millis * 4,
            ),
            (None, None),
        )

    def test_no_alert_when_not_executing_task(self):
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(task_id="", status=RobotStatus.idle), 0
            ),
            (None, None),
        )
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID,
                make_robot_state(task_id="", status=RobotStatus.idle),
                self.timeout_millis * 2,
            ),
            (None, None),
        )
        self.assertEqual(
            check_robot_stuck(
                ROBOT_ID,
                make_robot_state(task_id="task_1", status=RobotStatus.charging),
                self.timeout_millis * 4,
            ),
            (None, None),
        )

    def test_no_alert_without_location(self):
        state = make_robot_state()
        state.location = None
        self.assertEqual(check_robot_stuck(ROBOT_ID, state, 0), (None, None))
        self.assertEqual(
            check_robot_stuck(ROBOT_ID, state, self.timeout_millis * 2),
            (None, None),
        )


class TestClassifyChargeGhost(unittest.TestCase):
    GHOST_ID = "Charge041311"

    def test_leaves_own_current_task_alone(self):
        robot = make_robot_state(task_id=self.GHOST_ID)
        self.assertIsNone(classify_charge_ghost(robot, self.GHOST_ID))

    def test_superseded_by_other_task_is_killed(self):
        robot = make_robot_state(task_id="patrol.dispatch-1")
        self.assertEqual(classify_charge_ghost(robot, self.GHOST_ID), "killed")

    def test_idle_and_full_is_completed(self):
        robot = make_robot_state(
            battery=CHARGE_GHOST_FULL_SOC + 0.05,
            task_id="",
            status=RobotStatus.idle,
        )
        self.assertEqual(classify_charge_ghost(robot, self.GHOST_ID), "completed")

    def test_idle_and_low_is_left_pending(self):
        # a queued auto charge task the robot has not started yet
        robot = make_robot_state(battery=0.15, task_id="", status=RobotStatus.idle)
        self.assertIsNone(classify_charge_ghost(robot, self.GHOST_ID))

    def test_still_charging_is_left_alone(self):
        robot = make_robot_state(
            battery=CHARGE_GHOST_FULL_SOC + 0.05,
            task_id="",
            status=RobotStatus.charging,
        )
        self.assertIsNone(classify_charge_ghost(robot, self.GHOST_ID))

    def test_no_battery_reading_is_left_alone(self):
        robot = make_robot_state(task_id="", status=RobotStatus.idle)
        self.assertIsNone(classify_charge_ghost(robot, self.GHOST_ID))

    def test_empty_task_id_never_classified(self):
        robot = make_robot_state(task_id="patrol.dispatch-1")
        self.assertIsNone(classify_charge_ghost(robot, ""))


class TestChargeGhostStaleCutoff(unittest.TestCase):
    """F-37: a reap may only touch rows written during the current RMF run."""

    NOW = datetime(2026, 7, 25, 16, 0, 0, tzinfo=timezone.utc)

    def make_fleet_state(self, unix_millis_time: Optional[int]) -> FleetState:
        robot = make_robot_state()
        robot.unix_millis_time = unix_millis_time
        return FleetState(name="test_fleet", robots={"test_robot": robot})

    def test_sim_clock_yields_bringup_cutoff(self):
        # sim clock 2 h into the run: rows older than bringup (minus slack)
        # are from a previous run
        two_hours_ms = 2 * 3600 * 1000
        cutoff = charge_ghost_stale_cutoff(
            self.make_fleet_state(two_hours_ms), self.NOW
        )
        assert cutoff is not None
        bringup = self.NOW - timedelta(hours=2)
        self.assertEqual(cutoff, bringup - CURRENT_RUN_SLACK)
        # a row from yesterday's run is stale, one from this run is not
        self.assertLess(self.NOW - timedelta(days=1), cutoff)
        self.assertGreater(self.NOW - timedelta(hours=1), cutoff)

    def test_wall_clock_never_stale(self):
        # production: RMF runs on the wall epoch, the cutoff lands in 1970
        # and no honest row can predate it
        epoch_now_ms = round(self.NOW.timestamp() * 1000)
        cutoff = charge_ghost_stale_cutoff(
            self.make_fleet_state(epoch_now_ms), self.NOW
        )
        assert cutoff is not None
        self.assertLess(cutoff.year, 1971)

    def test_no_clock_disables_reaping(self):
        self.assertIsNone(
            charge_ghost_stale_cutoff(self.make_fleet_state(None), self.NOW)
        )
