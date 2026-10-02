"""F-141 regression: the class defect is 'a coordination restart leaves
non-terminal task rows the core no longer knows, forever'. F-454: its
mirror — 'the sweep convicts a mission of a core that never restarted'.
These pin what a REAL restart does to one row (decide); when a restart is
real is pinned in test_core_incarnation, and the whole path in
routes/test_interrupted_requeue."""

import unittest

from api_server import interrupted_tasks
from api_server.interrupted_tasks import (
    CLOSE_CANCELED,
    FAIL_LONG_OUTAGE,
    FAIL_NOT_STORED,
    LEAVE_TO_CHARGE_REAPER,
    SEND_AGAIN,
    away_for,
    decide,
    status_tail,
)

WINDOW = 3600.0


class DecideTest(unittest.TestCase):
    def test_a_mission_with_a_stored_request_after_a_short_restart_is_sent_again(self):
        self.assertEqual(
            decide("patrol.dispatch-1", False, True, 12.0, WINDOW), SEND_AGAIN
        )
        self.assertEqual(
            decide("patrol.dispatch-1", False, True, 0.0, WINDOW), SEND_AGAIN
        )
        self.assertEqual(
            decide("patrol.dispatch-1", False, True, WINDOW, WINDOW), SEND_AGAIN
        )

    def test_a_requested_cancellation_wins_over_everything_else(self):
        for stored in (True, False):
            for outage in (1.0, None, 10 * WINDOW):
                self.assertEqual(
                    decide("patrol.dispatch-1", True, stored, outage, WINDOW),
                    CLOSE_CANCELED,
                )

    def test_nothing_re_sendable_fails(self):
        self.assertEqual(decide("direct-7", False, False, 1.0, WINDOW), FAIL_NOT_STORED)

    def test_a_core_away_longer_than_the_window_never_sends_again(self):
        """A database restored from last week's backup, a fleet PC left off
        over the weekend: nobody can say the mission is still wanted."""
        self.assertEqual(
            decide("patrol.dispatch-1", False, True, WINDOW + 1, WINDOW),
            FAIL_LONG_OUTAGE,
        )

    def test_an_unknown_outage_is_never_read_as_a_short_one(self):
        self.assertEqual(
            decide("patrol.dispatch-1", False, True, None, WINDOW), FAIL_LONG_OUTAGE
        )

    def test_the_fleets_own_charge_task_is_the_charge_reapers(self):
        for cancel in (True, False):
            self.assertEqual(
                decide("Charge434ebc", cancel, False, 1.0, WINDOW),
                LEAVE_TO_CHARGE_REAPER,
            )

    def test_no_argument_is_an_age_a_status_or_a_silence(self):
        """F-454's class guard: the rule that convicted healthy missions on
        f1-n66 weighed how old and how quiet a row was, and whether a robot
        was assigned. None of that is an input any more — if somebody adds
        one, this fails and they read why."""
        import inspect

        self.assertEqual(
            list(inspect.signature(decide).parameters),
            [
                "task_id",
                "cancel_requested",
                "request_stored",
                "outage_s",
                "resume_window_s",
            ],
        )
        for gone in (
            "RunBoundary",
            "is_interrupted_row",
            "queued_verdict",
            "current_tasks_by_robot",
            "tasks_named_by",
            "FLEET_SILENCE_GAP",
            "QUEUED_START_GRACE_S",
        ):
            self.assertFalse(
                hasattr(interrupted_tasks, gone),
                f"{gone} inferred a restart or a loss from silence (F-454)",
            )


class WordingTest(unittest.TestCase):
    def test_away_for(self):
        self.assertEqual(away_for(None), "for an unknown time")
        self.assertEqual(away_for(42.4), "for 42 s")
        self.assertEqual(away_for(125), "for 2 min")
        self.assertEqual(away_for(3 * 3600 + 12 * 60 + 5), "for 3 h 12 min")

    def test_status_tail(self):
        self.assertEqual(status_tail("Status.underway"), "underway")
        self.assertEqual(status_tail("queued"), "queued")
        self.assertIsNone(status_tail(None))


if __name__ == "__main__":
    unittest.main()
