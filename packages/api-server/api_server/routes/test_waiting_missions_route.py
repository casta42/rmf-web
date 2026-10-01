"""G ruling 2026-10-01, ruling 2 (F-435) — "a mission never fails because a
robot is busy or charging. It waits, shown to the operator as 'waiting for
a robot' with its age, and raises one alert after a threshold." At the
api-server's edges, both ways, through the real paths: missions are
dispatched through POST /tasks/dispatch_task (their requests stored as the
product stores them), task states are fed to process_msg, and the ledger,
GET /tasks/waiting, the alerts and the next dispatch are read back. Only
the fleet is a mock (tasks_service().call), and the waits are shortened.

KNOWN BAD, must act: a handed-back mission is listed as waiting with its
mission, route, age, reason and attempts, and follows its re-dispatch; it
raises ONE alert at the threshold; an operator's cancel of the attempt it
waits on between auctions stops it; at start, a chain tail that a restart
dropped (a marked cancel with no successor) is re-dispatched.

KNOWN GOOD, must stay quiet: no alert before the threshold, none twice,
and the alert is resolved when the mission starts or ends; the waiting
list empties when the mission starts; at start, a tail with a successor,
an operator's cancel and a finished chain are left alone, nothing older
than the window is touched, and an empty ledger does nothing.
"""

import json
import time
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from api_server import models as mdl
from api_server import redispatch, waiting_missions
from api_server.models import tortoise_models as ttm
from api_server.redispatch import (
    CLASS_LABEL,
    ORIGIN_LABEL,
    REDISPATCH_LABEL,
    ROOT_LABEL,
    origin_of,
    root_of,
)
from api_server.rmf_io import tasks_service
from api_server.routes import internal
from api_server.test import AppFixture

HOLD = (
    "charge hold (F-319): [gentle_bot_4] is held for charging at award — a "
    "held robot is awarded no mission until it resumes; returned to the "
    "fleet for re-dispatch"
)
THRESHOLD = waiting_missions.WAITING_ALERT_S


def ok_reply(task_id):
    return f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'


def task_msg(task_id, status, cancellation=None, assigned=None, errors=None):
    data = {
        "booking": {"id": task_id, "unix_millis_earliest_start_time": 0},
        "category": "patrol",
        "detail": "description",
        "status": status,
        "unix_millis_start_time": 0,
    }
    if cancellation is not None:
        data["cancellation"] = {"unix_millis_request_time": 1, "labels": cancellation}
    if assigned is not None:
        data["assigned_to"] = {"group": "gentle_fleet", "name": assigned}
    if errors is not None:
        data["dispatch"] = {"status": "failed_to_assign", "errors": errors}
    return {"type": "task_state_update", "data": data}


def hand_back_msg(task_id, reason=HOLD):
    return task_msg(
        task_id, "canceled", [REDISPATCH_LABEL, reason], assigned="gentle_bot_4"
    )


@contextmanager
def short_waits(step=0.05):
    with patch.object(redispatch, "HAND_BACK_BACKOFF_STEP_S", step), patch.object(
        redispatch, "HAND_BACK_MAX_BACKOFF_S", step
    ), patch.object(redispatch, "NO_BID_BACKOFF_S", (step,) * 4), patch.object(
        redispatch, "NO_BID_MAX_BACKOFF_S", step
    ):
        yield


class WaitingMissionsRouteTest(AppFixture):
    def setUp(self):
        internal.waiting.clear()

    def new_id(self):
        return f"test.dispatch-{uuid4().hex[:10]}"

    def dispatch(self, labels=None, places=("s1", "s2")):
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
                        category="patrol",
                        description={"places": list(places), "rounds": 1},
                        labels=labels,
                    ),
                ).model_dump_json(exclude_none=True),
            )
        self.assertEqual(200, resp.status_code, resp.content)
        return task_id

    def ingest(self, msg):
        self.get_portal().call(internal.process_msg, msg, None)

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def waiting_list(self):
        resp = self.client.get("/tasks/waiting")
        self.assertEqual(200, resp.status_code, resp.content)
        return resp.json()

    def waiting_for(self, root):
        return [w for w in self.waiting_list() if w["root_id"] == root]

    def alert(self, alert_id):
        resp = self.client.get(f"/alerts/{alert_id}")
        if resp.status_code == 404:
            return None
        self.assertEqual(200, resp.status_code, resp.content)
        return resp.json()

    def hand_back_and_redispatch(self, root):
        """The fleet hands `root` back; the api-server puts it back on the
        floor. Returns the child's id."""
        child = self.new_id()
        with short_waits(), patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(child)
            self.ingest(hand_back_msg(root))
            self.assertTrue(self.wait_for(lambda: mock.called))
            self.assertTrue(
                self.wait_for(
                    lambda: getattr(internal.waiting.get(root), "task_id", None)
                    == child
                )
            )
        return child

    # -- the waiting list ------------------------------------------------------

    def test_FIRES_a_handed_back_mission_waits_and_leaves_when_it_starts(self):
        dispatched_at = time.time()
        root = self.dispatch(labels=["gf:kind=patrol"])
        self.assertEqual(self.waiting_for(root), [], "never handed back: not waiting")
        child = self.hand_back_and_redispatch(root)
        self.assertTrue(self.wait_for(lambda: internal.waiting.get(root).enriched))
        (row,) = self.waiting_for(root)
        self.assertEqual(row["task_id"], child, "the attempt it waits on now")
        self.assertEqual(row["category"], "patrol")
        self.assertEqual(row["places"], ["s1", "s2"])
        self.assertEqual(row["reason"], HOLD)
        self.assertEqual(row["attempts"], 1)
        self.assertAlmostEqual(row["since_unix"], dispatched_at, delta=60)
        self.assertGreaterEqual(row["age_s"], 0)
        for shown in (row["reason"], row["category"], *row["places"]):
            self.assertNotIn("gf:", shown, "no machine token reaches the operator")
        # the child is queued behind a robot: still waiting
        self.ingest(task_msg(child, "queued", assigned="gentle_bot_2"))
        self.assertEqual(len(self.waiting_for(root)), 1)
        # it starts: the mission is not waiting any more
        self.ingest(task_msg(child, "underway", assigned="gentle_bot_2"))
        self.assertEqual(self.waiting_for(root), [])

    def test_FIRES_a_hand_back_that_cannot_go_back_is_not_shown_waiting(self):
        """The fleet refuses the re-dispatch: nothing will put the mission
        back on the floor, so it must not sit in the list as waiting."""
        root = self.dispatch()
        refusal = (
            '{ "success": false, "errors": [ { "code": 1, "category": "x", '
            '"detail": "dispatcher shutting down" } ] }'
        )
        with short_waits(), patch.object(tasks_service(), "call") as mock:
            mock.return_value = refusal
            self.ingest(hand_back_msg(root))
            self.assertEqual(len(self.waiting_for(root)), 1)
            self.assertTrue(self.wait_for(lambda: mock.called))
            self.assertTrue(self.wait_for(lambda: not self.waiting_for(root)))
        # the ledger keeps the marked cancel: a LOST mission, as counted
        stored = self.client.get(f"/tasks/{root}/state").json()
        self.assertEqual(stored["status"], "canceled")
        self.assertEqual(stored["cancellation"]["labels"][0], REDISPATCH_LABEL)

    # -- the one alert ---------------------------------------------------------

    def test_FIRES_one_alert_at_the_threshold_and_resolves_it_when_it_starts(self):
        root = self.dispatch()
        child = self.hand_back_and_redispatch(root)
        since = internal.waiting.get(root).since_unix
        alert_id = f"waiting__{root}"
        portal = self.get_portal()
        # not before the threshold
        self.assertEqual(
            portal.call(internal.process_waiting_alerts, since + THRESHOLD - 1), 0
        )
        self.assertIsNone(self.alert(alert_id))
        # at it: one Warning naming the mission, the wait and the reason
        self.assertGreaterEqual(
            portal.call(internal.process_waiting_alerts, since + THRESHOLD), 1
        )
        alert = self.alert(alert_id)
        self.assertEqual(alert["severity"], "warning")
        minutes = int(THRESHOLD // 60)
        self.assertEqual(
            alert["message"],
            f"Mission {root} has been waiting for a robot for {minutes} min — {HOLD}",
        )
        self.assertIsNone(alert["unix_millis_resolved_time"])
        # never twice
        portal.call(internal.process_waiting_alerts, since + 10 * THRESHOLD)
        self.assertEqual(
            self.alert(alert_id)["unix_millis_created_time"],
            alert["unix_millis_created_time"],
        )
        # the mission starts: its alert is resolved
        self.ingest(task_msg(child, "underway", assigned="gentle_bot_2"))
        self.assertTrue(
            self.wait_for(
                lambda: self.alert(alert_id)["unix_millis_resolved_time"] is not None
            )
        )

    def test_FIRES_the_alert_is_resolved_when_the_mission_ends(self):
        root = self.dispatch()
        child = self.hand_back_and_redispatch(root)
        since = internal.waiting.get(root).since_unix
        self.get_portal().call(internal.process_waiting_alerts, since + THRESHOLD)
        alert_id = f"waiting__{root}"
        self.assertIsNotNone(self.alert(alert_id))
        self.ingest(task_msg(child, "completed", assigned="gentle_bot_2"))
        self.assertTrue(
            self.wait_for(
                lambda: self.alert(alert_id)["unix_millis_resolved_time"] is not None
            )
        )
        self.assertEqual(self.waiting_for(root), [])

    # -- an operator's cancel --------------------------------------------------

    def test_FIRES_an_operator_s_cancel_between_auctions_stops_the_mission(self):
        root = self.dispatch()
        before = internal.redispatcher.withdrawn
        with short_waits(1.0), patch.object(tasks_service(), "call") as mock:
            mock.return_value = ok_reply(self.new_id())
            self.ingest(hand_back_msg(root))
            self.assertEqual(len(self.waiting_for(root)), 1)
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
            self.assertIn("waiting for a robot", resp.json()["detail"])
            time.sleep(1.5)  # past the attempt's backoff
            mock.assert_not_called()
        self.assertEqual(internal.redispatcher.withdrawn, before + 1)
        self.assertEqual(self.waiting_for(root), [])
        stored = self.client.get(f"/tasks/{root}/state").json()
        self.assertEqual(stored["status"], "canceled")
        self.assertEqual(
            stored["cancellation"]["labels"],
            [
                "canceled from mission queue by admin",
                f"it was waiting for a robot — {HOLD}",
            ],
        )
        # a fleet re-sending the old state cannot bring the marker back
        self.ingest(hand_back_msg(root))
        self.assertNotIn(
            REDISPATCH_LABEL,
            self.client.get(f"/tasks/{root}/state").json()["cancellation"]["labels"],
        )
        self.assertEqual(self.waiting_for(root), [])

    def test_FIRES_a_cancel_of_a_replaced_attempt_reaches_the_live_one(self):
        root = self.dispatch()
        child = self.hand_back_and_redispatch(root)
        self.ingest(task_msg(child, "queued", assigned="gentle_bot_2"))
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/cancel_task",
                content=json.dumps(
                    {"type": "cancel_task_request", "task_id": root, "labels": ["g"]}
                ),
            )
        self.assertEqual(200, resp.status_code, resp.content)
        sent = json.loads(mock.call_args[0][0])
        self.assertEqual(
            (sent["type"], sent["task_id"]), ("cancel_task_request", child)
        )

    def test_PASSES_a_cancel_of_a_mission_that_is_not_waiting_is_unchanged(self):
        root = self.dispatch()
        self.ingest(task_msg(root, "canceled", ["canceled by g"]))
        resp = self.client.post(
            "/tasks/cancel_task",
            content=json.dumps({"type": "cancel_task_request", "task_id": root}),
        )
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual(resp.json()["detail"], "task is already canceled")

    # -- a restart -------------------------------------------------------------

    def save(self, task_id, status, labels=None, cancellation=None, errors=None):
        data = task_msg(task_id, status, cancellation, errors=errors)["data"]
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

    def test_FIRES_and_PASSES_a_restart_resumes_only_the_dropped_tails(self):
        ids = {k: self.new_id() for k in "ABCDEF"}
        A, B, C, D, E, F = (ids[k] for k in "ABCDEF")
        for root in (A, B, C, D, E, F):
            self.save_request(root, ["gf:kind=patrol"])

        def child_labels(parent, root):
            return [
                "gf:kind=patrol",
                f"{ORIGIN_LABEL}{parent}",
                f"{ROOT_LABEL}{root}",
                f"{CLASS_LABEL}hand-back",
                f"{redispatch.REASON_LABEL}{HOLD}",
            ]

        # A: handed back, the restart came inside the wait — no successor
        self.save(A, "canceled", ["gf:kind=patrol"], [REDISPATCH_LABEL, HOLD])
        # B: handed back and re-dispatched; the child is queued
        b1 = self.new_id()
        self.save(B, "canceled", ["gf:kind=patrol"], [REDISPATCH_LABEL, HOLD])
        self.save_request(b1, child_labels(B, B))
        self.save(b1, "queued", child_labels(B, B))
        # C: an operator canceled it
        self.save(C, "canceled", ["gf:kind=patrol"], ["canceled by g"])
        # D: handed back, re-dispatched, completed
        d1 = self.new_id()
        self.save(D, "canceled", ["gf:kind=patrol"], [REDISPATCH_LABEL, HOLD])
        self.save_request(d1, child_labels(D, D))
        self.save(d1, "completed", child_labels(D, D))
        # E: an auction nobody bid on, superseded, then the restart
        self.save(
            E,
            "canceled",
            ["gf:kind=patrol"],
            [REDISPATCH_LABEL, "no robot answered the auction (attempt 1)"],
            errors=[{"code": 10, "category": "rejection", "detail": "none"}],
        )
        # F: never handed back, running
        self.save(F, "underway", ["gf:kind=patrol"])
        # A's alert was already raised before the restart; an alert for a
        # mission that is no longer waiting is still open
        portal = self.get_portal()
        for root in (A, C):
            portal.call(
                internal.alert_repo.create_alert,
                f"waiting__{root}",
                "fleet",
                ttm.Alert.Severity.Warning,
                None,
                None,
                f"Mission {root} has been waiting for a robot for 15 min — x",
            )
        internal.waiting.clear()
        new_ids = iter(self.new_id() for _ in range(50))
        mine = set(ids.values()) | {b1, d1}
        with short_waits(), patch.object(tasks_service(), "call") as mock:
            mock.side_effect = lambda *a, **k: ok_reply(next(new_ids))
            resumed = portal.call(internal.resume_waiting_chains)

            def origins():
                return {
                    origin_of(json.loads(c[0][0])["request"]["labels"])
                    for c in mock.call_args_list
                }

            self.assertTrue(self.wait_for(lambda: {A, E} <= origins()))
            time.sleep(0.3)  # anything else it was going to send, it has
        self.assertEqual(set(resumed) & mine, {A, E})
        self.assertEqual(origins() & mine, {A, E})
        roots = {
            root_of(json.loads(c[0][0])["request"]["labels"])
            for c in mock.call_args_list
        }
        self.assertTrue({A, E} <= roots)
        # the registry was rebuilt: the two resumed tails and B, waiting on
        # its live child; nothing for C, D or F
        self.assertEqual(internal.waiting.get(B).task_id, b1)
        self.assertEqual(internal.waiting.get(B).reason, HOLD)
        self.assertIsNotNone(internal.waiting.get(A))
        self.assertIsNotNone(internal.waiting.get(E))
        for root in (C, D, F):
            self.assertIsNone(internal.waiting.get(root), root)
        self.assertEqual(internal.waiting.get(A).places, ["s9"])
        # A's one alert survived the restart and is not raised again
        self.assertEqual(internal.waiting.get(A).alerted, f"waiting__{A}")
        self.assertIsNone(internal.waiting.get(B).alerted)
        # C's stale alert was resolved
        self.assertIsNotNone(self.alert(f"waiting__{C}")["unix_millis_resolved_time"])
        self.assertIsNone(self.alert(f"waiting__{A}")["unix_millis_resolved_time"])

    def test_PASSES_a_restart_touches_nothing_older_than_the_window(self):
        old = self.new_id()
        self.save_request(old, ["gf:kind=patrol"])
        self.save(old, "canceled", ["gf:kind=patrol"], [REDISPATCH_LABEL, HOLD])
        internal.waiting.clear()
        with patch.object(tasks_service(), "call") as mock:
            later = time.time() + internal.RESUME_WINDOW_S + 3600
            self.assertEqual(
                self.get_portal().call(internal.resume_waiting_chains, later), []
            )
            time.sleep(0.2)
            mock.assert_not_called()
        self.assertIsNone(internal.waiting.get(old))
        self.assertEqual(len(internal.waiting), 0)
