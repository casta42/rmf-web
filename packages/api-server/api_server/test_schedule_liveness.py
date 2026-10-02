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
