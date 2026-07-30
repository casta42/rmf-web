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
