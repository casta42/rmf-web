"""F-285 cancel routing — proven both ways.

KNOWN BAD (the gf-a shape): a future-scheduled patrol, dispatch status
queued, assigned to nobody — must go to the dispatcher's service, not
time out on the task API topic.

KNOWN GOOD: an underway mission goes to the fleet as before; a queued
mission that HAS a robot (the fleet's own queue) goes to the fleet; an
unknown id is 404, a finished one 409, an already-canceled one a no-op
success.
"""
import unittest

from api_server.cancel_route import (
    ROUTE_ALREADY_CANCELED, ROUTE_DISPATCHER, ROUTE_FLEET, ROUTE_TERMINAL,
    ROUTE_UNKNOWN, cancel_route,
)


class CancelRouteTest(unittest.TestCase):
    def test_future_task_in_the_bidding_queue_goes_to_the_dispatcher(self):
        self.assertEqual(cancel_route("Status.queued", "Status2.queued", None),
                         ROUTE_DISPATCHER)
        self.assertEqual(cancel_route("queued", "selected", None),
                         ROUTE_DISPATCHER)

    def test_fleet_owned_tasks_go_to_the_fleet(self):
        self.assertEqual(cancel_route("underway", "dispatched",
                                      {"group": "gentle_fleet", "name": "b1"}),
                         ROUTE_FLEET)
        # queued INSIDE a robot's queue: it has an assignee
        self.assertEqual(cancel_route("queued", "dispatched",
                                      {"group": "gentle_fleet", "name": "b1"}),
                         ROUTE_FLEET)
        # a direct mission never had a dispatch record
        self.assertEqual(cancel_route("underway", None, None), ROUTE_FLEET)
        self.assertEqual(cancel_route("standby", None, None), ROUTE_FLEET)

    def test_terminal_and_unknown_states_are_answered_locally(self):
        self.assertEqual(cancel_route(None, None, None), ROUTE_UNKNOWN)
        self.assertEqual(cancel_route("completed", "dispatched", None),
                         ROUTE_TERMINAL)
        self.assertEqual(cancel_route("failed", "failed_to_assign", None),
                         ROUTE_TERMINAL)
        self.assertEqual(cancel_route("canceled", "canceled_in_flight", None),
                         ROUTE_ALREADY_CANCELED)
        self.assertEqual(cancel_route("Status.killed", None, None),
                         ROUTE_ALREADY_CANCELED)


if __name__ == "__main__":
    unittest.main()
