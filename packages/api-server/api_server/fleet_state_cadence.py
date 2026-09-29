"""F-395 (G ruling 2026-09-29, D-84) — fleet state reaches the dashboards
when it CHANGES, rate-capped, plus the existing periodic push.

NFR-2 wants dashboard state latency <= 1 s on the site LAN. Measured on
release f1-n54 (F-390): p50 0.49 s, p95 0.99 s, 5 % over 1 s — bimodal, the
signature of the fleet adapter pushing its state once a second
(`fleet_state_update_period`, upstream default 1 s). The adapter now pushes
at the rate CAP (FLEET_STATE_PUSH_CAP_S in gentle_fleet_adapter), and this
module decides, per message, what the api-server does with it:

- EMIT to the live dashboards when anything an operator can see has
  changed since the last emit: a robot's status, task, issues, the battery
  percentage every surface prints (JavaScript's Math.round of battery x 100),
  its map, its position to the centimetre or its heading to the hundredth of
  a radian, a robot appearing or leaving — and, conservatively, any field
  this module does not know. The adapter's per-message stamp
  (`unix_millis_time`) is not state and never counts as a change.
- FULL — the existing periodic push and everything that rides on it: the
  database write, the health-watchdog heartbeat, the alert rules and the
  reapers — at most once per PERIOD_S per fleet, exactly the cadence they
  had when the adapter pushed once a second. A full cycle always emits.
- Otherwise the message is dropped: nothing an operator can see changed,
  and the periodic cycle is not due.

So five-a-second pushes cost the api-server a JSON comparison each, not
five alert passes and five database writes.
"""

import json
import math
from typing import Any, Dict, Optional, Tuple

PERIOD_S = 1.0  # s — the periodic push the api-server had before F-395

# what the RMF API robot state carries that is a stamp, not state
_STAMPS = ("unix_millis_time",)


def _pct(battery: Any) -> Any:
    """The battery as the dashboards print it: Math.round(battery * 100),
    which rounds halves UP (Python's round() would round them to even)."""
    if not isinstance(battery, (int, float)) or isinstance(battery, bool):
        return battery
    if not math.isfinite(battery):
        return repr(battery)
    return math.floor(battery * 100.0 + 0.5)


def _place(location: Any) -> Any:
    if not isinstance(location, dict):
        return location
    seen = dict(location)
    for key, digits in (("x", 2), ("y", 2), ("yaw", 2)):
        value = seen.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seen[key] = (
                round(float(value), digits) if math.isfinite(value) else repr(value)
            )
    return seen


def signature(fleet_state: Dict[str, Any]) -> str:
    """What an operator can see of one fleet-state message, as a string that
    differs exactly when that view differs."""
    view: Dict[str, Any] = {}
    for key, value in fleet_state.items():
        if key != "robots":
            view[key] = value
    robots = fleet_state.get("robots") or {}
    shown: Dict[str, Any] = {}
    if isinstance(robots, dict):
        for name, robot in robots.items():
            if not isinstance(robot, dict):
                shown[str(name)] = robot
                continue
            seen = {k: v for k, v in robot.items() if k not in _STAMPS}
            if "battery" in seen:
                seen["battery"] = _pct(seen["battery"])
            if "location" in seen:
                seen["location"] = _place(seen["location"])
            shown[str(name)] = seen
    else:
        shown = robots  # an unknown shape is compared whole
    view["robots"] = shown
    return json.dumps(view, sort_keys=True, default=str)


class FleetStateCadence:
    """Per-fleet memory of the last emit and the last full cycle."""

    def __init__(self, period_s: float = PERIOD_S):
        self.period_s = period_s
        self._emitted: Dict[str, str] = {}
        self._full_at: Dict[str, float] = {}

    def decide(self, fleet_state: Dict[str, Any], now: float) -> Tuple[bool, bool]:
        """(emit, full) for one message received at monotonic time `now`.
        A message with no fleet name is always processed in full (it is
        not ours to drop)."""
        name = fleet_state.get("name") if isinstance(fleet_state, dict) else None
        if not isinstance(name, str):
            return True, True
        sig = signature(fleet_state)
        last_full: Optional[float] = self._full_at.get(name)
        full = last_full is None or now - last_full >= self.period_s
        emit = full or self._emitted.get(name) != sig
        if emit:
            self._emitted[name] = sig
        if full:
            self._full_at[name] = now
        return emit, full
