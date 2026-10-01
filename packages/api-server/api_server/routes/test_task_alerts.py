"""F-374 — a task alert the operator resolved never comes back, and a
failure's reason is never overwritten by the generic log line.

Before: every task_log_update carrying an error re-created the task's
alert under the task id — the SAME row as its failed/canceled alert —
and create_alert resets the ack and the resolution. So a resolved task
alert re-opened on the next log update ("the bell will not clear"), and
"Task X failed: <reason>" was replaced by "Task X reported an error in
its event log".

Both ways, on the REAL models (mdl.TaskState, mdl.TaskEventLog — the
F-343 lesson: a plain-dict double let a guard skip every live update);
only the repository is a fake, and it behaves like the real one
(create_alert overwrites and re-opens; resolved rows still exist).
"""

import unittest
from types import SimpleNamespace

from api_server import models as mdl
from api_server.models import tortoise_models as ttm
from api_server.redispatch import (
    NO_BID_ATTEMPT_LABEL,
    NO_BID_MAX_ATTEMPTS,
    NO_BID_SINCE_LABEL,
    REDISPATCH_LABEL,
    supersede,
)

from .internal import (
    TASK_LOG_ERROR_TEXT,
    alert_on_task_log,
    alert_on_task_state,
    no_bid_final_reason,
)

TASK = "patrol.dispatch-4242"


class Repo:
    """The real repository's semantics: one row per id; create_alert is
    update_or_create and CLEARS the resolution; a resolved row exists."""

    def __init__(self):
        self.rows = {}
        self.creates = 0

    async def get_alert(self, alert_id):
        return self.rows.get(alert_id)

    async def alert_exists(self, alert_id):
        return alert_id in self.rows

    async def create_alert(
        self, alert_id, category, severity=None, fleet=None, robot=None, message=None
    ):
        self.creates += 1
        row = SimpleNamespace(
            id=alert_id,
            category=category,
            severity=severity,
            fleet=fleet,
            robot=robot,
            message=message,
            unix_millis_resolved_time=None,
        )
        self.rows[alert_id] = row
        return row

    def resolve(self, alert_id):
        self.rows[alert_id].unix_millis_resolved_time = 123


def task_state(status, errors=None):
    data = {
        "booking": {"id": TASK, "unix_millis_earliest_start_time": 0},
        "status": status,
        "assigned_to": {"group": "gentle_fleet", "name": "gentle_bot_2"},
    }
    if errors is not None:
        data["dispatch"] = {"status": "failed_to_assign", "errors": errors}
    return mdl.TaskState(**data)


def task_log(error=True):
    return mdl.TaskEventLog(
        task_id=TASK,
        log=[
            {
                "seq": 0,
                "tier": "error" if error else "info",
                "unix_millis_time": 0,
                "text": "navigation aborted",
            }
        ],
    )


class TestTaskAlerts(unittest.IsolatedAsyncioTestCase):
    async def test_FIRES_before_the_fix_shape_a_resolved_alert_stays_resolved(self):
        repo = Repo()
        await alert_on_task_state(task_state("failed"), repo)
        repo.resolve(TASK)
        # the log keeps streaming errors after the verdict
        for _ in range(3):
            self.assertIsNone(await alert_on_task_log(task_log(), repo))
        self.assertEqual(repo.rows[TASK].unix_millis_resolved_time, 123)
        self.assertEqual(repo.creates, 1)

    async def test_a_failure_reason_is_never_overwritten_by_the_log_line(self):
        repo = Repo()
        await alert_on_task_state(task_state("failed"), repo)
        before = repo.rows[TASK].message
        await alert_on_task_log(task_log(), repo)
        self.assertEqual(repo.rows[TASK].message, before)
        self.assertNotIn(TASK_LOG_ERROR_TEXT, before)

    async def test_a_log_error_first_is_upgraded_by_the_verdict_while_open(self):
        repo = Repo()
        await alert_on_task_log(task_log(), repo)
        self.assertIn(TASK_LOG_ERROR_TEXT, repo.rows[TASK].message)
        await alert_on_task_state(task_state("failed"), repo)
        self.assertEqual(repo.rows[TASK].message, f"Task {TASK} failed")
        self.assertEqual(repo.rows[TASK].robot, "gentle_bot_2")

    async def test_a_resolved_log_error_is_not_reopened_by_the_verdict(self):
        repo = Repo()
        await alert_on_task_log(task_log(), repo)
        repo.resolve(TASK)
        self.assertIsNone(await alert_on_task_state(task_state("failed"), repo))
        self.assertEqual(repo.rows[TASK].unix_millis_resolved_time, 123)

    async def test_PASSES_the_boring_cases(self):
        repo = Repo()
        # a clean log alerts nothing; a completed task alerts nothing
        self.assertIsNone(await alert_on_task_log(task_log(error=False), repo))
        self.assertIsNone(await alert_on_task_state(task_state("completed"), repo))
        self.assertEqual(repo.rows, {})
        # a canceled task alerts once, Info; a re-broadcast adds nothing
        await alert_on_task_state(task_state("canceled"), repo)
        await alert_on_task_state(task_state("canceled"), repo)
        self.assertEqual(repo.creates, 1)
        self.assertEqual(repo.rows[TASK].message, f"Task {TASK} canceled")

    async def test_a_log_error_alone_alerts_exactly_once(self):
        repo = Repo()
        for _ in range(5):
            await alert_on_task_log(task_log(), repo)
        self.assertEqual(repo.creates, 1)


NO_BID_ERROR = {
    "code": 10,
    "category": "rejection",
    "detail": f"No fleet adapters offered a bid for task [{TASK}]",
}
REFUSAL = {
    "code": 9,
    "category": "Not feasible",
    "detail": (
        "[TaskPlanner] Failed to compute assignments for task_id [t] due to "
        "insufficient initial battery charge for all robots in this fleet."
    ),
}


def auction_state(attempt=1, errors=None, since=None, cancellation=None):
    """The dispatcher's state for an auction that closed, as the real
    model: nobody is assigned, and the attempt rides in the booking labels
    the way redispatch.next_no_bid_labels writes it."""
    labels = ["x=1"]
    if attempt > 1:
        labels.append(f"{NO_BID_ATTEMPT_LABEL}{attempt}")
    if since is not None:
        labels.append(f"{NO_BID_SINCE_LABEL}{since:.0f}")
    data = {
        "booking": {
            "id": TASK,
            "unix_millis_earliest_start_time": 0,
            "labels": labels,
        },
        "status": "failed",
        "dispatch": {
            "status": "failed_to_assign",
            "errors": [NO_BID_ERROR] if errors is None else errors,
        },
    }
    if cancellation is not None:
        data["cancellation"] = {"unix_millis_request_time": 1, "labels": cancellation}
    return mdl.TaskState(**data)


class TestNoBidAlerts(unittest.IsolatedAsyncioTestCase):
    """G ruling 2026-10-01 item 6 (F-410/F-412 class): "A timed-out auction
    never becomes a failed mission: re-auction with backoff, and fail only
    after N attempts with the reason named." At the bell, both ways: no
    Critical while attempts remain, a Critical that names the reason when
    the last one is spent — on the real model, through the same two calls
    process_msg makes (supersede, then alert_on_task_state)."""

    async def ingest(self, state, repo):
        supersede(state)
        return await alert_on_task_state(state, repo)

    async def test_PASSES_no_critical_while_attempts_remain(self):
        for attempt in range(1, NO_BID_MAX_ATTEMPTS):
            repo = Repo()
            alert = await self.ingest(auction_state(attempt), repo)
            self.assertEqual(alert.severity, ttm.Alert.Severity.Info, attempt)
            self.assertEqual(alert.message, f"Task {TASK} canceled")
            self.assertNotIn("failed", alert.message)
            # the dispatcher never re-sends it, but if anything did: once
            await self.ingest(auction_state(attempt), repo)
            self.assertEqual(repo.creates, 1)

    async def test_FIRES_a_critical_naming_the_reason_at_the_last_attempt(self):
        repo = Repo()
        state = auction_state(NO_BID_MAX_ATTEMPTS, since=1000)
        self.assertEqual(
            no_bid_final_reason(state, now_s=1077.4),
            "no robot answered 5 auctions in a row over 77 s — the fleet is "
            "not answering dispatches; check that the fleet coordination "
            "service is running",
        )
        alert = await self.ingest(state, repo)
        self.assertEqual(state.status, mdl.TaskStatus.failed)
        self.assertEqual(alert.severity, ttm.Alert.Severity.Critical)
        self.assertTrue(
            alert.message.startswith(
                f"Task {TASK} failed: no robot answered 5 auctions in a row over "
            ),
            alert.message,
        )
        self.assertIn("check that the fleet coordination service", alert.message)

    async def test_FIRES_quoting_a_fleet_that_answered_with_a_refusal(self):
        repo = Repo()
        state = auction_state(NO_BID_MAX_ATTEMPTS, errors=[REFUSAL, NO_BID_ERROR])
        alert = await self.ingest(state, repo)
        self.assertEqual(alert.severity, ttm.Alert.Severity.Critical)
        self.assertIn("no robot offered to take this mission at 5 auctions", alert.message)
        self.assertIn("too low on battery", alert.message)
        self.assertNotIn("not answering dispatches", alert.message)

    async def test_PASSES_the_failures_that_are_not_an_exhausted_auction(self):
        # a planner refusal with no code 10; a mission somebody canceled
        # while its auction was open; a failure during execution. Each is
        # a failure NOW, with the wording it always had.
        cases = [
            (auction_state(errors=[REFUSAL]), "too low on battery"),
            (
                auction_state(cancellation=["canceled from mission queue by g"]),
                "no robot answered the dispatch in time",
            ),
            (task_state("failed"), f"Task {TASK} failed"),
        ]
        for state, expected in cases:
            repo = Repo()
            self.assertIsNone(no_bid_final_reason(state))
            alert = await self.ingest(state, repo)
            self.assertEqual(state.status, mdl.TaskStatus.failed)
            self.assertEqual(alert.severity, ttm.Alert.Severity.Critical)
            self.assertIn(expected, alert.message)
            self.assertNotIn("auctions in a row", alert.message)

    async def test_no_final_reason_for_an_attempt_that_is_auctioned_again(self):
        state = auction_state(2)
        self.assertIsNone(no_bid_final_reason(state))
        supersede(state)
        self.assertEqual(state.cancellation.labels[0], REDISPATCH_LABEL)
        self.assertIsNone(no_bid_final_reason(state))
        # and a last attempt whose span was never recorded says no span
        self.assertEqual(
            no_bid_final_reason(auction_state(NO_BID_MAX_ATTEMPTS)),
            "no robot answered 5 auctions in a row — the fleet is not "
            "answering dispatches; check that the fleet coordination service "
            "is running",
        )


if __name__ == "__main__":
    unittest.main()
