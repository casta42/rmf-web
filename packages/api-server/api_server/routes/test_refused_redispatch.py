"""G close-out ruling 2026-10-01, D-86 (4), F-441 — "a refused re-dispatch
never drops a mission; it returns to the waiting queue under F-435." At the
api-server's edge, through the real dispatch path (POST /tasks/dispatch_task
and its guards, horizon and dispatcher call): only the fleet is a mock.

The classification, both ways:

  KNOWN BAD, must keep the mission waiting (Refusal, not permanent): a
  guard's 409 (F-34 a parked robot on the destination), the dispatcher not
  answering (500 "rmf service timed out"), the dispatcher's success=false
  with any answer but an invalid request.
  KNOWN BAD, must fail it by name (nothing can ever be re-sent): the
  dispatcher refusing the stored request itself as invalid (code 5); a
  hand-back whose request is not stored — its row becomes `failed` with
  the reason in a Critical alert, never a marked cancel with no successor.
  KNOWN GOOD, must not be a refusal: an accepted dispatch (its id); one the
  horizon HOLDS for its start (Held — never sent twice); one the dispatcher
  accepted whose recording then failed (its id — sending it again would
  duplicate the mission).
"""

import importlib
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api_server import models as mdl
from api_server import redispatch
from api_server.redispatch import REDISPATCH_LABEL, REFUSED_LABEL, Held, Refusal
from api_server.rmf_io import tasks_service
from api_server.routes import internal
from api_server.test import AppFixture

tasks_mod = importlib.import_module("api_server.routes.tasks.tasks")

HAND_BACK = (
    "charge preemption (F-36): [gentle_bot_3] at SoC 0.17 cannot finish "
    "this mission — mission returned to the fleet for re-dispatch"
)
F34 = (
    "destination [j_st2] is occupied by parked robot "
    "[gentle_fleet/gentle_bot_10] (F-34); dispatch rejected"
)


def ok_reply(task_id):
    return f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'


def refusal_reply(code, detail):
    return (
        f'{{ "success": false, "errors": [ {{ "code": {code}, '
        f'"category": "x", "detail": "{detail}" }} ] }}'
    )


def hand_back_msg(task_id):
    return {
        "type": "task_state_update",
        "data": {
            "booking": {"id": task_id, "unix_millis_earliest_start_time": 0},
            "category": "patrol",
            "detail": "description",
            "status": "canceled",
            "unix_millis_start_time": 0,
            "assigned_to": {"group": "gentle_fleet", "name": "gentle_bot_3"},
            "cancellation": {
                "unix_millis_request_time": 1,
                "labels": [REDISPATCH_LABEL, HAND_BACK],
            },
        },
    }


class RefusedRedispatchRouteTest(AppFixture):
    def setUp(self):
        internal.waiting.clear()

    def new_id(self):
        return f"test.dispatch-{uuid4().hex[:10]}"

    def request(self):
        return mdl.TaskRequest(
            category="patrol", description={"places": ["s1"], "rounds": 1}
        )

    def send(self, request=None):
        """_redispatch_request through the real dispatch path; returns
        ("id", new_id) or ("refusal"/"held", the exception)."""
        try:
            return "id", self.get_portal().call(
                internal._redispatch_request, request or self.request()
            )
        except Refusal as refusal:
            return "refusal", refusal
        except Held as held:
            return "held", held

    def alert(self, alert_id):
        resp = self.client.get(f"/alerts/{alert_id}")
        if resp.status_code == 404:
            return None
        self.assertEqual(200, resp.status_code, resp.content)
        return resp.json()

    # -- the classification ---------------------------------------------------

    def test_FIRES_refusals_of_the_moment_keep_the_mission_waiting(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.side_effect = HTTPException(500, "rmf service timed out")
            kind, refusal = self.send()
            self.assertEqual(kind, "refusal")
            self.assertFalse(refusal.permanent)
            self.assertEqual(refusal.reason, "rmf service timed out")

            mock.side_effect = None
            mock.return_value = refusal_reply(1, "dispatcher shutting down")
            kind, refusal = self.send()
            self.assertEqual(kind, "refusal")
            self.assertFalse(refusal.permanent)
            self.assertIn("dispatcher shutting down", refusal.reason)

        guard = AsyncMock(side_effect=HTTPException(409, detail=F34))
        with patch.object(tasks_mod, "guard_patrol_destination", guard), patch.object(
            tasks_service(), "call"
        ) as mock:
            kind, refusal = self.send()
            mock.assert_not_called()
        self.assertEqual(kind, "refusal")
        self.assertFalse(refusal.permanent, "F-34 clears when the robot leaves")
        self.assertEqual(refusal.reason, F34)

    def test_FIRES_an_invalid_stored_request_is_the_permanent_refusal(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = refusal_reply(5, "labels is not an array")
            kind, refusal = self.send()
        self.assertEqual(kind, "refusal")
        self.assertTrue(refusal.permanent)
        self.assertIn(
            "refuses its stored request as invalid: labels is not", refusal.reason
        )

    def test_PASSES_an_accepted_dispatch_is_its_id(self):
        new_id = self.new_id()
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(new_id)
            self.assertEqual(self.send(), ("id", new_id))

    def test_PASSES_a_dispatch_held_for_its_start_is_held_never_refused(self):
        deferred = JSONResponse(
            status_code=202,
            content={
                "success": True,
                "deferred": {"id": "deferred-7"},
                "detail": "held by GentleFleet (F-293)",
            },
        )
        with patch.object(
            tasks_mod, "_horizon_gate", AsyncMock(return_value=deferred)
        ), patch.object(tasks_service(), "call") as mock:
            kind, held = self.send()
            mock.assert_not_called()
        self.assertEqual(kind, "held")
        self.assertEqual(
            (held.held_id, held.detail), ("deferred-7", "held by GentleFleet (F-293)")
        )

    def test_PASSES_accepted_then_not_recorded_is_its_id_not_a_refusal(self):
        """Sending it again would make a duplicate mission."""
        new_id = self.new_id()
        broken = AsyncMock(side_effect=RuntimeError("database gone"))
        with patch.object(tasks_service(), "call") as mock, patch.object(
            internal.task_repo, "save_task_state", broken
        ):
            mock.return_value = ok_reply(new_id)
            self.assertEqual(self.send(), ("id", new_id))
        # and an answer of no known shape BEFORE any acceptance stays an
        # error — the Redispatcher sends it again (test_redispatch)
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = "not json"
            with self.assertRaises(ValidationError):
                self.send()

    # -- nothing re-sendable: a named failure, never a LOST row ---------------

    def test_FIRES_a_hand_back_with_no_stored_request_fails_by_name(self):
        task_id = self.new_id()  # never dispatched through the api-server
        with patch.object(redispatch, "HAND_BACK_BACKOFF_STEP_S", 0.05), patch.object(
            redispatch, "HAND_BACK_MAX_BACKOFF_S", 0.05
        ), patch.object(tasks_service(), "call") as mock:
            self.get_portal().call(internal.process_msg, hand_back_msg(task_id), None)
            deadline = time.time() + 5.0
            while time.time() < deadline and self.alert(task_id) is None:
                time.sleep(0.05)
            mock.assert_not_called()
        row = self.client.get(f"/tasks/{task_id}/state").json()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(
            row["cancellation"]["labels"],
            [HAND_BACK],
            "the fleet's reason kept, the marker gone",
        )
        alert = self.alert(task_id)
        self.assertEqual(alert["severity"], "critical")
        self.assertIn(
            f"Task {task_id} failed: handed back by the fleet", alert["message"]
        )
        self.assertIn(
            "cannot be sent again: its request is not stored", alert["message"]
        )
        self.assertIsNone(internal.waiting.find_task(task_id), "not shown waiting")

    # -- the refusal hook ------------------------------------------------------

    def test_the_refusal_is_listed_even_for_an_attempt_the_registry_missed(self):
        """A retried re-dispatch IS waiting: an attempt the registry never
        saw put back (it is rebuilt at start) is listed with the refusal."""
        task_id = self.new_id()
        state = mdl.TaskState(**hand_back_msg(task_id)["data"])
        self.get_portal().call(internal.task_repo.save_task_state, state)
        self.assertIsNone(internal.waiting.find_task(task_id))
        self.get_portal().call(internal._redispatch_refused, task_id, F34, 30.0)
        entry = internal.waiting.find_task(task_id)
        self.assertIsNotNone(entry)
        self.assertIn(F34, entry.reason)
        row = self.client.get(f"/tasks/{task_id}/state").json()
        self.assertEqual(
            row["cancellation"]["labels"],
            [REDISPATCH_LABEL, HAND_BACK, f"{REFUSED_LABEL}{F34}"],
        )
        # a second refusal replaces the first on the row, never piles up
        self.get_portal().call(
            internal._redispatch_refused, task_id, "rmf service timed out", 45.0
        )
        row = self.client.get(f"/tasks/{task_id}/state").json()
        self.assertEqual(
            row["cancellation"]["labels"],
            [REDISPATCH_LABEL, HAND_BACK, f"{REFUSED_LABEL}rmf service timed out"],
        )
        # an operator's cancel (no marker) is never stamped
        other = self.new_id()
        data = hand_back_msg(other)["data"]
        data["cancellation"]["labels"] = ["canceled by admin"]
        self.get_portal().call(
            internal.task_repo.save_task_state, mdl.TaskState(**data)
        )
        self.get_portal().call(internal._redispatch_refused, other, F34, 15.0)
        row = self.client.get(f"/tasks/{other}/state").json()
        self.assertEqual(row["cancellation"]["labels"], ["canceled by admin"])
