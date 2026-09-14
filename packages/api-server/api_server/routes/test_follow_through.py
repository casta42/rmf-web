"""F-292 (G ruling 2026-09-14: "a task the dispatcher canceled in flight
must never be awarded; guard both ways"). The api-server layer of the
guard: a task whose cancellation was requested and that then arrives
from the fleet NON-terminal with a robot is canceled again, at the
fleet, once. The fleet adapter refuses the same award the moment it
lands (fleet_adapter._refuse_award) and the charge governor's first
tick is the layer beneath that; this is the layer that sees the fleet's
own task state.

Root cause on the pin (Dispatcher.cpp): cancel_task moves a queued task
to CanceledInFlight and move_to_finished COPIES it into the finished
set without erasing it from the active one, so when the auction
concludes, conclude_bid finds it active, overwrites its status with
Selected and publishes the award."""

import json
import time
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from api_server import models as mdl
from api_server.models.rmf_api.task_state import AssignedTo, Cancellation
from api_server.rmf_io import cancellation as task_cancellation
from api_server.rmf_io import tasks_service
from api_server.routes import internal
from api_server.test import AppFixture, make_task_state


def state(task_id, status="queued", robot="gentle_bot_1"):
    st = make_task_state(task_id)
    st.status = mdl.TaskStatus(status)
    st.assigned_to = AssignedTo(group="gentle_fleet", name=robot) if robot else None
    return st


def latch(task_id):
    task_cancellation.latch(
        task_id,
        Cancellation(
            unix_millis_request_time=round(datetime.now().timestamp() * 1e3),
            labels=["operator canceled it during bidding"],
        ),
    )


class FollowThroughTest(AppFixture):
    def follow(self, *states):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{"success": true}'
            for st in states:
                self.get_portal().call(internal._follow_through_cancel, st)
            time.sleep(0.3)  # the cancel is sent off-handler
            return mock

    def new_id(self):
        return f"patrol.dispatch-{uuid4().hex[:10]}"

    def test_known_bad_an_award_after_a_cancel_is_canceled_at_the_fleet(self):
        task_id = self.new_id()
        latch(task_id)
        mock = self.follow(state(task_id))
        self.assertEqual(mock.call_count, 1)
        sent = json.loads(mock.call_args[0][0])
        self.assertEqual(sent["type"], "cancel_task_request")
        self.assertEqual(sent["task_id"], task_id)
        self.assertTrue(any("F-292" in label for label in sent["labels"]))

    def test_it_is_sent_once_per_task(self):
        task_id = self.new_id()
        latch(task_id)
        mock = self.follow(state(task_id), state(task_id, "underway"))
        self.assertEqual(mock.call_count, 1)

    def test_known_good_a_task_nobody_canceled_is_left_alone(self):
        mock = self.follow(state(self.new_id()))
        mock.assert_not_called()

    def test_a_canceled_task_that_arrives_terminal_is_left_alone(self):
        for status in ("canceled", "completed", "failed", "killed"):
            task_id = self.new_id()
            latch(task_id)
            mock = self.follow(state(task_id, status))
            mock.assert_not_called()

    def test_a_canceled_task_not_on_any_robot_is_left_alone(self):
        task_id = self.new_id()
        latch(task_id)
        mock = self.follow(state(task_id, robot=None))
        mock.assert_not_called()
