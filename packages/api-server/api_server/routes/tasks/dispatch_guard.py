# F-34: with `finishing_request: "nothing"` a robot parks wherever its
# mission ends, and RMF does not arbitrate two finished robots claiming the
# same waypoint — in sim they stack silently, on hardware that is a
# collision (0.30 m footprints). Until the finishing_request design is
# settled (Phase G hardware review), dispatch is guarded instead: a patrol
# whose final destination is already occupied by a parked robot is rejected
# with 409 at dispatch time.
#
# The guard fails open by design: no building map yet, unknown vertex,
# non-patrol category or malformed description all mean "no objection" —
# rejecting dispatches on missing data would break bringup.

import math
from typing import List, Optional, Tuple

from api_server import models as mdl
from api_server.models.building_map import BuildingMap
from api_server.models.rmf_api.robot_state import Status as RobotStatus

# Two Gentle Bot footprint radii (0.30 m each, CLAUDE.md spec): a robot
# parked closer than this to the destination vertex makes the mission a
# collision course on hardware.
OCCUPIED_TOLERANCE = 0.6


def _is_parked(robot: mdl.RobotState) -> bool:
    """True when the robot is going to stay where it is: idle with no
    task, or charging (a charging robot carries its charge task_id but
    holds its charger for the full dwell)."""
    if robot.status == RobotStatus.charging:
        return True
    return robot.status == RobotStatus.idle and not robot.task_id


def patrol_final_place(request: mdl.TaskRequest) -> Optional[str]:
    """Final destination place name of a patrol request, or None for
    anything the guard does not understand (fails open)."""
    if request.category != "patrol" or not isinstance(request.description, dict):
        return None
    places = request.description.get("places")
    if not isinstance(places, list) or not places:
        return None
    place = places[-1]
    return place if isinstance(place, str) else None


def find_vertex(building_map: BuildingMap, place: str) -> Optional[Tuple[float, float]]:
    """(x, y) of a named nav-graph vertex, or None if unknown."""
    for level in building_map.levels:
        for graph in level.nav_graphs:
            for vertex in graph.vertices:
                if vertex.name == place:
                    return (vertex.x, vertex.y)
    return None


def parked_robot_near(
    fleet_states: List[mdl.FleetState],
    x: float,
    y: float,
    tolerance: float = OCCUPIED_TOLERANCE,
    exclude: Optional[str] = None,
) -> Optional[str]:
    """`fleet/robot` id of a parked robot within tolerance of (x, y), or
    None. A robot counts as parked when it reports a parked status and no
    active task — robots mid-mission will vacate the spot on their own.
    `exclude` skips one `fleet/robot` id: for a direct robot task the
    target robot may already be parked at its own destination (e.g.
    send-to-charger while sitting near the charger, F-62) — it is not in
    its own way."""
    for fleet in fleet_states:
        if fleet.name is None or not fleet.robots:
            continue
        for robot_name, robot in fleet.robots.items():
            if f"{fleet.name}/{robot_name}" == exclude:
                continue
            if robot.location is None or not _is_parked(robot):
                continue
            if math.hypot(robot.location.x - x, robot.location.y - y) <= tolerance:
                return f"{fleet.name}/{robot_name}"
    return None


# F-111: a waypoint every one of whose lanes a no-go zone closed is
# ISOLATED — it still exists in the nav graph, with no way in or out.
# Dispatching a patrol that starts at such a place SEGFAULTS the fleet
# adapter (rmf_traffic planner, exit -11, reproduced deterministically);
# the adapter does not respawn, so the whole fleet silently stops bidding
# on every task until someone restarts fleet coordination. The layer we
# own refuses the dispatch instead, naming the place — an operator error
# must never be able to take the fleet down.
#
# Same fail-open posture as the F-34 guard: unknown place, no building
# map, or anything unparsed means "no objection".
def patrol_places(request: mdl.TaskRequest) -> List[str]:
    """Every place name of a patrol request, in order."""
    if request.category != "patrol" or not isinstance(request.description, dict):
        return []
    places = request.description.get("places")
    if not isinstance(places, list):
        return []
    return [p for p in places if isinstance(p, str)]


def isolated_place(nav_graph: dict, places: List[str]) -> Optional[str]:
    """First place that exists in the DERIVED nav graph but has no lane
    attached to it, or None. The derived graph (not the building map) is
    the only source that reflects zone closures — the building map keeps
    the authored lanes and would report every waypoint as reachable.
    A place the graph does not know at all is left to the fleet to
    reject (it may belong to another map)."""
    vertices = nav_graph.get("vertices") or []
    lanes = nav_graph.get("lanes") or []
    for place in places:
        index = next(
            (i for i, vertex in enumerate(vertices)
             if vertex.get("name") == place),
            None,
        )
        if index is None:
            continue
        attached = any(
            lane.get("a") == index or lane.get("b") == index
            for lane in lanes
        )
        if not attached:
            return place
    return None
