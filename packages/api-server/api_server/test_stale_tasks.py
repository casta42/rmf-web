"""GentleFleet fork: stale-mission janitor tests (F-77; F-458, G ruling
2026-10-02, second sheet, item 3): "the F-77 janitor acts only on rows
proven dead against live fleet state and the dispatcher queue, never by age
alone. Ghost charge rows are cleared by that reconciliation. Prove both
ways: a mission waiting 60+ min (F-435) is untouched; a real ghost is
cleared."

KNOWN GOOD, untouched however old: a mission queued on a robot that exists
(waiting 61 min behind its robot's task); a mission in the dispatcher's
queue; a mission its robot is running; one the dispatcher awarded whose
assignment has not reached the ledger.
KNOWN BAD, cleared: the charge row of a robot that left the fleet; a row
assigned to no robot that the dispatcher does not hold; a row still
"underway" on a robot that has been doing something else.
CANNOT SEE, skipped and said: no fleet state or no dispatcher heard by this
server, or not recently; a fleet heard for less than two minutes (robots
still joining). The first janitor would have failed every one of these.
"""

import logging
import unittest

from tortoise import Tortoise

from api_server import live_floor, stale_tasks
from api_server.models import TaskStatus
from api_server.models.tortoise_models import TaskState as DbTaskState
from api_server.stale_tasks import DEAD, KEEP, SKIP, fail_over_stale_tasks, judge
from api_server.test import AppFixture


class _Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


def _floor(
    robots=None,
    heard_for=3600.0,
    active=None,
    finished=None,
    dispatcher_heard=True,
    fleet_heard=True,
):
    """A fleet core this server has been hearing for `heard_for` seconds."""
    clock = _Clock()
    fleets = live_floor.FleetView(clock)
    dispatcher = live_floor.DispatcherView(clock)
    robots = (
        {
            "gentle_bot_1": {"task_id": "patrol.dispatch-1", "status": "working"},
            "gentle_bot_2": {"task_id": "", "status": "idle"},
        }
        if robots is None
        else robots
    )
    if fleet_heard:
        fleets.on_fleet_state("gentle_fleet", robots)
    if dispatcher_heard:
        dispatcher.on_dispatch_states(
            list((active or {}).items()), list((finished or {}).items())
        )
    clock.t += heard_for
    if fleet_heard:
        fleets.on_fleet_state("gentle_fleet", robots)
    if dispatcher_heard:
        dispatcher.on_dispatch_states(
            list((active or {}).items()), list((finished or {}).items())
        )
    return clock, fleets, dispatcher


def _judge(task_id, status, robot, floor, fleet="gentle_fleet"):
    clock, fleets, dispatcher = floor
    return judge(
        task_id,
        status,
        fleet if robot else None,
        robot,
        dispatcher.snapshot(),
        fleets.fleets(),
        clock.t,
    )


class TestProvenDeadOrLeftAlone(unittest.TestCase):
    # ---- known good: never closed, however old
    def test_a_mission_queued_on_a_robot_that_exists_is_kept(self):
        """Waiting 61 minutes behind its robot's task (F-435): the fleet
        re-announces a queued mission only when the queue changes."""
        verdict, why = _judge(
            "patrol.dispatch-9", "queued", "gentle_bot_1", _floor(heard_for=61 * 60)
        )
        self.assertEqual(KEEP, verdict)
        self.assertIn("may be waiting in its queue", why)

    def test_a_mission_its_robot_is_running_is_kept(self):
        verdict, why = _judge("patrol.dispatch-1", "underway", "gentle_bot_1", _floor())
        self.assertEqual((KEEP, "[gentle_bot_1] is running it"), (verdict, why))

    def test_a_mission_in_the_dispatchers_queue_is_kept(self):
        floor = _floor(active={"patrol.dispatch-9": live_floor.QUEUED})
        self.assertEqual(KEEP, _judge("patrol.dispatch-9", "queued", None, floor)[0])

    def test_an_awarded_mission_whose_assignment_is_not_in_yet_is_kept(self):
        floor = _floor(finished={"patrol.dispatch-9": live_floor.DISPATCHED})
        verdict, why = _judge("patrol.dispatch-9", "queued", None, floor)
        self.assertEqual(KEEP, verdict)
        self.assertIn("a fleet holds it", why)

    def test_a_standby_charge_row_of_a_robot_in_the_fleet_is_kept(self):
        """The F-12 reaper's row, not this janitor's."""
        self.assertEqual(
            KEEP, _judge("ChargeBattery-1", "standby", "gentle_bot_2", _floor())[0]
        )

    def test_a_robot_that_just_changed_task_is_not_proof(self):
        clock, fleets, dispatcher = _floor()
        fleets.on_fleet_state(
            "gentle_fleet",
            {"gentle_bot_1": {"task_id": "patrol.dispatch-2", "status": "working"}},
        )
        clock.t += 30
        fleets.on_fleet_state(
            "gentle_fleet",
            {"gentle_bot_1": {"task_id": "patrol.dispatch-2", "status": "working"}},
        )
        self.assertEqual(
            KEEP,
            _judge(
                "patrol.dispatch-1",
                "underway",
                "gentle_bot_1",
                (clock, fleets, dispatcher),
            )[0],
        )

    # ---- known bad: cleared, with the proof named
    def test_the_charge_row_of_a_robot_that_left_the_fleet_is_dead(self):
        """Twelve robots became six: the rows of the six that left."""
        verdict, why = _judge("ChargeBattery-7", "underway", "gentle_bot_7", _floor())
        self.assertEqual(DEAD, verdict)
        self.assertIn("robot gone: [gentle_bot_7] is not in the fleet", why)

    def test_a_robot_listed_until_it_left_is_dead_after_the_absence(self):
        clock, fleets, dispatcher = _floor(
            robots={
                "gentle_bot_1": {"task_id": "", "status": "idle"},
                "gentle_bot_7": {"task_id": "ChargeBattery-7", "status": "charging"},
            }
        )
        left = {"gentle_bot_1": {"task_id": "", "status": "idle"}}
        for _ in range(4):
            clock.t += 20
            fleets.on_fleet_state("gentle_fleet", left)
        self.assertEqual(
            KEEP,
            _judge(
                "ChargeBattery-7",
                "underway",
                "gentle_bot_7",
                (clock, fleets, dispatcher),
            )[0],
            "80 s: not yet proof that it left",
        )
        for _ in range(3):
            clock.t += 20
            fleets.on_fleet_state("gentle_fleet", left)
        verdict, why = _judge(
            "ChargeBattery-7", "underway", "gentle_bot_7", (clock, fleets, dispatcher)
        )
        self.assertEqual(DEAD, verdict)
        self.assertIn("has not listed [gentle_bot_7] for 140 s", why)

    def test_a_row_nobody_holds_is_dead(self):
        verdict, why = _judge("patrol.dispatch-9", "queued", None, _floor())
        self.assertEqual(DEAD, verdict)
        self.assertIn("neither queued nor awarded", why)

    def test_a_row_underway_on_a_robot_doing_something_else_is_dead(self):
        verdict, why = _judge("patrol.dispatch-5", "underway", "gentle_bot_1", _floor())
        self.assertEqual(DEAD, verdict)
        self.assertIn("robot elsewhere", why)
        self.assertIn("task [patrol.dispatch-1]", why)
        verdict, why = _judge("patrol.dispatch-5", "underway", "gentle_bot_2", _floor())
        self.assertEqual(DEAD, verdict)
        self.assertIn("no task", why)

    # ---- cannot see: skipped, never convicted
    def test_no_fleet_state_heard_is_a_skip(self):
        verdict, why = _judge(
            "ChargeBattery-7", "underway", "gentle_bot_7", _floor(fleet_heard=False)
        )
        self.assertEqual(SKIP, verdict)
        self.assertIn("has not been heard by this server", why)

    def test_a_fleet_gone_quiet_is_a_skip(self):
        clock, fleets, dispatcher = _floor()
        clock.t += 60  # the core is down, or restarting
        self.assertEqual(
            SKIP,
            _judge(
                "ChargeBattery-7",
                "underway",
                "gentle_bot_7",
                (clock, fleets, dispatcher),
            )[0],
        )

    def test_a_fleet_heard_for_under_two_minutes_is_a_skip(self):
        """A server (or a fleet core) that just started: robots are still
        being added, so a missing robot proves nothing."""
        verdict, why = _judge(
            "ChargeBattery-7", "underway", "gentle_bot_7", _floor(heard_for=60)
        )
        self.assertEqual(SKIP, verdict)
        self.assertIn("robots may still be joining", why)

    def test_no_dispatcher_heard_is_a_skip(self):
        verdict, why = _judge(
            "patrol.dispatch-9", "queued", None, _floor(dispatcher_heard=False)
        )
        self.assertEqual(SKIP, verdict)
        self.assertIn("dispatcher has not been heard", why)

    def test_a_dispatcher_gone_quiet_or_new_is_a_skip(self):
        clock, fleets, dispatcher = _floor()
        clock.t += 60
        self.assertEqual(
            SKIP,
            _judge("patrol.dispatch-9", "queued", None, (clock, fleets, dispatcher))[0],
        )
        self.assertEqual(
            SKIP, _judge("patrol.dispatch-9", "queued", None, _floor(heard_for=60))[0]
        )

    def test_an_unknown_fleet_name_is_a_skip_not_a_guess(self):
        self.assertEqual(
            SKIP,
            _judge(
                "ChargeBattery-7",
                "underway",
                "gentle_bot_7",
                _floor(),
                fleet="another_fleet",
            )[0],
        )


class TestStaleTaskJanitor(AppFixture):
    """Through the database, on the real models."""

    IDS = [
        "f458-ghost-charge",
        "f458-waiting-61min",
        "f458-not-held",
        "f458-in-queue",
        "f458-fresh",
        "f458-unseen",
    ]

    def _prepare(self, portal, rows):
        async def prepare():
            for task_id, status, robot in rows:
                data = {"status": status, "booking": {"id": task_id}}
                if robot:
                    data["assigned_to"] = {"group": "gentle_fleet", "name": robot}
                await DbTaskState.update_or_create(
                    {
                        "data": data,
                        "assigned_to": robot,
                        "status": TaskStatus(status),
                    },  # stored as enum repr
                    id_=task_id,
                )
            # Backdate under auto_now's nose (raw SQL): 61 minutes for all
            # but the fresh row.
            conn = Tortoise.get_connection("default")
            await conn.execute_query(
                "UPDATE taskstate SET updated_at = '2026-01-01 00:00:00' "
                "WHERE id LIKE 'f458-%' AND id != 'f458-fresh'"
            )

        portal.call(prepare)

    def _cleanup(self, portal):
        async def cleanup():
            await DbTaskState.filter(id___in=self.IDS).delete()

        portal.call(cleanup)

    def test_only_rows_proven_dead_are_closed(self):
        portal = self.get_portal()
        logger = logging.getLogger("test-janitor")
        clock, fleets, dispatcher = _floor(active={"f458-in-queue": live_floor.QUEUED})
        self._prepare(
            portal,
            [
                ("f458-ghost-charge", "underway", "gentle_bot_7"),
                ("f458-waiting-61min", "queued", "gentle_bot_1"),
                ("f458-not-held", "queued", None),
                ("f458-in-queue", "queued", None),
                ("f458-fresh", "underway", "gentle_bot_7"),
            ],
        )

        async def sweep():
            return await fail_over_stale_tasks(1800, logger, dispatcher, fleets)

        async def status(task_id):
            row = await DbTaskState.get(id_=task_id)
            return row.status, row.data["status"]

        try:
            with self.assertLogs(logger, level="INFO") as logs:
                self.assertEqual(2, portal.call(sweep))
            failed = (str(TaskStatus.failed), "failed")
            self.assertEqual(failed, portal.call(lambda: status("f458-ghost-charge")))
            self.assertEqual(failed, portal.call(lambda: status("f458-not-held")))
            for kept in ("f458-waiting-61min", "f458-in-queue"):
                self.assertEqual(
                    (str(TaskStatus.queued), "queued"),
                    portal.call(lambda k=kept: status(k)),
                )
            self.assertEqual(
                (str(TaskStatus.underway), "underway"),
                portal.call(lambda: status("f458-fresh")),
                "a row with a recent state is not even looked at",
            )
            text = "\n".join(logs.output)
            self.assertIn("[f458-ghost-charge]", text)
            self.assertIn("PROVEN dead — robot gone", text)
            self.assertIn("[f458-waiting-61min]", text)
            self.assertIn("kept — [gentle_bot_1] is in the fleet", text)
            # idempotent, and quiet: a second pass closes nothing and says
            # nothing new about the rows it keeps
            with self.assertNoLogs(logger, level="INFO"):
                self.assertEqual(0, portal.call(sweep))
        finally:
            self._cleanup(portal)

    def test_a_server_that_has_heard_nothing_closes_nothing(self):
        """The boring environment: a fresh boot, an api-server restart, a
        fleet core that is down. The first janitor failed these rows."""
        portal = self.get_portal()
        logger = logging.getLogger("test-janitor")
        self._prepare(
            portal,
            [
                ("f458-ghost-charge", "underway", "gentle_bot_7"),
                ("f458-unseen", "queued", None),
            ],
        )

        async def sweep():
            return await fail_over_stale_tasks(
                1800, logger, live_floor.DispatcherView(), live_floor.FleetView()
            )

        async def statuses():
            return sorted(
                [
                    r.status
                    for r in await DbTaskState.filter(
                        id___in=["f458-ghost-charge", "f458-unseen"]
                    )
                ]
            )

        try:
            stale_tasks._said.clear()
            with self.assertLogs(logger, level="INFO") as logs:
                self.assertEqual(0, portal.call(sweep))
            self.assertEqual(
                sorted([str(TaskStatus.underway), str(TaskStatus.queued)]),
                portal.call(statuses),
            )
            text = "\n".join(logs.output)
            self.assertEqual(2, text.count("SKIPPED, cannot judge"))
        finally:
            self._cleanup(portal)

    def test_disabled_janitor_touches_nothing(self):
        portal = self.get_portal()
        self.assertEqual(
            0,
            portal.call(
                lambda: fail_over_stale_tasks(0, logging.getLogger("test-janitor"))
            ),
        )


if __name__ == "__main__":
    unittest.main()
