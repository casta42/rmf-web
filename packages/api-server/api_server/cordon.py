# F-332 (api-server half) — an operator's lane closure is honoured BEFORE a
# task is queued, not only once a robot is driving.
#
# The adapter half (F-223's `_leg_blocker`, extended 2026-09-18) refuses a
# single direct-drive LEG that crosses a cordon. That is the last line, and
# it holds. But a direct task request never reaches it as a refusal the
# operator can see: RMF estimates the finish state, fails, logs
#
#     Unable to estimate final state for direct task request ... still added
#
# and queues the task anyway. The operator sees a mission accepted, then a
# robot that never arrives. The refusal belongs here, with a reason.
#
# ---------------------------------------------------------------------------
# WHICH GRAPH (F-333 — this is the whole difficulty)
# ---------------------------------------------------------------------------
# `/closed_lanes` carries bare integer lane indices and does not say what
# they index. Measured on testsite_a, 2026-09-18, the SAME index 16 is:
#
#   /nav_graphs (fleet gentle_fleet), 102 edges
#       -> gentle_bot_3_charger (1.50,7.60) -> j_w1 (3.00,7.60)   <-- correct
#   api-server derived_nav_graph(), 62 lanes
#       -> (22.70,10.40) -> (22.70,11.66)                          <-- 20 m away
#   api-server BuildingMap.nav_graphs[0], 29 lanes
#       -> j_w2 (3.00,11.60) -> j_e2 (21.50,11.60)                 <-- authored
#
# The fleet's graph is the DIRECTED expansion of the derived one (40
# bidirectional + 22 one-way = 62 stored -> 102 directed). `/closed_lanes` is
# published by the fleet adapter and indexes the fleet's graph.
#
# So this module reads `/nav_graphs` and works ONLY in that space. It never
# consults the derived graph or the building map, however convenient their
# vertex names are. Using the derived graph here would have refused missions
# into an unrelated aisle while queueing the one into the real cordon — a
# silent failure in the "cordon not enforced" direction.
#
# ---------------------------------------------------------------------------
# WHAT IT CLAIMS, AND WHAT IT DELIBERATELY DOES NOT
# ---------------------------------------------------------------------------
# It claims exactly one thing, the F-111 shape: a destination vertex EVERY
# ONE of whose lanes is closed has no way in, and a mission there cannot
# succeed. That is decidable from the destination alone.
#
# It does NOT attempt full reachability from the robot's current vertex. A
# nav graph's edges span one level; a legal route between levels goes through
# a lift, which is not an edge. A BFS over edges would call every cross-level
# mission unreachable and refuse it — a guard that blocks real work, which is
# the failure the both-ways rule exists to prevent. Partial-cordon cases
# (some routes closed, others open) are left to the planner, and to the
# adapter's leg gate as the last line.
#
# Fail-open throughout, per the rest of dispatch_guard.py and F-191: no graph,
# no closures, an unknown place, or a malformed request all mean "no
# objection". A check that cannot see must skip and say why, never convict.

from typing import Dict, FrozenSet, List, Optional, Tuple

# fleet name -> {"vertices": [(name, x, y)], "edges": [(v1, v2)]}
_graphs: Dict[str, dict] = {}
# fleet name -> frozenset of closed lane indices, in THAT fleet's index space
_closed: Dict[str, FrozenSet[int]] = {}


def on_nav_graph(msg) -> None:
    """`/nav_graphs` (rmf_building_map_msgs/Graph). `msg.name` is the fleet."""
    try:
        _graphs[str(msg.name)] = {
            "vertices": [(str(v.name), float(v.x), float(v.y))
                         for v in msg.vertices],
            "edges": [(int(e.v1_idx), int(e.v2_idx)) for e in msg.edges],
        }
    except Exception:  # a malformed graph must not take the api-server down
        pass


def on_closed_lanes(msg) -> None:
    """`/closed_lanes` (rmf_fleet_msgs/ClosedLanes)."""
    try:
        _closed[str(msg.fleet_name)] = frozenset(int(i) for i in msg.closed_lanes)
    except Exception:
        pass


def _reset_for_test() -> None:
    _graphs.clear()
    _closed.clear()


def closed_lanes_of(fleet: str) -> FrozenSet[int]:
    return _closed.get(fleet, frozenset())


def _vertex_index(graph: dict, place: str) -> Optional[int]:
    for i, (name, _x, _y) in enumerate(graph["vertices"]):
        if name == place:
            return i
    return None


def cordoned_place(
    graph: Optional[dict],
    closed: FrozenSet[int],
    places: List[str],
) -> Optional[Tuple[str, List[int]]]:
    """First place whose every lane is closed, with the lane indices that
    close it. None when there is no objection.

    Pure, so it can be proven both ways without ROS.
    """
    if not graph or not closed or not places:
        return None
    edges = graph.get("edges") or []
    for place in places:
        index = _vertex_index(graph, place)
        if index is None:
            continue  # not this fleet's graph — leave it to the fleet
        attached = [i for i, (a, b) in enumerate(edges)
                    if a == index or b == index]
        if not attached:
            continue  # F-111's case, already refused by isolated_place
        if all(i in closed for i in attached):
            return place, sorted(attached)
    return None


def cordon_refusal(fleet: str, places: List[str]) -> Optional[str]:
    """Operator-facing reason to refuse, or None."""
    found = cordoned_place(_graphs.get(fleet), _closed.get(fleet, frozenset()),
                           places)
    if found is None:
        return None
    place, lanes = found
    return (
        f"[{place}] is behind a closed lane — every lane into it is part of "
        f"the cordon (closed lanes {lanes}, F-332). The mission is refused "
        f"rather than queued to a robot that could never arrive. Reopen the "
        f"lanes, or send the mission somewhere else."
    )


def known_fleets() -> List[str]:
    """Fleets whose graph AND closure state we have. A fleet we have never
    heard from cannot be judged, and is not guessed about."""
    return [name for name in _graphs if _closed.get(name)]
