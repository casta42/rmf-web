"""F-379 (G ruling 2026-09-22) — the fleet's own tasks never page an operator
unless they fail; the bell shows what an operator must act on.

Both ways, on the REAL task models (the F-343 lesson). FIRES: an internal
task that FAILS still alerts, and every dispatched task alerts exactly as
before. PASSES: an internal task that is canceled, or logs an error, raises
nothing. The startup sweep archives the old backlog and leaves failures and
operator tasks open.
"""

import unittest
from types import SimpleNamespace

from api_server import models as mdl

from .internal_tasks import (
    INTERNAL_TASK_PREFIXES,
    SWEEP_RESOLVER,
    is_internal_task,
    pages_operator,
    sweep_internal_task_alerts,
)
from .routes.internal import alert_on_task_log, alert_on_task_state

INTERNAL = ["Charge4a8a39", "wait.gentle_bot_1-7", "f36-retreat-gentle_bot_3-1",
            "f87-sweep-2", "fr36-idle-gentle_bot_5-3", "f338-hold-gentle_bot_2-9587929",
            "ParkRobot12"]
DISPATCHED = ["patrol.dispatch-106350", "a417b316-4ff3-4d5e-b7e3-4387ffd086cd",
              "Charging-station-audit"]  # an operator task that merely starts with "Charg"


class Repo:
    def __init__(self):
        self.rows, self.creates = {}, 0

    async def get_alert(self, alert_id):
        return self.rows.get(alert_id)

    async def alert_exists(self, alert_id):
        return alert_id in self.rows

    async def create_alert(self, alert_id, category, severity=None, fleet=None,
                           robot=None, message=None):
        self.creates += 1
        row = SimpleNamespace(id=alert_id, category=category, severity=severity,
                              message=message, unix_millis_resolved_time=None)
        self.rows[alert_id] = row
        return row


def task_state(task_id, status):
    return mdl.TaskState(**{
        "booking": {"id": task_id, "unix_millis_earliest_start_time": 0},
        "status": status,
        "assigned_to": {"group": "gentle_fleet", "name": "gentle_bot_2"},
    })


def task_log(task_id):
    return mdl.TaskEventLog(task_id=task_id, log=[
        {"seq": 0, "tier": "error", "unix_millis_time": 0, "text": "navigation aborted"}])


class TestTheRule(unittest.IsolatedAsyncioTestCase):
    def test_every_internal_prefix_is_internal_and_operator_tasks_are_not(self):
        for t in INTERNAL:
            self.assertTrue(is_internal_task(t), t)
        for t in DISPATCHED:
            self.assertFalse(is_internal_task(t), t)
        self.assertFalse(is_internal_task(None))
        self.assertFalse(is_internal_task(""))

    async def test_PASSES_an_internal_cancel_raises_nothing(self):
        for t in INTERNAL:
            repo = Repo()
            self.assertIsNone(await alert_on_task_state(task_state(t, "canceled"), repo), t)
            self.assertEqual(repo.creates, 0, t)

    async def test_PASSES_an_internal_log_error_raises_nothing(self):
        for t in INTERNAL:
            repo = Repo()
            self.assertIsNone(await alert_on_task_log(task_log(t), repo), t)
            self.assertEqual(repo.creates, 0, t)

    async def test_FIRES_an_internal_FAILURE_still_pages_the_operator(self):
        for t in INTERNAL:
            repo = Repo()
            row = await alert_on_task_state(task_state(t, "failed"), repo)
            self.assertIsNotNone(row, t)
            self.assertEqual(row.message, f"Task {t} failed")

    async def test_FIRES_operator_tasks_alert_exactly_as_before(self):
        for t in DISPATCHED:
            for status in ("canceled", "failed"):
                repo = Repo()
                row = await alert_on_task_state(task_state(t, status), repo)
                self.assertIsNotNone(row, (t, status))
                self.assertEqual(row.message, f"Task {t} {status}")
            repo = Repo()
            self.assertIsNotNone(await alert_on_task_log(task_log(t), repo), t)

    def test_pages_operator_is_the_whole_rule(self):
        self.assertTrue(pages_operator("Charge1", failed=True))
        self.assertFalse(pages_operator("Charge1", failed=False))
        self.assertTrue(pages_operator("patrol.dispatch-1", failed=False))


class Row(SimpleNamespace):
    async def save(self):
        self.saved = getattr(self, "saved", 0) + 1


class Model:
    def __init__(self, rows):
        self.rows = rows

    async def filter(self, category=None, unix_millis_resolved_time__isnull=None):
        return [r for r in self.rows if r.category == category
                and (r.unix_millis_resolved_time is None) == unix_millis_resolved_time__isnull]


class TestTheSweep(unittest.IsolatedAsyncioTestCase):
    async def test_archives_the_backlog_and_keeps_what_needs_someone(self):
        rows = [
            Row(original_id="Charge4a8a39", category="task", message="Task Charge4a8a39 canceled",
                unix_millis_resolved_time=None),
            Row(original_id="f338-hold-gentle_bot_2-1", category="task",
                message="Task f338-hold-gentle_bot_2-1 canceled", unix_millis_resolved_time=None),
            Row(original_id="fr36-idle-gentle_bot_5-3", category="task",
                message="Task fr36-idle-gentle_bot_5-3 reported an error in its event log",
                unix_millis_resolved_time=None),
            # stays: an internal FAILURE
            Row(original_id="Charge9d12ce", category="task",
                message="Task Charge9d12ce failed: no route", unix_millis_resolved_time=None),
            # stays: an operator's mission
            Row(original_id="patrol.dispatch-106350", category="task",
                message="Task patrol.dispatch-106350 canceled", unix_millis_resolved_time=None),
            # untouched: already resolved
            Row(original_id="Charge0000", category="task", message="Task Charge0000 canceled",
                unix_millis_resolved_time=5, resolved_by="operator"),
            # untouched: not a task alert even though the id looks internal
            Row(original_id="charger__gentle_fleet__x", category="fleet",
                message="cannot reach its charger", unix_millis_resolved_time=None),
        ]
        n = await sweep_internal_task_alerts(Model(rows), 1000)
        self.assertEqual(n, 3)
        by = {r.original_id: r for r in rows}
        for rid in ("Charge4a8a39", "f338-hold-gentle_bot_2-1", "fr36-idle-gentle_bot_5-3"):
            self.assertEqual(by[rid].unix_millis_resolved_time, 1000, rid)
            self.assertEqual(by[rid].resolved_by, SWEEP_RESOLVER)
        self.assertIsNone(by["Charge9d12ce"].unix_millis_resolved_time)
        self.assertIsNone(by["patrol.dispatch-106350"].unix_millis_resolved_time)
        self.assertEqual(by["Charge0000"].resolved_by, "operator")
        self.assertIsNone(by["charger__gentle_fleet__x"].unix_millis_resolved_time)
        # idempotent
        self.assertEqual(await sweep_internal_task_alerts(Model(rows), 2000), 0)


if __name__ == "__main__":
    unittest.main()
