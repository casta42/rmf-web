"""F-141 (E6 run-2 blocker 3): honest closure of missions a fleet
coordination restart orphaned.

rmf-core keeps no task state across a restart (drill 2): every mission
that was underway simply stops being announced. The api-server's rows
then sit in their last non-terminal state forever — six 'Executing'
phantoms after the 2026-08-25 drill, un-cancelable because the restarted
core no longer knows the ids. The ledger must stay coherent: a mission
either completes honestly or terminates honestly.

Pure logic lives here (no app imports) so the class is testable without
a running server:

  - RunBoundary detects a coordination outage from the CADENCE of fleet
    state updates: the live fleet publishes ~1 Hz; silence of
    FLEET_SILENCE_GAP followed by resumption is a restart epoch. (The
    sim-clock cutoff used by the F-12 charge reaper cannot see drill-2:
    gazebo's clock keeps running while rmf-core restarts.)
  - is_interrupted_row() classifies a task row: non-terminal AND not
    re-announced since the outage began -> the core does not know it ->
    close it, with provenance.

D-86 (3a) and (4) (G close-out rulings 2026-10-01): the fail-closed design
RESTARTS rmf-core whenever the traffic schedule node dies, and "robots
resume only when the schedule is back"; "a refused re-dispatch never drops
a mission; it returns to the waiting queue under F-435". So a mission the
restart interrupted is NOT failed any more: its row is closed exactly like
a hand-back — `canceled`, the gf:redispatch marker first in its
cancellation labels, INTERRUPTED_REASON after it, the INTERRUPTED_LABEL
kept on its booking — and the api-server re-dispatches it as a new attempt
of the same chain, shown as "Waiting for a robot" until it is placed. Only
a mission with nothing re-sendable (its request is not stored: a direct
robot task, the fleet's own task) is closed `failed`, with that reason
named. Never 'Executing' forever, never dropped.

Re-sending is only safe when the core really lost the task — sending a
mission it still holds would run it twice — so the claim is held to the
evidence each status can give (queued_verdict):

  - an ACTIVE row (underway, blocked, delayed) is its robot's current task,
    re-published every second while the core holds it and named in the
    robot's fleet state: silent and unnamed, it is lost;
  - a QUEUED row is re-published only when its robot's queue changes, so
    its silence alone is no evidence. It is lost when its robot's active
    task was lost with it (the same task manager holds both: the stamp of
    the old core's last word on that active task, LOST_ACTIVE), when it
    was never assigned (the dispatcher's auction ended long ago), or when
    its robot is idle and its start passed QUEUED_START_GRACE_S ago on the
    robot's own clock (an idle robot begins a due task within a second; the
    start is RMF's time — the sim clock in simulation — so it is never
    compared with the api-server's). Otherwise it is left
    alone, and the reason is said — cannot see, so cannot convict.
"""

from datetime import datetime, timedelta
from typing import Dict, NamedTuple, Optional, Tuple

# The live fleet publishes states ~1 Hz; this much silence is a
# coordination outage, not jitter.
FLEET_SILENCE_GAP = 30.0
# After resumption, everything the core still knows re-announces within
# seconds; wait this long before declaring anything orphaned.
REANNOUNCE_GRACE = 90.0
# Terminal states never need closing.
TERMINAL_STATUSES = {"completed", "failed", "canceled", "killed", "skipped"}
# The booking label that carries the provenance into the stored state.
INTERRUPTED_LABEL = "gf:interrupted=coordination-restart"
# D-86 (3a)/(4): the reason an interrupted mission is back on the floor —
# the hand-back reason of its closed row (and so its waiting reason).
INTERRUPTED_REASON = (
    "interrupted by a coordination restart — the restarted fleet core no "
    "longer knew it; sent to the fleet again"
)
# A robot's current task, re-published every second while the core holds
# it (TaskManager _consider_publishing_updates).
ACTIVE_STATUSES = frozenset({"underway", "blocked", "delayed"})
# An idle robot begins a due task within a second; a queued row whose
# estimated start passed this long ago on an idle robot is not queued.
QUEUED_START_GRACE_S = 60.0
# A queued row the old core spoke of no later than this after its last
# word on the robot's lost active task belongs to the queue that was lost.
SAME_CORE_SLACK = timedelta(seconds=2)

Robot = Tuple[str, str]


class RunBoundary:
    """Coordination-outage detector over the fleet-state cadence."""

    def __init__(self):
        self.last_seen: Optional[float] = None  # monotonic
        self.epoch_started_mono: Optional[float] = None
        self.epoch_started_wall: Optional[datetime] = None
        self.reaped = True

    def observe(self, now_mono: float, now_wall: datetime) -> None:
        if (
            self.last_seen is not None
            and now_mono - self.last_seen >= FLEET_SILENCE_GAP
        ):
            self.epoch_started_mono = now_mono
            self.epoch_started_wall = now_wall
            self.reaped = False
        self.last_seen = now_mono

    def due(self, now_mono: float) -> bool:
        return (
            not self.reaped
            and self.epoch_started_mono is not None
            and now_mono - self.epoch_started_mono >= REANNOUNCE_GRACE
        )

    def mark_reaped(self) -> None:
        self.reaped = True


def status_tail(status_value) -> Optional[str]:
    """'TaskStatus.underway' / 'underway' / enum member -> 'underway'."""
    if status_value is None:
        return None
    return str(status_value).split(".")[-1].strip().lower()


def is_interrupted_row(
    status_value, updated_at: Optional[datetime], epoch_started_wall: datetime
) -> bool:
    """Non-terminal AND silent since the outage began: the restarted
    core does not know this task; its state machine can never close."""
    tail = status_tail(status_value)
    if tail in TERMINAL_STATUSES:
        return False
    if updated_at is None:
        return True
    if updated_at.tzinfo is None and epoch_started_wall.tzinfo is not None:
        updated_at = updated_at.replace(tzinfo=epoch_started_wall.tzinfo)
    return updated_at < epoch_started_wall


def tasks_named_by(fleet_states) -> set:
    """Task ids some robot's fleet state names as its CURRENT task — the
    core is tracking those, whatever the ledger's row age says (F-343:
    a task whose next stop has no route goes silent, not away). Pure;
    `fleet_states` is an iterable of fleet-state dicts."""
    out = set()
    for state in fleet_states or []:
        for robot in ((state or {}).get("robots") or {}).values():
            task_id = (robot or {}).get("task_id")
            if task_id:
                out.add(str(task_id))
    return out



class RobotNow(NamedTuple):
    """What a robot's fleet state says now: the task it names as CURRENT
    ("" when idle) and its own stamp, on RMF's clock (the sim clock under
    use_sim_time) — the clock a task's estimated start is on."""

    task_id: str
    now_ms: Optional[int]


def current_tasks_by_robot(fleet_states) -> Dict[Robot, RobotNow]:
    """(fleet, robot) -> RobotNow. A robot in no fleet state is absent:
    nobody can say what it runs. Pure; `fleet_states` is an iterable of
    fleet-state dicts."""
    out: Dict[Robot, RobotNow] = {}
    for state in fleet_states or []:
        fleet = str((state or {}).get("name") or "")
        for key, robot in ((state or {}).get("robots") or {}).items():
            robot = robot or {}
            name = str(robot.get("name") or key)
            stamp = robot.get("unix_millis_time")
            out[(fleet, name)] = RobotNow(
                str(robot.get("task_id") or ""),
                int(stamp) if isinstance(stamp, (int, float)) else None,
            )
    return out


def _aware(moment: Optional[datetime], like: datetime) -> Optional[datetime]:
    if moment is not None and moment.tzinfo is None and like.tzinfo is not None:
        return moment.replace(tzinfo=like.tzinfo)
    return moment


def queued_verdict(
    assigned: Optional[Robot],
    start_ms: Optional[int],
    updated_at: Optional[datetime],
    current: Dict[Robot, RobotNow],
    lost_active: Dict[Robot, datetime],
) -> Tuple[bool, str]:
    """(lost, why) for a silent row that is NOT its robot's active task —
    see the module docstring. `lost_active` holds, per robot, the old core's
    last word on an active task found lost. "Due" is judged on the robot's
    own clock (RobotNow.now_ms): the row's estimated start is RMF's time,
    which is the sim clock in simulation and never the api-server's."""
    if assigned is None:
        return True, "never assigned, and the auction ended long ago"
    stamp = lost_active.get(assigned)
    if stamp is not None and updated_at is not None:
        if _aware(updated_at, stamp) <= stamp + SAME_CORE_SLACK:
            return True, "queued on a robot whose active task was lost with it"
    robot = f"{assigned[0]}/{assigned[1]}"
    if assigned not in current:
        return False, f"{robot} is in no fleet state, so what it runs cannot be seen"
    running, robot_now_ms = current[assigned]
    if running:
        return False, (
            f"queued on {robot} behind [{running}], which the fleet still "
            "runs — a queued task is re-announced only when its queue changes"
        )
    if start_ms is None or robot_now_ms is None:
        return False, (
            f"{robot} is idle, and its start or its clock is unknown, so "
            "whether it is due cannot be seen"
        )
    if robot_now_ms - int(start_ms) >= QUEUED_START_GRACE_S * 1000:
        return True, f"{robot} is idle, and its start passed long ago on its clock"
    return False, f"{robot} is idle, and it is not due yet on its clock"
