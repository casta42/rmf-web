# F-95: dispatch failures must reach the operator with their WHY.
import unittest

from api_server.dispatch_reason import (
    dispatch_failure_reason,
    no_bid_failure_reason,
    no_bid_waiting_reason,
)
from api_server.models.rmf_api.error import Error

LIMITED_CAPACITY = Error(
    code=9,
    category="Not feasible",
    detail=(
        "[TaskPlanner] Failed to compute assignments for task_id "
        "[patrol.dispatch-682330] due to insufficient battery capacity to "
        "accommodate one or more requests by any of the robots in this fleet."
    ),
)
LOW_BATTERY = Error(
    code=9,
    category="Not feasible",
    detail=(
        "[TaskPlanner] Failed to compute assignments for task_id [t] due to "
        "insufficient initial battery charge for all robots in this fleet."
    ),
)
NO_BID = Error(
    code=10,
    category="rejection",
    detail="No fleet adapters offered a bid for task [patrol.dispatch-50140]",
)


class TestDispatchFailureReason(unittest.TestCase):
    def test_none_without_errors(self):
        self.assertIsNone(dispatch_failure_reason(None))
        self.assertIsNone(dispatch_failure_reason([]))

    def test_limited_capacity(self):
        reason = dispatch_failure_reason([LIMITED_CAPACITY])
        self.assertIn("one battery charge", reason)
        self.assertIn("shorten it", reason)

    def test_low_battery(self):
        self.assertIn("too low on battery", dispatch_failure_reason([LOW_BATTERY]))

    def test_generic_planner(self):
        reason = dispatch_failure_reason(
            [Error(code=9, category="Not feasible", detail="novel")]
        )
        self.assertIn("could not fit this mission", reason)

    def test_no_bid(self):
        self.assertIn(
            "no robot answered the dispatch", dispatch_failure_reason([NO_BID])
        )

    def test_specific_reason_wins_over_no_bid(self):
        # Real persisted shape: [code 9 detail, code 10 no-bid]
        reason = dispatch_failure_reason([LIMITED_CAPACITY, NO_BID])
        self.assertIn("one battery charge", reason)

    def test_unknown_falls_back_to_raw_detail(self):
        self.assertEqual(
            dispatch_failure_reason([Error(code=42, detail="weird")]), "weird"
        )


class TestNoBidFailureReason(unittest.TestCase):
    """G ruling 2026-10-01 item 6 (F-410/F-412 class): "fail only after N
    attempts with the reason named" — the reason on a mission whose LAST
    auction also got no bid. Silence and a refusal are different failures
    and must read differently."""

    def test_silence_sends_the_operator_to_fleet_coordination(self):
        reason = no_bid_failure_reason([NO_BID], 5, 77.4)
        self.assertEqual(
            reason,
            "no robot answered 5 auctions in a row over 77 s — the fleet is "
            "not answering dispatches; check that the fleet coordination "
            "service is running",
        )
        # not the robot-availability wording of a single no-bid: a healthy
        # fleet adapter answers even when it has no robot to offer
        self.assertNotIn("busy", reason)
        self.assertNotIn("dispatching again usually works", reason)

    def test_a_fleet_that_answered_is_quoted_not_called_silent(self):
        # the real persisted shape: the adapter's refusal, then code 10
        reason = no_bid_failure_reason([LOW_BATTERY, NO_BID], 5, 38)
        self.assertIn(
            "no robot offered to take this mission at 5 auctions in a row "
            "over 38 s",
            reason,
        )
        self.assertIn("too low on battery", reason)
        self.assertNotIn("not answering dispatches", reason)
        self.assertIn(
            "one battery charge", no_bid_failure_reason([NO_BID, LIMITED_CAPACITY], 5)
        )
        # an answer this module has no words for is quoted as it came
        self.assertIn(
            "weird", no_bid_failure_reason([Error(code=42, detail="weird"), NO_BID], 5)
        )
        # ...and one with nothing to quote still says the fleet answered
        self.assertIn(
            "answered every time without saying why",
            no_bid_failure_reason([Error(code=42), NO_BID], 5),
        )

    def test_the_boring_inputs(self):
        silent = "no robot answered 5 auctions in a row — the fleet is not"
        # no errors on the state at all, and no span known
        self.assertIn(silent, no_bid_failure_reason(None, 5))
        self.assertIn(silent, no_bid_failure_reason([], 5))
        self.assertIn(silent, no_bid_failure_reason([NO_BID], 5, None))
        # a span that came out negative (a clock stepped) is left out, not
        # printed
        self.assertIn(silent, no_bid_failure_reason([NO_BID], 5, -3.0))
        self.assertIn("over 0 s", no_bid_failure_reason([NO_BID], 5, 0.2))
        self.assertIn("no robot answered 1 auction —", no_bid_failure_reason([], 1))

    def test_a_single_no_bid_keeps_its_own_wording(self):
        # F-95's translation is untouched: it is still what the queue and a
        # mission somebody canceled mid-auction read
        self.assertIn(
            "no robot answered the dispatch in time",
            dispatch_failure_reason([NO_BID]),
        )


class TestNoBidWaitingReason(unittest.TestCase):
    """G ruling 2026-10-01, ruling 2 (F-435): the reason a mission whose
    last auction got no bid is WAITING — the line on its "waiting for a
    robot" row and in its one alert. It says what the fleet answered, and
    never tells the operator to dispatch again (the fleet already is)."""

    def test_each_answer_reads_as_itself(self):
        self.assertEqual(
            no_bid_waiting_reason([NO_BID]), "no robot answered its last auction"
        )
        self.assertEqual(
            no_bid_waiting_reason([LOW_BATTERY, NO_BID]),
            "every robot is too low on battery for it until charging finishes",
        )
        self.assertEqual(
            no_bid_waiting_reason([LIMITED_CAPACITY, NO_BID]),
            "no robot can finish it on one battery charge, even starting full",
        )
        self.assertEqual(
            no_bid_waiting_reason(
                [Error(code=9, category="Not feasible", detail="novel"), NO_BID]
            ),
            "no robot's schedule can fit it at the moment",
        )
        self.assertEqual(
            no_bid_waiting_reason([Error(code=13, detail="boom"), NO_BID]),
            "fleet coordination hit an internal error on its last auction",
        )
        self.assertEqual(
            no_bid_waiting_reason([Error(code=42, detail="weird"), NO_BID]),
            "no robot offered to take it at its last auction",
        )

    def test_the_permanent_answer_wins_over_a_transient_one(self):
        self.assertIn(
            "one battery charge",
            no_bid_waiting_reason([LOW_BATTERY, LIMITED_CAPACITY, NO_BID]),
        )

    def test_the_boring_inputs(self):
        for errors in (None, [], [NO_BID]):
            self.assertEqual(
                no_bid_waiting_reason(errors), "no robot answered its last auction"
            )
        for errors in ([NO_BID], [LOW_BATTERY, NO_BID], [LIMITED_CAPACITY]):
            reason = no_bid_waiting_reason(errors)
            self.assertNotIn("dispatch again", reason)
            self.assertNotIn("dispatching again", reason)


if __name__ == "__main__":
    unittest.main()
