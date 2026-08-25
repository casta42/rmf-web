"""F-139 (E6 run-2, Blocker 4): condition alerts re-fire after an operator
resolves them while the condition persists.

The class rule: resolving an ALERT row never resolves the CONDITION. Every
episode tracker (robot_stuck, low_battery, robot_fault, FR-36 conditions)
must re-raise a fresh row once the resolution is ALERT_REFIRE_GRACE_MILLIS
old and the condition still holds — and must NOT re-raise while the row is
open or inside the grace window.

Pure-logic tests: `alert_repo` is swapped for a fake; no DB.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from api_server.app_config import app_config
from api_server.models import FleetState, RobotState
from api_server.models.rmf_api.location_2D import Location2D
from api_server.models.rmf_api.robot_state import Issue
from api_server.models.rmf_api.robot_state import Status as RobotStatus

from . import internal
from .internal import (
    ALERT_REFIRE_GRACE_MILLIS,
    _fault_alerted,
    _fault_stale_swept,
    _fr36_alerted,
    _fr36_stale_swept,
    _low_battery_alerted,
    _low_battery_stale_swept,
    _StuckState,
    _stuck_stale_swept,
    _stuck_states,
    process_robot_alerts,
    refire_due,
)

FLEET = "test_fleet"
ROBOT = "test_robot"
ROBOT_ID = f"{FLEET}/{ROBOT}"
NOW = 10_000_000


class FakeAlertRepo:
    """Captures alert traffic; `resolved_at` maps alert id -> resolved
    millis (missing id behaves like the real repo's missing row: 0)."""

    def __init__(self):
        self.created = []
        self.server_resolved = []
        self.resolved_at = {}
        self.open_ids = set()

    async def resolved_millis(self, alert_id: str) -> Optional[int]:
        if alert_id in self.open_ids:
            return None
        return self.resolved_at.get(alert_id, 0)

    async def create_alert(self, alert_id, category, severity=None,
                           fleet=None, robot=None, message=None):
        self.created.append(alert_id)
        self.open_ids.add(alert_id)
        return SimpleNamespace(id=alert_id, message=message)

    async def resolve_alert(self, alert_id, resolved_by="system"):
        self.server_resolved.append(alert_id)
        self.open_ids.discard(alert_id)
        return SimpleNamespace(id=alert_id)

    async def resolve_alerts_by_prefix(self, prefix, resolved_by="sweep"):
        return []


def robot_state(battery=0.9, task_id="task_1", status=RobotStatus.working,
                issues=None) -> RobotState:
    return RobotState(
        name=ROBOT,
        status=status,
        task_id=task_id,
        location=Location2D(map="L1", x=5.0, y=5.0, yaw=0),
        battery=battery,
        issues=issues or [],
    )


def fleet_state(robot: RobotState) -> FleetState:
    return FleetState(name=FLEET, robots={ROBOT: robot})


class RefireBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # process_robot_alerts stamps episodes with the real wall clock —
        # resolved-at values must be relative to it, not to a constant
        self.now = round(datetime.now().timestamp() * 1e3)
        self.repo = FakeAlertRepo()
        self._real_repo = internal.alert_repo
        internal.alert_repo = self.repo
        for tracker in (_stuck_states, _low_battery_alerted,
                        _fault_alerted, _fr36_alerted):
            tracker.clear()
        # mark this robot/fleet already swept so sweeps stay out of the way
        _stuck_stale_swept.add(ROBOT_ID)
        _low_battery_stale_swept.add(ROBOT_ID)
        _fault_stale_swept.add(ROBOT_ID)
        _fr36_stale_swept.add(FLEET)

    def tearDown(self):
        internal.alert_repo = self._real_repo


class TestRefireDue(unittest.TestCase):
    def test_open_row_never_refires(self):
        self.assertFalse(refire_due(None, NOW))

    def test_inside_grace_waits(self):
        self.assertFalse(
            refire_due(NOW - ALERT_REFIRE_GRACE_MILLIS + 1, NOW))

    def test_after_grace_fires(self):
        self.assertTrue(refire_due(NOW - ALERT_REFIRE_GRACE_MILLIS, NOW))

    def test_missing_row_counts_as_long_resolved(self):
        self.assertTrue(refire_due(0, NOW))


class TestStuckRefire(RefireBase):
    def _stuck_episode(self, alert_id="robot_stuck__old"):
        # anchor matches the robot's position -> still stuck, episode open
        _stuck_states[ROBOT_ID] = _StuckState(
            5.0, 5.0, self.now - 600_000, alert_id=alert_id)

    async def test_operator_resolved_and_still_stuck_refires(self):
        self._stuck_episode()
        self.repo.resolved_at["robot_stuck__old"] = (
            self.now - ALERT_REFIRE_GRACE_MILLIS)
        await process_robot_alerts(fleet_state(robot_state()))
        refired = [a for a in self.repo.created
                   if a.startswith("robot_stuck__")]
        self.assertEqual(len(refired), 1)
        # the tracker now owns the fresh row
        self.assertEqual(_stuck_states[ROBOT_ID].alert_id, refired[0])

    async def test_open_row_does_not_refire(self):
        self._stuck_episode()
        self.repo.open_ids.add("robot_stuck__old")
        await process_robot_alerts(fleet_state(robot_state()))
        self.assertEqual(
            [a for a in self.repo.created if a.startswith("robot_stuck__")],
            [])

    async def test_inside_grace_does_not_refire(self):
        self._stuck_episode()
        self.repo.resolved_at["robot_stuck__old"] = self.now - 1_000
        await process_robot_alerts(fleet_state(robot_state()))
        self.assertEqual(
            [a for a in self.repo.created if a.startswith("robot_stuck__")],
            [])

    async def test_moved_robot_resolves_instead_of_refiring(self):
        _stuck_states[ROBOT_ID] = _StuckState(
            0.0, 0.0, self.now - 600_000, alert_id="robot_stuck__old")
        self.repo.resolved_at["robot_stuck__old"] = (
            self.now - ALERT_REFIRE_GRACE_MILLIS)
        await process_robot_alerts(fleet_state(robot_state()))  # at (5, 5)
        self.assertEqual(
            [a for a in self.repo.created if a.startswith("robot_stuck__")],
            [])
        self.assertIsNone(_stuck_states[ROBOT_ID].alert_id)


class TestLowBatteryRefire(RefireBase):
    async def test_still_low_after_resolve_refires(self):
        _low_battery_alerted[ROBOT_ID] = "low_battery__old"
        self.repo.resolved_at["low_battery__old"] = (
            self.now - ALERT_REFIRE_GRACE_MILLIS)
        low = app_config.low_battery_threshold - 0.05
        await process_robot_alerts(fleet_state(robot_state(battery=low)))
        refired = [a for a in self.repo.created
                   if a.startswith("low_battery__")]
        self.assertEqual(len(refired), 1)
        self.assertEqual(_low_battery_alerted[ROBOT_ID], refired[0])

    async def test_hysteresis_band_does_not_refire(self):
        # above threshold (episode legitimately open, condition not
        # strictly low) -> no re-fire; it re-fires only if it drops again
        _low_battery_alerted[ROBOT_ID] = "low_battery__old"
        self.repo.resolved_at["low_battery__old"] = (
            self.now - ALERT_REFIRE_GRACE_MILLIS)
        mid = app_config.low_battery_threshold + 0.01
        await process_robot_alerts(fleet_state(robot_state(battery=mid)))
        self.assertEqual(
            [a for a in self.repo.created if a.startswith("low_battery__")],
            [])

    async def test_faulted_robot_battery_never_refires(self):
        # F-42: a faulted robot's SoC is spoofed to 0 — the fault alert
        # owns the page, the battery row must stay quiet
        _low_battery_alerted[ROBOT_ID] = "low_battery__old"
        self.repo.resolved_at["low_battery__old"] = (
            self.now - ALERT_REFIRE_GRACE_MILLIS)
        down = robot_state(
            battery=0.0,
            issues=[Issue(category="robot_down", detail={"d": 1})])
        await process_robot_alerts(fleet_state(down))
        self.assertEqual(
            [a for a in self.repo.created if a.startswith("low_battery__")],
            [])


class TestFaultRefire(RefireBase):
    DOWN = [Issue(category="robot_down", detail={"detail": "battery floor"})]

    async def test_fault_persisting_after_resolve_refires(self):
        _fault_alerted[ROBOT_ID] = "robot_fault__old"
        self.repo.resolved_at["robot_fault__old"] = (
            self.now - ALERT_REFIRE_GRACE_MILLIS)
        await process_robot_alerts(
            fleet_state(robot_state(issues=list(self.DOWN))))
        refired = [a for a in self.repo.created
                   if a.startswith("robot_fault__")]
        self.assertEqual(len(refired), 1)
        self.assertEqual(_fault_alerted[ROBOT_ID], refired[0])

    async def test_open_fault_row_does_not_refire(self):
        _fault_alerted[ROBOT_ID] = "robot_fault__old"
        self.repo.open_ids.add("robot_fault__old")
        await process_robot_alerts(
            fleet_state(robot_state(issues=list(self.DOWN))))
        self.assertEqual(
            [a for a in self.repo.created if a.startswith("robot_fault__")],
            [])

    async def test_cleared_fault_resolves_and_does_not_refire(self):
        _fault_alerted[ROBOT_ID] = "robot_fault__old"
        self.repo.open_ids.add("robot_fault__old")
        await process_robot_alerts(fleet_state(robot_state()))
        self.assertIn("robot_fault__old", self.repo.server_resolved)
        self.assertEqual(
            [a for a in self.repo.created if a.startswith("robot_fault__")],
            [])


class TestFr36Refire(RefireBase):
    def _blocked_robot(self) -> RobotState:
        return robot_state(issues=[Issue(
            category="fleet_blocked_escalation",
            detail={"episode": "ep1", "reason": "no sidestep",
                    "blocker": ROBOT, "waiting": ["other"]})])

    async def test_live_escalation_resolved_by_operator_refires(self):
        key = f"{FLEET}--ep1"
        _fr36_alerted[key] = ("fr36__old", "fleet_blocked_escalation")
        self.repo.resolved_at["fr36__old"] = self.now - ALERT_REFIRE_GRACE_MILLIS
        await process_robot_alerts(fleet_state(self._blocked_robot()))
        refired = [a for a in self.repo.created if a.startswith("fr36__")]
        self.assertEqual(len(refired), 1)
        self.assertEqual(_fr36_alerted[key][0], refired[0])

    async def test_open_escalation_row_does_not_refire(self):
        key = f"{FLEET}--ep1"
        _fr36_alerted[key] = ("fr36__old", "fleet_blocked_escalation")
        self.repo.open_ids.add("fr36__old")
        await process_robot_alerts(fleet_state(self._blocked_robot()))
        self.assertEqual(
            [a for a in self.repo.created if a.startswith("fr36__")], [])


if __name__ == "__main__":
    unittest.main()
