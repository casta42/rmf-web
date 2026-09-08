"""GentleFleet fork: read-only site zone data (DR-3 map overlays, FR-10).

Serves the site's zones.yaml (FR-7 no-go polygons, FR-8 speed zones, FR-9
mutex zones — the same file the adapter zone manager enforces, D-13) as
JSON. Read-only: zone EDITING is the Phase E zone editor (DR-4, milestone
E5) and does not go through this route.
"""

import os
from typing import Any, Dict, Optional

import yaml
from fastapi import APIRouter, HTTPException

from api_server.app_config import app_config

router = APIRouter(tags=["Zones"])

_cache: Dict[str, Any] = {}
_cache_mtime: Optional[float] = None


def _load_zones() -> Dict[str, Any]:
    global _cache, _cache_mtime  # pylint: disable=global-statement
    zones_file = app_config.zones_file
    if not zones_file:
        raise HTTPException(404, "no zones file configured for this site (zones_file)")
    try:
        mtime = os.path.getmtime(zones_file)
    except OSError as e:
        raise HTTPException(404, f"zones file unreadable: {e}") from e
    if _cache_mtime != mtime:
        with open(zones_file, "r", encoding="utf8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise HTTPException(500, "zones file is not a mapping")
        _cache = data
        _cache_mtime = mtime
    return _cache


@router.get("")
async def get_zones() -> Dict[str, Any]:
    """The site's zone data: no_go_zones, speed_zones, mutex_zones
    (coordinates in RMF building-map meters)."""
    return _load_zones()


# ----------------------------------------------------------------------
# D-24: the DERIVED nav graph — the exact graph the fleet drives (base
# lanes minus zone closures, plus generated detours and destination
# splits/stubs), read from the config repo file rmf-core loads. The
# dashboard renders THIS graph, uniformly: closed lanes are simply not
# in it, generated lanes are ordinary lanes whose params carry
# provenance (gf_generated / gf_destination) for hover (DR-3). It also
# outlives an rmf-core restart, unlike building_map_server.
# ----------------------------------------------------------------------
_graph_cache: Dict[str, Any] = {}
_graph_mtime: Optional[float] = None


def _normalize_nav_graph(data: Dict[str, Any]) -> Dict[str, Any]:
    levels = data.get("levels") or {}
    if not levels:
        raise HTTPException(500, "nav graph has no levels")
    level_name = next(iter(levels))
    level = levels[level_name] or {}
    vertices = []
    for vertex in level.get("vertices") or []:
        params = dict(vertex[2]) if len(vertex) > 2 and vertex[2] else {}
        name = str(params.pop("name", "") or "")
        vertices.append(
            {"x": float(vertex[0]), "y": float(vertex[1]), "name": name, "params": params}
        )
    # The file stores directed entries (a bidirectional lane is two);
    # rendering wants undirected lanes with a direction flag.
    lanes: Dict[Any, Dict[str, Any]] = {}
    for lane in level.get("lanes") or []:
        u, v = int(lane[0]), int(lane[1])
        params = dict(lane[2]) if len(lane) > 2 and lane[2] else {}
        key = (u, v) if u <= v else (v, u)
        entry = lanes.get(key)
        if entry is None:
            lanes[key] = {"a": u, "b": v, "bidirectional": False, "params": params}
        else:
            entry["bidirectional"] = True
    return {
        "level": level_name,
        "vertices": vertices,
        "lanes": list(lanes.values()),
    }


def derived_nav_graph() -> Optional[Dict[str, Any]]:
    """The derived nav graph in service, or None when it is not readable.
    Shared by the /zones/nav_graph route and the F-111 dispatch guard —
    the DERIVED graph is the only honest answer to "can the fleet reach
    this waypoint right now", since the building map still carries the
    authored lanes a zone has closed."""
    global _graph_cache, _graph_mtime  # pylint: disable=global-statement
    zones_file = app_config.zones_file
    if not zones_file:
        return None
    graph_file = os.path.join(os.path.dirname(zones_file), "nav_graphs", "0.yaml")
    try:
        mtime = os.path.getmtime(graph_file)
    except OSError:
        return None
    if _graph_mtime != mtime:
        with open(graph_file, "r", encoding="utf8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        _graph_cache = _normalize_nav_graph(data)
        _graph_mtime = mtime
    return _graph_cache


@router.get("/nav_graph")
async def get_nav_graph() -> Dict[str, Any]:
    """The derived nav graph in service (D-24): vertices with names and
    params, undirected lanes with a bidirectional flag and provenance
    params (gf_generated, gf_destination, speed_limit)."""
    graph = derived_nav_graph()
    if graph is None:
        raise HTTPException(404, "nav graph unreadable for this site")
    return graph


# ----------------------------------------------------------------------
# FR-9d / D-58 (F-259) — who holds the aisle, and the guarded release
#
# DR-3's 2026-07-31 amendment makes a map element the operator cannot
# identify an automatic REJECT, and a mutex polygon labelled "one robot
# at a time" was exactly that: an operator watching a stuck aisle could
# see neither the holder nor the queue, and had no way to act.
#
# The adapter is the single source of both the ledger and the geometry
# (zone_manager.operator_state). This route caches its last publish and
# serves it; it never re-derives "is a body inside", because a second
# implementation of that question is a second answer waiting to differ.
# ----------------------------------------------------------------------
import json
import logging
import time as _time
from typing import List

from fastapi import Depends
from pydantic import BaseModel

from api_server.authenticator import user_dep
from api_server.models import User

logger = logging.getLogger("api_server.zones")

# A zone report older than this is not evidence of anything. The adapter
# publishes at 1 Hz; ten missed publishes means it is gone, restarting,
# or was never there — and the honest answer is then "unknown", never
# "free" (F-191: a check that cannot see must skip, never convict).
ZONE_STATE_MAX_AGE = 10.0  # s

_zone_states: Dict[str, Any] = {}
_zone_states_at: Optional[float] = None
_release_pub = None


def on_zone_states(payload: str) -> None:
    """Called from the ros gateway subscription (gf_zone_states)."""
    global _zone_states, _zone_states_at  # pylint: disable=global-statement
    try:
        data = json.loads(payload)
    except ValueError:
        logger.warning("gf_zone_states: undecodable payload")
        return
    if not isinstance(data, dict):
        return
    _zone_states = data
    _zone_states_at = _time.monotonic()


def _fresh_zone_states() -> Dict[str, Any]:
    if _zone_states_at is None:
        raise HTTPException(
            503,
            "the fleet adapter has not reported mutex-zone state yet — "
            "holders and waiters are unknown",
        )
    age = _time.monotonic() - _zone_states_at
    if age > ZONE_STATE_MAX_AGE:
        raise HTTPException(
            503,
            f"mutex-zone state is {age:.0f}s stale — the fleet adapter is not "
            "reporting; holders and waiters are unknown",
        )
    return _zone_states


@router.get("/mutex_state")
async def get_mutex_state() -> Dict[str, Any]:
    """Live FR-9 mutex-zone occupancy: per zone the holder, how long it
    has held, who is queued behind it, and which robot bodies are
    physically inside the polygon."""
    state = _fresh_zone_states()
    return {
        "fleet": state.get("fleet"),
        "unix_millis_time": state.get("unix_millis_time"),
        "age_s": round(_time.monotonic() - (_zone_states_at or 0.0), 1),
        "zones": state.get("zones") or [],
    }


class MutexReleaseBody(BaseModel):
    reason: str


def _admin_dep(user: User = Depends(user_dep)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "force-releasing a mutex zone is admin-only")
    return user


@router.post("/mutex/{zone_name}/release")
async def force_release_mutex_zone(
    zone_name: str,
    body: MutexReleaseBody,
    user: User = Depends(_admin_dep),
) -> Dict[str, Any]:
    """Force-release one mutex zone (admin only, FR-9d/D-58).

    REFUSED while any robot body is inside the polygon, naming that
    robot: a lock released under a body is not an operator freeing a
    stuck aisle, it is an invitation for a second robot to drive into
    the first. Refused too when the adapter's report is missing or
    stale — this control may only act on evidence it actually has
    (F-191), and the adapter re-checks the same rule when the command
    lands, so the guard does not live in one place only.
    """
    # pylint: disable=import-outside-toplevel
    import rclpy.qos
    from std_msgs.msg import String as StringMsg

    from api_server import ros

    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "a reason is required — this is an audit trail")

    state = _fresh_zone_states()
    zones: List[Dict[str, Any]] = state.get("zones") or []
    zone = next((z for z in zones if z.get("name") == zone_name), None)
    if zone is None:
        raise HTTPException(404, f"no mutex zone named '{zone_name}' at this site")
    inside = list(zone.get("bodies_inside") or [])
    if inside:
        raise HTTPException(
            409,
            f"{', '.join(inside)} {'is' if len(inside) == 1 else 'are'} physically "
            f"inside '{zone_name}'. Releasing the lock would let another robot drive "
            "in on top of it — move it out of the aisle first.",
        )
    if not zone.get("holder"):
        raise HTTPException(409, f"'{zone_name}' is already free — nothing to release")

    global _release_pub  # pylint: disable=global-statement
    if _release_pub is None:
        _release_pub = ros.ros_node().create_publisher(
            StringMsg,
            "gf_zone_release",
            rclpy.qos.QoSProfile(
                depth=10,
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.VOLATILE,
            ),
        )
    actor = getattr(user, "username", None) or "admin"
    _release_pub.publish(
        StringMsg(data=json.dumps({"zone": zone_name, "actor": actor, "reason": reason}))
    )
    logger.warning(
        "FR-9d: mutex zone '%s' force-released from '%s' by '%s' — reason: %s",
        zone_name,
        zone.get("holder"),
        actor,
        reason,
    )
    return {
        "ok": True,
        "zone": zone_name,
        "released_from": zone.get("holder"),
        "actor": actor,
        "reason": reason,
    }
