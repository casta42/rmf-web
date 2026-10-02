"""GentleFleet fork: stale-mission janitor (F-77, approved at E5 review
round 1; FR-29-adjacent) — since F-458 a RECONCILIATION, never a timeout.

Task states are a mirror of RMF's live state. A row that RMF no longer
holds never receives a terminal update and stays "queued"/"underway"
forever; those ghosts pollute every consumer of non-terminal state, most
visibly the D-17 zone-editor mission guard.

F-458 (G ruling 2026-10-02, second sheet, item 3): the first janitor failed
any non-terminal row whose state had not been updated for
`stale_task_timeout` — on AGE ALONE. A queued mission is re-announced only
when its queue changes, so one waiting behind a long task was failed in
the ledger while the fleet still held it; with the dispatcher wedged
(F-463) it failed 29 waiting missions one by one as each turned 30 minutes
old. The ruling: act only on rows PROVEN DEAD against the live fleet state
and the dispatcher's queue.

Age now only nominates: a non-terminal row not updated for NOMINATE_S
(five minutes) is looked at. It is closed only on one of these
proofs, each read from the fleet core's own live word (live_floor.py):

  robot gone      the row is assigned to a robot the fleet has not listed
                  for ABSENT_FOR_S, while the fleet itself is being heard
                  (the ghost charge rows of robots that left the fleet);
  not held        the row was never assigned, and the dispatcher — heard,
                  and for at least ABSENT_FOR_S — has it neither in its
                  queue nor among the dispatches it finished: nobody will
                  ever auction it;
  robot elsewhere the row says underway on a robot that has, for
                  ABSENT_FOR_S, been reporting a different task or none: a
                  task that left a robot does not come back to it.

Everything else is KEPT, however old: a row queued on a robot that exists
(it may be waiting behind that robot's task — no live source lists a
robot's queue, so there is no proof either way), a row in the dispatcher's
queue, a row its robot is running. And when the live word is missing — no
fleet state or no dispatch_states heard in this life of the server, or not
recently — the row is SKIPPED and the pass says why: no answer is not a
wrong answer.

Both the indexed status column and the embedded state JSON are updated, so
the UI tells the same story everywhere.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from . import live_floor
from .models import TaskStatus
from .models.tortoise_models import TaskState as DbTaskState

_TERMINAL = {
    TaskStatus.failed,
    TaskStatus.skipped,
    TaskStatus.canceled,
    TaskStatus.killed,
    TaskStatus.completed,
}
_NON_TERMINAL = [s for s in TaskStatus if s not in _TERMINAL]
# The book keeper stores str(enum) reprs ("Status.underway"); match both
# representations (F-75).
NON_TERMINAL_MATCH = [*_NON_TERMINAL, *(s.value for s in _NON_TERMINAL)]

# Age only nominates a row for the proofs, so it need not be long: a row
# silent this long is looked at (never more than `stale_task_timeout`,
# which still switches the janitor on and off).
NOMINATE_S = 300.0
# how recent the live word must be to be read at all
FRESH_S = 15.0
# how long an absence must have lasted, with the source heard throughout,
# to be a proof: a restarting fleet lists its robots within about a minute
ABSENT_FOR_S = 120.0

KEEP = "keep"
DEAD = "dead"
SKIP = "skip"

_STARTED = {"underway", "delayed", "blocked"}


def _raw_status(row_status: Optional[str]) -> str:
    text = str(row_status or "")
    return text.rsplit(".", 1)[-1].lower()


def _assignment(data: dict) -> Tuple[Optional[str], Optional[str]]:
    assigned = data.get("assigned_to") if isinstance(data, dict) else None
    if not isinstance(assigned, dict):
        return None, None
    return (
        str(assigned.get("group") or "") or None,
        str(assigned.get("name") or "") or None,
    )


def judge(
    task_id: str,
    status: str,
    fleet: Optional[str],
    robot: Optional[str],
    dispatcher: live_floor.DispatcherSnapshot,
    fleets: Dict[str, live_floor.FleetSeen],
    now: float,
) -> Tuple[str, str]:
    """(KEEP | DEAD | SKIP, why) for one nominated row, from the live
    views alone. Pure."""
    if robot:
        candidates = (
            [fleets[fleet]]
            if fleet and fleet in fleets
            else [] if fleet else list(fleets.values())
        )
        if not candidates:
            return SKIP, (
                f"its fleet [{fleet or '?'}] has not been heard " "by this server"
            )
        seen = [f for f in candidates if now - f.last_heard <= FRESH_S]
        if not seen:
            return SKIP, (
                f"its fleet [{fleet or '?'}] has not been heard "
                f"for more than {FRESH_S:.0f} s"
            )
        if any(now - f.first_heard < ABSENT_FOR_S for f in seen):
            return SKIP, (
                "its fleet has been heard for less than "
                f"{ABSENT_FOR_S:.0f} s: robots may still be "
                "joining"
            )
        there = [f.robots[robot] for f in seen if robot in f.robots]
        last_seen = max((r.last_seen for r in there), default=None)
        if last_seen is None:
            return DEAD, (
                f"robot gone: [{robot}] is not in the fleet "
                "(never listed since this server started)"
            )
        if now - last_seen > ABSENT_FOR_S:
            return DEAD, (
                f"robot gone: the fleet has not listed [{robot}] "
                f"for {now - last_seen:.0f} s"
            )
        if now - last_seen > FRESH_S:
            return KEEP, (
                f"[{robot}] was listed {now - last_seen:.0f} s "
                "ago: not yet proof that it left"
            )
        current = max(there, key=lambda r: r.last_seen)
        if current.task_id == task_id:
            return KEEP, f"[{robot}] is running it"
        if status in _STARTED and now - current.task_since >= ABSENT_FOR_S:
            return DEAD, (
                f"robot elsewhere: the row says {status} on [{robot}], "
                f"which has reported "
                + (f"task [{current.task_id}]" if current.task_id else "no task")
                + f" for {now - current.task_since:.0f} s"
            )
        return KEEP, (
            f"[{robot}] is in the fleet; a {status} row may be " "waiting in its queue"
        )
    # never assigned: only the dispatcher can hold it
    if dispatcher.last_heard is None:
        return SKIP, "the dispatcher has not been heard by this server"
    if now - dispatcher.last_heard > FRESH_S:
        return SKIP, (
            "the dispatcher has not been heard for "
            f"{now - dispatcher.last_heard:.0f} s"
        )
    if task_id in dispatcher.active:
        return KEEP, "it is in the dispatcher's queue"
    if dispatcher.finished.get(task_id) == live_floor.DISPATCHED:
        return KEEP, (
            "the dispatcher awarded it: a fleet holds it (its "
            "assignment has not reached the ledger)"
        )
    if now - (dispatcher.first_heard or now) < ABSENT_FOR_S:
        return SKIP, (
            "the dispatcher has been heard for less than " f"{ABSENT_FOR_S:.0f} s"
        )
    return DEAD, (
        "not held: assigned to no robot, and the dispatcher has "
        "it neither queued nor awarded"
    )


# what each pass said last, so a row that stays kept or skipped is logged
# once and not every minute
_said: Dict[str, str] = {}


async def fail_over_stale_tasks(
    timeout_seconds: float,
    logger: logging.Logger,
    dispatcher: Optional[live_floor.DispatcherView] = None,
    fleets: Optional[live_floor.FleetView] = None,
) -> int:
    """Close the nominated rows that are PROVEN dead; returns how many."""
    if timeout_seconds <= 0:
        return 0
    dispatcher = live_floor.DISPATCHER if dispatcher is None else dispatcher
    fleets = live_floor.FLEETS if fleets is None else fleets
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=min(timeout_seconds, NOMINATE_S)
    )
    stale = await DbTaskState.filter(
        status__in=NON_TERMINAL_MATCH, updated_at__lt=cutoff
    )
    if not stale:
        _said.clear()
        return 0
    snapshot = dispatcher.snapshot()
    seen = fleets.fleets()
    now = fleets.now()
    closed = 0
    current = set()
    for row in stale:
        # capture BEFORE save() — auto_now rewrites updated_at on save
        last_update = row.updated_at.isoformat() if row.updated_at else "never"
        data = row.data if isinstance(row.data, dict) else {}
        fleet, robot = _assignment(data)
        robot = robot or (row.assigned_to or None)
        status = _raw_status(row.status)
        verdict, why = judge(row.id_, status, fleet, robot, snapshot, seen, now)
        current.add(row.id_)
        if verdict != DEAD:
            if _said.get(row.id_) != f"{verdict}: {why}":
                _said[row.id_] = f"{verdict}: {why}"
                logger.info(
                    "stale-mission janitor: task [%s] (%s, no state update "
                    "since [%s]) %s — %s (F-458: never closed on age alone)",
                    row.id_,
                    status,
                    last_update,
                    "kept" if verdict == KEEP else "SKIPPED, cannot judge",
                    why,
                )
            continue
        data["status"] = TaskStatus.failed.value
        row.data = data
        row.status = str(TaskStatus.failed)
        await row.save()
        closed += 1
        logger.warning(
            "stale-mission janitor: task [%s] had no state update since "
            "[%s] and is PROVEN dead — %s — failed over (F-77, F-458)",
            row.id_,
            last_update,
            why,
        )
    for gone in [k for k in _said if k not in current]:
        del _said[gone]
    return closed
