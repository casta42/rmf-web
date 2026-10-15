"""D-114 (2) — the api-server's half of the audit: no dispatch decision is
made from "not found" in a list, cache or window. Each decision reads the
mission's own record, which lives as long as the mission; FIRES fails on
f1-n86's code (071d7837), PASSES holds both ways.

  * the cancellation latch (rmf_io/cancellation.py) was WIPED at 5000
    entries, live ones with them — F-292's follow-through and the restart
    sweep then read "nobody asked it to stop";
  * the restart sweep read only that in-memory latch, empty after an
    api-server restart — an operator's cancel already saved on the task's
    own row was sent again (test_interrupted_requeue.py, D-114 case);
  * the re-dispatcher's memory of what it sent (2048 ids, empty after a
    restart) was the only guard against sending one attempt twice — the
    ledger already says when an attempt was replaced;
  * the resume scan read the newest RESUME_SCAN_LIMIT rows of its window,
    and a waiting tail older than them was dropped.
"""

import json
import time
from unittest.mock import patch
from uuid import uuid4

from api_server import models as mdl
from api_server import redispatch
from api_server.models.rmf_api.task_state import Cancellation
from api_server.redispatch import ORIGIN_LABEL, REDISPATCH_LABEL, ROOT_LABEL
from api_server.rmf_io import cancellation as task_cancellation
from api_server.rmf_io import tasks_service
from api_server.routes import internal
from api_server.test import AppFixture, make_task_state

from .test_waiting_missions_route import (
    HOLD,
    hand_back_msg,
    ok_reply,
    short_waits,
    task_msg,
)

CANCEL = Cancellation(
    unix_millis_request_time=1, labels=["canceled from mission queue by g"]
)


def _restart_latch():
    """What an api-server restart does to the in-memory latch."""
    task_cancellation._latched.clear()
    getattr(task_cancellation, "_ended_at", {}).clear()


def _state(task_id, status):
    st = make_task_state(task_id)
    st.status = mdl.TaskStatus(status)
    st.cancellation = None
    return st


class D114DispatchRecordsTest(AppFixture):
    def setUp(self):
        _restart_latch()
        internal.waiting.clear()

    def new_id(self):
        return f"patrol.dispatch-{uuid4().hex[:10]}"

    def save(self, task_id, status, labels=None, cancellation=None):
        data = task_msg(task_id, status, cancellation)["data"]
        if labels is not None:
            data["booking"]["labels"] = labels
        self.get_portal().call(
            internal.task_repo.save_task_state, mdl.TaskState(**data)
        )

    def save_request(self, task_id, labels):
        self.get_portal().call(
            internal.task_repo.save_task_request,
            task_id,
            mdl.TaskRequest(
                category="patrol",
                description={"places": ["s9"], "rounds": 1},
                labels=labels,
            ),
        )

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    # -- the cancellation latch -------------------------------------------
    def test_FIRES_a_live_cancellation_outlives_ten_thousand_ended_ones(self):
        task_cancellation.latch("live-mission", CANCEL)
        for k in range(10000):
            tid = f"ended-{k}"
            task_cancellation.latch(tid, CANCEL)
            task_cancellation.apply(_state(tid, "canceled"))
        self.assertTrue(task_cancellation.requested("live-mission"))

    def test_PASSES_an_ended_cancellation_still_stamps_a_re_sent_state(self):
        task_cancellation.latch("m-1", CANCEL)
        task_cancellation.apply(_state("m-1", "canceled"))
        again = _state("m-1", "canceled")  # re-sent, its field empty
        task_cancellation.apply(again)
        self.assertIsNotNone(again.cancellation)

    # -- the re-dispatcher's memory -----------------------------------------
    def test_FIRES_an_attempt_already_replaced_is_never_sent_again(self):
        """A hand-back put back on the floor (its successor stored with
        gf:redispatch-of); the api-server restarted; the fleet re-sends the
        attempt's terminal state — it is not dispatched a second time."""
        parent, child = self.new_id(), self.new_id()
        self.save_request(parent, ["gf:kind=patrol"])
        self.save(parent, "canceled", ["gf:kind=patrol"], [REDISPATCH_LABEL, HOLD])
        child_labels = [
            "gf:kind=patrol",
            f"{ORIGIN_LABEL}{parent}",
            f"{ROOT_LABEL}{parent}",
        ]
        self.save_request(child, child_labels)
        self.save(child, "queued", child_labels)
        sent = []

        async def dispatch(request):
            sent.append(request)
            return self.new_id()

        async def load(task_id):
            return mdl.TaskRequest(
                category="patrol",
                description={"places": ["s9"]},
                labels=["gf:kind=patrol"],
            )

        kwargs = dict(wanted=lambda _t: True)
        if "replaced" in redispatch.Redispatcher.__init__.__code__.co_varnames:
            kwargs["replaced"] = internal._replaced_in_ledger
        fresh = redispatch.Redispatcher(dispatch, load, internal.logger, **kwargs)
        with short_waits():
            got = self.get_portal().call(
                fresh.maybe_redispatch,
                parent,
                "canceled",
                [REDISPATCH_LABEL, HOLD],
                ["gf:kind=patrol"],
            )
        self.assertIsNone(got)
        self.assertEqual(sent, [])

    def test_PASSES_an_attempt_not_yet_replaced_is_sent(self):
        parent = self.new_id()
        self.save_request(parent, ["gf:kind=patrol"])
        self.save(parent, "canceled", ["gf:kind=patrol"], [REDISPATCH_LABEL, HOLD])
        sent = []

        async def dispatch(request):
            sent.append(request)
            return self.new_id()

        async def load(task_id):
            return mdl.TaskRequest(
                category="patrol",
                description={"places": ["s9"]},
                labels=["gf:kind=patrol"],
            )

        kwargs = dict(wanted=lambda _t: True)
        if "replaced" in redispatch.Redispatcher.__init__.__code__.co_varnames:
            kwargs["replaced"] = internal._replaced_in_ledger
        fresh = redispatch.Redispatcher(dispatch, load, internal.logger, **kwargs)
        with short_waits():
            got = self.get_portal().call(
                fresh.maybe_redispatch,
                parent,
                "canceled",
                [REDISPATCH_LABEL, HOLD],
                ["gf:kind=patrol"],
            )
        self.assertIsNotNone(got)
        self.assertEqual(len(sent), 1)

    def test_PASSES_a_ledger_that_cannot_find_the_attempt_convicts_nothing(self):
        if not hasattr(internal, "_replaced_in_ledger"):
            self.skipTest("no ledger check in this tree")
        self.assertIsNone(
            self.get_portal().call(
                internal._replaced_in_ledger, "patrol.dispatch-unknown"
            )
        )

    # -- the resume scan ----------------------------------------------------
    def test_FIRES_a_waiting_tail_behind_many_newer_rows_is_resumed(self):
        """A tail (handed back, the restart inside its wait) with more newer
        rows in the window than the old scan limit read: still resumed. The
        limit is set to 10 where it exists (f1-n86), the rows to 12."""
        tail = self.new_id()
        self.save_request(tail, ["gf:kind=patrol"])
        self.save(tail, "canceled", ["gf:kind=patrol"], [REDISPATCH_LABEL, HOLD])
        time.sleep(0.01)
        for _ in range(12):
            other = self.new_id()
            self.save_request(other, ["gf:kind=patrol"])
            self.save(other, "completed", ["gf:kind=patrol"])
        internal.waiting.clear()
        new_ids = iter(self.new_id() for _ in range(20))
        with patch.object(
            internal, "RESUME_SCAN_LIMIT", 10, create=True
        ), short_waits(), patch.object(tasks_service(), "call") as mock:
            mock.side_effect = lambda *a, **k: ok_reply(next(new_ids))
            resumed = self.get_portal().call(internal.resume_waiting_chains)
        self.assertIn(tail, resumed)


class D114WithdrawalTest(AppFixture):
    """An operator canceled a mission while it waited between auctions;
    the api-server restarted; the fleet re-sends the attempt's hand-back
    state: the attempt stays the operator's cancel and is never sent again
    (f1-n86: the in-memory registry forgot the withdrawal, the marker came
    back, and the mission was re-dispatched against the operator)."""

    def setUp(self):
        _restart_latch()
        internal.waiting.clear()

    def test_FIRES_a_withdrawal_survives_an_api_server_restart(self):
        root = f"patrol.dispatch-{uuid4().hex[:10]}"
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(root)
            self.client.post(
                "/tasks/dispatch_task",
                content=mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="patrol", description={"places": ["s1"], "rounds": 1}
                    ),
                ).model_dump_json(exclude_none=True),
            )
        with short_waits(1.0), patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(f"patrol.dispatch-{uuid4().hex[:10]}")
            self.get_portal().call(internal.process_msg, hand_back_msg(root), None)
            resp = self.client.post(
                "/tasks/cancel_task",
                content=json.dumps(
                    {
                        "type": "cancel_task_request",
                        "task_id": root,
                        "labels": ["canceled from mission queue by admin"],
                    }
                ),
            )
            self.assertEqual(200, resp.status_code, resp.content)
            time.sleep(1.5)
            mock.assert_not_called()
        internal.waiting.clear()  # the api-server restarted
        _restart_latch()
        with short_waits(0.05), patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(f"patrol.dispatch-{uuid4().hex[:10]}")
            self.get_portal().call(internal.process_msg, hand_back_msg(root), None)
            time.sleep(0.6)
            mock.assert_not_called()
        stored = self.client.get(f"/tasks/{root}/state").json()
        self.assertNotIn(REDISPATCH_LABEL, stored["cancellation"]["labels"])

    def test_PASSES_a_hand_back_with_nothing_withdrawn_is_sent(self):
        root = f"patrol.dispatch-{uuid4().hex[:10]}"
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(root)
            self.client.post(
                "/tasks/dispatch_task",
                content=mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="patrol", description={"places": ["s1"], "rounds": 1}
                    ),
                ).model_dump_json(exclude_none=True),
            )
        with short_waits(0.05), patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(f"patrol.dispatch-{uuid4().hex[:10]}")
            self.get_portal().call(internal.process_msg, hand_back_msg(root), None)
            deadline = time.time() + 5
            while not mock.called and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(mock.called)
