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
    GENERATION_LABEL, MAX_GENERATIONS, ORIGIN_LABEL, REASON_LABEL,
    REDISPATCH_LABEL, Redispatcher, generation_of, next_labels, origin_of,
    wants_redispatch,
)

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

    def run_(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_governor_cancel_is_redispatched_once(self):
        new_id = self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", [REDISPATCH_LABEL, REASON]))
        self.assertEqual(new_id, "patrol.dispatch-101")
        self.assertEqual(len(self.dispatched), 1)
        labels = self.dispatched[0].labels
        self.assertIn("x=1", labels)
        self.assertIn(f"{ORIGIN_LABEL}patrol.dispatch-1", labels)
        # the fleet re-broadcasts terminal states: acted on ONCE
        again = self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", [REDISPATCH_LABEL, REASON]))
        self.assertIsNone(again)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.rd.redispatched, 1)

    def test_operator_cancel_and_completion_do_nothing(self):
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", ["operator"])))
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "completed", [REDISPATCH_LABEL])))
        self.assertEqual(self.dispatched, [])

    def test_direct_mission_without_a_stored_request_is_left_canceled(self):
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "op-send-77", "canceled", [REDISPATCH_LABEL, REASON])))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.rd.refused, 1)

    def test_capped_chain_is_refused_and_counted(self):
        self.requests["deep"] = FakeRequest(
            labels=[f"{GENERATION_LABEL}{MAX_GENERATIONS}"])
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "deep", "canceled", [REDISPATCH_LABEL, REASON])))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.rd.refused, 1)

    def test_a_refused_dispatch_is_logged_not_raised(self):
        async def refuse(_request):
            raise RuntimeError("destination occupied (F-34)")

        rd = Redispatcher(refuse, lambda tid: self.requests_get(tid),
                          logging.getLogger("t"))
        self.assertIsNone(self.run_(rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", [REDISPATCH_LABEL, REASON])))
        self.assertEqual(rd.refused, 1)

    async def requests_get(self, task_id):
        return self.requests.get(task_id)


if __name__ == "__main__":
    unittest.main()
