"""FR-39a (G close-out ruling 2026-10-01, 3a; D-86): ONE critical alert
when traffic coordination is lost — proven both ways on the real pass
(`process`) against a repository double with the real AlertRepository's
semantics (create_alert re-opens one row per id; resolve_alert archives an
OPEN alert and returns it, else None; resolve_alerts_by_prefix archives
every open one under the prefix).

  * the boring cases fire nothing: a fresh boot, the schedule alive, and a
    schedule never seen alive (boot order) — no alert;
  * a loss after the schedule was alive raises ONE critical alert, however
    many liveliness events repeat it; the return resolves it;
  * a drop-and-return faster than the loop still shows the episode;
  * an alert left open by an earlier api-server life stays open while the
    schedule is dead and is resolved when it is seen alive.
"""

import asyncio
import unittest
from types import SimpleNamespace

from api_server import schedule_liveness as sl


class Repo:
    def __init__(self):
        self.rows = {}
        self.creates = 0

    async def create_alert(self, alert_id, category, severity="warning",
                           fleet=None, robot=None, message=None):
        self.creates += 1
        row = SimpleNamespace(id=alert_id, category=category,
                              severity=severity, message=message,
                              unix_millis_resolved_time=None)
        self.rows[alert_id] = row
        return row

    async def resolve_alert(self, alert_id, resolved_by="system"):
        row = self.rows.get(alert_id)
        if row is None or row.unix_millis_resolved_time is not None:
            return None
        row.unix_millis_resolved_time = 1
        return row

    async def resolve_alerts_by_prefix(self, prefix, resolved_by="sweep"):
        out = []
        for row in self.rows.values():
            if row.id.startswith(prefix) and row.unix_millis_resolved_time is None:
                row.unix_millis_resolved_time = 1
                out.append(row)
        return out

    def open(self):
        return [r for r in self.rows.values()
                if r.unix_millis_resolved_time is None]


class Events:
    def __init__(self):
        self.sent = []
        self.alerts = SimpleNamespace(on_next=self.sent.append)


class Clock:
    def __init__(self):
        self.t = 1_790_900_000.0

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


class ScheduleLivenessTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = sl.ScheduleLiveness(self.clock)
        self.repo = Repo()
        self.events = Events()

    def tick(self):
        return run(sl.process(self.state, self.repo, self.events, "critical"))

    def drain(self, n=4):
        return [self.tick() for _ in range(n)]

    def test_the_boring_cases_fire_nothing(self):
        self.assertEqual(self.drain(), [None] * 4)          # fresh boot
        self.state.on_liveliness(1)
        self.assertEqual(self.drain(), [sl.SWEEP, None, None, None])
        self.assertEqual(self.repo.creates, 0)
        self.assertEqual(self.repo.open(), [])

    def test_a_schedule_never_seen_alive_raises_nothing(self):
        self.state.on_liveliness(0)
        self.assertEqual(self.drain(), [None] * 4)
        self.assertEqual(self.repo.creates, 0)

    def test_a_loss_raises_one_critical_alert_and_the_return_resolves_it(self):
        self.state.on_liveliness(1)
        self.drain()
        self.state.on_liveliness(0)
        self.state.on_liveliness(0)
        self.assertEqual(self.drain(), [sl.RAISE, None, None, None])
        (alert,) = self.repo.open()
        self.assertEqual(alert.severity, "critical")
        self.assertTrue(alert.id.startswith(sl.ALERT_PREFIX))
        self.assertIn("Traffic coordination lost", alert.message)
        self.assertEqual(self.repo.creates, 1)
        self.state.on_liveliness(1)
        self.assertEqual(self.drain(), [sl.RESOLVE, None, None, None])
        self.assertEqual(self.repo.open(), [])
        self.assertEqual(len(self.events.sent), 2)

    def test_a_drop_faster_than_the_loop_still_shows_the_episode(self):
        self.state.on_liveliness(1)
        self.drain()
        self.state.on_liveliness(0)
        self.state.on_liveliness(1)
        self.assertEqual(self.drain(), [sl.RAISE, sl.RESOLVE, None, None])
        self.assertEqual(self.repo.creates, 1)
        self.assertEqual(self.repo.open(), [])

    # -- the shapes measured on the stack's DDS, 2026-10-02 (a reader's
    # liveliness events when the schedule node goes and a new one starts
    # 2 s later; ops/e6/evidence/f4/stress-d85/f454-liveliness-on-kill/) --

    def test_a_crash_and_fast_restart_never_reads_zero_and_is_still_one_alert(self):
        """SIGSEGV / SIGKILL: the dead writer is only dropped when its
        lease runs out, so the count reads 1 -> 2 -> 1. Before this the
        watcher raised only on 0: the crash it exists for raised nothing."""
        self.state.on_liveliness(1, 1)
        self.drain()
        self.state.on_liveliness(2, 1)        # the restarted node matches
        self.assertEqual(self.drain(), [sl.RAISE, sl.RESOLVE, None, None])
        self.state.on_liveliness(1, -1)       # the dead writer is dropped
        self.assertEqual(self.drain(), [None] * 4)
        self.assertEqual(self.repo.creates, 1, "ONE alert for the outage")
        self.assertEqual(self.repo.open(), [])
        self.assertEqual(self.state.outages, 1)

    def test_a_clean_stop_and_restart_reads_zero_and_is_one_alert(self):
        """SIGTERM (docker restart, a site-config apply): 1 -> 0 -> 1."""
        self.state.on_liveliness(1, 1)
        self.drain()
        self.state.on_liveliness(0, -1)
        self.assertEqual(self.drain(), [sl.RAISE, None, None, None])
        self.state.on_liveliness(1, 1)
        self.assertEqual(self.drain(), [sl.RESOLVE, None, None, None])
        self.assertEqual(self.repo.creates, 1)
        self.assertEqual(self.state.outages, 1)

    def test_a_stall_past_the_lease_then_the_restart_is_one_alert(self):
        """SIGSTOP past the lease (F-453): 1 -> 0, the adapter restarts
        rmf-core, the new writer is alive, the stalled one is dropped."""
        self.state.on_liveliness(1, 1)
        self.drain()
        self.state.on_liveliness(0, -1)
        self.assertEqual(self.drain(), [sl.RAISE, None, None, None])
        self.state.on_liveliness(1, 1)
        self.state.on_liveliness(1, 0)        # the stalled writer's removal
        self.assertEqual(self.drain(), [sl.RESOLVE, None, None, None])
        self.assertEqual(self.repo.creates, 1)

    def test_PASSES_a_boot_that_finds_two_writers_raises_nothing(self):
        """The api-server starting inside a crash's overlap window: it never
        saw the schedule alive before, so nothing was lost on its watch."""
        self.state.on_liveliness(2, 2)
        self.state.on_liveliness(1, -1)
        self.assertEqual(self.drain(), [sl.SWEEP, None, None, None])
        self.assertEqual(self.repo.creates, 0)
        self.assertEqual(self.state.outages, 0)

    def test_PASSES_repeated_events_of_one_live_writer_raise_nothing(self):
        self.state.on_liveliness(1, 1)
        for _ in range(5):
            self.state.on_liveliness(1, 0)
        self.assertEqual(self.drain(), [sl.SWEEP, None, None, None])
        self.assertEqual(self.repo.creates, 0)

    def test_the_alerts_id_is_the_shape_the_runbook_keys_on(self):
        """F3.2: the id is built at the emit site from a literal prefix
        (the alert catalogue's guard reads it there) and is the shape
        alert_id_of() and the start-of-life sweep's prefix use."""
        self.state.on_liveliness(1, 1)
        self.drain()
        self.clock.t = 1_790_000_123.9
        self.state.on_liveliness(0, -1)
        self.drain()
        (alert,) = self.repo.open()
        self.assertEqual(alert.id, sl.alert_id_of(1, 1_790_000_123.9))
        self.assertEqual(alert.id, "traffic_schedule_lost__1790000123_1")
        self.assertTrue(alert.id.startswith(sl.ALERT_PREFIX))

    def test_the_gateway_hands_the_change_count_through(self):
        import pathlib

        gateway = (pathlib.Path(sl.__file__).parent / "gateway.py").read_text()
        self.assertIn("event.alive_count, event.alive_count_change", gateway)

    def test_a_second_outage_is_a_second_alert(self):
        self.state.on_liveliness(1)
        self.drain()
        self.state.on_liveliness(0)
        self.drain()
        self.state.on_liveliness(1)
        self.drain()
        self.clock.t += 600
        self.state.on_liveliness(0)
        self.assertEqual(self.drain(), [sl.RAISE, None, None, None])
        self.assertEqual(self.repo.creates, 2)
        self.assertEqual(len(self.repo.open()), 1)
        self.assertEqual(self.state.outages, 2)

    def test_an_earlier_lifes_alert_waits_for_the_schedule_to_be_alive(self):
        run(self.repo.create_alert(sl.ALERT_PREFIX + "old_1", "fleet",
                                   severity="critical", message="old"))
        self.state.on_liveliness(0)
        self.assertEqual(self.drain(), [None] * 4)
        self.assertEqual(len(self.repo.open()), 1)          # still true
        self.state.on_liveliness(1)
        self.assertEqual(self.drain(), [sl.SWEEP, None, None, None])
        self.assertEqual(self.repo.open(), [])


if __name__ == "__main__":
    unittest.main()
