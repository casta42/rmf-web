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

from .internal import TASK_LOG_ERROR_TEXT, alert_on_task_log, alert_on_task_state

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


if __name__ == "__main__":
    unittest.main()
