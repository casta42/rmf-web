"""F-386 (G ruling 2026-09-22, D-82): is this robot CHARGING — by battery
truth, from the one process that can tell.

The Charging chip, the state line and the map marker used to decide from
what the dashboard could see: RMF's task status (F-384 took that away) and
then the REPORTED position against the charger. F-386 fooled the second:
a robot driven 6 m off its dock stayed reported ON it, and read a green
"Charging 78 %" while it drained. The verdict now comes from the fleet
adapter (`gf_charging`, latched, 1 Hz) — the process holding both halves,
the LIVE pose and the resting-SoC history its charge governor judges by:
charging = on its own dock by the live pose AND its SoC rising.

This module only relays it. Per robot: `charging` (true / false / null =
cannot tell), `docked`, `moving` (both by the live pose; null = no pose),
and `why`. A feed older than MAX_AGE_S is not an answer: the route says so
and every robot is "cannot tell" — never "charging" (F-191).
"""

import json
import time
from typing import Any, Dict, Optional, Tuple

from .logger import logger as base_logger

logger = base_logger.getChild("ChargingTruth")

# The adapter publishes at 1 Hz; ten missed publishes is a feed that has
# stopped, not a slow one.
MAX_AGE_S = 10.0

_latest: Dict[str, Tuple[dict, float]] = {}


def on_charging(raw: str, now: Optional[float] = None) -> None:
    """The adapter's `gf_charging` (ROS thread: store only, never raise)."""
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("gf_charging: undecodable payload")
        return
    if not isinstance(data, dict) or not data.get("fleet"):
        return
    _latest[str(data["fleet"])] = (data, time.monotonic() if now is None else now)


def _reset_for_test() -> None:
    _latest.clear()


def snapshot(now: Optional[float] = None) -> Dict[str, Any]:
    """Every fleet's verdicts, or why there are none."""
    now = time.monotonic() if now is None else now
    if not _latest:
        return {
            "available": False,
            "reason": "no charging verdict has been received from the fleet "
            "yet — whether any robot is charging is unknown, which is not "
            "the same as not charging",
            "robots": [],
        }
    rows = []
    stale = []
    for fleet, (data, at) in sorted(_latest.items()):
        age = now - at
        if age > MAX_AGE_S:
            stale.append(f"{fleet} ({age:.0f} s)")
            continue
        for robot, row in sorted((data.get("robots") or {}).items()):
            if not isinstance(row, dict):
                continue
            charging = row.get("charging")
            rows.append(
                {
                    "fleet": fleet,
                    "robot": robot,
                    "key": f"{fleet}/{robot}",
                    "charging": charging if isinstance(charging, bool) else None,
                    "docked": (
                        row.get("docked")
                        if isinstance(row.get("docked"), bool)
                        else None
                    ),
                    "moving": (
                        row.get("moving")
                        if isinstance(row.get("moving"), bool)
                        else None
                    ),
                    "why": str(row.get("why") or ""),
                }
            )
    if stale and not rows:
        return {
            "available": False,
            "reason": "the fleet stopped reporting charging verdicts ("
            + ", ".join(stale)
            + " ago) — cannot tell",
            "robots": [],
        }
    return {
        "available": True,
        "reason": (None if not stale else "no fresh verdict from " + ", ".join(stale)),
        "robots": rows,
    }
