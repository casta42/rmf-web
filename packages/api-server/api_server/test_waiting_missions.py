"""G ruling 2026-10-01, ruling 2 (F-435) — the "waiting for a robot"
registry, proven both ways on the REAL task-state model (the F-343
lesson: a guard proven on dict doubles skipped every live update).

KNOWN BAD, must act: a chain whose attempt is handed back (any class) or
whose auction got no bid ENTERS; its re-dispatch UPDATES the attempt it
waits on; another supersede updates the reason and the attempts; a chain
that has waited the threshold is due its ONE alert.

KNOWN GOOD, must stay quiet: a re-broadcast of the same terminal state
counts nothing twice; a mission that was never handed back never enters;
a chain LEAVES the moment any row of it starts (underway, blocked,
delayed) or ends (completed, failed), or an operator cancels it; nothing
is due before the threshold, or twice; a state the registry cannot read
changes nothing and never raises.
"""

import unittest

from api_server import models as mdl
from api_server.redispatch import (
    NO_BID_ATTEMPT_LABEL,
    REDISPATCH_LABEL,
    ROOT_LABEL,
    next_labels,
)
from api_server.waiting_missions import (
    ALERT_ENV,
    DEFAULT_ALERT_S,
    WaitingRegistry,
    alert_id_of,
    alert_message,
    alert_threshold_s,
    waiting_reason,
)

ROOT = "patrol.dispatch-100"
HOLD = (
    "charge hold (F-319): [gentle_bot_4] is held for charging at award — a "
    "held robot is awarded no mission until it resumes; returned to the "
    "fleet for re-dispatch"
)
NO_BID = {
    "code": 10,
    "category": "rejection",
    "detail": "No fleet adapters offered a bid",
}
LOW = {
    "code": 9,
    "category": "Not feasible",
    "detail": "[TaskPlanner] Failed to compute assignments for task_id [t] due "
    "to insufficient initial battery charge for all robots in this fleet.",
}


def state(task_id, status, labels=None, cancellation=None, errors=None):
    data = {
        "booking": {"id": task_id, "unix_millis_earliest_start_time": 0},
        "category": "patrol",
        "status": status,
    }
    if labels is not None:
        data["booking"]["labels"] = labels
    if cancellation is not None:
        data["cancellation"] = {"unix_millis_request_time": 1, "labels": cancellation}
    if errors is not None:
        data["dispatch"] = {"status": "failed_to_assign", "errors": errors}
    return mdl.TaskState(**data)


def child_labels(n):
    """Labels of the n-th re-dispatched attempt of ROOT."""
    return [f"{ROOT_LABEL}{ROOT}", f"gf:redispatch-gen={n}", "gf:kind=goto"]


def handed_back(task_id, labels=None, reason=HOLD):
    return state(task_id, "canceled", labels, [REDISPATCH_LABEL, reason])


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class EnterUpdateLeaveTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reg = WaitingRegistry(clock=self.clock)

    def test_FIRES_a_hand_back_of_the_first_attempt_enters(self):
        change = self.reg.observe(handed_back(ROOT, ["gf:kind=goto"]))
        self.assertEqual(change.kind, "entered")
        entry = self.reg.get(ROOT)
        self.assertEqual((entry.root_id, entry.task_id), (ROOT, ROOT))
        self.assertEqual(entry.attempts, 1)
        self.assertEqual(entry.reason, HOLD)
        self.assertEqual(entry.since_unix, 1000.0)

    def test_FIRES_a_no_bid_put_back_enters_with_the_fleet_s_answer(self):
        superseded = state(
            ROOT,
            "canceled",
            [f"{NO_BID_ATTEMPT_LABEL}3"],
            [REDISPATCH_LABEL, "no robot offered to take this mission (attempt 3)"],
            [LOW, NO_BID],
        )
        self.assertEqual(
            waiting_reason(superseded),
            "every robot is too low on battery for it until charging finishes",
        )
        self.assertEqual(self.reg.observe(superseded).kind, "entered")
        self.assertIn("too low on battery", self.reg.get(ROOT).reason)

    def test_FIRES_re_dispatch_and_the_next_supersede_update_it(self):
        self.reg.observe(handed_back(ROOT))
        self.assertIsNotNone(self.reg.redispatched(ROOT, ROOT, "c1"))
        self.assertEqual(self.reg.get(ROOT).task_id, "c1")
        # the child is queued — still waiting, nothing changes
        self.assertIsNone(self.reg.observe(state("c1", "queued", child_labels(1))))
        change = self.reg.observe(handed_back("c1", child_labels(1), "robot fault"))
        self.assertEqual(change.kind, "updated")
        entry = self.reg.get(ROOT)
        self.assertEqual((entry.task_id, entry.attempts), ("c1", 2))
        self.assertEqual(entry.reason, "robot fault")
        self.assertEqual(entry.since_unix, 1000.0, "the age is the mission's")

    def test_PASSES_a_re_broadcast_counts_nothing_twice(self):
        self.reg.observe(handed_back(ROOT))
        self.reg.redispatched(ROOT, ROOT, "c1")
        for _ in range(3):
            self.assertIsNone(self.reg.observe(handed_back(ROOT)))
        entry = self.reg.get(ROOT)
        self.assertEqual((entry.task_id, entry.attempts), ("c1", 1))

    def test_PASSES_a_mission_never_handed_back_never_enters(self):
        for status in ("queued", "standby", "uninitialized", "underway", "completed"):
            self.assertIsNone(self.reg.observe(state(ROOT, status)), status)
        self.assertIsNone(self.reg.observe(state(ROOT, "failed", errors=[{"code": 9}])))
        self.assertIsNone(
            self.reg.observe(state(ROOT, "canceled", cancellation=["operator"]))
        )
        self.assertEqual(len(self.reg), 0)
        self.assertEqual(self.reg.views(), [])

    def test_PASSES_any_row_starting_or_ending_takes_the_chain_out(self):
        for status in ("underway", "blocked", "delayed", "completed", "failed"):
            reg = WaitingRegistry(clock=self.clock)
            reg.observe(handed_back(ROOT))
            reg.redispatched(ROOT, ROOT, "c1")
            change = reg.observe(state("c1", status, child_labels(1)))
            self.assertEqual(change.kind, "left", status)
            self.assertIsNone(reg.get(ROOT), status)
            # and a late re-broadcast of the superseded row does not bring
            # it back
            self.assertIsNone(reg.observe(handed_back(ROOT)), status)
            self.assertEqual(len(reg), 0)

    def test_PASSES_an_operator_s_cancel_takes_the_chain_out(self):
        self.reg.observe(handed_back(ROOT))
        self.reg.redispatched(ROOT, ROOT, "c1")
        change = self.reg.observe(
            state("c1", "canceled", child_labels(1), ["canceled by g"])
        )
        self.assertEqual(change.kind, "left")
        self.assertEqual(len(self.reg), 0)

    def test_a_chain_that_ran_can_wait_again(self):
        """A mission handed back mid-run (a charge preemption) is waiting
        again: a NEW attempt of it was superseded."""
        self.reg.observe(handed_back(ROOT))
        self.reg.redispatched(ROOT, ROOT, "c1")
        self.reg.observe(state("c1", "underway", child_labels(1)))
        self.assertEqual(len(self.reg), 0)
        change = self.reg.observe(handed_back("c1", child_labels(1)))
        self.assertEqual(change.kind, "entered")
        self.assertEqual(self.reg.get(ROOT).task_id, "c1")

    def test_a_stale_re_dispatch_does_not_overwrite_a_newer_attempt(self):
        self.reg.observe(handed_back(ROOT))
        self.assertIsNone(self.reg.redispatched(ROOT, "someone-else", "c9"))
        self.assertIsNone(self.reg.redispatched("unknown", ROOT, "c9"))
        self.assertEqual(self.reg.get(ROOT).task_id, ROOT)

    def test_withdraw_takes_the_chain_out_and_stops_its_re_dispatch(self):
        self.reg.observe(handed_back(ROOT))
        self.assertTrue(self.reg.wanted(ROOT))
        self.assertIsNone(self.reg.withdraw("not-waited-on", ["x"]))
        entry = self.reg.withdraw(ROOT, ["canceled by g", "it was waiting"])
        self.assertEqual(entry.root_id, ROOT)
        self.assertIsNone(self.reg.get(ROOT))
        self.assertFalse(self.reg.wanted(ROOT))
        self.assertTrue(self.reg.wanted("anything-else"))
        self.assertEqual(
            self.reg.withdrawn_labels(ROOT), ["canceled by g", "it was waiting"]
        )
        # the withdrawn attempt, re-sent in either shape, changes nothing
        self.assertIsNone(self.reg.observe(handed_back(ROOT)))
        self.assertIsNone(
            self.reg.observe(state(ROOT, "canceled", cancellation=["canceled by g"]))
        )
        self.assertEqual(len(self.reg), 0)

    def test_it_never_raises_on_a_state_it_cannot_read(self):
        for junk in (None, object(), "canceled"):
            self.assertIsNone(self.reg.observe(junk))
        self.assertEqual(len(self.reg), 0)

    def test_the_view_is_what_get_tasks_waiting_returns(self):
        self.reg.observe(handed_back(ROOT))
        entry = self.reg.get(ROOT)
        entry.category, entry.places = "patrol", ["s1", "s2"]
        self.clock.t = 1420.0
        self.assertEqual(
            self.reg.views(),
            [
                {
                    "root_id": ROOT,
                    "task_id": ROOT,
                    "category": "patrol",
                    "places": ["s1", "s2"],
                    "since_unix": 1000.0,
                    "age_s": 420.0,
                    "reason": HOLD,
                    "attempts": 1,
                }
            ],
        )

    def test_the_longest_wait_comes_first(self):
        self.reg.superseded("b", "b", "r", since_unix=50.0)
        self.reg.superseded("a", "a", "r", since_unix=10.0)
        self.reg.superseded("c", "c", "r", since_unix=90.0)
        self.assertEqual([v["root_id"] for v in self.reg.views(100.0)], ["a", "b", "c"])


class OneAlertTest(unittest.TestCase):
    """The ONE alert per waiting chain: due at the threshold and not
    before, and never twice."""

    def setUp(self):
        self.reg = WaitingRegistry(clock=Clock(1000.0))
        self.reg.observe(handed_back(ROOT))

    def test_due_at_the_threshold_and_not_before(self):
        self.assertEqual(self.reg.due_alerts(1000.0 + 899.0, 900.0), [])
        due = self.reg.due_alerts(1000.0 + 900.0, 900.0)
        self.assertEqual([e.root_id for e in due], [ROOT])

    def test_never_twice(self):
        entry = self.reg.due_alerts(5000.0, 900.0)[0]
        entry.alerted = alert_id_of(ROOT)
        self.assertEqual(self.reg.due_alerts(9000.0, 900.0), [])

    def test_the_message_names_the_mission_its_wait_and_its_reason(self):
        entry = self.reg.get(ROOT)
        self.assertEqual(alert_id_of(ROOT), f"waiting__{ROOT}")
        self.assertEqual(
            alert_message(entry, 1000.0 + 15 * 60 + 59),
            f"Mission {ROOT} has been waiting for a robot for 15 min — {HOLD}",
        )

    def test_the_threshold_comes_from_the_environment_or_the_default(self):
        self.assertEqual(DEFAULT_ALERT_S, 900.0)
        self.assertEqual(alert_threshold_s({}), 900.0)
        self.assertEqual(alert_threshold_s({ALERT_ENV: ""}), 900.0)
        self.assertEqual(alert_threshold_s({ALERT_ENV: "1200"}), 1200.0)
        self.assertEqual(alert_threshold_s({ALERT_ENV: " 45.5 "}), 45.5)
        # never "page at once" or "never page" by accident
        for bad in ("0", "-5", "nan", "inf", "soon"):
            self.assertEqual(alert_threshold_s({ALERT_ENV: bad}), 900.0, bad)

    def test_the_default_outlasts_a_full_charge(self):
        """0.19 -> 0.98 is ~630 s on the compressed pack: a fleet that is
        only busy charging must not page."""
        self.assertGreater(DEFAULT_ALERT_S, 630.0)


class LabelsFoldTest(unittest.TestCase):
    """The registry folds by the same labels redispatch writes."""

    def test_a_re_dispatched_attempt_folds_into_its_root(self):
        reg = WaitingRegistry(clock=Clock())
        reg.observe(handed_back(ROOT, ["gf:kind=goto"]))
        labels = next_labels(["gf:kind=goto"], ROOT, HOLD)
        reg.redispatched(ROOT, ROOT, "c1")
        change = reg.observe(handed_back("c1", labels))
        self.assertEqual(change.kind, "updated")
        self.assertEqual(change.entry.root_id, ROOT)


if __name__ == "__main__":
    unittest.main()
