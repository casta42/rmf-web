"""G rulings 2026-10-02, second sheet, items 2 and 5 (F-465, F-464) — at the
api-server's edge, through the real dispatch path (POST /tasks/dispatch_task)
and the real ingest (routes/internal.process_msg), on the real models: only
the dispatcher is a mock.

F-465 — "a replayed dispatch request must never create a mission. Dedupe by
request ID against the ledger":
  * every dispatch writes its request id to the ledger BEFORE it is
    published and closes it with the task the dispatcher gave;
  * a restarted dispatcher's answer to that same request id, naming a NEW
    task, is a replay's: the task is canceled, the row its first state made
    is removed, its later states are refused — and the real mission and a
    new dispatch are untouched;
  * a request nobody answered in time is closed with no mission;
  * a ledger that cannot be written never stops a dispatch.

F-464 — "fix the row labels so drill 13 attributes every chain correctly":
the dispatcher's first state reaches the ledger before the dispatch call
returns; the row still ends with the labels the mission was dispatched
with — in its data and in the label rows queries match on — and keeps them
through the fleet's label-less states.
"""

import asyncio
import importlib
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from api_server import booking_labels, dispatch_ledger
from api_server import models as mdl
from api_server.models import tortoise_models as ttm
from api_server.rmf_io import tasks_service
from api_server.routes import internal
from api_server.test import AppFixture

tasks_mod = importlib.import_module("api_server.routes.tasks.tasks")

LABELS = [
    "gf:redispatch-root=patrol.dispatch-100",
    "gf:redispatch-of=patrol.dispatch-100",
    "gf:redispatch-class=hand-back",
]


def ok_reply(task_id):
    return (
        f'{{ "success": true, "state": {{ "booking": {{ "id": '
        f'"{task_id}" }}, "status": "queued" }} }}'
    )


def state_msg(task_id, status="queued", robot=None):
    data = {
        "booking": {"id": task_id, "unix_millis_earliest_start_time": 0},
        "category": "patrol",
        "detail": "description",
        "status": status,
    }
    if robot:
        data["assigned_to"] = {"group": "gentle_fleet", "name": robot}
    return {"type": "task_state_update", "data": data}


class TestDispatchReplay(AppFixture):
    def setUp(self):
        booking_labels.reset()

    def _ingest(self, msg):
        self.get_portal().call(internal.process_msg, msg, None)

    def _dispatch(self, task_id, labels=None, first_state=True, side_effect=None):
        """POST a dispatch the mock dispatcher answers as `task_id`. Its
        first state reaches the ledger BEFORE the answer, as live."""
        calls = {}

        async def call(payload, timeout=5, request_id=None):
            calls["request_id"] = request_id
            if side_effect is not None:
                raise side_effect
            if first_state:
                await internal.process_msg(state_msg(task_id), None)
            return ok_reply(task_id)

        with patch.object(tasks_service(), "call", side_effect=call):
            resp = self.client.post(
                "/tasks/dispatch_task",
                content=mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="patrol", description="description", labels=labels
                    ),
                ).model_dump_json(exclude_none=True),
            )
        return resp, calls.get("request_id")

    def _ledger(self, request_id):
        async def get():
            return await ttm.DispatchRequest.get_or_none(request_id=request_id)

        return self.get_portal().call(get)

    def _row(self, task_id):
        async def get():
            return await ttm.TaskState.get_or_none(id_=task_id)

        return self.get_portal().call(get)

    def _label_rows(self, task_id):
        async def get():
            row = await ttm.TaskState.get_or_none(id_=task_id)
            return sorted(
                [
                    f"{l.label_name}={l.label_value}"
                    for l in await ttm.TaskLabel.filter(state=row)
                ]
            )

        return self.get_portal().call(get)

    # ------------------------------------------------------------- F-465
    def test_every_dispatch_is_in_the_ledger_with_its_task(self):
        task_id = f"patrol.dispatch-{uuid4().hex[:8]}"
        resp, request_id = self._dispatch(task_id)
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertTrue(request_id)
        row = self._ledger(request_id)
        self.assertEqual(
            (dispatch_ledger.ANSWERED, task_id), (row.outcome, row.task_id)
        )
        self.assertIsNotNone(row.closed_at)

    def test_a_replayed_request_makes_no_mission(self):
        real = f"patrol.dispatch-{uuid4().hex[:8]}"
        phantom = f"patrol.dispatch-{uuid4().hex[:8]}"
        after = f"patrol.dispatch-{uuid4().hex[:8]}"
        resp, request_id = self._dispatch(real)
        self.assertEqual(200, resp.status_code, resp.content)
        # the dispatcher restarts, is handed the latched request again and
        # makes a NEW task of it: first its state, then its answer
        self._ingest(state_msg(phantom))
        self.assertIsNotNone(self._row(phantom), "its first state made a row")
        canceled = []

        async def cancel(task_id):
            canceled.append(task_id)
            return "dispatcher"

        async def no_sleep(_s):
            return None

        async def judge():
            return await dispatch_ledger.handle_unclaimed(
                request_id,
                ok_reply(phantom),
                cancel,
                dispatch_ledger._remove_row,
                sleep=no_sleep,
            )

        try:
            self.assertEqual(dispatch_ledger.REPLAY, self.get_portal().call(judge))
            self.assertEqual([phantom], canceled)
            self.assertIsNone(self._row(phantom), "the phantom is no mission")
            # whatever the dispatcher or a fleet still says about it
            self._ingest(state_msg(phantom, "failed"))
            self._ingest(state_msg(phantom, "canceled", "gentle_bot_1"))
            self.assertIsNone(self._row(phantom))
            # the real mission, and a new dispatch, are untouched
            self.assertEqual("Status.queued", str(self._row(real).status))
            self._ingest(state_msg(real, "underway", "gentle_bot_1"))
            self.assertEqual("underway", self._row(real).data["status"])
            self.assertEqual([phantom], self._ledger(request_id).refused_tasks)
            resp, _ = self._dispatch(after)
            self.assertEqual(200, resp.status_code, resp.content)
            self.assertIsNotNone(self._row(after))
        finally:
            dispatch_ledger.REFUSED.discard(phantom)

    def test_the_live_answer_of_a_pending_request_is_not_a_replay(self):
        """The both-ways rule: the guard must pass the ordinary dispatch.
        Its own answer, judged while the request is still pending."""
        task_id = f"patrol.dispatch-{uuid4().hex[:8]}"
        seen = {}

        async def call(payload, timeout=5, request_id=None):
            async def never(_task_id):
                raise AssertionError("an ordinary dispatch was canceled")

            async def no_sleep(_s):
                return None

            seen["verdict"] = await dispatch_ledger.handle_unclaimed(
                request_id, ok_reply(task_id), never, never, sleep=no_sleep
            )
            return ok_reply(task_id)

        with patch.object(tasks_service(), "call", side_effect=call):
            resp = self.client.post(
                "/tasks/dispatch_task",
                content=mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="patrol", description="description"
                    ),
                ).model_dump_json(exclude_none=True),
            )
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual(dispatch_ledger.STILL_PENDING, seen["verdict"])
        self.assertNotIn(task_id, dispatch_ledger.REFUSED)

    def test_a_request_nobody_answered_is_closed_with_no_mission(self):
        resp, request_id = self._dispatch(
            "unused", side_effect=HTTPException(500, "rmf service timed out")
        )
        self.assertEqual(500, resp.status_code)
        row = self._ledger(request_id)
        self.assertEqual(dispatch_ledger.CLOSED, row.outcome)
        self.assertIsNone(row.task_id)
        self.assertIn("rmf service timed out", row.why)

    def test_a_ledger_that_cannot_be_written_never_stops_a_dispatch(self):
        task_id = f"patrol.dispatch-{uuid4().hex[:8]}"

        async def broken(_request_id):
            raise RuntimeError("database is away")

        with patch.object(
            dispatch_ledger, "opened", side_effect=broken
        ), self.assertLogs(tasks_mod.logger, level="ERROR") as logs:
            resp, _ = self._dispatch(task_id)
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertIsNotNone(self._row(task_id))
        self.assertIn("not guarded against a replay", logs.output[0])

    def test_the_ledger_never_forgets_what_may_still_be_latched(self):
        """The latched topic replays by COUNT (its last ten requests, for
        as long as the server lives), so pruning by age alone would forget
        a week-old dispatch a quiet site can still be handed again."""
        from tortoise import Tortoise

        ids = [f"old-{uuid4().hex[:8]}" for _ in range(5)]

        async def prepare():
            await ttm.DispatchRequest.all().delete()
            for i, request_id in enumerate(ids):
                await ttm.DispatchRequest.create(
                    request_id=request_id,
                    outcome=dispatch_ledger.ANSWERED,
                    task_id=f"patrol.dispatch-{i}",
                )
                await Tortoise.get_connection("default").execute_query(
                    "UPDATE dispatchrequest SET sent_at = "
                    f"'2026-01-0{i + 1} 00:00:00' WHERE request_id = "
                    f"'{request_id}'"
                )

        async def left():
            return sorted([r.request_id for r in await ttm.DispatchRequest.all()])

        portal = self.get_portal()
        portal.call(prepare)
        # all five are months old; the three newest are kept whatever their age
        self.assertEqual(2, portal.call(dispatch_ledger.prune, 3600.0, 3))
        self.assertEqual(sorted(ids[2:]), portal.call(left))
        self.assertEqual(0, portal.call(dispatch_ledger.prune, 3600.0, 3))
        self.assertEqual(3, portal.call(dispatch_ledger.prune, 3600.0, 0))

    def test_the_log_of_a_refused_replay_is_not_kept_either(self):
        phantom = f"patrol.dispatch-{uuid4().hex[:8]}"
        dispatch_ledger.REFUSED.add(phantom, "req")
        try:
            self._ingest(
                {"type": "task_log_update", "data": {"task_id": phantom, "log": []}}
            )

            async def logs():
                return await ttm.TaskEventLog.filter(task_id=phantom).count()

            self.assertEqual(0, self.get_portal().call(logs))
        finally:
            dispatch_ledger.REFUSED.discard(phantom)

    # ------------------------------------------------------------- F-464
    def test_a_row_made_before_its_request_is_stored_still_gets_its_labels(self):
        task_id = f"patrol.dispatch-{uuid4().hex[:8]}"
        resp, _ = self._dispatch(task_id, labels=LABELS, first_state=True)
        self.assertEqual(200, resp.status_code, resp.content)
        row = self._row(task_id)
        self.assertEqual(LABELS, row.data["booking"]["labels"])
        self.assertEqual(
            sorted(LABELS),
            self._label_rows(task_id),
            "the label rows that queries match on",
        )
        # the fleet's states carry no labels on this pin: they are kept
        self._ingest(state_msg(task_id, "queued", "gentle_bot_1"))
        self._ingest(state_msg(task_id, "underway", "gentle_bot_1"))
        row = self._row(task_id)
        self.assertEqual("underway", row.data["status"])
        self.assertEqual(LABELS, row.data["booking"]["labels"])

    def test_the_first_versions_cached_miss_is_gone(self):
        """KNOWN BAD, reproduced: a state ingested before the request is
        stored, then the request stored by another path (no remember()),
        then a second state. The first version left labels null for good."""
        task_id = f"patrol.dispatch-{uuid4().hex[:8]}"
        self._ingest(state_msg(task_id))
        self.assertIsNone(self._row(task_id).data["booking"].get("labels"))

        async def store():
            await internal.task_repo.save_task_request(
                task_id,
                mdl.TaskRequest(
                    category="patrol", description="description", labels=LABELS
                ),
            )

        self.get_portal().call(store)
        self._ingest(state_msg(task_id, "queued", "gentle_bot_1"))
        self.assertEqual(LABELS, self._row(task_id).data["booking"]["labels"])

    def test_a_mission_dispatched_without_labels_has_none(self):
        task_id = f"patrol.dispatch-{uuid4().hex[:8]}"
        resp, _ = self._dispatch(task_id)
        self.assertEqual(200, resp.status_code, resp.content)
        self._ingest(state_msg(task_id, "underway", "gentle_bot_1"))
        self.assertFalse(self._row(task_id).data["booking"].get("labels"))
        self.assertEqual([], self._label_rows(task_id))
