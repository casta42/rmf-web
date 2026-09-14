"""F-293 (FR-4 amendment, G ruling 2026-09-14): how far ahead a one-off
dispatch may be scheduled, and when a scheduled one is released to the
dispatcher.

WHAT FAILED (Drill 8, F-292/F-293). On the Humble pin a task whose
earliest start lies hours ahead is bid on and awarded at once and then
sits in the winning robot's queue until its start: the fleet adapter
re-commands the robot toward its own waypoint at 1 Hz for as long as it
waits (six hours would twitch a physical robot once a second), the
waiting task rides inside every allocation the fleet computes, and its
presence coincided with fleet-wide planner failures.

Three lines on a request's earliest start T, on the api-server clock:

  refuse   T - now > the site's maximum lead (one shift, 8 h unless the
           site config says otherwise): a mission that far out is a
           schedule, and schedules belong to the scheduler (F-137).
  defer    T - now > the dispatch horizon: held by the api-server and
           released at T - horizon, so no far-future task ever occupies
           a robot's queue.
  now      anything else, including a start in the past.

The horizon is DERIVED from the site: the longest trip on the derived
nav graph (the largest shortest-path distance between any two
waypoints, along its lanes), at the fleet's nominal speed, stretched by
the same traffic factor the FR-12 governor costs a trip home with, plus
the dispatcher's bid window — the time the fleet needs to bring any
robot to any start. Released at T - horizon, the winner can still
arrive by T, and it never waits in its queue longer than the horizon.
Pure: graph in, seconds out; clock in, verdict out.
"""

import heapq
import math
from typing import Any, Dict, Optional, Tuple

# The fleet config's limits.linear[0] — the conservative spec value
# (CLAUDE.md: never raised without G's instruction). The api-server
# cannot read the fleet config; a faster fleet would only make the
# derived horizon longer than it needs to be (dispatch earlier), never
# shorter.
V_MAX_MPS = 0.5
# charge_governor.NOMINAL_SPEED_FACTOR / RESCUE_TRAFFIC_FACTOR: a robot
# averages 0.8 v_max over a leg and a trip runs 1.5x its nominal time.
NOMINAL_SPEED_FACTOR = 0.8
TRAFFIC_FACTOR = 1.5
# rmf_core.launch.xml bidding_time_window (F-44).
BID_WINDOW_S = 10.0
# The graph cannot be read (no zones file, unparseable graph): the
# horizon cannot be derived. Deferring is not a refusal, so the gate
# still defers — with this bound, and says so in its answer.
FALLBACK_HORIZON_S = 300.0
DEFAULT_MAX_LEAD_S = 8 * 3600.0

NOW = "now"
DEFER = "defer"
REFUSE = "refuse"


def graph_diameter_m(graph: Optional[Dict[str, Any]]) -> Optional[float]:
    """Longest shortest-path distance (m) between any two waypoints of a
    normalized nav graph (routes.zones._normalize_nav_graph: vertices
    [{x, y, ...}], undirected lanes [{a, b, bidirectional}]), along its
    lanes and their directions. None when there is no graph or no lane."""
    if not graph:
        return None
    vertices = graph.get("vertices") or []
    lanes = graph.get("lanes") or []
    if not vertices or not lanes:
        return None
    xy = [(float(v["x"]), float(v["y"])) for v in vertices]
    adjacency: Dict[int, list] = {}
    for lane in lanes:
        a, b = int(lane["a"]), int(lane["b"])
        if not (0 <= a < len(xy) and 0 <= b < len(xy)):
            continue
        length = math.dist(xy[a], xy[b])
        adjacency.setdefault(a, []).append((b, length))
        if lane.get("bidirectional"):
            adjacency.setdefault(b, []).append((a, length))
    longest = 0.0
    for source in adjacency:
        dist = {source: 0.0}
        heap = [(0.0, source)]
        while heap:
            d, v = heapq.heappop(heap)
            if d > dist.get(v, math.inf):
                continue
            for w, length in adjacency.get(v, ()):
                nd = d + length
                if nd < dist.get(w, math.inf):
                    dist[w] = nd
                    heapq.heappush(heap, (nd, w))
        longest = max(longest, max(dist.values()))
    return longest if longest > 0.0 else None


def horizon_s(graph: Optional[Dict[str, Any]]) -> Tuple[float, str]:
    """(seconds, how it was derived) — the dispatch horizon for a site."""
    diameter = graph_diameter_m(graph)
    if diameter is None:
        return FALLBACK_HORIZON_S, (
            "the site's nav graph could not be read, so the horizon could "
            f"not be derived; a {FALLBACK_HORIZON_S:.0f} s bound is used"
        )
    trip = diameter / (V_MAX_MPS * NOMINAL_SPEED_FACTOR) * TRAFFIC_FACTOR
    seconds = math.ceil(trip + BID_WINDOW_S)
    return float(seconds), (
        f"the longest trip on this site ({diameter:.0f} m) at "
        f"{V_MAX_MPS * NOMINAL_SPEED_FACTOR:.1f} m/s x {TRAFFIC_FACTOR} "
        f"traffic, plus the {BID_WINDOW_S:.0f} s bid window"
    )


def classify(
    start_ms: Optional[int],
    now_ms: int,
    horizon: float,
    max_lead: float = DEFAULT_MAX_LEAD_S,
) -> str:
    """NOW, DEFER or REFUSE for a request whose earliest start is
    `start_ms` (None or 0 means now)."""
    if not start_ms:
        return NOW
    lead = (int(start_ms) - int(now_ms)) / 1000.0
    if lead > max_lead:
        return REFUSE
    if lead > horizon:
        return DEFER
    return NOW


def human(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"
