"""F-345 hold guard — proven both ways.

KNOWN BAD: cancelling a `f338-hold-*` task outright is refused with the
way out named; releasing a hold for a trip that was never queued is
refused (None).

KNOWN GOOD: every other task cancels as before; a held robot's hold is
released — as a cancel request carrying the trip it was released for —
once the trip has an id; a robot with no hold gets no release; a fleet
state without the robot yields '' and never a conviction.
"""
import unittest

from api_server.hold_guard import (
    HOLD_TASK_PREFIX, RELEASED_FOR_LABEL, cancel_refusal, current_task_of,
    hold_release, is_hold_task, wait_for_confirmation_plan,
)

HOLD = f"{HOLD_TASK_PREFIX}gentle_bot_3-330240726"


class HoldGuardTest(unittest.TestCase):
    def test_a_hold_cannot_be_cancelled_outright(self):
        why = cancel_refusal(HOLD)
        self.assertIsNotNone(why)
        self.assertIn(HOLD, why)
        self.assertIn("send the robot somewhere it can reach", why)
        self.assertIn("Reopen the lanes", why)

    def test_every_other_task_cancels_as_before(self):
        for tid in ("patrol.dispatch-7", "Charge37c251", "f36-retreat-x-1",
                    "fr36-idle-x-2", "", None):
            self.assertIsNone(cancel_refusal(tid), tid)

    def test_the_hold_is_released_for_a_queued_trip_and_names_it(self):
        req = hold_release(HOLD, "op-trip-1")
        self.assertIsNotNone(req)
        self.assertEqual(req["type"], "cancel_task_request")
        self.assertEqual(req["task_id"], HOLD)
        self.assertIn(f"{RELEASED_FOR_LABEL}=op-trip-1", req["labels"])

    def test_a_hold_is_never_released_for_a_trip_that_was_not_queued(self):
        self.assertIsNone(hold_release(HOLD, ""))
        self.assertIsNone(hold_release(HOLD, None))

    def test_a_robot_with_no_hold_gets_no_release(self):
        self.assertIsNone(hold_release("patrol.dispatch-7", "op-trip-1"))
        self.assertIsNone(hold_release("", "op-trip-1"))

    def test_current_task_is_read_from_the_stored_fleet_state(self):
        state = {"robots": {"gentle_bot_3": {"task_id": HOLD},
                            "gentle_bot_1": {"task_id": ""}}}
        self.assertEqual(current_task_of(state, "gentle_bot_3"), HOLD)
        self.assertEqual(current_task_of(state, "gentle_bot_1"), "")
        self.assertEqual(current_task_of(state, "gentle_bot_9"), "")
        self.assertEqual(current_task_of(None, "gentle_bot_3"), "")
        self.assertEqual(current_task_of({"robots": "junk"}, "gentle_bot_3"), "")

    def test_is_hold_task(self):
        self.assertTrue(is_hold_task(HOLD))
        self.assertTrue(is_hold_task("f338-hold-refuge-gentle_bot_3-1"))
        self.assertFalse(is_hold_task("f338-holdx"))
        self.assertFalse(is_hold_task(None))
        self.assertFalse(is_hold_task(""))

    def test_the_cut_sweep_waits_for_every_intended_lane(self):
        self.assertEqual(wait_for_confirmation_plan([16, 17], [16, 17]), [])
        self.assertEqual(wait_for_confirmation_plan([16, 17], [16]), [17])
        self.assertEqual(wait_for_confirmation_plan([], []), [])
        self.assertEqual(wait_for_confirmation_plan([16], []), [16])


if __name__ == "__main__":
    unittest.main()
