"""FR-12 governor re-dispatch (F-36/F-286) — proven both ways.

KNOWN BAD, must act: a task canceled with the governor's marker comes
back as a new dispatch carrying its origin, generation and reason, once,
even though the fleet re-broadcasts the terminal state.

KNOWN GOOD, must stay silent: an operator's cancel (no marker), a
completed mission, a failed one, a direct mission with no stored request,
and a chain that has already run MAX_GENERATIONS hops.
"""
import asyncio
import logging
import unittest
from typing import List, Optional

from pydantic import BaseModel

from api_server.redispatch import (
    CHARGE_HOLD_MAX_BACKOFF_S, CHARGE_HOLD_MAX_WAIT_S, GENERATION_LABEL,
    MAX_GENERATIONS, NO_BID_CODE, ORIGIN_LABEL, REASON_LABEL,
    REDISPATCH_LABEL, Redispatcher, WAITING_LABEL, charge_hold_backoff,
    generation_of, is_charge_hold, next_labels, origin_of, waiting_since_of,
    wants_redispatch, wants_retry,
)

# F-319: the two reasons that mean "a robot is charging", byte-identical
# to what the fleet adapter writes.
HOLD_AT_AWARD = ("charge hold (F-319): [gentle_bot_4] is held for charging "
                 "at award — a held robot is awarded no mission until it "
                 "resumes; returned to the fleet for re-dispatch")
HOLD_FIRST_TICK = ("charge hold (F-36): [gentle_bot_6] at SoC 0.31 is below "
                   "the 0.35 retreat threshold and is held for charging "
                   "until 0.98; this mission was awarded while it was busy")

REASON = ("charge preemption (F-36): [gentle_bot_3] at SoC 0.17 cannot "
          "finish this mission and still reach [gentle_bot_3_charger] "
          "above 0.10 (the trip home costs 0.06) — mission returned to "
          "the fleet for re-dispatch")


class FakeRequest(BaseModel):
    category: str = "patrol"
    labels: Optional[List[str]] = None


class WantsTest(unittest.TestCase):
    def test_marker_on_a_canceled_state_returns_the_human_reason(self):
        self.assertEqual(
            wants_redispatch("Status.canceled", [REDISPATCH_LABEL, REASON]),
            REASON)
        self.assertEqual(wants_redispatch("killed", [REDISPATCH_LABEL]),
                         "returned to the fleet by the charge governor")

    def test_operator_cancel_completed_failed_and_none_are_ignored(self):
        self.assertIsNone(wants_redispatch("canceled", ["operator: wrong dock"]))
        self.assertIsNone(wants_redispatch("canceled", None))
        self.assertIsNone(wants_redispatch("completed", [REDISPATCH_LABEL]))
        self.assertIsNone(wants_redispatch("failed", [REDISPATCH_LABEL]))
        self.assertIsNone(wants_redispatch("underway", [REDISPATCH_LABEL]))
        self.assertIsNone(wants_redispatch(None, [REDISPATCH_LABEL]))


class LabelsTest(unittest.TestCase):
    def test_first_hop_keeps_operator_labels_and_adds_bookkeeping(self):
        labels = next_labels(["shift=night"], "patrol.dispatch-1", REASON)
        self.assertIn("shift=night", labels)
        self.assertIn(f"{ORIGIN_LABEL}patrol.dispatch-1", labels)
        self.assertIn(f"{GENERATION_LABEL}1", labels)
        self.assertTrue(any(lab.startswith(REASON_LABEL) for lab in labels))
        self.assertEqual(generation_of(labels), 1)
        self.assertEqual(origin_of(labels), "patrol.dispatch-1")

    def test_later_hops_replace_bookkeeping_and_bump_generation(self):
        first = next_labels([], "a", REASON)
        second = next_labels(first, "b", REASON)
        self.assertEqual(generation_of(second), 2)
        self.assertEqual(origin_of(second), "b")
        self.assertEqual(
            sum(1 for lab in second if lab.startswith(GENERATION_LABEL)), 1)

    def test_chain_stops_at_the_cap(self):
        labels = [f"{GENERATION_LABEL}{MAX_GENERATIONS}"]
        self.assertIsNone(next_labels(labels, "x", REASON))
        labels = [f"{GENERATION_LABEL}{MAX_GENERATIONS - 1}"]
        self.assertIsNotNone(next_labels(labels, "x", REASON))

    def test_garbage_generation_reads_as_zero(self):
        self.assertEqual(generation_of([f"{GENERATION_LABEL}abc"]), 0)
        self.assertEqual(generation_of(None), 0)


class RedispatcherTest(unittest.TestCase):
    def setUp(self):
        self.dispatched: List[FakeRequest] = []
        self.requests = {"patrol.dispatch-1": FakeRequest(labels=["x=1"])}

        async def dispatch(request):
            self.dispatched.append(request)
            return f"patrol.dispatch-{100 + len(self.dispatched)}"

        async def load(task_id):
            return self.requests.get(task_id)

        self.rd = Redispatcher(dispatch, load, logging.getLogger("t"))
        self.slept = []

    async def _sleep(self, seconds):
        self.slept.append(seconds)

    def run_(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_governor_cancel_is_redispatched_once(self):
        new_id = self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", [REDISPATCH_LABEL, REASON],
            sleep=self._sleep))
        self.assertEqual(new_id, "patrol.dispatch-101")
        self.assertEqual(self.slept, [5.0])            # settle first (F-291)
        self.assertEqual(len(self.dispatched), 1)
        labels = self.dispatched[0].labels
        self.assertIn("x=1", labels)
        self.assertIn(f"{ORIGIN_LABEL}patrol.dispatch-1", labels)
        # the fleet re-broadcasts terminal states: acted on ONCE
        again = self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", [REDISPATCH_LABEL, REASON],
            sleep=self._sleep))
        self.assertIsNone(again)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.rd.redispatched, 1)

    def test_operator_cancel_and_completion_do_nothing(self):
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", ["operator"], sleep=self._sleep)))
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "completed", [REDISPATCH_LABEL],
            sleep=self._sleep)))
        self.assertEqual(self.dispatched, [])

    def test_a_child_nobody_bid_on_is_retried_once_per_generation(self):
        # F-291: the fleet planner failed the immediate child twice
        child_labels = next_labels(["x=1"], "patrol.dispatch-1", REASON)
        self.requests["patrol.dispatch-101"] = FakeRequest(labels=child_labels)
        self.assertEqual(
            wants_retry("failed", child_labels, [{"code": NO_BID_CODE}]),
            REASON[:200])        # the reason label is capped at 200 chars
        new_id = self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-101", "failed", None,
            booking_labels=child_labels,
            dispatch_errors=[{"code": NO_BID_CODE}], sleep=self._sleep))
        self.assertIsNotNone(new_id)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(generation_of(self.dispatched[0].labels), 2)
        self.assertEqual(origin_of(self.dispatched[0].labels),
                         "patrol.dispatch-101")
        self.assertEqual(self.slept, [10.0])
        # a failure with another cause, or a mission that is not a child,
        # is never retried
        self.assertIsNone(wants_retry("failed", child_labels, [{"code": 9}]))
        self.assertIsNone(wants_retry("failed", ["x=1"],
                                      [{"code": NO_BID_CODE}]))
        self.assertIsNone(wants_retry("canceled", child_labels,
                                      [{"code": NO_BID_CODE}]))

    def test_direct_mission_without_a_stored_request_is_left_canceled(self):
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "op-send-77", "canceled", [REDISPATCH_LABEL, REASON],
            sleep=self._sleep)))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.rd.refused, 1)

    def test_capped_chain_is_refused_and_counted(self):
        self.requests["deep"] = FakeRequest(
            labels=[f"{GENERATION_LABEL}{MAX_GENERATIONS}"])
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "deep", "canceled", [REDISPATCH_LABEL, REASON],
            sleep=self._sleep)))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.rd.refused, 1)

    def test_a_refused_dispatch_is_logged_not_raised(self):
        async def refuse(_request):
            raise RuntimeError("destination occupied (F-34)")

        rd = Redispatcher(refuse, lambda tid: self.requests_get(tid),
                          logging.getLogger("t"))
        self.assertIsNone(self.run_(rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", [REDISPATCH_LABEL, REASON],
            sleep=self._sleep)))
        self.assertEqual(rd.refused, 1)

    async def requests_get(self, task_id):
        return self.requests.get(task_id)


class ChargeHoldWaitTest(unittest.TestCase):
    """F-319: a charge hold is a WAIT, not a defect.

    KNOWN BAD, must not recur: with every healthy robot busy, the planner
    picks the held robot again the moment the mission is back on the
    floor. Same robot, same refusal — the old hop cap burned all eight in
    about two minutes and killed the mission. The drill lost four that
    way before this existed.

    KNOWN GOOD, must stay untouched: every OTHER reason keeps the
    eight-hop cap, because eight hand-backs for any other cause really is
    a robot winning a mission it cannot run.
    """

    def test_the_charge_hold_reasons_are_recognised(self):
        self.assertTrue(is_charge_hold(HOLD_AT_AWARD))
        self.assertTrue(is_charge_hold(HOLD_FIRST_TICK))

    def test_nothing_else_is_mistaken_for_a_charge_hold(self):
        # the rescue preemption is a real hand-back but NOT a wait: the
        # robot is going home now, and another robot should take this.
        self.assertFalse(is_charge_hold(REASON))
        self.assertFalse(is_charge_hold("robot fault (F-299): [b2] is faulted"))
        self.assertFalse(is_charge_hold(None))
        self.assertFalse(is_charge_hold(""))

    def test_a_charge_hold_outlives_the_hop_cap(self):
        """The defect this fixes: a mission that only needed to wait."""
        labels = [f"{GENERATION_LABEL}{MAX_GENERATIONS + 4}",
                  f"{WAITING_LABEL}1000"]
        out = next_labels(labels, "origin-1", HOLD_AT_AWARD, now_s=1100.0)
        self.assertIsNotNone(out, "a charge hold must not die on hop count")
        self.assertEqual(generation_of(out), MAX_GENERATIONS + 5)
        self.assertEqual(waiting_since_of(out), 1000.0,
                         "the clock must start at the FIRST hand-back")

    def test_a_charge_hold_still_stops_on_the_clock(self):
        labels = [f"{WAITING_LABEL}1000"]
        self.assertIsNone(
            next_labels(labels, "origin-1", HOLD_AT_AWARD,
                        now_s=1000.0 + CHARGE_HOLD_MAX_WAIT_S + 1),
            "a robot that never resumes IS a defect")

    def test_the_clock_starts_on_the_first_hand_back(self):
        out = next_labels([], "origin-1", HOLD_AT_AWARD, now_s=500.0)
        self.assertEqual(waiting_since_of(out), 500.0)
        # ...and is carried down the chain unchanged
        out2 = next_labels(out, "origin-2", HOLD_AT_AWARD, now_s=800.0)
        self.assertEqual(waiting_since_of(out2), 500.0)

    def test_every_other_reason_keeps_the_eight_hop_cap(self):
        """The boring half: this change must not loosen anything else."""
        capped = [f"{GENERATION_LABEL}{MAX_GENERATIONS}"]
        self.assertIsNone(next_labels(capped, "origin-1", REASON),
                          "the rescue-preemption cap must be untouched")
        self.assertIsNotNone(
            next_labels([f"{GENERATION_LABEL}{MAX_GENERATIONS - 1}"],
                        "origin-1", REASON))

    def test_the_wait_outlasts_a_full_charge(self):
        """0.19 -> 0.98 is ~630 s on the compressed pack; the bound has
        to comfortably exceed that or the fix does not fix anything."""
        self.assertGreater(CHARGE_HOLD_MAX_WAIT_S, 630.0)

    def test_the_backoff_grows_and_is_capped(self):
        first = charge_hold_backoff(0)
        self.assertGreater(first, 0.0)
        self.assertGreaterEqual(charge_hold_backoff(2), charge_hold_backoff(1))
        self.assertEqual(charge_hold_backoff(99), CHARGE_HOLD_MAX_BACKOFF_S)
        # and several attempts still fit inside the wall-clock bound
        self.assertGreater(CHARGE_HOLD_MAX_WAIT_S / CHARGE_HOLD_MAX_BACKOFF_S,
                           4.0)


if __name__ == "__main__":
    unittest.main()
