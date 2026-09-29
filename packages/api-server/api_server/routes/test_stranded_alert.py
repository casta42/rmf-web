"""F-391 (G ruling 2026-09-28): a robot the fleet has REFUSED TO RECOVER
reaches the operator's bell.

The adapter raises ONE standing issue while it cannot drive a robot back
to the route network (`robot_stranded_off_graph`); this turns it into ONE
critical alert per episode, naming the robot, where it is, the keep-out
across its way home and what a person must do — and resolves it the
moment the adapter drops the issue.

Both ways: the alert fires on the live shape and says the actionable
thing; and nothing fires for a robot with no such issue, for an issue
with no episode, or twice for one episode. Pure logic — `alert_repo` is
the same fake the refire tests use, no DB.
"""

import unittest
from types import SimpleNamespace

from api_server.models import FleetState, RobotState
from api_server.models.rmf_api.location_2D import Location2D
from api_server.models.rmf_api.robot_state import Issue
from api_server.models.rmf_api.robot_state import Status as RobotStatus

from . import internal
from .internal import (
    _reset_stranded_for_test,
    _stranded_alerted,
    _stranded_message,
    process_stranded_conditions,
)
from .test_alert_refire import FakeAlertRepo

FLEET = "test_fleet"
ROBOT = "gentle_bot_5"
NOW = 10_000_000
EPISODE = "stranded-gentle_bot_5-1790000000"


def _issue(zone="office", position=(7.22, 18.07), episode=EPISODE):
    detail = {
        "robot": ROBOT,
        "fleet": FLEET,
        "zone": zone,
        "position": list(position) if position else None,
        "detail": "the robot is off the lane graph and the fleet cannot "
        "drive it back; a person must move it clear",
    }
    if episode is not None:
        detail["episode"] = episode
    return Issue(category="robot_stranded_off_graph", detail=detail)


def _fleet(issues) -> FleetState:
    return FleetState(
        name=FLEET,
        robots={
            ROBOT: RobotState(
                name=ROBOT,
                status=RobotStatus.idle,
                task_id=None,
                location=Location2D(map="L1", x=7.22, y=18.07, yaw=0),
                battery=0.53,
                issues=issues,
            )
        },
    )


class StrandedBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = FakeAlertRepo()
        self._real_repo = internal.alert_repo
        internal.alert_repo = self.repo
        _reset_stranded_for_test()
        # the per-fleet sweep is not what these tests are about
        internal._stranded_stale_swept.add(FLEET)

    def tearDown(self):
        internal.alert_repo = self._real_repo
        _reset_stranded_for_test()


class TestStrandedFires(StrandedBase):
    async def test_the_live_shape_raises_one_critical_alert(self):
        await process_stranded_conditions(_fleet([_issue()]), NOW)
        self.assertEqual(1, len(self.repo.created))
        self.assertTrue(self.repo.created[0].startswith(f"stranded__{FLEET}__"))

    async def test_the_message_says_where_why_and_what_to_do(self):
        text = _stranded_message(_issue().detail)
        self.assertIn(ROBOT, text)
        self.assertIn("off the route network", text)
        self.assertIn("(7.2, 18.1)", text)
        self.assertIn("[office]", text)
        self.assertIn("someone must move the robot", text)
        self.assertIn("Site settings", text)

    async def test_with_no_zone_it_still_says_the_fleet_cannot_reach_it(self):
        text = _stranded_message(_issue(zone="", position=None).detail)
        self.assertIn("no waypoint it may rest on can be reached", text)
        self.assertNotIn("[]", text)

    async def test_one_alert_per_episode_however_often_the_state_arrives(self):
        for _ in range(5):
            await process_stranded_conditions(_fleet([_issue()]), NOW)
        self.assertEqual(1, len(self.repo.created))

    async def test_it_resolves_when_the_robot_is_recovered(self):
        await process_stranded_conditions(_fleet([_issue()]), NOW)
        alert_id = self.repo.created[0]
        await process_stranded_conditions(_fleet([]), NOW + 1000)
        self.assertIn(alert_id, self.repo.server_resolved)
        self.assertEqual({}, dict(_stranded_alerted))

    async def test_a_new_episode_after_recovery_alerts_again(self):
        await process_stranded_conditions(_fleet([_issue()]), NOW)
        await process_stranded_conditions(_fleet([]), NOW + 1000)
        await process_stranded_conditions(
            _fleet([_issue(episode="stranded-gentle_bot_5-1790000999")]),
            NOW + 2000,
        )
        self.assertEqual(2, len(self.repo.created))


class TestStrandedPasses(StrandedBase):
    async def test_a_robot_with_no_such_issue_raises_nothing(self):
        await process_stranded_conditions(_fleet([]), NOW)
        self.assertEqual([], self.repo.created)

    async def test_another_condition_is_not_this_one(self):
        other = Issue(
            category="charger_unreachable",
            detail={"robot": ROBOT, "episode": "charger-1"},
        )
        await process_stranded_conditions(_fleet([other]), NOW)
        self.assertEqual([], self.repo.created)

    async def test_an_issue_with_no_episode_is_not_alerted_on(self):
        """Without an episode there is nothing to resolve later — the
        alert would outlive the condition, which is F-39's ghost."""
        await process_stranded_conditions(_fleet([_issue(episode=None)]), NOW)
        self.assertEqual([], self.repo.created)

    async def test_a_malformed_detail_never_takes_the_loop_down(self):
        broken = Issue(category="robot_stranded_off_graph", detail=None)
        await process_stranded_conditions(_fleet([broken]), NOW)
        self.assertEqual([], self.repo.created)
        # ...and a position that is not a pair is simply left unsaid
        text = _stranded_message({"robot": ROBOT, "position": "nowhere"})
        self.assertIn(ROBOT, text)
        self.assertNotIn("nowhere", text)


if __name__ == "__main__":
    unittest.main()
