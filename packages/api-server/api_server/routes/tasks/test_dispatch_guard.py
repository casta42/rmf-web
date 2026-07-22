import unittest
from typing import Optional

from api_server.models import FleetState, RobotState, TaskRequest
from api_server.models.building_map import BuildingMap
from api_server.models.rmf_api.location_2D import Location2D
from api_server.models.rmf_api.robot_state import Status as RobotStatus

from .dispatch_guard import (
    OCCUPIED_TOLERANCE,
    find_vertex,
    parked_robot_near,
    patrol_final_place,
)


def make_map() -> BuildingMap:
    return BuildingMap(
        name="testsite_a",
        levels=[
            {
                "name": "L1",
                "elevation": 0.0,
                "images": [],
                "places": [],
                "doors": [],
                "nav_graphs": [
                    {
                        "name": "0",
                        "vertices": [
                            {"x": 26.1, "y": 1.5, "name": "j_s3", "params": []},
                            {"x": 28.5, "y": 2.0, "name": "patrol_1", "params": []},
                            {"x": 3.0, "y": 3.0, "name": "", "params": []},
                        ],
                        "edges": [],
                        "params": [],
                    }
                ],
                "wall_graph": {
                    "name": "",
                    "vertices": [],
                    "edges": [],
                    "params": [],
                },
            }
        ],
        lifts=[],
    )


def make_fleet(
    x: float,
    y: float,
    status: RobotStatus = RobotStatus.idle,
    task_id: Optional[str] = "",
) -> FleetState:
    return FleetState(
        name="test_fleet",
        robots={
            "test_robot": RobotState(
                name="test_robot",
                status=status,
                task_id=task_id,
                location=Location2D(map="L1", x=x, y=y, yaw=0),
                battery=1.0,
            )
        },
    )


def patrol_request(places) -> TaskRequest:
    return TaskRequest(category="patrol", description={"places": places, "rounds": 1})


class TestPatrolFinalPlace(unittest.TestCase):
    def test_final_place_of_patrol(self):
        self.assertEqual(patrol_final_place(patrol_request(["j_na", "j_s3"])), "j_s3")

    def test_non_patrol_fails_open(self):
        request = TaskRequest(category="compose", description={"anything": 1})
        self.assertIsNone(patrol_final_place(request))

    def test_malformed_description_fails_open(self):
        self.assertIsNone(patrol_final_place(patrol_request([])))
        self.assertIsNone(patrol_final_place(patrol_request([{"not": "a str"}])))
        request = TaskRequest(category="patrol", description="not a dict")
        self.assertIsNone(patrol_final_place(request))


class TestFindVertex(unittest.TestCase):
    def test_known_vertex(self):
        self.assertEqual(find_vertex(make_map(), "j_s3"), (26.1, 1.5))

    def test_unknown_vertex_fails_open(self):
        self.assertIsNone(find_vertex(make_map(), "no_such_place"))


class TestParkedRobotNear(unittest.TestCase):
    def test_parked_robot_occupies(self):
        fleets = [make_fleet(26.1, 1.5)]
        self.assertEqual(parked_robot_near(fleets, 26.1, 1.5), "test_fleet/test_robot")

    def test_within_tolerance_occupies(self):
        fleets = [make_fleet(26.1 + OCCUPIED_TOLERANCE - 0.01, 1.5)]
        self.assertIsNotNone(parked_robot_near(fleets, 26.1, 1.5))

    def test_outside_tolerance_is_free(self):
        fleets = [make_fleet(26.1 + OCCUPIED_TOLERANCE + 0.01, 1.5)]
        self.assertIsNone(parked_robot_near(fleets, 26.1, 1.5))

    def test_working_robot_does_not_occupy(self):
        # a robot mid-mission will vacate the spot on its own
        fleets = [make_fleet(26.1, 1.5, status=RobotStatus.working, task_id="task_1")]
        self.assertIsNone(parked_robot_near(fleets, 26.1, 1.5))

    def test_charging_robot_occupies_despite_task_id(self):
        # a charging robot carries its charge task_id but holds the spot
        fleets = [
            make_fleet(26.1, 1.5, status=RobotStatus.charging, task_id="Charge001")
        ]
        self.assertIsNotNone(parked_robot_near(fleets, 26.1, 1.5))

    def test_no_location_fails_open(self):
        fleet = make_fleet(0, 0)
        fleet.robots["test_robot"].location = None
        self.assertIsNone(parked_robot_near([fleet], 26.1, 1.5))
