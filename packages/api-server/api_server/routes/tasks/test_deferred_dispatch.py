"""F-293 (FR-4 amendment, G ruling 2026-09-14) at the route, both ways:
a mission starting beyond the derived dispatch horizon is held and never
reaches the fleet early; one more than a shift ahead is refused; an
ordinary mission (now, inside the horizon, in the past) goes straight to
the fleet exactly as before. The test config has no zones file, so the
horizon is the stated fallback bound (dispatch_horizon.FALLBACK_HORIZON_S)."""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from api_server import models as mdl
from api_server.models import User
from api_server.models import tortoise_models as ttm
from api_server.rmf_io import tasks_service
from api_server.routes.tasks import tasks as tasks_module
from api_server.test import AppFixture

LOG = logging.getLogger("test.deferred_dispatch")


def ok_reply(mock, task_id):
    mock.return_value = (
        f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'
    )


class TestDeferredDispatch(AppFixture):
    def post(self, seconds_ahead, robot=None):
        request = mdl.TaskRequest(
            category="test",
            description="description",
            unix_millis_earliest_start_time=round((time.time() + seconds_ahead) * 1000),
        )
        if robot:
            path, body = "/tasks/robot_task", mdl.RobotTaskRequest(
                type="robot_task_request",
                robot=robot,
                fleet="gentle_fleet",
                request=request,
            )
        else:
            path, body = "/tasks/dispatch_task", mdl.DispatchTaskRequest(
                type="dispatch_task_request", request=request
            )
        return self.client.post(path, content=body.model_dump_json(exclude_none=True))

    def cancel(self, task_id):
        return self.client.post(
            "/tasks/cancel_task",
            content=json.dumps(
                {"type": "cancel_task_request", "task_id": task_id, "labels": ["test"]}
            ),
        )

    def row(self, dispatch_offset_s, status="pending"):
        async def make():
            await User.load_or_create_from_db("admin")
            now = datetime.now(timezone.utc)
            body = json.loads(
                mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="test",
                        description="description",
                        unix_millis_earliest_start_time=round(
                            (now.timestamp() + 60) * 1000
                        ),
                    ),
                ).model_dump_json(exclude_none=True)
            )
            return await ttm.DeferredDispatch.create(
                request_type="dispatch_task_request",
                body=body,
                earliest_start=now + timedelta(seconds=60),
                dispatch_at=now + timedelta(seconds=dispatch_offset_s),
                status=status,
                created_by="admin",
            )

        return self.get_portal().call(make)

    def status(self, row_id):
        async def read():
            return await ttm.DeferredDispatch.get(id=row_id)

        return self.get_portal().call(read)

    def wait_status(self, row_id, want, timeout=8.0):
        deadline = time.time() + timeout
        row = self.status(row_id)
        while row.status != want and time.time() < deadline:
            time.sleep(0.2)
            row = self.status(row_id)
        return row

    # -- the gate ----------------------------------------------------------

    def test_known_bad_a_far_future_mission_is_held_not_sent(self):
        with patch.object(tasks_service(), "call") as mock:
            resp = self.post(3600)
            mock.assert_not_called()
        self.assertEqual(202, resp.status_code, resp.content)
        data = resp.json()
        self.assertTrue(data["success"])
        deferred = data["deferred"]
        self.assertTrue(deferred["id"].startswith("deferred-"))
        self.assertEqual("pending", deferred["status"])
        self.assertIn("F-293", data["detail"])
        self.assertAlmostEqual(
            (deferred["earliest_start_ms"] - deferred["dispatch_at_ms"]) / 1000,
            data["horizon_s"],
            delta=1,
        )
        listed = self.client.get("/tasks/deferred?status=pending").json()
        self.assertIn(deferred["id"], [row["id"] for row in listed])

    def test_known_bad_more_than_a_shift_ahead_is_refused(self):
        with patch.object(tasks_service(), "call") as mock:
            resp = self.post(9 * 3600)
            mock.assert_not_called()
        self.assertEqual(422, resp.status_code, resp.content)
        self.assertIn("shift", resp.json()["detail"])

    def test_known_good_ordinary_missions_go_straight_to_the_fleet(self):
        for ahead in (0, 60, -600):
            with patch.object(tasks_service(), "call") as mock:
                ok_reply(mock, f"t{ahead}-{time.time()}")
                resp = self.post(ahead)
                self.assertEqual(200, resp.status_code, (ahead, resp.content))
                mock.assert_called_once()

    def test_a_robot_task_is_held_too(self):
        with patch.object(tasks_service(), "call") as mock:
            resp = self.post(3600, robot="gentle_bot_1")
            mock.assert_not_called()
        self.assertEqual(202, resp.status_code, resp.content)
        self.assertEqual("gentle_bot_1", resp.json()["deferred"]["robot"])
        self.assertEqual("robot_task_request", resp.json()["deferred"]["type"])

    # -- cancel --------------------------------------------------------------

    def test_a_held_mission_is_canceled_without_touching_the_fleet(self):
        deferred_id = self.post(3600).json()["deferred"]["id"]
        with patch.object(tasks_service(), "call") as mock:
            first = self.cancel(deferred_id)
            again = self.cancel(deferred_id)
            mock.assert_not_called()
        self.assertEqual(200, first.status_code, first.content)
        self.assertIn("never sent", first.json()["detail"])
        self.assertEqual(200, again.status_code, again.content)
        self.assertIn("already", again.json()["detail"])
        self.assertEqual(404, self.cancel("deferred-987654").status_code)

    # -- release ---------------------------------------------------------------

    def test_a_held_mission_is_released_on_time_with_provenance(self):
        row = self.row(-1)
        with patch.object(tasks_service(), "call") as mock:
            ok_reply(mock, "patrol.dispatch-released-1")
            self.get_portal().call(tasks_module.dispatch_due_deferrals, LOG)
            done = self.wait_status(row.id, "dispatched")
            self.assertEqual(1, mock.call_count)
            sent = json.loads(mock.call_args[0][0])
        self.assertEqual("dispatched", done.status)
        self.assertEqual("patrol.dispatch-released-1", done.task_id)
        self.assertIn(f"gf:deferred-of=deferred-{row.id}", sent["request"]["labels"])

    def test_a_mission_not_yet_due_stays_held(self):
        row = self.row(3600)
        with patch.object(tasks_service(), "call") as mock:
            self.get_portal().call(tasks_module.dispatch_due_deferrals, LOG)
            mock.assert_not_called()
        self.assertEqual("pending", self.status(row.id).status)

    def test_a_canceled_mission_is_never_released(self):
        self.row(-1, status="canceled")
        with patch.object(tasks_service(), "call") as mock:
            self.get_portal().call(tasks_module.dispatch_due_deferrals, LOG)
            mock.assert_not_called()

    def test_a_release_the_fleet_refuses_is_recorded_failed(self):
        row = self.row(-1)
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = (
                '{ "success": false, "errors": [ { "code": 1, '
                '"category": "x", "detail": "no robot" } ] }'
            )
            self.get_portal().call(tasks_module.dispatch_due_deferrals, LOG)
            done = self.wait_status(row.id, "failed")
        self.assertEqual("failed", done.status)
        self.assertIn("refused", done.detail)

    def test_a_dispatch_interrupted_by_a_restart_is_failed_never_resent(self):
        row = self.row(-1, status="dispatching")
        with patch.object(tasks_service(), "call") as mock:
            self.get_portal().call(tasks_module.recover_interrupted_deferrals, LOG)
            self.get_portal().call(tasks_module.dispatch_due_deferrals, LOG)
            mock.assert_not_called()
        done = self.status(row.id)
        self.assertEqual("failed", done.status)
        self.assertIn("restarted", done.detail)
