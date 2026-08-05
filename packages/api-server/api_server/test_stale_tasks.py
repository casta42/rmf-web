"""GentleFleet fork: stale-mission janitor tests (F-77)."""

import logging

from tortoise import Tortoise

from api_server.models import TaskStatus
from api_server.models.tortoise_models import TaskState as DbTaskState
from api_server.stale_tasks import fail_over_stale_tasks
from api_server.test import AppFixture


class TestStaleTaskJanitor(AppFixture):
    def test_orphaned_task_fails_over_and_fresh_one_survives(self):
        portal = self.get_portal()
        logger = logging.getLogger("test-janitor")

        async def prepare():
            await DbTaskState.update_or_create(
                {
                    "data": {"status": "underway"},
                    "status": TaskStatus.underway,  # stored as enum repr
                },
                id_="f77-orphan",
            )
            await DbTaskState.update_or_create(
                {
                    "data": {"status": "underway"},
                    "status": TaskStatus.underway,
                },
                id_="f77-fresh",
            )
            # Backdate the orphan under auto_now's nose (raw SQL); the
            # fresh row keeps its just-now timestamp.
            conn = Tortoise.get_connection("default")
            await conn.execute_query(
                "UPDATE taskstate SET updated_at = '2026-01-01 00:00:00' "
                "WHERE id = 'f77-orphan'"
            )

        async def sweep():
            return await fail_over_stale_tasks(1800, logger)

        async def fetch(task_id):
            return await DbTaskState.get(id_=task_id)

        async def cleanup():
            await DbTaskState.filter(
                id___in=["f77-orphan", "f77-fresh"]
            ).delete()

        portal.call(prepare)
        try:
            swept = portal.call(sweep)
            self.assertEqual(1, swept)
            orphan = portal.call(lambda: fetch("f77-orphan"))
            self.assertEqual(str(TaskStatus.failed), orphan.status)
            self.assertEqual("failed", orphan.data["status"])
            fresh = portal.call(lambda: fetch("f77-fresh"))
            self.assertEqual(str(TaskStatus.underway), fresh.status)
            # idempotent: a second sweep finds nothing
            self.assertEqual(0, portal.call(sweep))
        finally:
            portal.call(cleanup)

    def test_disabled_janitor_touches_nothing(self):
        portal = self.get_portal()
        self.assertEqual(
            0,
            portal.call(
                lambda: fail_over_stale_tasks(
                    0, logging.getLogger("test-janitor")
                )
            ),
        )
