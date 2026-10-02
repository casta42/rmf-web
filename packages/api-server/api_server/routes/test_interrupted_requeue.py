"""F-141 under D-86 (3a) and (4) (G close-out rulings 2026-10-01): a mission
a coordination restart interrupted is NOT failed. Its row is closed like a
hand-back and it is re-dispatched as the next attempt of its chain, waiting
for a robot until it is placed. Only a mission with nothing re-sendable is
failed, with the reason named. Through the real paths: missions dispatched
through POST /tasks/dispatch_task, task and fleet states fed as the fleet
sends them, the closure the outage reaper and the stale sweep run, the
ledger, GET /tasks/waiting, the alerts and the next dispatch read back. Only
the fleet is a mock (tasks_service().call); the waits are shortened.

KNOWN BAD, must act: an underway mission the restarted core does not know
is closed canceled with the marker and the reason, listed as waiting, and
re-dispatched (same root, hand-back class); a queued mission whose robot's
active task was lost with it is too; a mission with nothing re-sendable is
failed and named in a Warning.

KNOWN GOOD, must stay as it is: a mission re-announced after the restart, a
mission a robot's fleet state still names, a mission queued behind a task
the fleet still runs (sending it again would run it twice), a mission whose
cancellation was requested (closed canceled, never sent again), and — the
boring case — no outage at all: nothing is touched.
"""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from api_server import models as mdl
from api_server import redispatch
from api_server.interrupted_tasks import (
    INTERRUPTED_LABEL,
    INTERRUPTED_REASON,
    RunBoundary,
)
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


async def _clear_ledger(fleets=True):
    await ttm.TaskState.all().delete()
    if fleets:
        await ttm.FleetState.all().delete()


async def _interrupted_alerts():
    return await ttm.Alert.filter(original_id__startswith="interrupted__").values_list(
        "message", flat=True
    )


@contextmanager
def short_waits(step=0.05):
    with patch.object(redispatch, "HAND_BACK_BACKOFF_STEP_S", step), patch.object(
        redispatch, "HAND_BACK_MAX_BACKOFF_S", step
    ):
        yield


class InterruptedRequeueRouteTest(AppFixture):
    def setUp(self):
        internal.waiting.clear()
        internal._lost_active.clear()  # pylint: disable=protected-access
        internal._left_alone.clear()  # pylint: disable=protected-access
        self.get_portal().call(_clear_ledger)

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

    def fleet(self, clock_ms=None, **current):
        """The fleet's state as it publishes it: robot -> current task, and
        the robots' clock (RMF's time) when given."""
        state = mdl.FleetState(
            name=FLEET,
            robots={
                robot: mdl.RobotState(
                    name=robot, task_id=task, unix_millis_time=clock_ms
                )
                for robot, task in current.items()
            },
        )
        self.get_portal().call(FleetRepository(self.admin_user).save_fleet_state, state)

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

    def close(self, epoch, replies=()):
        """The closure the reaper and the sweep run, with the fleet mocked.
        Returns the mock; waits until it has been asked len(replies) times."""
        with short_waits(), patch.object(tasks_service(), "call") as mock:
            mock.side_effect = list(replies)
            self.get_portal().call(internal._close_interrupted_rows, epoch)
            self.assertTrue(self.wait_for(lambda: mock.call_count >= len(replies)))
            time.sleep(0.3)  # let the re-dispatch finish what it does after the call
        return mock

    def after(self):
        """An epoch just after everything stored so far."""
        time.sleep(0.05)
        return datetime.now(timezone.utc)

    # -- known bad: must act ----------------------------------------------------

    def test_FIRES_an_interrupted_mission_waits_and_is_sent_again(self):
        root = self.dispatch()
        self.ingest(task_msg(root, "underway", assigned="gentle_bot_1"))
        self.fleet(gentle_bot_1="")  # the restarted core: the robot is idle
        child = self.new_id()
        mock = self.close(self.after(), [ok_reply(child)])
        self.assertEqual(mock.call_count, 1)
        row = self.row(root)
        self.assertEqual(row["status"], "canceled", "never failed, never Executing")
        self.assertEqual(
            row["cancellation"]["labels"], [REDISPATCH_LABEL, INTERRUPTED_REASON]
        )
        self.assertIn(INTERRUPTED_LABEL, row["booking"]["labels"])
        sent = json.loads(mock.call_args[0][0])["request"]["labels"]
        self.assertEqual(origin_of(sent), root)
        self.assertEqual(root_of(sent), root)
        self.assertEqual(class_of(sent), CLASS_HAND_BACK, "exactly like a hand-back")
        self.assertEqual(generation_of(sent), 1)
        self.assertNotIn(INTERRUPTED_LABEL, sent, "the new attempt was not interrupted")
        (waiting,) = self.waiting_for(root)
        self.assertEqual(waiting["task_id"], child)
        self.assertEqual(waiting["reason"], INTERRUPTED_REASON)
        self.assertEqual(404, self.client.get(f"/alerts/{root}").status_code)

    def test_FIRES_a_queued_mission_lost_with_its_robot_s_active_task(self):
        active = self.dispatch()
        queued = self.dispatch()
        self.ingest(task_msg(queued, "queued", assigned="gentle_bot_1"))
        self.ingest(task_msg(active, "underway", assigned="gentle_bot_1"))
        self.fleet(gentle_bot_1="")
        mock = self.close(
            self.after(), [ok_reply(self.new_id()), ok_reply(self.new_id())]
        )
        self.assertEqual(mock.call_count, 2)
        for task_id in (active, queued):
            self.assertEqual(self.row(task_id)["status"], "canceled")
            self.assertEqual(len(self.waiting_for(task_id)), 1)

    def test_FIRES_and_PASSES_due_is_judged_on_the_sim_clock(self):
        """A queued mission on an idle robot: its start is RMF's time — the
        sim clock, minutes since bringup — and is judged on the robot's own
        clock, never against the api-server's wall clock."""
        task_id = self.dispatch()
        self.ingest(
            task_msg(task_id, "queued", assigned="gentle_bot_4", start_ms=100_000)
        )
        epoch = self.after()
        # 20 s past its start on the robot's clock: it may start any moment
        self.fleet(clock_ms=120_000, gentle_bot_4="")
        self.close(epoch).assert_not_called()
        self.assertEqual(self.row(task_id)["status"], "queued")
        # 5 min past on the robot's clock, the robot idle: the core lost it
        self.fleet(clock_ms=400_000, gentle_bot_4="")
        self.assertEqual(self.close(epoch, [ok_reply(self.new_id())]).call_count, 1)
        self.assertEqual(self.row(task_id)["status"], "canceled")
        self.assertEqual(len(self.waiting_for(task_id)), 1)

    def test_FIRES_nothing_re_sendable_fails_with_the_reason_named(self):
        direct = self.new_id()  # never dispatched through the api-server
        self.ingest(task_msg(direct, "underway", assigned="gentle_bot_3"))
        self.fleet(gentle_bot_3="")
        mock = self.close(self.after())
        mock.assert_not_called()
        row = self.row(direct)
        self.assertEqual(row["status"], "failed")
        self.assertIn(INTERRUPTED_LABEL, row["booking"]["labels"])
        self.assertEqual(self.waiting_for(direct), [])
        alerts = self.get_portal().call(_interrupted_alerts)
        (message,) = [m for m in alerts if direct in m]
        self.assertIn("cannot be sent again", message)
        self.assertIn("requests are not stored", message)

    # -- known good: must stay as it is -------------------------------------------

    def test_PASSES_a_mission_re_announced_after_the_restart(self):
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        epoch = self.after()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        self.fleet(gentle_bot_1="")
        self.close(epoch).assert_not_called()
        self.assertEqual(self.row(task_id)["status"], "underway")
        self.assertEqual(self.waiting_for(task_id), [])

    def test_PASSES_a_mission_a_robot_s_fleet_state_still_names(self):
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        self.fleet(gentle_bot_1=task_id)
        self.close(self.after()).assert_not_called()
        self.assertEqual(self.row(task_id)["status"], "underway")

    def test_PASSES_a_mission_queued_behind_a_task_the_fleet_still_runs(self):
        """Its silence is no evidence: a queued task is re-announced only
        when its queue changes. Sending it again would run it twice."""
        running = self.dispatch()
        queued = self.dispatch()
        self.ingest(
            task_msg(
                queued,
                "queued",
                assigned="gentle_bot_2",
                start_ms=round(time.time() * 1e3) - 600_000,
            )
        )
        epoch = self.after()
        self.ingest(task_msg(running, "underway", assigned="gentle_bot_2"))
        self.fleet(gentle_bot_2=running)
        self.close(epoch).assert_not_called()
        self.assertEqual(self.row(queued)["status"], "queued")
        self.assertEqual(self.row(running)["status"], "underway")
        self.assertEqual(self.waiting_for(queued), [])

    def test_PASSES_a_mission_whose_cancellation_was_requested(self):
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        task_cancellation.latch(
            task_id,
            Cancellation(
                unix_millis_request_time=1, labels=["canceled from mission queue by g"]
            ),
        )
        self.fleet(gentle_bot_1="")
        self.close(self.after()).assert_not_called()
        row = self.row(task_id)
        self.assertEqual(row["status"], "canceled")
        self.assertEqual(
            row["cancellation"]["labels"], ["canceled from mission queue by g"]
        )
        self.assertEqual(self.waiting_for(task_id), [])

    def test_PASSES_the_boring_case_no_outage_touches_nothing(self):
        task_id = self.dispatch()
        self.ingest(task_msg(task_id, "underway", assigned="gentle_bot_1"))
        boundary = RunBoundary()
        with patch.object(internal, "_run_boundary", boundary), patch.object(
            tasks_service(), "call"
        ) as mock:
            # a fleet publishing steadily: no silence, no epoch
            for _ in range(5):
                boundary.observe(time.monotonic(), datetime.now(timezone.utc))
                self.get_portal().call(internal.reap_interrupted_tasks)
            mock.assert_not_called()
        self.assertEqual(self.row(task_id)["status"], "underway")
        self.assertEqual(self.waiting_for(task_id), [])
        # and an empty ledger closes nothing at all
        self.get_portal().call(_clear_ledger, False)
        self.close(self.after() + timedelta(hours=1)).assert_not_called()
