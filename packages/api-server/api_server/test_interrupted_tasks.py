"""F-141 regression: the class defect is 'a coordination restart leaves
non-terminal task rows the core no longer knows, forever'. These pin
outage detection and row classification for that class."""
import unittest
from datetime import datetime, timedelta, timezone

from api_server.interrupted_tasks import (
    FLEET_SILENCE_GAP,
    QUEUED_START_GRACE_S,
    REANNOUNCE_GRACE,
    RobotNow,
    RunBoundary,
    current_tasks_by_robot,
    is_interrupted_row,
    queued_verdict,
    status_tail,
)

NOW = datetime(2026, 8, 25, 3, 13, 43, tzinfo=timezone.utc)


class RunBoundaryTest(unittest.TestCase):
    def test_steady_cadence_never_declares_an_outage(self):
        b = RunBoundary()
        for t in range(0, 300):
            b.observe(float(t), NOW + timedelta(seconds=t))
        self.assertTrue(b.reaped)
        self.assertFalse(b.due(300.0))

    def test_drill2_silence_then_resumption_is_an_epoch(self):
        b = RunBoundary()
        b.observe(0.0, NOW)
        resumed = FLEET_SILENCE_GAP + 5.0
        b.observe(resumed, NOW + timedelta(seconds=resumed))
        self.assertFalse(b.reaped)
        self.assertEqual(b.epoch_started_wall,
                         NOW + timedelta(seconds=resumed))
        # not due until the re-announce grace has passed
        self.assertFalse(b.due(resumed + 1.0))
        self.assertTrue(b.due(resumed + REANNOUNCE_GRACE))

    def test_three_consecutive_restarts_each_get_an_epoch(self):
        # G's stress bar: three drill-2s in a row, coherent after each.
        b = RunBoundary()
        t = 0.0
        for _ in range(3):
            b.observe(t, NOW + timedelta(seconds=t))
            t += FLEET_SILENCE_GAP + 10.0
            b.observe(t, NOW + timedelta(seconds=t))
            self.assertFalse(b.reaped)
            self.assertTrue(b.due(t + REANNOUNCE_GRACE))
            b.mark_reaped()
            t += REANNOUNCE_GRACE + 5.0


class RowClassificationTest(unittest.TestCase):
    EPOCH = NOW

    def test_the_six_e6_ghosts_are_interrupted(self):
        # underway/standby, last touched before the kill — the exact
        # 2026-08-25 rows (patrol.dispatch-82800170 et al).
        for status in ("TaskStatus.underway", "TaskStatus.standby",
                       "underway", "standby", "queued"):
            self.assertTrue(is_interrupted_row(
                status, self.EPOCH - timedelta(minutes=5), self.EPOCH),
                status)

    def test_terminal_rows_are_never_touched(self):
        for status in ("TaskStatus.completed", "failed", "canceled",
                       "killed", "skipped"):
            self.assertFalse(is_interrupted_row(
                status, self.EPOCH - timedelta(days=2), self.EPOCH),
                status)

    def test_a_task_the_core_reannounced_is_left_alone(self):
        self.assertTrue(status_tail("TaskStatus.underway") == "underway")
        self.assertFalse(is_interrupted_row(
            "TaskStatus.underway", self.EPOCH + timedelta(seconds=30),
            self.EPOCH))

    def test_naive_timestamps_do_not_crash_the_reaper(self):
        naive = (self.EPOCH - timedelta(minutes=5)).replace(tzinfo=None)
        self.assertTrue(is_interrupted_row("underway", naive, self.EPOCH))



class QueuedVerdictTest(unittest.TestCase):
    """D-86 (3a)/(4): an interrupted mission is sent to the fleet again —
    so a silent QUEUED row (re-announced only when its queue changes) is
    judged lost only on evidence, or the mission would run twice."""

    R = ("gentle_fleet", "gentle_bot_2")
    LAST_WORD = NOW - timedelta(minutes=3)   # the old core's last word on R's active task
    BUSY = {R: RobotNow("patrol.dispatch-9", 5_000_000)}

    def verdict(self, assigned=R, start_ms=None, updated_at=None, current=None,
                lost_active=None):
        return queued_verdict(
            assigned, start_ms,
            updated_at or self.LAST_WORD - timedelta(seconds=40),
            self.BUSY if current is None else current,
            lost_active or {})

    def idle(self, now_ms):
        return {self.R: RobotNow("", now_ms)}

    # -- known bad: lost, must be sent again ---------------------------------
    def test_FIRES_queued_behind_an_active_task_that_was_lost(self):
        lost, why = self.verdict(lost_active={self.R: self.LAST_WORD})
        self.assertTrue(lost, why)
        # within the slack of the old core's last word, too
        lost, _ = self.verdict(updated_at=self.LAST_WORD + timedelta(seconds=1),
                               lost_active={self.R: self.LAST_WORD})
        self.assertTrue(lost)

    def test_FIRES_never_assigned(self):
        self.assertTrue(self.verdict(assigned=None)[0])

    def test_FIRES_on_an_idle_robot_long_past_its_start_on_its_clock(self):
        grace_ms = int(QUEUED_START_GRACE_S * 1000)
        # wall-clock shaped stamps
        wall = int(NOW.timestamp() * 1000)
        self.assertTrue(self.verdict(start_ms=wall - grace_ms - 1,
                                     current=self.idle(wall))[0])
        # sim-clock shaped stamps: RMF's clock restarts near zero at bringup
        self.assertTrue(self.verdict(start_ms=100_000,
                                     current=self.idle(100_000 + grace_ms))[0])

    # -- known good: cannot see, so cannot convict ---------------------------
    def test_PASSES_queued_behind_a_task_the_fleet_still_runs(self):
        lost, why = self.verdict(start_ms=0)
        self.assertFalse(lost)
        self.assertIn("behind [patrol.dispatch-9]", why)

    def test_PASSES_a_row_the_NEW_core_announced_after_the_lost_task(self):
        lost, _ = self.verdict(updated_at=self.LAST_WORD + timedelta(seconds=30),
                               lost_active={self.R: self.LAST_WORD})
        self.assertFalse(lost, "the new core spoke of it: it is queued there")

    def test_PASSES_a_sim_clock_start_is_never_read_against_the_wall(self):
        """The defect this guards: a sim-clock start (minutes since bringup)
        read against wall-clock now is "decades past", and every queued
        mission on an idle robot would be sent twice."""
        self.assertFalse(self.verdict(start_ms=100_000,
                                      current=self.idle(120_000))[0],
                         "20 s past on the robot's clock: not overdue")

    def test_PASSES_an_idle_robot_whose_task_is_not_due(self):
        wall = int(NOW.timestamp() * 1000)
        for start_ms in (wall + 600_000, wall - 1_000):
            self.assertFalse(self.verdict(start_ms=start_ms,
                                          current=self.idle(wall))[0])

    def test_PASSES_an_idle_robot_with_no_clock_or_a_row_with_no_start(self):
        lost, why = self.verdict(start_ms=0, current=self.idle(None))
        self.assertFalse(lost)
        self.assertIn("cannot be seen", why)
        self.assertFalse(self.verdict(start_ms=None,
                                      current=self.idle(5_000_000))[0])

    def test_PASSES_a_robot_in_no_fleet_state(self):
        lost, why = self.verdict(current={})
        self.assertFalse(lost)
        self.assertIn("cannot be seen", why)

    def test_naive_timestamps_compare(self):
        naive = (self.LAST_WORD - timedelta(seconds=5)).replace(tzinfo=None)
        self.assertTrue(self.verdict(updated_at=naive,
                                     lost_active={self.R: self.LAST_WORD})[0])


class CurrentTasksByRobotTest(unittest.TestCase):
    def test_every_robot_with_its_current_task_and_its_clock(self):
        states = [{"name": "gentle_fleet", "robots": {
            "gentle_bot_1": {"name": "gentle_bot_1", "task_id": "p.dispatch-1",
                             "unix_millis_time": 120_000},
            "gentle_bot_2": {"name": "gentle_bot_2", "task_id": ""},
            "gentle_bot_3": {"name": "gentle_bot_3", "unix_millis_time": "x"}}}]
        self.assertEqual(current_tasks_by_robot(states), {
            ("gentle_fleet", "gentle_bot_1"): RobotNow("p.dispatch-1", 120_000),
            ("gentle_fleet", "gentle_bot_2"): RobotNow("", None),
            ("gentle_fleet", "gentle_bot_3"): RobotNow("", None)})

    def test_the_boring_inputs(self):
        for states in (None, [], [None], [{}], [{"name": "f", "robots": None}]):
            self.assertEqual(current_tasks_by_robot(states), {})

if __name__ == "__main__":
    unittest.main()
