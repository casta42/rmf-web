"""FR-12 governor re-dispatch (F-36/F-286) — proven both ways.

KNOWN BAD, must act: a task canceled with the governor's marker comes
back as a new dispatch carrying its origin, generation and reason, once,
even though the fleet re-broadcasts the terminal state.

KNOWN GOOD, must stay silent: an operator's cancel (no marker), a
completed mission, a failed one, a direct mission with no stored request,
and a chain that has already run MAX_GENERATIONS hops.

G ruling 2026-10-01 item 6 (F-410/F-412 class) — "A timed-out auction
never becomes a failed mission: re-auction with backoff, and fail only
after N attempts with the reason named" — also proven both ways, below.

KNOWN BAD, must act: ANY dispatched mission whose auction closed with no
bid (failed + dispatcher code 10), first generation included, is
auctioned again after its backoff, recorded as put back on the floor and
not as a failure, exactly once, without spending a hand-back.

KNOWN GOOD, must stay silent: a failure with any other cause, a mission
somebody canceled, an ordinary hand-back (which must keep its own path),
the LAST attempt (which is the failure), and the boring shapes — no
labels, empty labels, no dispatch block, no stored request.
"""
import asyncio
import logging
import unittest
from typing import List, Optional

from pydantic import BaseModel

from api_server import models as mdl
from api_server.redispatch import (
    CHARGE_HOLD_MAX_BACKOFF_S,
    CHARGE_HOLD_MAX_WAIT_S,
    CLASS_CHARGE_HOLD,
    CLASS_HAND_BACK,
    CLASS_NO_BID,
    GENERATION_LABEL,
    MAX_GENERATIONS,
    NO_BID_ATTEMPT_LABEL,
    NO_BID_BACKOFF_S,
    NO_BID_CODE,
    NO_BID_MAX_ATTEMPTS,
    NO_BID_SINCE_LABEL,
    ORIGIN_LABEL,
    REASON_LABEL,
    REDISPATCH_LABEL,
    WAITING_LABEL,
    Redispatcher,
    charge_hold_backoff,
    class_of,
    generation_of,
    is_charge_hold,
    next_labels,
    next_no_bid_labels,
    no_bid_attempt_of,
    no_bid_backoff,
    no_bid_since_of,
    no_bid_verdict,
    no_bid_verdict_of,
    origin_of,
    root_of,
    supersede,
    unsupersede,
    waiting_since_of,
    wants_redispatch,
    wants_retry,
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
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

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

    def test_a_child_nobody_bid_on_is_auctioned_again_as_a_no_bid(self):
        # F-291: the fleet planner failed the immediate child twice.
        # THE CONTRACT CHANGED (G ruling 2026-10-01 item 6, F-410/F-412
        # class). This test used to pin "retried once per generation,
        # after 10 s, as generation 2" and, in its last lines, "a mission
        # that is not a child is never retried". Both are gone: a no-bid
        # auction is its own class now. The child is auctioned again on
        # the no-bid schedule and its hand-back bookkeeping is carried,
        # not advanced — an unanswered auction is not a hand-back.
        child_labels = next_labels(["x=1"], "patrol.dispatch-1", REASON)
        self.requests["patrol.dispatch-101"] = FakeRequest(labels=child_labels)
        self.assertIn(
            "attempt 1 of 5",
            wants_retry("failed", child_labels, [{"code": NO_BID_CODE}]))
        new_id = self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-101", "failed", None,
            booking_labels=child_labels,
            dispatch_errors=[{"code": NO_BID_CODE}], sleep=self._sleep))
        self.assertIsNotNone(new_id)
        self.assertEqual(len(self.dispatched), 1)
        sent = self.dispatched[0].labels
        self.assertEqual(generation_of(sent), 1, "no hand-back was spent")
        self.assertIn(f"{REASON_LABEL}{REASON[:200]}", sent)
        self.assertEqual(origin_of(sent), "patrol.dispatch-101")
        self.assertEqual(root_of(sent), "patrol.dispatch-1")
        self.assertEqual(no_bid_attempt_of(sent), 2)
        self.assertEqual(self.slept, [NO_BID_BACKOFF_S[0]])
        self.assertEqual((self.rd.reauctioned, self.rd.redispatched), (1, 0))
        # a failure with another cause is never retried...
        self.assertIsNone(wants_retry("failed", child_labels, [{"code": 9}]))
        # ...and the first-generation assertion is REVERSED by the ruling:
        # it read assertIsNone, and a mission that is not a child is now
        # auctioned again like any other.
        self.assertIsNotNone(wants_retry("failed", ["x=1"],
                                         [{"code": NO_BID_CODE}]))
        # a canceled state with code 10 but no marker is somebody's
        # cancel, not ours
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


NO_BID = [{"code": NO_BID_CODE}]
# the real persisted shape when the fleet adapter answered with a refusal:
# its own error first, then the dispatcher's code 10 (Dispatcher.cpp)
ANSWERED = [{"code": 9}, {"code": NO_BID_CODE}]


class NoBidVerdictTest(unittest.TestCase):
    """G ruling 2026-10-01 item 6 (F-410/F-412 class): which states are an
    auction nobody bid on, and which attempt it was. Both ways."""

    def test_FIRES_on_a_first_generation_mission(self):
        # the boring first-generation shapes: no labels, empty labels,
        # an operator's own labels
        for labels in (None, [], ["x=1"], ["shift=night", "gf:deferred-of=3"]):
            verdict = no_bid_verdict("Status.failed", labels, NO_BID)
            self.assertIsNotNone(verdict, labels)
            self.assertEqual(verdict.attempt, 1)
            self.assertEqual(verdict.delay_s, NO_BID_BACKOFF_S[0])
            self.assertFalse(verdict.final)
            self.assertFalse(verdict.answered)

    def test_FIRES_and_says_when_the_fleet_answered_with_a_refusal(self):
        verdict = no_bid_verdict("failed", None, ANSWERED)
        self.assertTrue(verdict.answered)
        self.assertIn("no robot offered to take this mission",
                      wants_retry("failed", None, ANSWERED))
        self.assertIn("no robot answered the auction",
                      wants_retry("failed", None, NO_BID))

    def test_the_superseded_row_is_the_same_auction(self):
        """supersede() rewrites failed -> canceled + marker and keeps the
        dispatcher's errors: that shape must still read as the no-bid it
        is, or it would be taken for a hand-back."""
        labels = [f"{NO_BID_ATTEMPT_LABEL}3"]
        failed = no_bid_verdict("failed", labels, NO_BID)
        superseded = no_bid_verdict(
            "canceled", labels, NO_BID,
            [REDISPATCH_LABEL, "no robot answered the auction ..."])
        self.assertEqual(failed, superseded)
        self.assertEqual(failed.attempt, 3)

    def test_PASSES_everything_that_is_not_a_no_bid(self):
        # a failure with another cause, or with no dispatch errors at all
        self.assertIsNone(no_bid_verdict("failed", None, [{"code": 9}]))
        self.assertIsNone(no_bid_verdict("failed", None, [{"code": 13}]))
        self.assertIsNone(no_bid_verdict("failed", None, None))
        self.assertIsNone(no_bid_verdict("failed", None, []))
        self.assertIsNone(no_bid_verdict("failed", None, [{}]))
        self.assertIsNone(no_bid_verdict("failed", None, [{"code": "x"}]))
        # code 10 on a state that is not a closed auction
        for status in ("queued", "underway", "completed", "standby", None):
            self.assertIsNone(no_bid_verdict(status, None, NO_BID), status)
        # an ordinary hand-back: marked cancel, no dispatch errors
        self.assertIsNone(
            no_bid_verdict("canceled", None, None, [REDISPATCH_LABEL, REASON]))

    def test_PASSES_a_mission_somebody_canceled(self):
        """An operator's cancel that raced the auction (F-292): the state
        arrives failed + code 10 with the latched cancellation stamped
        on it. That mission is not wanted any more."""
        self.assertIsNone(no_bid_verdict(
            "failed", None, NO_BID, ["canceled from mission queue by g"]))
        self.assertIsNone(no_bid_verdict("failed", None, NO_BID, []))
        self.assertIsNone(no_bid_verdict(
            "canceled", None, NO_BID, ["operator: wrong dock"]))
        self.assertIsNone(no_bid_verdict("canceled", None, NO_BID, None))

    def test_the_backoff_schedule_is_the_ruled_one(self):
        self.assertEqual(NO_BID_MAX_ATTEMPTS, 5)
        self.assertEqual(
            [no_bid_backoff(n) for n in (1, 2, 3, 4)], [2.0, 5.0, 10.0, 20.0])
        # one wait before each of attempts 2..N, and none after the last
        self.assertEqual(len(NO_BID_BACKOFF_S), NO_BID_MAX_ATTEMPTS - 1)
        self.assertIsNone(no_bid_backoff(NO_BID_MAX_ATTEMPTS))
        self.assertIsNone(no_bid_backoff(NO_BID_MAX_ATTEMPTS + 7))
        # a count below one is a first attempt, never an index error
        self.assertEqual(no_bid_backoff(0), 2.0)
        self.assertEqual(no_bid_backoff(-3), 2.0)
        # a mission the fleet never answers has failed 37 s of waiting
        # (plus its five auctions) after it was dispatched
        self.assertEqual(sum(NO_BID_BACKOFF_S), 37.0)

    def test_the_last_attempt_is_final_and_is_not_retried(self):
        last = [f"{NO_BID_ATTEMPT_LABEL}{NO_BID_MAX_ATTEMPTS}"]
        verdict = no_bid_verdict("failed", last, NO_BID)
        self.assertTrue(verdict.final)
        self.assertEqual(verdict.attempt, NO_BID_MAX_ATTEMPTS)
        self.assertIsNone(wants_retry("failed", last, NO_BID))
        # the one before it still is
        fourth = [f"{NO_BID_ATTEMPT_LABEL}{NO_BID_MAX_ATTEMPTS - 1}"]
        self.assertFalse(no_bid_verdict("failed", fourth, NO_BID).final)
        self.assertIn("auctioned again in 20 s",
                      wants_retry("failed", fourth, NO_BID))

    def test_the_reason_reads_as_the_ruling_wrote_it(self):
        second = [f"{NO_BID_ATTEMPT_LABEL}2"]
        self.assertEqual(
            wants_retry("failed", second, NO_BID),
            "no robot answered the auction (attempt 2 of 5) — auctioned "
            "again in 5 s")

    def test_a_garbage_count_reads_as_the_first_attempt(self):
        self.assertEqual(no_bid_attempt_of([f"{NO_BID_ATTEMPT_LABEL}abc"]), 1)
        self.assertEqual(no_bid_attempt_of([f"{NO_BID_ATTEMPT_LABEL}0"]), 1)
        self.assertEqual(no_bid_attempt_of([f"{NO_BID_ATTEMPT_LABEL}-4"]), 1)
        self.assertEqual(no_bid_attempt_of(None), 1)
        self.assertIsNone(no_bid_since_of([f"{NO_BID_SINCE_LABEL}soon"]))
        self.assertIsNone(no_bid_since_of(None))


class NoBidLabelsTest(unittest.TestCase):
    """The two budgets must not spend each other, and every row must
    carry enough to fold its chain (root, attempt, class)."""

    def test_first_generation_gets_the_fold_labels_and_no_hand_back(self):
        out = next_no_bid_labels(["shift=night"], "patrol.dispatch-1", 1000.0)
        self.assertIn("shift=night", out)
        self.assertEqual(origin_of(out), "patrol.dispatch-1")
        self.assertEqual(root_of(out), "patrol.dispatch-1")
        self.assertEqual(class_of(out), CLASS_NO_BID)
        self.assertEqual(no_bid_attempt_of(out), 2)
        self.assertEqual(no_bid_since_of(out), 1000.0)
        # nothing of the hand-back class was invented
        self.assertEqual(generation_of(out), 0)
        self.assertFalse(any(lab.startswith((GENERATION_LABEL, REASON_LABEL,
                                             WAITING_LABEL)) for lab in out))
        # the boring inputs
        self.assertEqual(no_bid_attempt_of(next_no_bid_labels(None, "a", 1.0)),
                         2)
        self.assertEqual(no_bid_attempt_of(next_no_bid_labels([], "a", 1.0)),
                         2)

    def test_the_count_climbs_the_root_and_clock_stay_and_it_stops(self):
        labels, origin = None, "t1"
        for attempt in range(2, NO_BID_MAX_ATTEMPTS + 1):
            labels = next_no_bid_labels(labels, origin, 1000.0 + attempt)
            self.assertEqual(no_bid_attempt_of(labels), attempt)
            self.assertEqual(origin_of(labels), origin)
            self.assertEqual(root_of(labels), "t1")
            self.assertEqual(no_bid_since_of(labels), 1002.0,
                             "the clock starts at the FIRST auction")
            for prefix in (ORIGIN_LABEL, NO_BID_ATTEMPT_LABEL,
                           NO_BID_SINCE_LABEL):
                self.assertEqual(
                    sum(1 for lab in labels if lab.startswith(prefix)), 1)
            origin = f"t{attempt}"
        self.assertIsNone(next_no_bid_labels(labels, origin, 9999.0),
                          "the fifth auction is the last")

    def test_mixed_chain_hand_back_then_no_bid_then_hand_back(self):
        # P is handed back (rescue preemption) -> C1
        c1 = next_labels(["x=1"], "P", REASON)
        self.assertEqual((generation_of(c1), root_of(c1), class_of(c1)),
                         (1, "P", CLASS_HAND_BACK))
        # C1's auction gets no bid, twice -> C2, C3: the hand-back
        # provenance is carried untouched, the link moves to the row
        # each one replaces
        c2 = next_no_bid_labels(c1, "C1", 500.0)
        c3 = next_no_bid_labels(c2, "C2", 600.0)
        for labels, parent, attempt in ((c2, "C1", 2), (c3, "C2", 3)):
            self.assertIn("x=1", labels)
            self.assertEqual(generation_of(labels), 1)
            self.assertIn(f"{REASON_LABEL}{REASON[:200]}", labels)
            self.assertEqual(origin_of(labels), parent)
            self.assertEqual(root_of(labels), "P")
            self.assertEqual(class_of(labels), CLASS_NO_BID)
            self.assertEqual(no_bid_attempt_of(labels), attempt)
            self.assertEqual(no_bid_since_of(labels), 500.0)
        # C3 is awarded, and handed back again -> C4: one more hand-back,
        # and the no-bid run is over — the fleet answered
        c4 = next_labels(c3, "C3", REASON)
        self.assertEqual(generation_of(c4), 2)
        self.assertEqual((origin_of(c4), root_of(c4), class_of(c4)),
                         ("C3", "P", CLASS_HAND_BACK))
        self.assertEqual(no_bid_attempt_of(c4), 1)
        self.assertIsNone(no_bid_since_of(c4))
        self.assertFalse(any(lab.startswith((NO_BID_ATTEMPT_LABEL,
                                             NO_BID_SINCE_LABEL))
                             for lab in c4))
        self.assertIn("x=1", c4)

    def test_no_bid_retries_never_spend_the_hand_back_budget(self):
        labels = [f"{GENERATION_LABEL}{MAX_GENERATIONS - 1}"]
        origin = "t0"
        for n in range(NO_BID_MAX_ATTEMPTS - 1):
            labels = next_no_bid_labels(labels, origin, 1.0)
            origin = f"t{n + 1}"
        self.assertEqual(generation_of(labels), MAX_GENERATIONS - 1)
        # the eighth hand-back is still available after four no-bids...
        self.assertIsNotNone(next_labels(labels, origin, REASON))
        # ...and a mission already AT the hand-back cap is still auctioned
        # again when nobody bids: that is not a ninth hand-back
        capped = [f"{GENERATION_LABEL}{MAX_GENERATIONS}"]
        out = next_no_bid_labels(capped, "x", 1.0)
        self.assertEqual(generation_of(out), MAX_GENERATIONS)
        self.assertIsNone(next_labels(out, "y", REASON),
                          "and the cap itself is exactly where it was")

    def test_hand_backs_never_spend_the_no_bid_budget(self):
        labels = next_no_bid_labels(None, "t0", 1.0)
        for n in range(NO_BID_MAX_ATTEMPTS - 2):
            labels = next_no_bid_labels(labels, f"n{n}", 1.0)
        self.assertEqual(no_bid_attempt_of(labels), NO_BID_MAX_ATTEMPTS)
        self.assertIsNone(next_no_bid_labels(labels, "last", 1.0))
        # an award and a hand-back later, the mission has all five again
        handed_back = next_labels(labels, "last", REASON)
        self.assertEqual(no_bid_attempt_of(handed_back), 1)
        self.assertEqual(
            no_bid_attempt_of(next_no_bid_labels(handed_back, "h", 2.0)), 2)

    def test_a_charge_hold_clock_survives_a_no_bid(self):
        held = next_labels([], "P", HOLD_AT_AWARD, now_s=500.0)
        self.assertEqual(class_of(held), CLASS_CHARGE_HOLD)
        retried = next_no_bid_labels(held, "H1", 520.0)
        self.assertEqual(waiting_since_of(retried), 500.0)
        self.assertEqual(generation_of(retried), 1)
        again = next_labels(retried, "N1", HOLD_AT_AWARD, now_s=800.0)
        self.assertEqual(waiting_since_of(again), 500.0)
        self.assertEqual(class_of(again), CLASS_CHARGE_HOLD)
        # and the wall-clock bound is still the charge hold's own
        self.assertIsNone(next_labels(
            retried, "N1", HOLD_AT_AWARD,
            now_s=500.0 + CHARGE_HOLD_MAX_WAIT_S + 1))


class ReauctionTest(unittest.TestCase):
    """The async hook, for the no-bid class."""

    def setUp(self):
        self.dispatched: List[FakeRequest] = []
        self.requests = {"patrol.dispatch-1": FakeRequest(labels=["x=1"])}
        self.abandoned = []
        self.slept = []
        self.first_seen = {"patrol.dispatch-1": 4000.0}

        async def dispatch(request):
            self.dispatched.append(request)
            new_id = f"patrol.dispatch-{100 + len(self.dispatched)}"
            self.requests[new_id] = request
            return new_id

        async def load(task_id):
            return self.requests.get(task_id)

        async def abandon(task_id, reason):
            self.abandoned.append((task_id, reason))

        async def first_seen(task_id):
            return self.first_seen.get(task_id)

        self.rd = Redispatcher(dispatch, load, logging.getLogger("t"),
                               abandon=abandon, first_seen=first_seen,
                               clock=lambda: 5000.0)

    async def _sleep(self, seconds):
        self.slept.append(seconds)

    def run_(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def no_bid(self, task_id, errors=None, status="failed",
               cancellation=None):
        request = self.requests.get(task_id)
        return self.run_(self.rd.maybe_redispatch(
            task_id, status, cancellation,
            booking_labels=request.labels if request else None,
            dispatch_errors=errors or NO_BID, sleep=self._sleep))

    def test_FIRES_a_first_generation_mission_is_auctioned_again(self):
        new_id = self.no_bid("patrol.dispatch-1")
        self.assertEqual(new_id, "patrol.dispatch-101")
        self.assertEqual(self.slept, [2.0])
        labels = self.dispatched[0].labels
        self.assertIn("x=1", labels)
        self.assertEqual(origin_of(labels), "patrol.dispatch-1")
        self.assertEqual(root_of(labels), "patrol.dispatch-1")
        self.assertEqual(class_of(labels), CLASS_NO_BID)
        self.assertEqual(no_bid_attempt_of(labels), 2)
        # the span is counted from when the ledger first saw the mission
        self.assertEqual(no_bid_since_of(labels), 4000.0)
        self.assertEqual(generation_of(labels), 0)
        self.assertEqual(self.rd.reauctioned, 1)
        # the hand-back counters are not this class's
        self.assertEqual((self.rd.redispatched, self.rd.refused), (0, 0))
        self.assertEqual(self.abandoned, [])

    def test_a_mission_nobody_ever_answers_gets_five_auctions_then_fails(self):
        task_id = "patrol.dispatch-1"
        for _ in range(NO_BID_MAX_ATTEMPTS - 1):
            task_id = self.no_bid(task_id)
            self.assertIsNotNone(task_id)
        self.assertEqual(self.slept, [2.0, 5.0, 10.0, 20.0])
        self.assertEqual(len(self.dispatched), NO_BID_MAX_ATTEMPTS - 1)
        self.assertEqual(
            [no_bid_attempt_of(r.labels) for r in self.dispatched],
            [2, 3, 4, 5])
        self.assertEqual({root_of(r.labels) for r in self.dispatched},
                         {"patrol.dispatch-1"})
        self.assertEqual({no_bid_since_of(r.labels) for r in self.dispatched},
                         {4000.0})
        # the fifth auction also gets no bid: that is the failure. No
        # sixth auction, no wait, and it is counted as what it is.
        self.assertIsNone(self.no_bid(task_id))
        self.assertEqual(len(self.dispatched), NO_BID_MAX_ATTEMPTS - 1)
        self.assertEqual(self.slept, [2.0, 5.0, 10.0, 20.0])
        self.assertEqual(self.rd.no_bid_exhausted, 1)
        self.assertEqual(self.rd.reauctioned, NO_BID_MAX_ATTEMPTS - 1)
        # the last row is already `failed` at ingest: nothing to amend
        self.assertEqual(self.abandoned, [])

    def test_no_double_dispatch_from_the_superseded_row(self):
        """The row as supersede() stored it is a marked cancel — the very
        shape the hand-back path acts on. It must be auctioned again
        ONCE, on the no-bid path, and never also handed back."""
        marked = [REDISPATCH_LABEL,
                  "no robot answered the auction (attempt 1 of 5) — "
                  "auctioned again in 2 s"]
        new_id = self.no_bid("patrol.dispatch-1", status="canceled",
                             cancellation=marked)
        self.assertEqual(new_id, "patrol.dispatch-101")
        self.assertEqual(self.slept, [2.0], "not the hand-back settle delay")
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(generation_of(self.dispatched[0].labels), 0)
        self.assertEqual((self.rd.reauctioned, self.rd.redispatched), (1, 0))
        # the same task id again, in either shape: nothing more
        self.assertIsNone(self.no_bid("patrol.dispatch-1", status="canceled",
                                      cancellation=marked))
        self.assertIsNone(self.no_bid("patrol.dispatch-1"))
        self.assertEqual(len(self.dispatched), 1)

    def test_PASSES_the_states_that_are_not_ours(self):
        # another cause; an operator's cancel that raced the auction; a
        # completed mission that somehow carries the error
        self.assertIsNone(self.no_bid("patrol.dispatch-1",
                                      errors=[{"code": 9}]))
        self.assertIsNone(self.no_bid("patrol.dispatch-1",
                                      cancellation=["operator: wrong dock"]))
        self.assertIsNone(self.no_bid("patrol.dispatch-1",
                                      status="completed"))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.slept, [])
        self.assertEqual(self.abandoned, [])
        # ...and none of them used up the task id
        self.assertIsNotNone(self.no_bid("patrol.dispatch-1"))

    def test_an_ordinary_hand_back_still_takes_its_own_path(self):
        new_id = self.run_(self.rd.maybe_redispatch(
            "patrol.dispatch-1", "canceled", [REDISPATCH_LABEL, REASON],
            booking_labels=["x=1"], dispatch_errors=None, sleep=self._sleep))
        self.assertIsNotNone(new_id)
        self.assertEqual(self.slept, [5.0])
        self.assertEqual(generation_of(self.dispatched[0].labels), 1)
        self.assertEqual(no_bid_attempt_of(self.dispatched[0].labels), 1)
        self.assertEqual((self.rd.redispatched, self.rd.reauctioned), (1, 0))

    def test_a_mission_with_no_stored_request_is_failed_by_name(self):
        """Not dispatched through the api-server: there is nothing to
        auction again, and the row must not keep saying it will be."""
        self.assertIsNone(self.no_bid("ros-cli-dispatch-9"))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.rd.no_bid_abandoned, 1)
        self.assertEqual(len(self.abandoned), 1)
        task_id, reason = self.abandoned[0]
        self.assertEqual(task_id, "ros-cli-dispatch-9")
        self.assertIn("no robot answered the auction (attempt 1 of 5)", reason)
        self.assertIn("its request is not stored", reason)

    def test_a_refused_re_auction_is_failed_with_the_refusal(self):
        class Refusal(Exception):
            detail = "destination [dock_2] is occupied by parked robot (F-34)"

        async def refuse(_request):
            raise Refusal()

        self.rd._dispatch = refuse      # pylint: disable=protected-access
        self.assertIsNone(self.no_bid("patrol.dispatch-1"))
        self.assertEqual(self.rd.no_bid_abandoned, 1)
        self.assertEqual(self.rd.reauctioned, 0)
        self.assertIn("could not be auctioned again: destination [dock_2] "
                      "is occupied", self.abandoned[0][1])

    def test_giving_up_without_a_callback_or_with_a_broken_one_is_safe(self):
        async def load(_task_id):
            return None

        bare = Redispatcher(None, load, logging.getLogger("t"))
        self.assertIsNone(self.run_(bare.maybe_redispatch(
            "x", "failed", None, dispatch_errors=NO_BID, sleep=self._sleep)))
        self.assertEqual(bare.no_bid_abandoned, 1)

        async def broken(_task_id, _reason):
            raise RuntimeError("database gone")

        loud = Redispatcher(None, load, logging.getLogger("t"),
                            abandon=broken)
        self.assertIsNone(self.run_(loud.maybe_redispatch(
            "y", "failed", None, dispatch_errors=NO_BID, sleep=self._sleep)))

    def test_the_bound_is_read_from_the_stored_request(self):
        """A state that lost its labels reads as a first attempt. The
        stored request is the truth: if it says this was the last
        auction, there is no sixth."""
        self.requests["lost"] = FakeRequest(
            labels=[f"{NO_BID_ATTEMPT_LABEL}{NO_BID_MAX_ATTEMPTS}"])
        self.assertIsNone(self.run_(self.rd.maybe_redispatch(
            "lost", "failed", None, booking_labels=None,
            dispatch_errors=NO_BID, sleep=self._sleep)))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.rd.no_bid_exhausted, 1)
        self.assertIn("5 auctions in a row", self.abandoned[0][1])

    def test_the_span_falls_back_to_this_process_clock(self):
        """No ledger stamp (or a lookup that throws): the span starts
        when the first no-bid reached us. Never a crash, never a guess
        about a time nobody recorded."""
        self.first_seen.clear()
        self.no_bid("patrol.dispatch-1")
        self.assertEqual(no_bid_since_of(self.dispatched[0].labels), 5000.0)

        async def throws(_task_id):
            raise RuntimeError("database gone")

        self.rd._first_seen = throws    # pylint: disable=protected-access
        self.requests["patrol.dispatch-2"] = FakeRequest(labels=None)
        self.no_bid("patrol.dispatch-2")
        self.assertEqual(no_bid_since_of(self.dispatched[1].labels), 5000.0)


def real_state(status="failed", labels=None, errors=None, cancellation=None,
               dispatch=True):
    """The dispatcher's own task state for a closed auction, as the REAL
    model (the F-343 lesson: a guard proven only on doubles skipped every
    live update)."""
    data = {
        "booking": {"id": "patrol.dispatch-4242",
                    "unix_millis_earliest_start_time": 0},
        "category": "patrol",
        "status": status,
    }
    if labels is not None:
        data["booking"]["labels"] = labels
    if dispatch:
        data["dispatch"] = {
            "status": "failed_to_assign",
            "errors": errors if errors is not None else [
                {"code": 10, "category": "rejection",
                 "detail": "No fleet adapters offered a bid for task "
                           "[patrol.dispatch-4242]"}],
        }
    if cancellation is not None:
        data["cancellation"] = {"unix_millis_request_time": 1,
                                "labels": cancellation}
    return mdl.TaskState(**data)


class SupersedeRealModelTest(unittest.TestCase):
    """G ruling 2026-10-01 item 6: "a timed-out auction never becomes a
    failed mission" — the rewrite applied at ingest, on mdl.TaskState."""

    def test_FIRES_a_no_bid_with_attempts_left_is_recorded_as_put_back(self):
        for labels in (None, [], ["x=1"]):
            state = real_state(labels=labels)
            verdict = supersede(state, now_ms=1_700_000_000_000)
            self.assertEqual(verdict.attempt, 1)
            self.assertEqual(state.status, mdl.TaskStatus.canceled)
            self.assertEqual(
                state.cancellation.labels,
                [REDISPATCH_LABEL,
                 "no robot answered the auction (attempt 1 of 5) — "
                 "auctioned again in 2 s"])
            self.assertEqual(state.cancellation.unix_millis_request_time,
                             1_700_000_000_000)
            # the dispatcher's own verdict stays on the row as provenance
            self.assertEqual(state.dispatch.status.value, "failed_to_assign")
            self.assertEqual(state.dispatch.errors[0].code, 10)
            self.assertEqual(state.booking.labels, labels)
            # it is the hand-back shape the product already presents...
            self.assertIsNotNone(wants_redispatch(
                state.status, state.cancellation.labels))
            # ...and still reads as the no-bid it is
            self.assertEqual(no_bid_verdict_of(state), verdict)
            # the stored row is that JSON: it must survive the round trip
            again = mdl.TaskState(**state.model_dump(mode="json"))
            self.assertEqual(again.status, mdl.TaskStatus.canceled)
            self.assertEqual(no_bid_verdict_of(again), verdict)

    def test_FIRES_with_the_attempt_and_the_fleet_answer_in_the_reason(self):
        state = real_state(
            labels=["x=1", f"{NO_BID_ATTEMPT_LABEL}2"],
            errors=[{"code": 9, "category": "Not feasible",
                     "detail": "[TaskPlanner] Failed to compute assignments"},
                    {"code": 10, "category": "rejection", "detail": "none"}])
        verdict = supersede(state)
        self.assertEqual((verdict.attempt, verdict.delay_s, verdict.answered),
                         (2, 5.0, True))
        self.assertEqual(
            state.cancellation.labels[1],
            "no robot offered to take this mission (attempt 2 of 5) — "
            "auctioned again in 5 s")
        self.assertEqual(len(state.dispatch.errors), 2)

    def test_applying_it_twice_changes_nothing_more(self):
        state = real_state()
        supersede(state, now_ms=5)
        before = state.model_dump(mode="json")
        self.assertIsNotNone(supersede(state, now_ms=99))
        self.assertEqual(state.model_dump(mode="json"), before)

    def test_PASSES_the_last_attempt_stays_the_failure_it_is(self):
        state = real_state(
            labels=[f"{NO_BID_ATTEMPT_LABEL}{NO_BID_MAX_ATTEMPTS}"])
        verdict = supersede(state)
        self.assertTrue(verdict.final)
        self.assertEqual(state.status, mdl.TaskStatus.failed)
        self.assertIsNone(state.cancellation)

    def test_PASSES_everything_that_is_not_a_no_bid(self):
        untouched = [
            # a non-allocation failure: the planner refused, nobody timed out
            real_state(errors=[{"code": 9, "detail": "not feasible"}]),
            real_state(errors=[{"code": 13, "detail": "internal"}]),
            real_state(errors=[]),
            # a failure during execution carries no dispatch block at all
            real_state(dispatch=False),
            # the boring statuses
            real_state(status="completed", dispatch=False),
            real_state(status="underway", dispatch=False),
            real_state(status="queued"),
            # an operator's cancel that raced the auction
            real_state(cancellation=["canceled from mission queue by g"]),
            real_state(status="canceled", cancellation=["operator"]),
            # an ordinary hand-back
            real_state(status="canceled", dispatch=False,
                       cancellation=[REDISPATCH_LABEL, REASON]),
        ]
        for state in untouched:
            before = state.model_dump(mode="json")
            self.assertIsNone(supersede(state), before)
            self.assertEqual(state.model_dump(mode="json"), before)

    def test_it_never_raises_on_a_state_it_cannot_read(self):
        self.assertIsNone(supersede(None))
        self.assertIsNone(supersede(object()))
        self.assertIsNone(no_bid_verdict_of(None))

    def test_unsupersede_undoes_exactly_our_own_rewrite(self):
        state = real_state(labels=["x=1"])
        supersede(state)
        self.assertTrue(unsupersede(state))
        self.assertEqual(state.status, mdl.TaskStatus.failed)
        self.assertIsNone(state.cancellation)
        self.assertEqual(state.dispatch.errors[0].code, 10)
        # known good: a hand-back, an operator's cancel, a plain failure
        # and a completed mission are never turned into failures
        for other in (
            real_state(status="canceled", dispatch=False,
                       cancellation=[REDISPATCH_LABEL, REASON]),
            real_state(status="canceled", cancellation=["operator"]),
            real_state(),
            real_state(status="completed", dispatch=False),
        ):
            before = other.model_dump(mode="json")
            self.assertFalse(unsupersede(other))
            self.assertEqual(other.model_dump(mode="json"), before)
        self.assertFalse(unsupersede(None))


if __name__ == "__main__":
    unittest.main()
