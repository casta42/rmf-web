"""F-141 under D-86 (3a)/(4) and F-454 (G ruling 2026-10-02, item 1): a
mission a REAL coordination restart interrupted is put back on the floor,
once; nothing else is ever touched.

On release f1-n66 the sweep re-dispatched healthy queued missions 946 times
in 3 h while rmf-core never restarted: it inferred the restart from how old
and how quiet a row was. The restart is now the fleet core's stated boot
identity (core_incarnation.py), compared with the one stored in the
database. Through the real paths: missions dispatched through POST
/tasks/dispatch_task, task and fleet states fed as the fleet sends them,
the sweep the app runs every second (internal.reap_interrupted_tasks), the
ledger, GET /tasks/waiting, the alerts and the next dispatch read back.
Only the fleet is a mock (tasks_service().call); the waits are shortened.

KNOWN BAD, must act — the core states a NEW identity: every non-terminal
mission created before the new core started is handled exactly once (an
underway one, one queued on a robot, one still in the dispatcher's bidding
queue): closed canceled with the marker and the reason, listed as waiting,
re-dispatched (same root, hand-back class). One with nothing re-sendable,
and every one after an outage longer than the resume window or of unknown
length, is failed and named in a Warning. One whose cancellation was
requested is closed canceled and never sent again. A pass that dies midway
runs again and still handles each mission once. A second restart handles
what the first restart's core held, once.

KNOWN GOOD, must stay as it is — the boring cases, which is where this
failed: no identity ever stated; a first identity with nothing stored; the
SAME identity again (an api-server restart, a DDS rediscovery); a record
that cannot be read; a mission created after the new core started; a
terminal row; a start in the future; an empty ledger. (A first identity
does close a row that PREDATES the core that states it — never re-sent.) In each, a queued,
never-assigned row untouched for 400 s — the exact row f1-n66 convicted —
stays queued and is never sent again.
"""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from api_server import core_incarnation
from api_server import models as mdl
from api_server import redispatch
from api_server.interrupted_tasks import INTERRUPTED_LABEL, INTERRUPTED_REASON
from api_server.models import tortoise_models as ttm
from api_server.models.rmf_api.task_state import Cancellation
from api_server.redispatch import (
    CLASS_HAND_BACK,
    REDISPATCH_LABEL,
    class_of,
    generation_of,
    origin_of,
    root_of,
)
from api_server.repositories import FleetRepository
from api_server.rmf_io import cancellation as task_cancellation
from api_server.rmf_io import tasks_service
from api_server.routes import internal
from api_server.test import AppFixture

FLEET = "gentle_fleet"


def ok_reply(task_id):
    return f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'


def task_msg(task_id, status, assigned=None, start_ms=0):
    data = {
        "booking": {"id": task_id, "unix_millis_earliest_start_time": 0},
        "category": "patrol",
        "detail": "description",
        "status": status,
        "unix_millis_start_time": start_ms,
    }
    if assigned is not None:
        data["assigned_to"] = {"group": FLEET, "name": assigned}
    return {"type": "task_state_update", "data": data}


async def _clear_ledger():
    await ttm.TaskState.all().delete()
    await ttm.FleetState.all().delete()
    await ttm.CoreBoot.all().delete()
    await ttm.Alert.filter(original_id__startswith="interrupted__").delete()


async def _interrupted_alerts():
    return await ttm.Alert.filter(original_id__startswith="interrupted__").values_list(
        "message", flat=True
    )


async def _store_core(boot_id, started, last_heard):
    await ttm.CoreBoot.update_or_create(
        id=1,
        defaults={
            "boot_id": boot_id,
            "started_at": started,
            "last_heard_at": last_heard,
        },
    )


async def _stored_core():
    row = await ttm.CoreBoot.get_or_none(id=1)
    return None if row is None else row.boot_id


async def _corrupt(task_id):
    """A stored row whose data the model refuses."""
    await ttm.TaskState.filter(id_=task_id).update(data={"not": "a task state"})


async def _age(task_id, seconds):
    then = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    await ttm.TaskState.filter(id_=task_id).update(created_at=then, updated_at=then)


@contextmanager
def short_waits(step=0.05):
    with patch.object(redispatch, "HAND_BACK_BACKOFF_STEP_S", step), patch.object(
        redispatch, "HAND_BACK_MAX_BACKOFF_S", step
    ):
        yield


class InterruptedRequeueRouteTest(AppFixture):
    def setUp(self):
        internal.waiting.clear()
        internal._core_settled.update(  # pylint: disable=protected-access
            boot_id=None, heard_persisted=None
        )
        fresh = patch.object(
            core_incarnation, "STATE", core_incarnation.CoreIncarnation()
        )
        fresh.start()
        self.addCleanup(fresh.stop)
        self.get_portal().call(_clear_ledger)

    # -- the floor ----------------------------------------------------------------

    def new_id(self):
        return f"test.dispatch-{uuid4().hex[:10]}"

    def dispatch(self):
        task_id = self.new_id()
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(task_id)
            resp = self.client.post(
                "/tasks/dispatch_task",
                content=mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="patrol",
                        description={"places": ["s1", "s2"], "rounds": 1},
                    ),
                ).model_dump_json(exclude_none=True),
            )
        self.assertEqual(200, resp.status_code, resp.content)
        return task_id

    def ingest(self, msg):
        self.get_portal().call(internal.process_msg, msg, None)

    def fleet(self, **current):
        state = mdl.FleetState(
            name=FLEET,
            robots={
                robot: mdl.RobotState(name=robot, task_id=task)
                for robot, task in current.items()
            },
        )
        self.get_portal().call(FleetRepository(self.admin_user).save_fleet_state, state)

    def fleet_talks(self, times=5, **current):
        """Fleet states flowing through the gateway, as on a live floor.
        The health watchdog's feed is stubbed: under full discovery an
        earlier app's watchdog is still subscribed to it on a closed event
        loop (F-448's cause), and it is not what is under test here."""
        repo = FleetRepository(self.admin_user)
        with patch.object(internal.rmf_events.fleet_states, "on_next"):
            for _ in range(times):
                self.get_portal().call(
                    internal.process_msg,
                    {
                        "type": "fleet_state_update",
                        "data": {
                            "name": FLEET,
                            "robots": {
                                robot: {"name": robot, "task_id": task}
                                for robot, task in current.items()
                            },
                        },
                    },
                    repo,
                )

    def row(self, task_id):
        resp = self.client.get(f"/tasks/{task_id}/state")
        self.assertEqual(200, resp.status_code, resp.content)
        return resp.json()

    def waiting_for(self, root):
        return [
            w for w in self.client.get("/tasks/waiting").json() if w["root_id"] == root
        ]

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    # -- the core -----------------------------------------------------------------

    def after(self):
        """A moment just after everything stored so far."""
        time.sleep(0.05)
        return time.time()

    def core_was(self, boot_id="old-core", heard_ago=3.0):
        """The identity the api-server had settled before, and when that
        core was last heard (seconds ago; None: never)."""
        now = datetime.now(timezone.utc)
        heard = None if heard_ago is None else now - timedelta(seconds=heard_ago)
        self.get_portal().call(_store_core, boot_id, now - timedelta(hours=1), heard)

    def core_says(self, boot_id, started_unix):
        core_incarnation.STATE.on_boot(
            json.dumps(
                {
                    "boot_id": boot_id,
                    "started_unix": started_unix,
                    "started_source": "launch",
                }
            )
        )

    def sweep(self, replies=(), passes=1):
        """The pass the app runs every second, with the fleet mocked.
        Returns (the mock, the verdicts); waits until the fleet has been
        asked len(replies) times."""
        with short_waits(), patch.object(tasks_service(), "call") as mock:
            mock.side_effect = list(replies)
            verdicts = [
                self.get_portal().call(internal.reap_interrupted_tasks)
                for _ in range(passes)
            ]
            self.assertTrue(self.wait_for(lambda: mock.call_count >= len(replies)))
            time.sleep(0.3)  # let the re-dispatch finish what it does after the call
        return mock, verdicts

    def stored_core(self):
        return self.get_portal().call(_stored_core)

    def the_f1_n66_row(self):
        """The row f1-n66 convicted: queued, never assigned, untouched for
        400 s — a mission waiting in the dispatcher's bidding queue."""
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "queued"))
        self.get_portal().call(_age, task_id, 400)
        return task_id

    def assert_untouched(self, task_id, mock, status="queued"):
        mock.assert_not_called()
        row = self.row(task_id)
        self.assertEqual(row["status"], status)
        self.assertNotIn(INTERRUPTED_LABEL, row["booking"].get("labels") or [])
        self.assertEqual(self.waiting_for(task_id), [])
        self.assertEqual(self.get_portal().call(_interrupted_alerts), [])

    # -- known bad: a real restart, must act ----------------------------------------

    def test_FIRES_a_restart_sends_each_interrupted_mission_again_once(self):
        underway = self.dispatch()
        on_a_robot = self.dispatch()
        in_auction = self.dispatch()
        self.ingest(task_msg(underway, "underway", assigned="gentle_bot_1"))
        self.ingest(task_msg(on_a_robot, "queued", assigned="gentle_bot_1"))
        self.ingest(task_msg(in_auction, "queued"))
        self.core_was("old-core")
        self.core_says("old-core", time.time() - 3600)
        _, verdicts = self.sweep()
        self.assertEqual(verdicts, [core_incarnation.SAME])
        self.fleet_talks(gentle_bot_1=underway)
        # the core restarts: a new identity, started after those missions
        self.core_says("new-core", self.after())
        children = [self.new_id() for _ in range(3)]
        mock, verdicts = self.sweep([ok_reply(c) for c in children], passes=4)
        self.assertEqual(
            verdicts,
            [core_incarnation.RESTARTED, None, None, None],
            "one restart is settled once",
        )
        self.assertEqual(mock.call_count, 3, "each mission sent again exactly once")
        sent_roots = set()
        for call in mock.call_args_list:
            sent = json.loads(call[0][0])["request"]["labels"]
            sent_roots.add(root_of(sent))
            self.assertEqual(
                class_of(sent), CLASS_HAND_BACK, "exactly like a hand-back"
            )
            self.assertEqual(generation_of(sent), 1)
            self.assertEqual(origin_of(sent), root_of(sent))
            self.assertNotIn(
                INTERRUPTED_LABEL, sent, "the new attempt was not interrupted"
            )
        self.assertEqual(sent_roots, {underway, on_a_robot, in_auction})
        for task_id in (underway, on_a_robot, in_auction):
            row = self.row(task_id)
            self.assertEqual(row["status"], "canceled", "never failed, never Executing")
            self.assertEqual(
                row["cancellation"]["labels"], [REDISPATCH_LABEL, INTERRUPTED_REASON]
            )
            self.assertIn(INTERRUPTED_LABEL, row["booking"]["labels"])
            (waiting,) = self.waiting_for(task_id)
            self.assertIn(waiting["task_id"], children)
            self.assertEqual(waiting["reason"], INTERRUPTED_REASON)
            self.assertEqual(404, self.client.get(f"/alerts/{task_id}").status_code)
        self.assertEqual(self.stored_core(), "new-core")
        # the new attempts are the NEW core's missions: however long they
        # wait, no later pass touches them
        for child in children:
            self.ingest(task_msg(child, "queued"))
            self.get_portal().call(_age, child, 400)
        self.fleet_talks()
        mock, verdicts = self.sweep(passes=5)
        mock.assert_not_called()
        self.assertEqual(verdicts, [None] * 5)
        for child in children:
            self.assertEqual(self.row(child)["status"], "queued")

    def test_FIRES_across_an_api_server_restart(self):
        """A host reboot restarts both: this life has settled nothing, the
        database remembers the old core, the new core says who it is."""
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_2"))
        self.core_was("old-core", heard_ago=40.0)
        self.core_says("new-core", self.after())
        mock, verdicts = self.sweep([ok_reply(self.new_id())], passes=2)
        self.assertEqual(verdicts, [core_incarnation.RESTARTED, None])
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(self.row(task_id)["status"], "canceled")
        self.assertEqual(len(self.waiting_for(task_id)), 1)

    def test_FIRES_a_second_restart_handles_what_the_first_ones_core_held(self):
        root = self.dispatch()
        self.ingest(task_msg(root, "underway", assigned="gentle_bot_1"))
        self.core_was("core-a")
        self.core_says("core-b", self.after())
        child = self.new_id()
        mock, _ = self.sweep([ok_reply(child)])
        self.assertEqual(mock.call_count, 1)
        self.ingest(task_msg(child, "underway", assigned="gentle_bot_3"))
        self.fleet_talks(gentle_bot_3=child)
        self.core_says("core-c", self.after())
        grandchild = self.new_id()
        mock, verdicts = self.sweep([ok_reply(grandchild)], passes=3)
        self.assertEqual(verdicts, [core_incarnation.RESTARTED, None, None])
        self.assertEqual(mock.call_count, 1, "only what core-b held, once")
        sent = json.loads(mock.call_args[0][0])["request"]["labels"]
        self.assertEqual(root_of(sent), root)
        self.assertEqual(origin_of(sent), child)
        self.assertEqual(generation_of(sent), 2)
        self.assertEqual(self.row(child)["status"], "canceled")
        self.assertEqual(self.stored_core(), "core-c")

    def test_FIRES_nothing_re_sendable_fails_with_the_reason_named(self):
        direct = self.new_id()  # never dispatched through the api-server
        self.ingest(task_msg(direct, "underway", assigned="gentle_bot_3"))
        self.core_was("old-core")
        self.core_says("new-core", self.after())
        mock, _ = self.sweep()
        mock.assert_not_called()
        row = self.row(direct)
        self.assertEqual(row["status"], "failed")
        self.assertIn(INTERRUPTED_LABEL, row["booking"]["labels"])
        self.assertEqual(self.waiting_for(direct), [])
        alerts = self.get_portal().call(_interrupted_alerts)
        (message,) = [m for m in alerts if direct in m]
        self.assertIn("requests are not stored", message)

    def test_FIRES_a_long_outage_closes_the_missions_and_never_resends(self):
        """A database restored from an old backup, a fleet PC off over the
        weekend: the missions are closed with the reason, an operator
        decides — they are not sent to the floor days later."""
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        self.core_was("old-core", heard_ago=internal.RESUME_WINDOW_S + 600)
        self.core_says("new-core", self.after())
        mock, verdicts = self.sweep(passes=2)
        self.assertEqual(verdicts, [core_incarnation.RESTARTED, None])
        mock.assert_not_called()
        row = self.row(task_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn(INTERRUPTED_LABEL, row["booking"]["labels"])
        self.assertEqual(self.waiting_for(task_id), [])
        (message,) = [
            m for m in self.get_portal().call(_interrupted_alerts) if task_id in m
        ]
        self.assertIn("resume window", message)

    def test_FIRES_an_unknown_outage_is_never_read_as_a_short_one(self):
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        self.core_was("old-core", heard_ago=None)
        self.core_says("new-core", self.after())
        mock, _ = self.sweep()
        mock.assert_not_called()
        self.assertEqual(self.row(task_id)["status"], "failed")

    def test_FIRES_a_mission_whose_cancellation_was_requested_stays_canceled(self):
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        task_cancellation.latch(
            task_id,
            Cancellation(
                unix_millis_request_time=1, labels=["canceled from mission queue by g"]
            ),
        )
        self.core_was("old-core")
        self.core_says("new-core", self.after())
        mock, _ = self.sweep()
        mock.assert_not_called()
        row = self.row(task_id)
        self.assertEqual(row["status"], "canceled")
        self.assertEqual(
            row["cancellation"]["labels"], ["canceled from mission queue by g"]
        )
        self.assertEqual(self.waiting_for(task_id), [])

    def test_FIRES_the_new_core_heard_before_its_boot_record(self):
        """The new core's fleet states (the websocket) can reach the
        gateway before its boot record (DDS discovery). A pass in that gap
        must not store the new core's stamp as "the core was last heard":
        the old core's last word would be gone, the outage unknown, and
        every interrupted mission failed instead of sent again."""
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        self.core_was("old-core", heard_ago=40.0)
        self.core_says("old-core", time.time() - 3600)
        self.sweep()  # settled: the old core
        started = self.after()  # the new core starts
        time.sleep(0.05)
        self.fleet_talks(gentle_bot_1="")  # ... and is heard first
        _, verdicts = self.sweep(passes=2)  # passes in the gap
        self.assertEqual(verdicts, [None, None])
        self.assertEqual(self.row(task_id)["status"], "underway")
        self.core_says("new-core", started)  # the record arrives
        mock, verdicts = self.sweep([ok_reply(self.new_id())])
        self.assertEqual(verdicts, [core_incarnation.RESTARTED])
        self.assertEqual(mock.call_count, 1, "sent again, not failed")
        self.assertEqual(self.row(task_id)["status"], "canceled")
        self.assertEqual(len(self.waiting_for(task_id)), 1)

    def test_FIRES_every_row_is_reached_past_the_ones_left_alone(self):
        """The fleet's own charge task and a row that cannot be read sit
        among the interrupted missions: neither stops the rest."""
        charge = "Charge" + uuid4().hex[:6]
        self.ingest(task_msg(charge, "underway", assigned="gentle_bot_9"))
        missions = []
        for n in range(5):
            missions.append(self.dispatch())
            self.ingest(task_msg(missions[-1], "underway", assigned=f"gentle_bot_{n}"))
        self.get_portal().call(_corrupt, missions[1])
        self.core_was("old-core")
        self.core_says("new-core", self.after())
        good = [m for m in missions if m != missions[1]]
        mock, _ = self.sweep([ok_reply(self.new_id()) for _ in good], passes=2)
        self.assertEqual(mock.call_count, len(good))
        for task_id in good:
            self.assertEqual(self.row(task_id)["status"], "canceled")
        self.assertEqual(self.row(charge)["status"], "underway", "the F-12 reaper's")

    def test_FIRES_the_warning_survives_a_pass_that_dies(self):
        """Rows closed failed are terminal: the next pass does not see
        them, so the pass that closed them must still tell the operator."""
        first, second = self.new_id(), self.new_id()  # direct: nothing re-sendable
        self.ingest(task_msg(first, "underway", assigned="gentle_bot_1"))
        time.sleep(0.02)
        self.ingest(task_msg(second, "underway", assigned="gentle_bot_2"))
        self.core_was("old-core")
        self.core_says("new-core", self.after())
        real_save = internal.task_repo.save_task_state
        saves = []

        async def dies_on_the_second(task_state):
            saves.append(task_state.booking.id)
            if len(saves) == 2:
                raise RuntimeError("the database went away")
            return await real_save(task_state)

        with patch.object(internal.task_repo, "save_task_state", dies_on_the_second):
            with self.assertRaises(RuntimeError):
                self.get_portal().call(internal.reap_interrupted_tasks)
        alerts = self.get_portal().call(_interrupted_alerts)
        self.assertTrue(any(first in m for m in alerts), alerts)
        self.assertFalse(any(second in m for m in alerts), alerts)
        self.assertEqual(
            self.get_portal().call(internal.reap_interrupted_tasks),
            core_incarnation.RESTARTED,
        )
        alerts = self.get_portal().call(_interrupted_alerts)
        self.assertTrue(any(second in m for m in alerts), alerts)
        for task_id in (first, second):
            self.assertEqual(self.row(task_id)["status"], "failed")

    def test_FIRES_a_pass_that_dies_midway_runs_again_each_mission_once(self):
        """The identity is stored only after a whole pass, and a closed row
        is terminal: the second pass finishes the job, nothing is repeated."""
        first, second = self.dispatch(), self.dispatch()
        self.ingest(task_msg(first, "underway", assigned="gentle_bot_1"))
        time.sleep(0.02)
        self.ingest(task_msg(second, "underway", assigned="gentle_bot_2"))
        self.core_was("old-core")
        self.core_says("new-core", self.after())
        real_save = internal.task_repo.save_task_state
        saves = []

        async def dies_on_the_second(task_state):
            saves.append(task_state.booking.id)
            if len(saves) == 2:
                raise RuntimeError("the database went away")
            return await real_save(task_state)

        with short_waits(), patch.object(tasks_service(), "call") as mock:
            mock.side_effect = [ok_reply(self.new_id()), ok_reply(self.new_id())]
            with patch.object(
                internal.task_repo, "save_task_state", dies_on_the_second
            ):
                with self.assertRaises(RuntimeError):
                    self.get_portal().call(internal.reap_interrupted_tasks)
            self.assertEqual(self.stored_core(), "old-core", "not settled yet")
            self.assertEqual(
                self.get_portal().call(internal.reap_interrupted_tasks),
                core_incarnation.RESTARTED,
            )
            self.assertTrue(self.wait_for(lambda: mock.call_count >= 2))
            time.sleep(0.3)
            self.assertEqual(mock.call_count, 2, "each mission sent again once")
        self.assertEqual(self.stored_core(), "new-core")
        for task_id in (first, second):
            self.assertEqual(self.row(task_id)["status"], "canceled")
            self.assertEqual(len(self.waiting_for(task_id)), 1)

    # -- known good: the boring cases, must stay as they are --------------------------

    def test_PASSES_no_identity_ever_stated_touches_nothing(self):
        """The F-454 case itself. On f1-n66 this row was closed and sent
        again every five minutes, 946 times in 3 h, with rmf-core never
        restarting: this test would have FAILED there. No core identity is
        stated here (an idle fleet, a core older than the record), fleet
        states flow, and the pass runs as it does every second."""
        task_id = self.the_f1_n66_row()
        underway = self.dispatch()
        self.ingest(task_msg(underway, "underway", assigned="gentle_bot_1"))
        self.get_portal().call(_age, underway, 4000)
        self.fleet_talks(gentle_bot_1="")
        mock, verdicts = self.sweep(passes=5)
        self.assertEqual(verdicts, [None] * 5)
        self.assert_untouched(task_id, mock)
        self.assertEqual(self.row(underway)["status"], "underway")
        self.assertIsNone(self.stored_core())

    def test_PASSES_the_same_core_touches_nothing(self):
        """rmf-core runs on; the api-server restarted (or DDS rediscovered
        the latched record) and reads the identity it already stored."""
        task_id = self.the_f1_n66_row()
        self.core_was("the-core")
        self.core_says("the-core", time.time() - 7200)
        self.fleet_talks()
        mock, verdicts = self.sweep(passes=3)
        self.assertEqual(verdicts, [core_incarnation.SAME, None, None])
        self.assert_untouched(task_id, mock)
        # read again, any number of times: still the same core
        self.core_says("the-core", time.time() - 7200)
        mock, verdicts = self.sweep(passes=2)
        self.assertEqual(verdicts, [None, None])
        self.assert_untouched(task_id, mock)

    def test_PASSES_a_first_identity_with_nothing_stored_touches_nothing(self):
        """A first run with no history — and an upgrade from a release that
        stored no identity — on a floor whose missions are this core's own
        (it started two hours ago; they came after): nothing is touched,
        however long the f1-n66 row has been waiting."""
        task_id = self.the_f1_n66_row()
        self.core_says("first-core", time.time() - 7200)
        mock, verdicts = self.sweep(passes=2)
        self.assertEqual(verdicts, [core_incarnation.FIRST, None])
        self.assert_untouched(task_id, mock)
        self.assertEqual(self.stored_core(), "first-core")

    def test_FIRES_a_first_identity_closes_what_predates_the_core(self):
        """An upgrade from a release that stored no identity, with a
        mission in flight; a database restored from before F-454. No old
        identity to compare — but a row created before THIS core started
        is not this core's. When its core went is unknown, so it is closed
        and named, never sent again; before this it stayed 'underway' and
        un-cancelable until the 30-min janitor failed it without a word."""
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        self.core_says("first-core", self.after())
        mock, verdicts = self.sweep(passes=2)
        self.assertEqual(verdicts, [core_incarnation.FIRST, None])
        mock.assert_not_called()
        row = self.row(task_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn(INTERRUPTED_LABEL, row["booking"]["labels"])
        self.assertEqual(self.waiting_for(task_id), [])
        (message,) = [
            m for m in self.get_portal().call(_interrupted_alerts) if task_id in m
        ]
        self.assertIn("nobody can say how long", message)
        self.assertEqual(self.stored_core(), "first-core")

    def test_PASSES_a_record_that_cannot_be_read_touches_nothing(self):
        task_id = self.the_f1_n66_row()
        self.core_was("old-core")
        core_incarnation.STATE.on_boot("not a record")
        core_incarnation.STATE.on_boot('{"boot_id": "", "started_unix": 5}')
        mock, verdicts = self.sweep(passes=2)
        self.assertEqual(verdicts, [None, None])
        self.assert_untouched(task_id, mock)
        self.assertEqual(self.stored_core(), "old-core")

    def test_PASSES_a_mission_created_after_the_new_core_started(self):
        """The new core's own mission — dispatched before the api-server
        had read the new identity — is never one the restart interrupted."""
        self.core_was("old-core")
        started = time.time() - 30
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "queued"))
        self.core_says("new-core", started)
        mock, verdicts = self.sweep(passes=2)
        self.assertEqual(verdicts, [core_incarnation.RESTARTED, None])
        self.assert_untouched(task_id, mock)
        self.assertEqual(self.stored_core(), "new-core")

    def test_PASSES_a_terminal_row_and_an_empty_ledger(self):
        done = self.dispatch()
        self.ingest(task_msg(done, "completed", assigned="gentle_bot_1"))
        self.core_was("old-core")
        self.core_says("new-core", self.after())
        mock, verdicts = self.sweep(passes=2)
        self.assertEqual(verdicts, [core_incarnation.RESTARTED, None])
        mock.assert_not_called()
        self.assertEqual(self.row(done)["status"], "completed")
        self.assertEqual(self.get_portal().call(_interrupted_alerts), [])
        # and a restart over an empty ledger closes nothing at all
        self.get_portal().call(_clear_ledger)
        self.core_was("new-core")
        self.core_says("newer-core", self.after())
        mock, verdicts = self.sweep()
        self.assertEqual(verdicts, [core_incarnation.RESTARTED])
        mock.assert_not_called()
        self.assertEqual(self.get_portal().call(_interrupted_alerts), [])

    def test_PASSES_a_start_in_the_future_sweeps_nothing(self):
        """The clocks disagree: nothing can be said about which rows
        predate the new core, so nothing is convicted."""
        task_id = self.the_f1_n66_row()
        self.core_was("old-core")
        self.core_says("new-core", time.time() + 3600)
        mock, verdicts = self.sweep(passes=2)
        self.assertEqual(verdicts, [core_incarnation.RESTARTED, None])
        self.assert_untouched(task_id, mock)

    def test_PASSES_fleet_states_alone_never_run_the_sweep(self):
        """The sweep used to run from the fleet-state handler, on a rolling
        age. A gap in the stream and its return, then steady flow, with the
        f1-n66 row on the floor and no pass called: nothing happens."""
        task_id = self.the_f1_n66_row()
        with patch.object(tasks_service(), "call") as mock:
            self.fleet_talks(times=3)
            time.sleep(0.2)
            self.fleet_talks(times=3)
            self.assert_untouched(task_id, mock)
        for gone in ("sweep_stale_tasks", "_run_boundary", "STALE_TASK_SWEEP_AGE"):
            self.assertFalse(hasattr(internal, gone), f"{gone} is back (F-454)")
