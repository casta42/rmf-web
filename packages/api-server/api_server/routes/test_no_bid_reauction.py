"""G ruling 2026-10-01 item 6 (F-410/F-412 class) — "A timed-out auction
never becomes a failed mission: re-auction with backoff, and fail only
after N attempts with the reason named" — at the ingest route, both ways.

Everything here goes through the real path: the mission is dispatched
through POST /tasks/dispatch_task (so its request is stored the way the
product stores it), the dispatcher's own task_state_update is fed to
process_msg, and the ledger row, the alert and the next dispatch are read
back. Only the fleet is a mock (tasks_service().call), and the waits are
shortened — the schedule itself is pinned in test_redispatch.py.

KNOWN BAD, must act: a first-generation mission whose auction closed with
no bid is stored as put back on the floor (never as failed), rings nothing
— the mission is waiting (F-435: one alert per waiting mission, after a
threshold, never one per attempt) — and is auctioned again exactly once
with the labels that fold its chain; so is a SIXTH silent auction (F-435:
a transient no-bid is never a failure). When the re-auction cannot be
made, the row is amended to failed and a Critical alert names why.

KNOWN GOOD, must stay as it was: the last PERMANENT answer (failed,
Critical, the reason named — the one no-bid that still fails), a failure
with another cause, a mission somebody canceled while its auction was
open, and an ordinary hand-back — which still takes its own path.
"""

import json
import time
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from api_server import models as mdl
from api_server import redispatch
from api_server.models.rmf_api.task_state import Cancellation
from api_server.redispatch import (
    CLASS_HAND_BACK,
    CLASS_NO_BID,
    NO_BID_ATTEMPT_LABEL,
    NO_BID_MAX_ATTEMPTS,
    NO_BID_PERMANENT_LABEL,
    NO_BID_SINCE_LABEL,
    REDISPATCH_LABEL,
    class_of,
    generation_of,
    no_bid_attempt_of,
    no_bid_since_of,
    origin_of,
    root_of,
)
from api_server.rmf_io import cancellation as task_cancellation
from api_server.rmf_io import tasks_service
from api_server.routes import internal
from api_server.test import AppFixture

FAST = (0.05, 0.05, 0.05, 0.05)
HAND_BACK = (
    "charge preemption (F-36): [gentle_bot_3] at SoC 0.17 cannot finish "
    "this mission — mission returned to the fleet for re-dispatch"
)


def no_bid_error(task_id):
    return {
        "code": 10,
        "category": "rejection",
        "detail": f"No fleet adapters offered a bid for task [{task_id}]",
    }


def ok_reply(task_id):
    return f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'


def closed_auction(task_id, labels=None, errors=None):
    """What rmf_task_ros2 Dispatcher.cpp publish_task_state_ws sends when
    an auction concludes with no winner."""
    booking = {"id": task_id, "unix_millis_earliest_start_time": 0}
    if labels:
        booking["labels"] = labels
    return {
        "type": "task_state_update",
        "data": {
            "booking": booking,
            "category": "test",
            "detail": "description",
            "status": "failed",
            "unix_millis_start_time": 0,
            "dispatch": {
                "status": "failed_to_assign",
                "errors": [no_bid_error(task_id)] if errors is None else errors,
            },
        },
    }


class NoBidReauctionRouteTest(AppFixture):
    def new_id(self):
        return f"test.dispatch-{uuid4().hex[:10]}"

    def dispatch(self, labels=None):
        """Dispatch a mission the way an operator does; its request is
        stored. Returns the task id the (mock) dispatcher minted."""
        task_id = self.new_id()
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(task_id)
            resp = self.client.post(
                "/tasks/dispatch_task",
                content=mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="test", description="description", labels=labels
                    ),
                ).model_dump_json(exclude_none=True),
            )
        self.assertEqual(200, resp.status_code, resp.content)
        return task_id

    def state(self, task_id):
        resp = self.client.get(f"/tasks/{task_id}/state")
        self.assertEqual(200, resp.status_code, resp.content)
        return resp.json()

    def alert(self, task_id):
        """The task's alert, or None when it raised none."""
        resp = self.client.get(f"/alerts/{task_id}")
        if resp.status_code == 404:
            return None
        self.assertEqual(200, resp.status_code, resp.content)
        return resp.json()

    def ingest(self, msg, reply=None, expect_dispatch=True):
        """Feed one message to process_msg and let its re-dispatch hook
        run. Returns (the row and alert as stored at ingest, before any
        wait; the fleet mock)."""
        task_id = msg["data"]["booking"]["id"]
        with patch.object(redispatch, "NO_BID_BACKOFF_S", FAST), patch.object(
            redispatch, "NO_BID_MAX_BACKOFF_S", 0.05
        ), patch.object(redispatch, "HAND_BACK_BACKOFF_STEP_S", 0.05), patch.object(
            redispatch, "HAND_BACK_MAX_BACKOFF_S", 0.05
        ), patch.object(tasks_service(), "call") as mock:
            if reply is not None:
                mock.return_value = reply
            self.get_portal().call(internal.process_msg, msg, None)
            at_ingest = (self.state(task_id), self.alert(task_id))
            deadline = time.time() + (5.0 if expect_dispatch else 0.6)
            while time.time() < deadline and not (expect_dispatch and mock.called):
                time.sleep(0.05)
            time.sleep(0.2)  # let the hook finish what it does after the call
            return at_ingest, mock

    # -- known bad: must act -------------------------------------------------

    def test_FIRES_a_first_generation_no_bid_is_put_back_not_failed(self):
        before = internal.redispatcher.reauctioned
        task_id = self.dispatch(labels=["shift=night"])
        child_id = self.new_id()
        dispatched_at = time.time()
        (row, alert), mock = self.ingest(
            closed_auction(task_id, labels=["shift=night"]), reply=ok_reply(child_id)
        )
        # the ledger row: the hand-back shape, never `failed`
        self.assertEqual(row["status"], "canceled")
        marker, reason = row["cancellation"]["labels"]
        self.assertEqual(marker, REDISPATCH_LABEL)
        # (the seconds are this test's shortened wait; the real line is
        # pinned on the real model in test_redispatch.py)
        self.assertRegex(
            reason,
            r"^no robot answered the auction \(attempt 1\) — auctioned "
            r"again in \d+ s$",
        )
        # ...with the dispatcher's own verdict kept as provenance
        self.assertEqual(row["dispatch"]["status"], "failed_to_assign")
        self.assertEqual(row["dispatch"]["errors"][0]["code"], 10)
        # the bell: nothing — the mission is waiting, not canceled (F-435)
        self.assertIsNone(alert)
        # auctioned again, once, through the operator's dispatch path
        self.assertEqual(mock.call_count, 1)
        sent = json.loads(mock.call_args[0][0])
        self.assertEqual(sent["type"], "dispatch_task_request")
        labels = sent["request"]["labels"]
        self.assertIn("shift=night", labels)
        self.assertEqual(origin_of(labels), task_id)
        self.assertEqual(root_of(labels), task_id)
        self.assertEqual(class_of(labels), CLASS_NO_BID)
        self.assertEqual(no_bid_attempt_of(labels), 2)
        self.assertEqual(generation_of(labels), 0)
        # the span starts when the ledger first recorded the mission
        self.assertAlmostEqual(no_bid_since_of(labels), dispatched_at, delta=60)
        # the child is a stored mission of its own, and the superseded row
        # is still what it was — no Critical arrived late either
        resp = self.client.get(f"/tasks/{child_id}/request")
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual(resp.json()["labels"], labels)
        self.assertEqual(self.state(task_id)["status"], "canceled")
        self.assertIsNone(self.alert(task_id))
        self.assertEqual(internal.redispatcher.reauctioned, before + 1)

    def test_FIRES_a_re_auction_that_cannot_be_made_is_a_named_failure(self):
        # (1) the mission was never dispatched through the api-server, so
        # there is no request to auction again
        task_id = self.new_id()
        (row, alert), mock = self.ingest(closed_auction(task_id), expect_dispatch=False)
        self.assertEqual(row["status"], "canceled")
        self.assertIsNone(alert)
        mock.assert_not_called()
        row, alert = self.state(task_id), self.alert(task_id)
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row.get("cancellation"))
        self.assertEqual(row["dispatch"]["errors"][0]["code"], 10)
        self.assertEqual(alert["severity"], "critical")
        self.assertIn(f"Task {task_id} failed: no robot answered", alert["message"])
        self.assertIn("its request is not stored", alert["message"])
        # (2) the fleet refuses the re-auction
        task_id = self.dispatch()
        _, mock = self.ingest(
            closed_auction(task_id),
            reply='{ "success": false, "errors": [ { "code": 1, '
            '"category": "x", "detail": "dispatcher shutting down" } ] }',
        )
        self.assertEqual(mock.call_count, 1)
        row, alert = self.state(task_id), self.alert(task_id)
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row.get("cancellation"))
        self.assertEqual(alert["severity"], "critical")
        self.assertIn("could not be auctioned again", alert["message"])

    # -- known good: must stay as it was -------------------------------------

    def test_FIRES_a_sixth_silent_auction_is_auctioned_again(self):
        """F-435: THE CONTRACT CHANGED. A fifth silent auction used to be
        the failure; silence is transient now, and the sixth is put back on
        the floor like the first."""
        since = time.time() - 61
        labels = [
            f"{NO_BID_ATTEMPT_LABEL}{NO_BID_MAX_ATTEMPTS + 1}",
            f"{NO_BID_SINCE_LABEL}{since:.0f}",
        ]
        task_id = self.dispatch(labels=labels)
        child_id = self.new_id()
        before = internal.redispatcher.no_bid_exhausted
        (row, alert), mock = self.ingest(
            closed_auction(task_id, labels=labels), reply=ok_reply(child_id)
        )
        self.assertEqual(row["status"], "canceled")
        self.assertRegex(
            row["cancellation"]["labels"][1],
            r"^no robot answered the auction \(attempt 6\) — auctioned again",
        )
        self.assertIsNone(alert)
        self.assertEqual(mock.call_count, 1)
        sent = json.loads(mock.call_args[0][0])["request"]["labels"]
        self.assertEqual(no_bid_attempt_of(sent), NO_BID_MAX_ATTEMPTS + 2)
        self.assertEqual(int(no_bid_since_of(sent)), int(float(f"{since:.0f}")))
        self.assertEqual(internal.redispatcher.no_bid_exhausted, before)

    def test_PASSES_the_last_permanent_answer_fails_with_the_reason_named(self):
        since = time.time() - 61
        labels = [
            f"{NO_BID_ATTEMPT_LABEL}{NO_BID_MAX_ATTEMPTS}",
            f"{NO_BID_SINCE_LABEL}{since:.0f}",
            f"{NO_BID_PERMANENT_LABEL}{NO_BID_MAX_ATTEMPTS - 1}",
        ]
        task_id = self.dispatch(labels=labels)
        before = internal.redispatcher.no_bid_exhausted
        limited = {
            "code": 9,
            "category": "Not feasible",
            "detail": "[TaskPlanner] Failed to compute assignments for task_id "
            f"[{task_id}] due to insufficient battery capacity to accommodate "
            "one or more requests by any of the robots in this fleet.",
        }
        (row, alert), mock = self.ingest(
            closed_auction(task_id, labels=labels, errors=[limited, no_bid_error(task_id)]),
            expect_dispatch=False,
        )
        mock.assert_not_called()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row.get("cancellation"))
        self.assertEqual(alert["severity"], "critical")
        self.assertRegex(
            alert["message"],
            rf"^Task {task_id} failed: no robot offered to take this mission at 5 "
            r"auctions in a row over 6\d s — no robot can finish this mission on "
            r"one battery charge, even starting full — shorten it \(fewer rounds "
            r"or stops\) or split it into smaller missions$",
        )
        self.assertEqual(internal.redispatcher.no_bid_exhausted, before + 1)

    def test_PASSES_a_failure_with_another_cause_is_a_failure_now(self):
        task_id = self.dispatch()
        refusal = {
            "code": 9,
            "category": "Not feasible",
            "detail": "[TaskPlanner] insufficient battery capacity",
        }
        (row, alert), mock = self.ingest(
            closed_auction(task_id, errors=[refusal]), expect_dispatch=False
        )
        mock.assert_not_called()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row.get("cancellation"))
        self.assertEqual(alert["severity"], "critical")
        self.assertIn("one battery charge", alert["message"])

    def test_PASSES_a_mission_somebody_canceled_is_never_auctioned_again(self):
        task_id = self.dispatch()
        # the operator's cancel was latched at request time (F-71) and the
        # auction then closed with no bid
        task_cancellation.latch(
            task_id,
            Cancellation(
                unix_millis_request_time=round(datetime.now().timestamp() * 1e3),
                labels=["canceled from mission queue by admin"],
            ),
        )
        (row, _), mock = self.ingest(closed_auction(task_id), expect_dispatch=False)
        mock.assert_not_called()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(
            row["cancellation"]["labels"], ["canceled from mission queue by admin"]
        )

    def test_PASSES_an_ordinary_hand_back_still_takes_its_own_path(self):
        task_id = self.dispatch(labels=["shift=night"])
        child_id = self.new_id()
        before = (
            internal.redispatcher.redispatched,
            internal.redispatcher.reauctioned,
        )
        hand_back = {
            "type": "task_state_update",
            "data": {
                "booking": {"id": task_id},
                "status": "canceled",
                "assigned_to": {"group": "gentle_fleet", "name": "gentle_bot_3"},
                "cancellation": {
                    "unix_millis_request_time": 1,
                    "labels": [REDISPATCH_LABEL, HAND_BACK],
                },
            },
        }
        (row, alert), mock = self.ingest(hand_back, reply=ok_reply(child_id))
        self.assertEqual(row["status"], "canceled")
        self.assertIsNone(alert, "a hand-back is a wait, not a cancel (F-435)")
        self.assertEqual(mock.call_count, 1)
        labels = json.loads(mock.call_args[0][0])["request"]["labels"]
        self.assertIn("shift=night", labels)
        self.assertEqual(generation_of(labels), 1)
        self.assertEqual(class_of(labels), CLASS_HAND_BACK)
        self.assertEqual(root_of(labels), task_id)
        self.assertEqual(no_bid_attempt_of(labels), 1)
        self.assertFalse(any(lab.startswith(NO_BID_ATTEMPT_LABEL) for lab in labels))
        self.assertEqual(
            (internal.redispatcher.redispatched, internal.redispatcher.reauctioned),
            (before[0] + 1, before[1]),
        )
