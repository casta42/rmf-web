"""F-141 regression: the class defect is 'a coordination restart leaves
non-terminal task rows the core no longer knows, forever'. These pin
outage detection and row classification for that class."""
import unittest
from datetime import datetime, timedelta, timezone

from api_server.interrupted_tasks import (
    FLEET_SILENCE_GAP, REANNOUNCE_GRACE, RunBoundary, is_interrupted_row,
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


if __name__ == "__main__":
    unittest.main()
