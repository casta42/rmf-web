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
    _chargers.clear()


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


def graph_of(fleet: str) -> Optional[dict]:
    return _graphs.get(fleet)


def known_graphs() -> List[str]:
    """Fleets whose graph we hold (closure state or not)."""
    return sorted(_graphs)


# fleet -> {robot: charger waypoint name} — published by the fleet adapter
# (`gf_chargers`, latched) because the adapter is the one authority on who
# charges where; the api-server has no fleet config of its own.
_chargers: Dict[str, Dict[str, str]] = {}


def on_chargers(fleet: str, mapping: dict) -> None:
    try:
        _chargers[str(fleet)] = {str(k): str(v) for k, v in mapping.items()}
    except Exception:
        pass


def chargers_of(fleet: str) -> Dict[str, str]:
    return dict(_chargers.get(fleet, {}))


# ---------------------------------------------------------------------------
# F-339 / F-338 — lane GEOMETRY and REACHABILITY, pure and both-ways testable.
# ---------------------------------------------------------------------------
# A lane index is only meaningful against the graph the fleet is driving
# NOW (F-333). Intent that has to outlive a restart — and a re-derivation,
# which renumbers every lane — is therefore stored as the lane's two
# endpoints and re-resolved against the current graph each time it is
# needed. A lane whose endpoints no longer exist is RETIRED, never guessed.

# Two endpoints within this distance are the same point. The derivation
# writes vertices to the millimetre; anything looser would let a lane
# resolve onto a neighbouring parallel lane (FR-33 siblings are 0.6 m apart).
LANE_MATCH_TOL_M = 0.05
# A robot is "at" the nearest graph vertex only within this radius —
# beyond it (mid-lane, off-graph, settling) its reachability is unknown.
ROBOT_VERTEX_RADIUS_M = 1.0


def lane_geometry(graph: Optional[dict], index: int) -> Optional[dict]:
    """{"entry": (name, x, y), "exit": (name, x, y)} for lane `index`, or
    None when the graph does not have that lane."""
    if not graph:
        return None
    edges = graph.get("edges") or []
    vertices = graph.get("vertices") or []
    if index < 0 or index >= len(edges):
        return None
    a, b = edges[index]
    if a < 0 or b < 0 or a >= len(vertices) or b >= len(vertices):
        return None
    return {"entry": tuple(vertices[a]), "exit": tuple(vertices[b])}


def resolve_lane(
    graph: Optional[dict],
    entry_xy: Tuple[float, float],
    exit_xy: Tuple[float, float],
    tol: float = LANE_MATCH_TOL_M,
) -> Optional[int]:
    """The index of the DIRECTED lane from entry_xy to exit_xy in `graph`,
    or None when no lane joins those two points any more."""
    if not graph:
        return None
    vertices = graph.get("vertices") or []
    for i, (a, b) in enumerate(graph.get("edges") or []):
        if a >= len(vertices) or b >= len(vertices):
            continue
        _na, ax, ay = vertices[a]
        _nb, bx, by = vertices[b]
        if (
            abs(ax - entry_xy[0]) <= tol
            and abs(ay - entry_xy[1]) <= tol
            and abs(bx - exit_xy[0]) <= tol
            and abs(by - exit_xy[1]) <= tol
        ):
            return i
    return None


def _open_adjacency(graph: dict, closed: FrozenSet[int]) -> Dict[int, List[int]]:
    adjacency: Dict[int, List[int]] = {}
    for i, (a, b) in enumerate(graph.get("edges") or []):
        if i in closed:
            continue
        adjacency.setdefault(a, []).append(b)
    return adjacency


def reachable(
    graph: Optional[dict], closed: FrozenSet[int], start: int, goal: int
) -> Optional[bool]:
    """Directed reachability over OPEN lanes. None when the question
    cannot be asked (no graph, or an index the graph does not have)."""
    if not graph:
        return None
    vertices = graph.get("vertices") or []
    if start < 0 or goal < 0 or start >= len(vertices) or goal >= len(vertices):
        return None
    if start == goal:
        return True
    adjacency = _open_adjacency(graph, closed)
    seen = {start}
    queue = [start]
    while queue:
        node = queue.pop()
        for nxt in adjacency.get(node, ()):
            if nxt == goal:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def nearest_vertex(
    graph: Optional[dict], x: float, y: float, radius: float = ROBOT_VERTEX_RADIUS_M
) -> Optional[int]:
    """The graph vertex a robot at (x, y) is standing on, or None when it
    is not within `radius` of any — then its reachability is unknown, and
    an unknown is never a refusal."""
    if not graph:
        return None
    best: Optional[Tuple[float, int]] = None
    for i, (_name, vx, vy) in enumerate(graph.get("vertices") or []):
        d = ((vx - x) ** 2 + (vy - y) ** 2) ** 0.5
        if d <= radius and (best is None or d < best[0]):
            best = (d, i)
    return None if best is None else best[1]


def unreachable_hop(
    graph: Optional[dict],
    closed: FrozenSet[int],
    starts: List[int],
    places: List[str],
) -> Optional[Tuple[str, str]]:
    """The first hop of `places` no route can make over OPEN lanes: the
    first place unreachable from EVERY start, or a later place unreachable
    from the place before it. Returns (from, to) or None.

    Fail-open: no graph, no starts, no closures, or a place this graph
    does not name all mean "no objection" — a place another fleet or
    another map owns is that fleet's business, not ours.
    """
    if not graph or not closed or not starts or not places:
        return None
    indices: List[Optional[int]] = [_vertex_index(graph, p) for p in places]
    previous: List[int] = [s for s in starts if s is not None]
    previous_name = "the robot" if len(previous) == 1 else "any robot"
    for place, index in zip(places, indices):
        if index is None:
            continue  # not this fleet's graph — leave it to the fleet
        if not any(reachable(graph, closed, s, index) for s in previous):
            return previous_name, place
        previous = [index]
        previous_name = place
    return None


def stranded_chargers(
    graph: Optional[dict],
    closed_after: FrozenSet[int],
    robots: List[dict],
) -> List[dict]:
    """F-338 (editor half): robots whose OWN charger the closure would put
    out of reach — reachable with no cordon, unreachable with this one —
    each with the closed lanes on the boundary of what it can still reach.

    `robots`: [{"name", "x", "y", "charger"}]. A robot not standing on a
    vertex, or whose charger the graph does not name, is skipped: a warning
    that cannot see must not invent a strand.
    """
    out: List[dict] = []
    if not graph or not closed_after:
        return out
    for robot in robots:
        try:
            x, y = float(robot["x"]), float(robot["y"])
        except (KeyError, TypeError, ValueError):
            continue
        charger = str(robot.get("charger") or "")
        goal = _vertex_index(graph, charger) if charger else None
        start = nearest_vertex(graph, x, y)
        if goal is None or start is None:
            continue
        if reachable(graph, frozenset(), start, goal) is not True:
            continue  # already unreachable without the cordon: not ours
        if reachable(graph, closed_after, start, goal) is True:
            continue
        # which closed lanes sit on the edge of the robot's reachable set
        adjacency = _open_adjacency(graph, closed_after)
        seen = {start}
        queue = [start]
        while queue:
            node = queue.pop()
            for nxt in adjacency.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        boundary = sorted(
            i for i, (a, _b) in enumerate(graph.get("edges") or [])
            if i in closed_after and a in seen
        )
        out.append({
            "robot": str(robot.get("name") or "?"),
            "charger": charger,
            "lanes": boundary or sorted(closed_after),
        })
    return out


def reachability_refusal(
    fleet: str,
    places: List[str],
    robot_positions: List[Tuple[float, float]],
    has_lifts: bool = False,
) -> Optional[str]:
    """F-338 (api-server half): operator-facing reason to refuse a mission
    whose route has been cut by the cordon, or None.

    A route between levels goes through a lift, which is not an edge, so a
    site with lifts cannot be judged this way and is not — the
    fully-cordoned check (`cordon_refusal`) still applies there.
    """
    if has_lifts:
        return None
    graph = _graphs.get(fleet)
    closed = _closed.get(fleet, frozenset())
    starts = [
        v for v in (nearest_vertex(graph, x, y) for x, y in robot_positions)
        if v is not None
    ]
    hop = unreachable_hop(graph, closed, starts, places)
    if hop is None:
        return None
    origin, place = hop
    return (
        f"[{place}] cannot be reached from {origin} while lanes "
        f"{sorted(closed)} are closed (F-338). The mission is refused rather "
        f"than queued: on this fleet a task that cannot route its next stop "
        f"aborts the fleet adapter when it starts. Reopen the lanes, or send "
        f"the mission somewhere else."
    )
