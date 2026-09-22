"""F-387 (G ruling 2026-09-22, D-82) — the D-17 guard judges by motion.

The motion half, both ways and in the boring environments (the F-191
lesson): MOVING fires on a robot driving; STATIONARY is reached only by a
robot seen still for the whole window on a current pose; everything the
watch cannot see — no pose, a short watch, a pose that stopped arriving, a
STALE pose, a name two fleets share — is UNKNOWN, never stationary.
Pure: runs on the host (`python3 -m pytest -p no:anyio`).
"""

import unittest

from .robot_motion import (
    MOVE_EPS_M,
    MOVING,
    SAMPLE_MAX_AGE_S,
    STATIONARY,
    STATIONARY_WINDOW_S,
    UNKNOWN,
    MotionWatch,
)

KEY = "gentle_fleet/gentle_bot_5"


def _feed(watch, key, t0, seconds, x0=1.5, y0=15.5, vx=0.0, rate_hz=10.0):
    n = int(seconds * rate_hz) + 1
    for k in range(n):
        t = t0 + k / rate_hz
        watch.sample(t, key, x0 + vx * (t - t0), y0)
    return t0 + (n - 1) / rate_hz


class TestMotionFires(unittest.TestCase):
    def test_a_robot_driving_home_is_moving(self):
        # a ChargeBattery robot on its way to the dock: 0.5 m/s
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, 20.0, vx=0.5)
        verdict = watch.motion(KEY, end)
        self.assertEqual(MOVING, verdict.state, verdict.reason)
        self.assertFalse(verdict.stationary)

    def test_motion_that_just_started_is_moving_before_the_window_fills(self):
        # still for 20 s, then it sets off: 2 s at 0.3 m/s = 0.6 m
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, 20.0)
        end = _feed(watch, KEY, end + 0.1, 2.0, vx=0.3)
        self.assertEqual(MOVING, watch.motion(KEY, end).state)

    def test_a_robot_that_moved_within_the_window_is_not_yet_stationary(self):
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, 5.0, vx=0.5)  # drove in
        end = _feed(
            watch, KEY, end + 0.1, STATIONARY_WINDOW_S - 2.0, x0=1.5 + 2.5
        )  # just stopped
        self.assertEqual(MOVING, watch.motion(KEY, end).state)


class TestMotionPasses(unittest.TestCase):
    def test_a_robot_charging_on_its_dock_is_stationary(self):
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, 30.0)
        verdict = watch.motion(KEY, end)
        self.assertEqual(STATIONARY, verdict.state, verdict.reason)
        self.assertTrue(verdict.stationary)

    def test_localisation_jitter_is_not_motion(self):
        watch = MotionWatch()
        for k in range(300):
            # ±2 cm on both axes: 5.7 cm peak to peak, inside the bar
            jitter = (MOVE_EPS_M * 0.2) * (1 if k % 2 else -1)
            watch.sample(100.0 + k * 0.1, KEY, 1.5 + jitter, 15.5 - jitter)
        self.assertEqual(STATIONARY, watch.motion(KEY, 129.9).state)

    def test_a_robot_that_stopped_a_window_ago_is_stationary_again(self):
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, 5.0, vx=0.5)
        end = _feed(watch, KEY, end + 0.1, STATIONARY_WINDOW_S + 2.0, x0=4.0)
        self.assertEqual(STATIONARY, watch.motion(KEY, end).state)


class TestMotionCannotSee(unittest.TestCase):
    """Every one of these is the boring environment a guard meets: a
    fresh api-server, a robot not yet reported, a frozen feed. None may
    read as stationary."""

    def test_no_pose_at_all(self):
        verdict = MotionWatch().motion(KEY, 100.0)
        self.assertEqual(UNKNOWN, verdict.state)
        self.assertIn("no position", verdict.reason)

    def test_a_fresh_watch_is_not_long_enough(self):
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, STATIONARY_WINDOW_S / 2)
        verdict = watch.motion(KEY, end)
        self.assertEqual(UNKNOWN, verdict.state)
        self.assertIn("watched for only", verdict.reason)

    def test_a_pose_that_stopped_arriving(self):
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, 30.0)
        verdict = watch.motion(KEY, end + SAMPLE_MAX_AGE_S + 1.0)
        self.assertEqual(UNKNOWN, verdict.state)
        self.assertIn("stopped arriving", verdict.reason)

    def test_a_stale_pose_looks_still_and_is_not_evidence(self):
        # F-268: RMF refused the robot's updates; the pose repeats exactly
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, 30.0)
        verdict = watch.motion(KEY, end, stale=True)
        self.assertEqual(UNKNOWN, verdict.state)
        self.assertIn("STALE", verdict.reason)

    def test_task_rows_resolve_the_robot_by_name(self):
        watch = MotionWatch()
        end = _feed(watch, KEY, 100.0, 30.0)
        self.assertEqual(STATIONARY, watch.motion_of_robot("gentle_bot_5", end).state)
        self.assertEqual(UNKNOWN, watch.motion_of_robot("gentle_bot_9", end).state)

    def test_a_name_two_fleets_share_is_not_guessed(self):
        watch = MotionWatch()
        _feed(watch, "fleet_a/bot", 100.0, 30.0)
        end = _feed(watch, "fleet_b/bot", 100.0, 30.0, vx=0.5)
        verdict = watch.motion_of_robot("bot", end)
        self.assertEqual(UNKNOWN, verdict.state)
        self.assertIn("more than one fleet", verdict.reason)
        # ...and naming the fleet answers it
        self.assertEqual(
            MOVING, watch.motion_of_robot("bot", end, fleet="fleet_b").state
        )


if __name__ == "__main__":
    unittest.main()
