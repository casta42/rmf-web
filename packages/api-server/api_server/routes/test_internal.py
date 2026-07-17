import unittest
from typing import Optional

from api_server.app_config import app_config
from api_server.models import RobotState
from api_server.models.rmf_api.location_2D import Location2D
from api_server.models.rmf_api.robot_state import Status as RobotStatus

from .internal import (
    LOW_BATTERY_HYSTERESIS,
    STUCK_REARM_DISTANCE,
    _low_battery_alerted,
    _stuck_states,
    check_low_battery,
    check_robot_stuck,
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
                make_robot_state(battery=self.threshold + LOW_BATTERY_HYSTERESIS + 0.01),
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
        self.assertIsNone(check_robot_stuck(ROBOT_ID, make_robot_state(), 0))
        # stationary but the timeout has not elapsed yet
        self.assertIsNone(
            check_robot_stuck(ROBOT_ID, make_robot_state(), self.timeout_millis - 1)
        )
        alert_id = check_robot_stuck(
            ROBOT_ID, make_robot_state(), self.timeout_millis
        )
        self.assertEqual(
            alert_id, f"robot_stuck__test_fleet__test_robot__{self.timeout_millis}"
        )
        # still stuck, no new alert
        self.assertIsNone(
            check_robot_stuck(ROBOT_ID, make_robot_state(), self.timeout_millis * 2)
        )

    def test_movement_resets_timer(self):
        self.assertIsNone(check_robot_stuck(ROBOT_ID, make_robot_state(), 0))
        self.assertIsNone(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(x=1.0), self.timeout_millis - 1
            )
        )
        # only half the timeout has elapsed since the robot last moved
        self.assertIsNone(
            check_robot_stuck(
                ROBOT_ID,
                make_robot_state(x=1.0),
                self.timeout_millis - 1 + self.timeout_millis // 2,
            )
        )

    def test_rearms_after_moving_away(self):
        check_robot_stuck(ROBOT_ID, make_robot_state(), 0)
        alert_id = check_robot_stuck(ROBOT_ID, make_robot_state(), self.timeout_millis)
        self.assertIsNotNone(alert_id)
        # small movement does not re-arm the episode
        self.assertIsNone(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(x=0.1), self.timeout_millis * 2
            )
        )
        self.assertIsNone(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(x=0.1), self.timeout_millis * 4
            )
        )
        # moving away re-arms, a new stuck episode alerts again
        rearm_x = STUCK_REARM_DISTANCE + 0.01
        self.assertIsNone(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(x=rearm_x), self.timeout_millis * 4 + 1
            )
        )
        alert_id = check_robot_stuck(
            ROBOT_ID, make_robot_state(x=rearm_x), self.timeout_millis * 6
        )
        self.assertIsNotNone(alert_id)

    def test_no_alert_when_not_executing_task(self):
        self.assertIsNone(
            check_robot_stuck(
                ROBOT_ID, make_robot_state(task_id="", status=RobotStatus.idle), 0
            )
        )
        self.assertIsNone(
            check_robot_stuck(
                ROBOT_ID,
                make_robot_state(task_id="", status=RobotStatus.idle),
                self.timeout_millis * 2,
            )
        )
        self.assertIsNone(
            check_robot_stuck(
                ROBOT_ID,
                make_robot_state(task_id="task_1", status=RobotStatus.charging),
                self.timeout_millis * 4,
            )
        )

    def test_no_alert_without_location(self):
        state = make_robot_state()
        state.location = None
        self.assertIsNone(check_robot_stuck(ROBOT_ID, state, 0))
        self.assertIsNone(check_robot_stuck(ROBOT_ID, state, self.timeout_millis * 2))
