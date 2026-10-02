"""F-464 (G ruling 2026-10-02, second sheet, item 5): a mission row whose
first state arrives before its request is stored still gets its labels.

KNOWN BAD (the first version): the lookup's MISS was cached, so the
dispatcher's first state — which reaches the ledger before the dispatch
call has stored the request — pinned "no labels" on the task for the rest
of its life (77 of 330 mission rows in an hour on release f1-n68).
KNOWN GOOD, and the boring side: a task with no stored request at all (the
fleet's own charge tasks, direct robot tasks) stops costing a query per
state once it is no longer new; a request stored without labels settles at
once; a hit is never looked up twice.
"""

import asyncio
import types
import unittest

from api_server import booking_labels as bl

LABELS = ["gf:redispatch-root=patrol.dispatch-1", "gf:redispatch-of=patrol.dispatch-1"]


class _Store:
    def __init__(self):
        self.requests = {}
        self.loads = 0

    async def load(self, task_id):
        self.loads += 1
        return self.requests.get(task_id)


def _request(labels):
    return types.SimpleNamespace(labels=labels)


class TestBookingLabels(unittest.TestCase):
    def setUp(self):
        bl.reset()
        self.store = _Store()

    def _lookup(self, task_id, now):
        return asyncio.run(bl.lookup(task_id, self.store.load, now=now))

    def test_a_first_state_before_the_request_is_stored_is_not_final(self):
        # the dispatcher's first state: the request is not stored yet
        self.assertIsNone(self._lookup("t2", now=100.0))
        # the dispatch call returns and stores it
        self.store.requests["t2"] = _request(LABELS)
        # the fleet's next state gets the labels — the miss was not cached
        self.assertEqual(LABELS, self._lookup("t2", now=100.4))

    def test_the_dispatch_path_remembers_before_anything_is_stored(self):
        bl.remember("t2", LABELS)
        self.assertEqual(LABELS, self._lookup("t2", now=100.0))
        self.assertEqual(0, self.store.loads, "no query at all")

    def test_remembering_overrides_a_miss_that_had_settled(self):
        self.assertIsNone(self._lookup("t2", now=100.0))
        self.assertIsNone(self._lookup("t2", now=100.0 + bl.MISS_SETTLES_S))
        bl.remember("t2", LABELS)
        self.assertEqual(LABELS, self._lookup("t2", now=200.0))

    def test_a_task_with_no_request_stops_being_asked_about(self):
        """The fleet's own charge task: many states, never a request."""
        for i in range(10):
            self.assertIsNone(self._lookup("Charge-1", now=100.0 + i))
        self.assertEqual(10, self.store.loads, "asked while it is new")
        self.assertIsNone(self._lookup("Charge-1", now=100.0 + bl.MISS_SETTLES_S))
        before = self.store.loads
        for i in range(100):
            self.assertIsNone(self._lookup("Charge-1", now=200.0 + i))
        self.assertEqual(before, self.store.loads, "settled: no more queries")

    def test_a_request_stored_without_labels_settles_at_once(self):
        self.store.requests["t3"] = _request([])
        self.assertIsNone(self._lookup("t3", now=100.0))
        self.assertIsNone(self._lookup("t3", now=100.1))
        self.assertEqual(1, self.store.loads)

    def test_a_hit_is_looked_up_once(self):
        self.store.requests["t4"] = _request(LABELS)
        for i in range(5):
            self.assertEqual(LABELS, self._lookup("t4", now=100.0 + i))
        self.assertEqual(1, self.store.loads)

    def test_remembering_nothing_changes_nothing(self):
        bl.remember("t5", [])
        bl.remember("t5", None)
        self.assertIsNone(self._lookup("t5", now=100.0))
        self.assertEqual(1, self.store.loads)


if __name__ == "__main__":
    unittest.main()
