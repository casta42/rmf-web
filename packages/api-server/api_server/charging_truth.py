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

F-483 (G ruling 2026-10-03, D-90/D-91): the same feed carries the robot's
REAL battery reading. While a robot is excluded from task allocation (a
fault, F-42; RMF cannot place it, F-411) the fleet state's `battery` is a
spoofed 0.0 — a planner device, which reached the dashboard as "Battery
0 %" in red beside a robot at 60 %. Per robot: `battery` (the reading,
0..1; null = the fleet has none — "unknown", never a guess) and
`battery_spoofed` (true while the fleet state is carrying the device;
null when an older fleet adapter does not say). The battery readouts show
`battery`, or "unknown" — never the fleet state's number while it is
spoofed.
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


def _battery(value: Any) -> Optional[float]:
    """F-483: a state of charge, or None. Anything that is not a number in
    0..1 is "unknown" — a reading is never invented."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0.0 or value > 1.0:  # NaN, out of range
        return None
    return round(float(value), 4)


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
                    "battery": _battery(row.get("soc")),
                    "battery_spoofed": (
                        row.get("soc_published") == "spoofed"
                        if row.get("soc_published") in ("spoofed", "real")
                        else None
                    ),
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
