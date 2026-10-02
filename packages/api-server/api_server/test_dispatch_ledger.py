"""F-465 (G ruling 2026-10-02, second sheet, item 2): "a replayed dispatch
request must never create a mission. Dedupe by request ID against the
ledger."

The logic, both ways, with the ledger and the fleet as seams:

  FIRES: an answer to a request id the ledger has CLOSED (answered with
  another task, or given up with no mission) is a replay's — its task is
  canceled and its row removed, and later states of it are refused.
  BORING, left alone: an answer to a request the ledger does not know (not
  ours), to one still pending (the live answer), the same answer again
  (the dispatcher repeating itself), anything that is not a successful
  dispatch answer, and — a guard that cannot act must not hide — a replay
  whose task could NOT be canceled stays a visible mission.

The dispatch path's side (the request id written before it is published,
closed with its task) and the live proof (a restarted dispatcher with
latched requests present: zero phantom missions, new requests still
dispatch) are in routes/test_dispatch_replay.py and on a release.
"""

import asyncio
import json
import unittest

from api_server import dispatch_ledger as dl


def _answer(task_id="patrol.dispatch-2", success=True):
    body = {"success": success}
    if task_id is not None:
        body["state"] = {"booking": {"id": task_id}, "status": "queued"}
    return json.dumps(body)


class _World:
    """The ledger's rows and the fleet's cancel, recorded."""

    def __init__(self, rows=None, cancel_answers=("dispatcher",), stored=1):
        self.rows = dict(rows or {})
        self.cancel_answers = list(cancel_answers)
        self.stored = stored
        self.canceled, self.removed, self.noted, self.slept = [], [], [], []
        self.reads, self.later = 0, None

    async def row_of(self, request_id):
        self.reads += 1
        if self.later and self.reads > 1:
            return self.later
        return self.rows.get(request_id, (None, None))

    async def cancel(self, task_id):
        self.canceled.append(task_id)
        return self.cancel_answers.pop(0) if self.cancel_answers else None

    async def remove_row(self, task_id):
        self.removed.append(task_id)
        removed, self.stored = self.stored, 0
        return removed

    async def note(self, request_id, task_id):
        self.noted.append((request_id, task_id))

    async def sleep(self, seconds):
        self.slept.append(seconds)

    def handle(self, request_id, json_msg):
        return asyncio.run(
            dl.handle_unclaimed(
                request_id,
                json_msg,
                self.cancel,
                self.remove_row,
                sleep=self.sleep,
                row_of=self.row_of,
                note=self.note,
            )
        )


class TestJudge(unittest.TestCase):
    def test_the_four_answers(self):
        self.assertEqual(dl.NOT_OURS, dl.judge(None, None, "t2"))
        self.assertEqual(dl.STILL_PENDING, dl.judge(dl.PENDING, None, "t2"))
        self.assertEqual(dl.SAME, dl.judge(dl.ANSWERED, "t2", "t2"))
        self.assertEqual(dl.REPLAY, dl.judge(dl.ANSWERED, "t1", "t2"))
        self.assertEqual(dl.REPLAY, dl.judge(dl.CLOSED, None, "t2"))

    def test_only_a_successful_dispatch_answer_names_a_task(self):
        self.assertEqual("patrol.dispatch-2", dl.task_of_response(_answer()))
        self.assertIsNone(dl.task_of_response(_answer(success=False)))
        self.assertIsNone(dl.task_of_response(_answer(task_id=None)))
        self.assertIsNone(dl.task_of_response('{"success": true}'))
        self.assertIsNone(dl.task_of_response("not json"))
        self.assertIsNone(dl.task_of_response("[1, 2]"))
        self.assertIsNone(
            dl.task_of_response('{"success": true, "state": {"booking": {"id": ""}}}')
        )


class TestAReplayIsNeverAMission(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: dl.REFUSED.discard("patrol.dispatch-2"))

    def test_a_request_answered_before_makes_no_second_mission(self):
        world = _World({"req-1": (dl.ANSWERED, "patrol.dispatch-1")})
        with self.assertLogs(dl.logger, level="WARNING") as logs:
            verdict = world.handle("req-1", _answer("patrol.dispatch-2"))
        self.assertEqual(dl.REPLAY, verdict)
        self.assertEqual(["patrol.dispatch-2"], world.canceled)
        self.assertEqual(
            ["patrol.dispatch-2"] * 2,
            world.removed,
            "removed, and looked for once more after a write " "that was in flight",
        )
        self.assertIn("patrol.dispatch-2", dl.REFUSED)
        self.assertNotIn(
            "patrol.dispatch-1", dl.REFUSED, "the real mission is untouched"
        )
        self.assertEqual([("req-1", "patrol.dispatch-2")], world.noted)
        self.assertIn("REPLAYED request [req-1]", logs.output[0])
        self.assertIn("as [patrol.dispatch-1]", logs.output[0])
        self.assertIn("it is not a mission", logs.output[0])

    def test_a_request_that_was_given_up_makes_no_late_mission(self):
        """The dispatcher answered after the caller had stopped waiting:
        the caller was told "no answer" and the mission is offered again —
        the late task would be its duplicate."""
        world = _World({"req-1": (dl.CLOSED, None)})
        with self.assertLogs(dl.logger, level="WARNING") as logs:
            self.assertEqual(dl.REPLAY, world.handle("req-1", _answer()))
        self.assertEqual(["patrol.dispatch-2"], world.canceled)
        self.assertIn("with no mission", logs.output[0])

    def test_an_answer_that_crosses_its_callers_timeout_is_caught(self):
        """The answer arrives in the instant between the caller giving up
        and the ledger being closed: pending at the first look, closed at
        the second."""
        world = _World({"req-1": (dl.PENDING, None)})
        world.later = (dl.CLOSED, None)
        with self.assertLogs(dl.logger, level="WARNING"):
            self.assertEqual(dl.REPLAY, world.handle("req-1", _answer()))
        self.assertEqual(["patrol.dispatch-2"], world.canceled)
        self.assertEqual(dl.PENDING_RECHECK_S, world.slept[0])

    def test_the_cancel_is_tried_again_while_the_new_dispatcher_starts(self):
        world = _World(
            {"req-1": (dl.ANSWERED, "patrol.dispatch-1")},
            cancel_answers=[None, None, "dispatcher"],
        )
        with self.assertLogs(dl.logger, level="WARNING"):
            self.assertEqual(dl.REPLAY, world.handle("req-1", _answer()))
        self.assertEqual(3, len(world.canceled))
        self.assertIn("patrol.dispatch-2", dl.REFUSED)

    def test_awarded_already_it_is_canceled_at_the_fleet(self):
        world = _World(
            {"req-1": (dl.ANSWERED, "patrol.dispatch-1")}, cancel_answers=["fleet"]
        )
        with self.assertLogs(dl.logger, level="WARNING") as logs:
            self.assertEqual(dl.REPLAY, world.handle("req-1", _answer()))
        self.assertIn("canceled at the fleet", logs.output[0])

    def test_a_replay_that_cannot_be_canceled_stays_visible(self):
        """A guard that cannot act must not hide: the task may be auctioned
        and run, so its row and its states are kept."""
        world = _World({"req-1": (dl.ANSWERED, "patrol.dispatch-1")}, cancel_answers=[])
        with self.assertLogs(dl.logger, level="ERROR") as logs:
            verdict = world.handle("req-1", _answer())
        self.assertEqual("replay-not-canceled", verdict)
        self.assertEqual(dl.CANCEL_TRIES, len(world.canceled))
        self.assertEqual([], world.removed)
        self.assertNotIn("patrol.dispatch-2", dl.REFUSED)
        self.assertIn("could NOT be canceled", logs.output[0])


class TestTheBoringSide(unittest.TestCase):
    def _untouched(self, world, verdict, request_id="req-1", json_msg=None):
        got = world.handle(request_id, json_msg or _answer())
        self.assertEqual(verdict, got)
        self.assertEqual(([], [], []), (world.canceled, world.removed, world.noted))
        self.assertNotIn("patrol.dispatch-2", dl.REFUSED)

    def test_a_request_the_ledger_does_not_know_is_not_ours(self):
        self._untouched(_World(), dl.NOT_OURS)

    def test_a_pending_request_is_the_live_answer(self):
        self._untouched(_World({"req-1": (dl.PENDING, None)}), dl.STILL_PENDING)

    def test_the_same_answer_again_is_the_dispatcher_repeating_itself(self):
        self._untouched(_World({"req-1": (dl.ANSWERED, "patrol.dispatch-2")}), dl.SAME)

    def test_an_answer_that_is_not_a_dispatch_is_not_even_looked_up(self):
        world = _World({"req-1": (dl.ANSWERED, "patrol.dispatch-1")})
        self._untouched(world, None, json_msg='{"success": true}')
        self._untouched(world, None, json_msg=_answer(success=False))

    def test_the_refused_set_is_bounded(self):
        refused = dl.Refused(limit=3)
        for i in range(10):
            refused.add(f"t{i}", "r")
        self.assertEqual(3, len(refused))
        self.assertIn("t9", refused)
        self.assertNotIn("t0", refused)

    def test_the_ros_thread_hands_on_only_dispatch_answers(self):
        async def run():
            dl._loop = asyncio.get_running_loop()  # as guard_loop sets them
            dl._inbox = asyncio.Queue()
            try:
                dl.on_unclaimed_response("r1", '{"success": true}')
                dl.on_unclaimed_response("r2", _answer())
                await asyncio.sleep(0)
                return [dl._inbox.get_nowait() for _ in range(dl._inbox.qsize())]
            finally:
                dl._loop = dl._inbox = None

        got = asyncio.run(run())
        self.assertEqual(["r2"], [request_id for request_id, _msg in got])
        # before the app has started its loop: dropped, never raised
        dl.on_unclaimed_response("r3", _answer())


if __name__ == "__main__":
    unittest.main()
