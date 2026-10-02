"""GentleFleet fork: what the fleet core says it holds RIGHT NOW (G rulings
2026-10-02, second sheet, items 1 and 3; F-463, F-458).

Two things need the live truth and must never rule on the ledger's own age:

- the stale-row reconciliation (stale_tasks.py, F-458): a row is closed
  only when it is PROVEN dead — its robot is not in the fleet, the
  dispatcher does not hold it, its robot is running something else;
- the dispatcher-liveness alarm (dispatcher_liveness.py, F-463): tasks
  queued at the dispatcher and no auction started.

Both read the two views here.

DispatcherView is fed from the ROS thread (gateway.py): the dispatcher's own
`dispatch_states` (every 2 s: what it has queued, awarded, finished) and any
`fleet_states` message (proof that this api-server hears the fleet core on
ROS at all). FleetView is fed from the fleet websocket (routes/internal.py),
every fleet state the core pushes.

An auction is read off `dispatch_states` itself: a task that was QUEUED in
one message and is not in the next was auctioned (or canceled) in between.
The api-server must NOT subscribe to `rmf_task/bid_notice` to hear auctions
start: the dispatcher counts that topic's subscribers as the bidders it
waits for (the early close of F-410), and a listener that never bids would
make every auction wait its whole window.

Absence of evidence is never evidence (the both-ways rule): each view
records WHEN it was first and last heard, and its readers must skip — and
say why — when a view has not been heard, instead of reading silence as
"nothing holds it".
"""

import threading
import time
from typing import Callable, Dict, Iterable, NamedTuple, Optional, Tuple

# rmf_task_msgs/msg/DispatchState
QUEUED = 1
SELECTED = 2
DISPATCHED = 3
FAILED_TO_ASSIGN = 4
CANCELED_IN_FLIGHT = 5


# a source not heard for this long has stopped being heard: what is
# missing from its next message has been missing only since then
GAP_S = 15.0


class DispatcherSnapshot(NamedTuple):
    first_heard: Optional[float]  # start of the current unbroken hearing
    last_heard: Optional[float]  # newest dispatch_states
    active: Dict[str, int]  # task id -> status, newest message
    finished: Dict[str, int]
    queued: int  # QUEUED entries in the newest message
    queued_since: Optional[float]  # every message since then had some
    last_progress: Optional[float]  # a queued task last left the queue
    auctions: int  # how many have left it, this life
    core_heard: Optional[float]  # newest ROS fleet_states heard


class DispatcherView:
    """Thread-safe: written by the ROS thread, read by the app's loop."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._first: Optional[float] = None
        self._last: Optional[float] = None
        self._active: Dict[str, int] = {}
        self._finished: Dict[str, int] = {}
        self._queued_since: Optional[float] = None
        self._last_progress: Optional[float] = None
        self._auctions = 0
        self._core_heard: Optional[float] = None

    def on_dispatch_states(
        self, active: Iterable[Tuple[str, int]], finished: Iterable[Tuple[str, int]]
    ) -> None:
        active = {str(tid): int(status) for tid, status in active}
        finished = {str(tid): int(status) for tid, status in finished}
        with self._lock:
            now = self._clock()
            if self._first is None or now - (self._last or now) > GAP_S:
                # first word, or first word after a silence (a restarted
                # fleet core): "heard for N seconds" starts again
                self._first = now
            self._last = now
            # a task queued in the last message and not queued in this one
            # was auctioned (awarded, or closed with no bid) or canceled
            left = [
                tid
                for tid, status in self._active.items()
                if status == QUEUED and active.get(tid) != QUEUED
            ]
            if left:
                self._last_progress = now
                self._auctions += len(left)
            self._active = active
            self._finished = finished
            if any(status == QUEUED for status in active.values()):
                if self._queued_since is None:
                    self._queued_since = now
            else:
                self._queued_since = None

    def on_core_heard(self) -> None:
        with self._lock:
            self._core_heard = self._clock()

    def snapshot(self) -> DispatcherSnapshot:
        with self._lock:
            return DispatcherSnapshot(
                self._first,
                self._last,
                dict(self._active),
                dict(self._finished),
                sum(1 for s in self._active.values() if s == QUEUED),
                self._queued_since,
                self._last_progress,
                self._auctions,
                self._core_heard,
            )


class RobotSeen(NamedTuple):
    last_seen: float
    task_id: str
    status: str
    task_since: float  # its task id has read this since then


class FleetSeen(NamedTuple):
    first_heard: float  # start of the current unbroken hearing
    last_heard: float
    robots: Dict[str, RobotSeen]


class FleetView:
    """Every fleet state the core pushes, kept as "who was seen, when, and
    doing what". Robots that leave the fleet stay in the record with their
    last_seen: that age is the proof."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._fleets: Dict[str, FleetSeen] = {}

    def on_fleet_state(self, fleet: str, robots: Dict[str, dict]) -> None:
        """`robots`: name -> the robot's state as a dict (task_id, status)."""
        with self._lock:
            now = self._clock()
            seen = self._fleets.get(fleet)
            known = dict(seen.robots) if seen else {}
            for name, state in (robots or {}).items():
                state = state if isinstance(state, dict) else {}
                task_id = str(state.get("task_id") or "")
                status = str(state.get("status") or "")
                before = known.get(name)
                since = (
                    before.task_since if before and before.task_id == task_id else now
                )
                known[name] = RobotSeen(now, task_id, status, since)
            unbroken = seen is not None and now - seen.last_heard <= GAP_S
            self._fleets[fleet] = FleetSeen(
                seen.first_heard if unbroken else now, now, known
            )

    def fleet(self, name: str) -> Optional[FleetSeen]:
        with self._lock:
            return self._fleets.get(name)

    def fleets(self) -> Dict[str, FleetSeen]:
        with self._lock:
            return dict(self._fleets)

    def last_heard(self) -> Optional[float]:
        with self._lock:
            return max((f.last_heard for f in self._fleets.values()), default=None)

    def now(self) -> float:
        return self._clock()


# robots that are there and could take a mission (models/rmf_api
# robot_state.Status values that mean it cannot)
_OUT = {"uninitialized", "offline", "shutdown", "error"}


def idle_robots(
    fleets: Dict[str, FleetSeen], now: float, fresh_s: float
) -> Optional[int]:
    """How many robots stand with no task, by fleet states no older than
    `fresh_s`. None when no fleet has been heard that recently: unknown,
    not zero."""
    heard = False
    count = 0
    for seen in fleets.values():
        if now - seen.last_heard > fresh_s:
            continue
        heard = True
        for robot in seen.robots.values():
            if now - robot.last_seen > fresh_s:
                continue
            if not robot.task_id and robot.status.lower() not in _OUT:
                count += 1
    return count if heard else None


# The api-server's one copy of each view.
DISPATCHER = DispatcherView()
FLEETS = FleetView()
