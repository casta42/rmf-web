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
