"""F-387 (G ruling 2026-09-22, D-82): is this robot MOVING?

The D-17 apply guard and the upgrade gate counted every non-terminal task
row as a running mission. On f1-n51 (the OPERATIONS 1.12 zone apply) that
refused an apply with every robot idle — the one "mission" was the fleet's
own ChargeBattery on a robot standing on its dock — and the same shape
refused the upgrade on orphaned internal rows until `--hard-confirm`. An
admin told to cancel missions nobody dispatched learns to confirm past the
gate, which is the habit that makes it useless the day a real mission is
running.

G's ruling: judge by motion. A robot stationary on an internal task is not
a running mission; a robot in motion on ANY task is. This module answers
the motion half, from the one feed the api-server sees every pose on:
`/fleet_states` (routes/fleets.on_fleet_positions, ~10 Hz). Three answers,
never two:

  moving      it moved more than MOVE_EPS_M within the last
              STATIONARY_WINDOW_S;
  stationary  watched for at least STATIONARY_WINDOW_S on a current pose,
              and it did not;
  unknown     no pose, a pose that stopped arriving, a pose RMF stopped
              accepting (F-268 STALE: where it stands now cannot be seen),
              or not watched long enough yet.

Unknown is never "stationary" (F-191: a check that cannot see says so and
does not convict). For the guard that means an internal task on a robot of
unknown motion is still counted as running, with the reason stated.

Pure: no ROS, no clock of its own — callers pass the times they read.
"""

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

# More than this much displacement inside the window is motion. The fleet
# adapter's drain watch uses the same bar (DRAIN_WATCH_MOVE_EPS): localisation
# jitter on a parked robot stays well inside it.
MOVE_EPS_M = 0.10
# How long a robot must be SEEN still before it is "stationary". Longer
# than a pause at a traffic hold is not required — a robot paused mid-route
# on the fleet's own task is exactly what the ruling lets through (the task
# is re-created after the restart); an operator's mission blocks regardless.
STATIONARY_WINDOW_S = 10.0
# A pose older than this is not "current". The healthy feed arrives at
# ~10 Hz; the worst stall measured on the dev laptop under load was 7.9 s
# (position_freshness.py), so this sits above it.
SAMPLE_MAX_AGE_S = 10.0
KEEP_S = STATIONARY_WINDOW_S + 5.0

MOVING = "moving"
STATIONARY = "stationary"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Motion:
    state: str  # MOVING | STATIONARY | UNKNOWN
    reason: str

    @property
    def stationary(self) -> bool:
        return self.state == STATIONARY


class MotionWatch:
    """Per-robot pose history, keyed `fleet/robot`."""

    def __init__(self) -> None:
        self._samples: Dict[str, Deque[Tuple[float, float, float]]] = {}

    def sample(self, now: float, key: str, x: float, y: float) -> None:
        history = self._samples.setdefault(key, deque())
        history.append((now, float(x), float(y)))
        while history and now - history[0][0] > KEEP_S:
            history.popleft()

    def keys_for(self, robot: str) -> list:
        return [k for k in self._samples if k.rsplit("/", 1)[-1] == robot]

    def motion(self, key: str, now: float, stale: bool = False) -> Motion:
        history = self._samples.get(key)
        if not history:
            return Motion(UNKNOWN, "no position has been received for it")
        age = now - history[-1][0]
        if age > SAMPLE_MAX_AGE_S:
            return Motion(UNKNOWN, f"its position stopped arriving {age:.0f} s ago")
        if stale:
            return Motion(
                UNKNOWN,
                "its position is STALE — the fleet stopped accepting where "
                "it is, so whether it is moving cannot be seen",
            )
        _, lx, ly = history[-1]
        recent = [s for s in history if history[-1][0] - s[0] <= STATIONARY_WINDOW_S]
        moved = max(((x - lx) ** 2 + (y - ly) ** 2) ** 0.5 for _, x, y in recent)
        if moved > MOVE_EPS_M:
            return Motion(
                MOVING,
                f"it moved {moved:.2f} m in the last {STATIONARY_WINDOW_S:.0f} s",
            )
        watched = history[-1][0] - history[0][0]
        if watched < STATIONARY_WINDOW_S:
            return Motion(
                UNKNOWN,
                f"it has been watched for only {watched:.0f} s — not long "
                "enough to call it stationary",
            )
        return Motion(
            STATIONARY,
            f"it has not moved in {STATIONARY_WINDOW_S:.0f} s",
        )

    def motion_of_robot(
        self,
        robot: str,
        now: float,
        fleet: Optional[str] = None,
        stale: bool = False,
    ) -> Motion:
        """Task rows carry only the robot's name (`assigned_to`); resolve
        it to the one fleet that has it. Two fleets with the same robot
        name cannot be told apart — say so rather than guess."""
        if fleet:
            return self.motion(f"{fleet}/{robot}", now, stale)
        keys = self.keys_for(robot)
        if not keys:
            return Motion(UNKNOWN, "no position has been received for it")
        if len(keys) > 1:
            return Motion(
                UNKNOWN,
                f"more than one fleet has a robot named [{robot}] — which "
                "one this task is on cannot be told",
            )
        return self.motion(keys[0], now, stale)
