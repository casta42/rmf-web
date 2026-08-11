# F-95: dispatch failures must reach the operator with their WHY.
import unittest

from api_server.dispatch_reason import dispatch_failure_reason
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


if __name__ == "__main__":
    unittest.main()
