"""F-463 (G ruling 2026-10-02, second sheet, item 1): the dispatcher-liveness
alarm and the live views it reads — "missions queued with no auction for N
seconds while robots are idle raises one critical alert. Prove both ways:
same-millisecond dispatch bursts never wedge it [the dispatcher's own
check, ops/e6/auction_early_close_check.py I-L]; a healthy idle queue never
alarms."

FIRES: QUEUED entries in every dispatch_states message for the whole
period, none of them leaving the queue, an idle robot — one alert, once;
resolved when a queued mission is auctioned again.
BORING, never an alert: nothing queued for hours (an idle night); a busy
queue being auctioned; queued with no idle robot; a server that has not
watched for the period yet; a first boot that has heard nothing; a server
that cannot hear the fleet core on ROS (cannot see: said in the log, never
convicted); a fleet state gone stale.
"""

import asyncio
import unittest

from api_server import dispatcher_liveness as dl
from api_server import live_floor

AFTER = 60.0


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Alerts:
    def __init__(self):
        self.created, self.resolved, self.swept = [], [], []

    async def create_alert(
        self, alert_id, category, severity=None, message=None, **_kw
    ):
        self.created.append((alert_id, category, severity, message))
        return {"id": alert_id}

    async def resolve_alert(self, alert_id, resolved_by="system"):
        self.resolved.append(alert_id)
        return {"id": alert_id}

    async def resolve_alerts_by_prefix(self, prefix, resolved_by="sweep"):
        self.swept.append(prefix)
        return []


class _Events:
    class _Sub:
        def __init__(self):
            self.seen = []

        def on_next(self, value):
            self.seen.append(value)

    def __init__(self):
        self.alerts = self._Sub()


class _Floor:
    """A fleet core as the server hears it: a dispatcher publishing every
    2 s, a fleet state every second, ROS heard."""

    def __init__(self):
        self.clock = _Clock()
        self.dispatcher = live_floor.DispatcherView(self.clock)
        self.fleets = live_floor.FleetView(self.clock)
        self.watch = dl.DispatchWatch()
        self.alerts = _Alerts()
        self.events = _Events()
        self.queued = []
        self.robots = {
            "r1": {"task_id": "", "status": "idle"},
            "r2": {"task_id": "patrol.dispatch-1", "status": "working"},
        }
        self.dispatcher_speaks = True
        self.ros_heard = True
        self.fleet_speaks = True
        self.did = []
        self.auctioned = 0

    def run(self, seconds, auction_every=None):
        end = self.clock.t + seconds
        tick = 0
        while self.clock.t < end:
            self.clock.t += 1.0
            tick += 1
            if self.fleet_speaks:
                self.fleets.on_fleet_state("gentle_fleet", self.robots)
            if self.ros_heard:
                self.dispatcher.on_core_heard()
            if auction_every and tick % auction_every == 0 and self.queued:
                # the head of the queue is auctioned; another mission comes
                self.auctioned += 1
                self.queued = self.queued[1:] + [f"new-{self.auctioned}"]
            if self.dispatcher_speaks and tick % 2 == 0:
                self.dispatcher.on_dispatch_states(
                    [(t, live_floor.QUEUED) for t in self.queued], []
                )
            if tick % 5 == 0:
                did = asyncio.run(
                    dl.process(
                        self.watch,
                        self.dispatcher,
                        self.fleets,
                        self.alerts,
                        self.events,
                        "critical",
                        after_s=AFTER,
                        wall=lambda: 1_790_000_000.0,
                    )
                )
                if did:
                    self.did.append(did)


class TestItFires(unittest.TestCase):
    def test_queued_no_auction_idle_robot_raises_one_alert_once(self):
        floor = _Floor()
        floor.run(30)  # in order, nothing queued
        floor.queued = ["patrol.dispatch-7", "patrol.dispatch-8"]
        floor.run(AFTER - 10)
        self.assertEqual([], floor.alerts.created, "not before the period")
        floor.run(20)
        self.assertEqual(1, len(floor.alerts.created))
        alert_id, category, severity, message = floor.alerts.created[0]
        self.assertTrue(alert_id.startswith("dispatch_stalled__"))
        self.assertEqual(("fleet", "critical"), (category, severity))
        self.assertIn("2 mission(s) are queued", message)
        self.assertIn("1 robot(s) stand idle", message)
        floor.run(600)  # it stays wedged: still ONE alert
        self.assertEqual(1, len(floor.alerts.created))
        self.assertEqual([], floor.alerts.resolved)

    def test_it_resolves_when_an_auction_starts_again(self):
        floor = _Floor()
        floor.queued = ["patrol.dispatch-7"]
        floor.run(AFTER + 20)
        self.assertEqual(1, len(floor.alerts.created))
        floor.queued = []  # it was auctioned at last
        floor.run(10)
        self.assertEqual([floor.alerts.created[0][0]], floor.alerts.resolved)
        # and a second episode is a second alert
        floor.queued = ["patrol.dispatch-9"]
        floor.run(AFTER + 20)
        self.assertEqual(2, len(floor.alerts.created))

    def test_a_dispatcher_that_went_silent_with_a_queue_is_the_same_stall(self):
        """Frozen or dead with missions queued: it publishes nothing more,
        and nothing is being auctioned either."""
        floor = _Floor()
        floor.queued = ["patrol.dispatch-7"]
        floor.run(10)
        floor.dispatcher_speaks = False
        floor.run(AFTER + 10)
        self.assertEqual(1, len(floor.alerts.created))


class TestTheBoringSide(unittest.TestCase):
    def test_an_idle_night_never_alarms(self):
        floor = _Floor()
        floor.run(8 * 3600)  # nothing queued, no auction
        self.assertEqual([], floor.alerts.created)

    def test_a_first_boot_that_has_heard_nothing_never_alarms(self):
        floor = _Floor()
        floor.dispatcher_speaks = floor.ros_heard = floor.fleet_speaks = False
        floor.run(3600)
        self.assertEqual([], floor.alerts.created)
        self.assertEqual([], floor.alerts.swept, "nothing heard: no sweep")

    def test_a_queue_that_is_being_auctioned_never_alarms(self):
        floor = _Floor()
        floor.queued = [f"patrol.dispatch-{i}" for i in range(39)]
        floor.run(3600, auction_every=20)  # a full window and an ack apart
        self.assertEqual([], floor.alerts.created)

    def test_queued_with_no_idle_robot_never_alarms(self):
        floor = _Floor()
        floor.robots["r1"] = {"task_id": "patrol.dispatch-2", "status": "working"}
        floor.queued = ["patrol.dispatch-7"]
        floor.run(3600)
        self.assertEqual([], floor.alerts.created)

    def test_a_server_that_just_started_waits_its_own_period(self):
        """The dispatcher was already wedged when this server came up: the
        server has to have watched for the period itself."""
        floor = _Floor()
        floor.queued = ["patrol.dispatch-7"]
        floor.dispatcher.on_dispatch_states(
            [("patrol.dispatch-7", live_floor.QUEUED)], []
        )
        floor.clock.t += 3600  # an hour before the server watches
        floor.run(AFTER - 10)
        self.assertEqual([], floor.alerts.created)
        floor.run(20)
        self.assertEqual(1, len(floor.alerts.created))

    def test_a_server_deaf_on_ros_cannot_see_and_never_convicts(self):
        floor = _Floor()
        floor.queued = ["patrol.dispatch-7"]
        floor.run(10)
        floor.ros_heard = False  # no ROS traffic reaches the server
        floor.dispatcher_speaks = False
        with self.assertLogs(dl.logger, level="WARNING") as logs:
            floor.run(600)
        self.assertEqual([], floor.alerts.created)
        said = [m for m in logs.output if "cannot judge" in m]
        self.assertEqual(1, len(said), "said once, not every pass")

    def test_a_stale_fleet_state_is_unknown_not_zero_and_not_idle(self):
        floor = _Floor()
        floor.queued = ["patrol.dispatch-7"]
        floor.run(10)
        floor.fleet_speaks = False
        with self.assertLogs(dl.logger, level="WARNING"):
            floor.run(600)
        self.assertEqual([], floor.alerts.created)

    def test_an_earlier_lifes_alert_is_swept_once_the_dispatcher_is_heard(self):
        floor = _Floor()
        floor.run(20)
        self.assertEqual(["dispatch_stalled__"], floor.alerts.swept)
        floor.run(600)
        self.assertEqual(["dispatch_stalled__"], floor.alerts.swept)


class TestVerdict(unittest.TestCase):
    def _snap(self, **kw):
        base = dict(
            first_heard=0.0,
            last_heard=100.0,
            active={},
            finished={},
            queued=1,
            queued_since=10.0,
            last_progress=None,
            auctions=0,
            core_heard=100.0,
        )
        base.update(kw)
        return live_floor.DispatcherSnapshot(**base)

    def test_the_quiet_time_runs_from_the_latest_of_three_moments(self):
        v = dl.verdict(
            self._snap(queued_since=10.0, last_progress=50.0), 1, 100.0, 0.0, AFTER
        )
        self.assertEqual(dl.OK, v[0], "a mission was auctioned 50 s ago")
        v = dl.verdict(self._snap(queued_since=10.0), 1, 100.0, 45.0, AFTER)
        self.assertEqual(dl.OK, v[0], "this server has watched for 55 s")
        v = dl.verdict(self._snap(queued_since=10.0), 1, 100.0, 0.0, AFTER)
        self.assertEqual(dl.STALLED, v[0])
        self.assertIn("no auction for 90 s", v[1])

    def test_the_period_is_the_sites_and_never_under_15_s(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"GF_DISPATCH_STALL_ALERT_S": "3"}):
            self.assertEqual(15.0, dl.stall_after_s())
        with patch.dict(os.environ, {"GF_DISPATCH_STALL_ALERT_S": "junk"}):
            self.assertEqual(60.0, dl.stall_after_s())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(60.0, dl.stall_after_s())


class TestTheViews(unittest.TestCase):
    def test_queued_since_restarts_when_the_queue_empties(self):
        clock = _Clock()
        view = live_floor.DispatcherView(clock)
        self.assertIsNone(view.snapshot().queued_since)
        view.on_dispatch_states([("a", live_floor.QUEUED)], [])
        first = view.snapshot().queued_since
        clock.t += 30
        view.on_dispatch_states([("b", live_floor.QUEUED)], [])
        self.assertEqual(first, view.snapshot().queued_since)
        view.on_dispatch_states([("b", live_floor.SELECTED)], [])
        self.assertIsNone(
            view.snapshot().queued_since,
            "awarded and awaiting its acknowledgment: not queued",
        )
        clock.t += 5
        view.on_dispatch_states([("c", live_floor.QUEUED)], [])
        self.assertEqual(clock.t, view.snapshot().queued_since)

    def test_an_auction_is_a_queued_task_that_left_the_queue(self):
        clock = _Clock()
        view = live_floor.DispatcherView(clock)
        view.on_dispatch_states(
            [("a", live_floor.QUEUED), ("b", live_floor.QUEUED)], []
        )
        self.assertIsNone(view.snapshot().last_progress)
        clock.t += 2
        view.on_dispatch_states(
            [("a", live_floor.QUEUED), ("b", live_floor.QUEUED)], []
        )
        self.assertIsNone(view.snapshot().last_progress, "nothing moved")
        clock.t += 2
        view.on_dispatch_states(
            [("a", live_floor.SELECTED), ("b", live_floor.QUEUED)], []
        )
        self.assertEqual(clock.t, view.snapshot().last_progress, "awarded")
        clock.t += 2
        view.on_dispatch_states(
            [], [("a", live_floor.DISPATCHED), ("b", live_floor.FAILED_TO_ASSIGN)]
        )
        self.assertEqual(clock.t, view.snapshot().last_progress, "no bid")
        self.assertEqual(2, view.snapshot().auctions)

    def test_the_server_never_listens_on_the_bid_notice_topic(self):
        """The dispatcher counts that topic's subscribers as the bidders
        an auction waits for (F-410's early close): a listener that never
        bids makes every auction run its whole window."""
        import pathlib
        import re

        here = pathlib.Path(__file__).resolve().parent
        for path in here.rglob("*.py"):
            if path.name.startswith("test_"):
                continue
            code = re.sub(r"#.*", "", path.read_text())
            self.assertNotRegex(
                code,
                r"create_subscription\([^)]*bid_notice",
                f"{path.name} subscribes to the dispatcher's bid notices",
            )

    def test_the_fleet_view_keeps_who_left_and_since_when_each_task(self):
        clock = _Clock()
        fleets = live_floor.FleetView(clock)
        fleets.on_fleet_state(
            "f",
            {
                "a": {"task_id": "t1", "status": "working"},
                "b": {"task_id": "", "status": "idle"},
            },
        )
        clock.t += 50
        fleets.on_fleet_state("f", {"a": {"task_id": "t1", "status": "working"}})
        seen = fleets.fleet("f")
        self.assertEqual(50, clock.t - seen.robots["b"].last_seen)
        self.assertEqual(50, clock.t - seen.robots["a"].task_since)
        fleets.on_fleet_state("f", {"a": {"task_id": "t2", "status": "working"}})
        self.assertEqual(0, clock.t - fleets.fleet("f").robots["a"].task_since)

    def test_idle_robots_are_unknown_when_no_fleet_is_fresh(self):
        clock = _Clock()
        fleets = live_floor.FleetView(clock)
        self.assertIsNone(live_floor.idle_robots(fleets.fleets(), clock.t, 10))
        fleets.on_fleet_state(
            "f",
            {
                "a": {"task_id": "", "status": "idle"},
                "b": {"task_id": "", "status": "offline"},
                "c": {"task_id": "", "status": "charging"},
            },
        )
        self.assertEqual(2, live_floor.idle_robots(fleets.fleets(), clock.t, 10))
        clock.t += 60
        self.assertIsNone(live_floor.idle_robots(fleets.fleets(), clock.t, 10))


if __name__ == "__main__":
    unittest.main()
