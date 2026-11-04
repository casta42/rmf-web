import unittest
from typing import Optional

from api_server.models import FleetState, RobotState, TaskRequest
from api_server.models.building_map import BuildingMap
from api_server.models.rmf_api.location_2D import Location2D
from api_server.models.rmf_api.robot_state import Status as RobotStatus

from .dispatch_guard import (
    OCCUPIED_TOLERANCE,
    find_vertex,
    isolated_place,
    no_stop_junction,
    parked_robot_near,
    patrol_places,
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

    def test_excluded_robot_does_not_occupy(self):
        # F-62: a direct robot task may target the robot's own parking
        # spot (send-to-charger from the charger) — exclude the target
        fleets = [make_fleet(26.1, 1.5)]
        self.assertIsNone(
            parked_robot_near(fleets, 26.1, 1.5, exclude="test_fleet/test_robot")
        )

    def test_exclude_other_robot_still_occupies(self):
        fleets = [make_fleet(26.1, 1.5)]
        self.assertEqual(
            parked_robot_near(fleets, 26.1, 1.5, exclude="test_fleet/other_robot"),
            "test_fleet/test_robot",
        )


# ----------------------------------------------------------------------
# F-111: a waypoint a zone has isolated must never reach the planner
# ----------------------------------------------------------------------
def _graph_with(vertices, lanes):
    """Derived-nav-graph stand-in, in the /zones/nav_graph shape."""
    return {
        "vertices": [{"name": n, "x": 0.0, "y": 0.0} for n in vertices],
        "lanes": [{"a": a, "b": b} for a, b in lanes],
    }


def test_isolated_place_is_found_by_name():
    # j_e1 (index 1) has no edges: a zone closed every lane to it.
    nav_graph = _graph_with(["j_w1", "j_e1", "j_s2"], [(0, 2)])
    assert isolated_place(nav_graph, ["j_e1"]) == "j_e1"
    assert isolated_place(nav_graph, ["j_w1"]) is None
    # order preserved: the first stranded place is the one reported
    assert (
        isolated_place(nav_graph, ["j_w1", "j_e1"]) == "j_e1"
    )


def test_unknown_place_is_left_to_the_fleet():
    nav_graph = _graph_with(["j_w1"], [(0, 0)])
    assert isolated_place(nav_graph, ["elsewhere"]) is None


def test_a_place_a_zone_cut_off_is_refused_by_name():
    """F-388 (D-24 §5): the fleet's graph drops the orphan, the derivation
    records it — the guard refuses it with the F-111 reason instead of
    leaving the fleet to reject an unknown name. Both ways: a place not
    recorded still passes, and a name no graph knows is still the fleet's."""
    nav_graph = _graph_with(["j_w1", "j_s2"], [(0, 1)])
    nav_graph["out_of_service"] = [{"name": "patrol_1", "x": 29.0, "y": 2.6}]
    assert isolated_place(nav_graph, ["patrol_1"]) == "patrol_1"
    assert isolated_place(nav_graph, ["j_w1"]) is None
    assert isolated_place(nav_graph, ["elsewhere"]) is None


def test_patrol_places_reads_every_stop_and_fails_open():
    request = TaskRequest(
        category="patrol", description={"places": ["a", "b"], "rounds": 1}
    )
    assert patrol_places(request) == ["a", "b"]
    assert patrol_places(
        TaskRequest(category="compose", description={})
    ) == []
    assert patrol_places(
        TaskRequest(category="patrol", description="nonsense")
    ) == []


# ----------------------------------------------------------------------
# F-268 / D-64: a frozen position feed is not a parked robot.
# ----------------------------------------------------------------------
class TestOccupancyIgnoresFrozenFeeds(unittest.TestCase):
    def tearDown(self):
        from api_server.routes import fleets as fleets_route

        fleets_route._reset_freshness_for_test()

    def test_a_robot_whose_feed_froze_does_not_block_the_waypoint(self):
        """KNOWN BAD: the fleet state says a parked robot sits on j_s3,
        but that pose stopped being accepted 30 s ago. The guard has no
        evidence it is there and must not refuse the dispatch on it."""
        from api_server.routes.test_internal import _freeze_robot

        _freeze_robot("test_fleet", "test_robot")
        fleets = [make_fleet(26.1, 1.5)]
        self.assertIsNone(parked_robot_near(fleets, 26.1, 1.5))

    def test_a_parked_robot_with_a_live_feed_still_blocks(self):
        """KNOWN GOOD: same fleet state, current feed -> occupied."""
        from api_server.routes import fleets as fleets_route
        from api_server.routes.test_internal import _Msg

        fleets_route._reset_freshness_for_test()
        for k in range(900):
            stamps = {"test_robot": 500.0 + k * 0.1, "other_a": 500.0 + k * 0.1}
            fleets_route.on_fleet_positions(_Msg("test_fleet", stamps), now=1000.0 + k * 0.1)
        fleets = [make_fleet(26.1, 1.5)]
        self.assertEqual(parked_robot_near(fleets, 26.1, 1.5), "test_fleet/test_robot")

    def test_with_no_freshness_report_the_guard_behaves_as_before(self):
        from api_server.routes import fleets as fleets_route

        fleets_route._reset_freshness_for_test()
        fleets = [make_fleet(26.1, 1.5)]
        self.assertEqual(parked_robot_near(fleets, 26.1, 1.5), "test_fleet/test_robot")


def _t_graph(**j_params):
    """A T in the /zones/nav_graph shape: junction j (0, 0), three two-way
    arms to named holding points a (4 m west), b (2 m south), c (6 m east)."""
    hold = {"is_holding_point": True}
    return {
        "vertices": [
            {"name": "j", "x": 0.0, "y": 0.0, "params": dict(j_params)},
            {"name": "a", "x": -4.0, "y": 0.0, "params": dict(hold)},
            {"name": "b", "x": 0.0, "y": -2.0, "params": dict(hold)},
            {"name": "c", "x": 6.0, "y": 0.0, "params": dict(hold)},
        ],
        "lanes": [{"a": 0, "b": i, "bidirectional": True} for i in (1, 2, 3)],
    }


def test_FIRES_PASSES_a_mission_to_a_named_no_stop_junction_is_refused_F909():
    """F-909 (D-142 second addendum, ee3). FIRE: a patrol through j, three
    lanes and no place mark, is refused with the reason and the nearest place
    (b, 2.0 m). PASS: the same patrol with j marked a holding point (a place
    at a junction), a patrol to places only, and a name the graph does not
    know (left to the fleet)."""
    got = no_stop_junction(_t_graph(), ["a", "j", "c"])
    assert got is not None and got["name"] == "j"
    assert got["nearest"] == "b" and got["metres"] == 2.0
    assert "no-stop junction" in got["message"] and "[b], 2.0 m" in got["message"]
    assert no_stop_junction(_t_graph(is_holding_point=True), ["j"]) is None
    assert no_stop_junction(_t_graph(), ["a", "c"]) is None
    assert no_stop_junction(_t_graph(), ["elsewhere"]) is None
    assert no_stop_junction(_t_graph(), []) is None


def test_FIRES_PASSES_the_nearest_place_follows_the_graphs_lane_directions_F909():
    """The normalized graph keeps each lane's direction: with j-b one way
    (b to j only, stored a=2, b=0), b cannot be reached from j and a (4 m)
    is named. PASS as two-way: b."""
    graph = _t_graph()
    graph["lanes"][1] = {"a": 2, "b": 0, "bidirectional": False}
    assert no_stop_junction(graph, ["j"])["nearest"] == "a"
    assert no_stop_junction(_t_graph(), ["j"])["nearest"] == "b"

