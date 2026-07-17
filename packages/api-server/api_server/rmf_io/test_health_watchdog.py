import asyncio
import unittest
from typing import Any, List, Optional, Tuple, cast

from reactivex.scheduler.historicalscheduler import HistoricalScheduler

from api_server.models import FleetState, HealthStatus, RobotHealth, RobotState

from .events import RmfEvents
from .health_watchdog import HealthWatchdog


class StubAlertRepository:
    def __init__(self):
        self.created: List[Tuple[str, str]] = []

    async def create_alert(self, alert_id: str, category: str):
        self.created.append((alert_id, category))
        return None


class TestHealthWatchdogRobotHealth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = HistoricalScheduler()
        self.rmf = RmfEvents()
        self.alert_repository = StubAlertRepository()
        self.watchdog = HealthWatchdog(
            self.rmf,
            scheduler=self.scheduler,
            alert_repository=cast(Any, self.alert_repository),
        )
        # pylint: disable=protected-access
        self.watchdog._loop = asyncio.get_event_loop()
        await self.watchdog._watch_robot_health()

        self.health: Optional[RobotHealth] = None

        def assign(v: RobotHealth):
            self.health = v

        self.rmf.robot_health.subscribe(assign)

    @staticmethod
    def _fleet_state() -> FleetState:
        return FleetState(
            name="test_fleet", robots={"test_robot": RobotState(name="test_robot")}
        )

    async def test_heartbeat(self):
        self.rmf.fleet_states.on_next(self._fleet_state())
        assert self.health is not None
        self.assertEqual(self.health.id_, "test_fleet/test_robot")
        self.assertEqual(self.health.health_status, HealthStatus.HEALTHY)

        # no fleet state within the liveliness window, the robot is dead
        self.scheduler.advance_by(HealthWatchdog.LIVELINESS * 2)
        self.assertEqual(self.health.health_status, HealthStatus.DEAD)
        self.assertEqual(self.health.health_message, "heartbeat failed")

        # fleet state comes in again, the robot is alive
        self.rmf.fleet_states.on_next(self._fleet_state())
        self.assertEqual(self.health.health_status, HealthStatus.HEALTHY)

    async def test_offline_alert_once_per_episode(self):
        self.rmf.fleet_states.on_next(self._fleet_state())
        self.scheduler.advance_by(HealthWatchdog.LIVELINESS * 2)
        assert self.health is not None
        self.assertEqual(self.health.health_status, HealthStatus.DEAD)
        await asyncio.sleep(0)
        self.assertEqual(len(self.alert_repository.created), 1)
        alert_id, category = self.alert_repository.created[0]
        self.assertTrue(alert_id.startswith("robot_offline__test_fleet__test_robot__"))
        self.assertEqual(category, "robot")

        # repeated DEAD reports within the same episode do not create new alerts
        # pylint: disable=protected-access
        self.watchdog._on_robot_health(
            RobotHealth(
                id_="test_fleet/test_robot",
                health_status=HealthStatus.DEAD,
                health_message="heartbeat failed",
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(len(self.alert_repository.created), 1)

        # robot comes back online, the episode is reset
        self.rmf.fleet_states.on_next(self._fleet_state())
        self.assertEqual(self.health.health_status, HealthStatus.HEALTHY)
        self.scheduler.advance_by(HealthWatchdog.LIVELINESS * 2)
        self.assertEqual(self.health.health_status, HealthStatus.DEAD)
        await asyncio.sleep(0)
        self.assertEqual(len(self.alert_repository.created), 2)
