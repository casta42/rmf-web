"""F-463 (G ruling 2026-10-02, second sheet, item 1): ONE critical alert
when the dispatcher stops auctioning.

On releases f1-n66 and f1-n68 the task dispatcher issued one task id twice,
its auctioneer was never released, and from then on every mission was
queued and none auctioned — 68 missions, half an hour, with every surface
healthy. The dispatcher is patched (deploy/patches/f463-…); this is the
alarm for whatever stops it next: missions queued at the dispatcher, no
auction for STALL_AFTER_S, robots standing idle.

What it reads (live_floor.py), all of it the fleet core's own word:
  - `dispatch_states`: the dispatcher's list of what it holds, every 2 s.
    QUEUED entries are missions waiting for an auction, and a task that was
    QUEUED in one message and not in the next was auctioned in between —
    that is how an auction is seen (not on `rmf_task/bid_notice`: the
    dispatcher counts that topic's listeners as bidders to wait for);
  - the fleet state: robots with no task.

STALL_AFTER_S is 60 s. A healthy dispatcher starts the next auction within
0.2 s of the last one ending (its timer) and ends it when every fleet has
answered; over five windows of 12-robot load on release f1-n68
(ops/e6/auction_gap.py, 764 stretches) the longest stretch with a task
queued and no auction ENDED was 10.4 s — one whole auction window, right
after a restart, before the fleet was bidding — and half were under 0.2 s.
By design the worst healthy case is one whole window with a silent bidder
(10 s) plus an award never acknowledged (10 s): 20 s. 60 s is three times
that, and a wedged dispatcher never ends another auction (the same logs:
203 s to 1835 s, until the log ends).

Both ways. It fires on: QUEUED entries in every dispatch_states message
for STALL_AFTER_S, none of them leaving the queue in that time, an idle
robot. It stays
silent on the boring cases: an empty queue however long nobody auctions (an
idle night), a busy queue being auctioned, a fleet with no idle robot, an
api-server that has not watched for STALL_AFTER_S yet, and an api-server
that cannot hear the fleet core on ROS at all — that is "cannot see", said
once in the log, never an alert.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional, Tuple

from . import live_floor

# a child of the app's logger (api_server/logger.py configures "fastapi"):
# its lines carry a time stamp in the server's log, which a plain
# module logger's do not (found on FR-39a's alert lines, f1-n68)
logger = logging.getLogger("fastapi.DispatcherLiveness")

ALERT_CATEGORY = "fleet"
ALERT_PREFIX = "dispatch_stalled__"
PERIOD_S = 5.0
# how recent the fleet state and the ROS side must be to count
FRESH_S = 10.0

OK = "ok"
STALLED = "stalled"
CANNOT_SEE = "cannot-see"


def stall_after_s() -> float:
    try:
        return max(15.0, float(os.environ.get("GF_DISPATCH_STALL_ALERT_S", "60")))
    except ValueError:
        return 60.0


def verdict(
    snap: live_floor.DispatcherSnapshot,
    idle: Optional[int],
    now: float,
    watching_since: float,
    after_s: float,
) -> Tuple[str, str]:
    """(OK | STALLED | CANNOT_SEE, why) from one look at the live views.
    `idle` is the number of robots with no task, None when unknown."""
    if snap.queued_since is None:
        return OK, "nothing is queued at the dispatcher"
    quiet_since = max(snap.queued_since, snap.last_progress or 0.0, watching_since)
    quiet = now - quiet_since
    if quiet < after_s:
        return OK, (
            f"{snap.queued} queued; a queued mission last left the "
            f"queue {quiet:.0f} s ago"
        )
    if snap.core_heard is None or now - snap.core_heard > FRESH_S:
        return CANNOT_SEE, (
            "this server hears nothing from the fleet core "
            "on ROS, so it could not hear an auction either"
        )
    if idle is None:
        return CANNOT_SEE, "no fleet state newer than 10 s: robots unknown"
    if idle == 0:
        return OK, (
            f"{snap.queued} queued and no auction for {quiet:.0f} s, "
            "but no robot is idle"
        )
    return STALLED, (
        f"{snap.queued} queued, no auction for " f"{quiet:.0f} s, {idle} robot(s) idle"
    )


def alert_message(queued: int, quiet_s: float, idle: int, at: float) -> str:
    when = datetime.fromtimestamp(at).strftime("%H:%M:%S")
    return (
        f"The dispatcher stopped auctioning at about {when}: {queued} "
        f"mission(s) are queued, none has been auctioned for "
        f"{quiet_s:.0f} s and {idle} robot(s) stand idle (F-463). Queued "
        "missions are not being assigned to robots. This alert resolves by "
        "itself if the dispatcher auctions again. If it stays open for more "
        "than a few minutes, escalate: the fleet core has to be restarted by "
        "the integrator, and missions in flight are then sent again by "
        "themselves."
    )


class DispatchWatch:
    """One alert per episode; lives on the app's loop."""

    def __init__(self):
        self.watching_since: Optional[float] = None
        self.alerted: Optional[str] = None
        self.swept = False
        self.blind_said = False
        self.episodes = 0


async def process(
    watch: DispatchWatch,
    dispatcher: live_floor.DispatcherView,
    fleets: live_floor.FleetView,
    alert_repo,
    alert_events,
    severity_critical,
    after_s: Optional[float] = None,
    wall=time.time,
) -> Optional[str]:
    """One pass: raise or resolve what is due. Returns what it did."""
    after_s = stall_after_s() if after_s is None else after_s
    now = fleets.now()
    if watch.watching_since is None:
        watch.watching_since = now
    snap = dispatcher.snapshot()
    idle = live_floor.idle_robots(fleets.fleets(), now, FRESH_S)
    state, why = verdict(snap, idle, now, watch.watching_since, after_s)
    if state == CANNOT_SEE:
        if not watch.blind_said:
            watch.blind_said = True
            logger.warning(
                "F-463: the dispatcher-liveness check cannot "
                "judge — %s (%d queued). No alert is raised on "
                "that.",
                why,
                snap.queued,
            )
        return None
    watch.blind_said = False
    if state == STALLED:
        if watch.alerted is not None:
            return None
        quiet = now - max(
            snap.queued_since or now, snap.last_progress or 0.0, watch.watching_since
        )
        at = wall() - quiet
        # the id is built HERE, from a literal: the alert catalogue's guard
        # (F3.2, test_alert_catalogue.py) reads every alert's key off its
        # emit site, and the runbook's row for this alert is keyed on it
        alert_id = f"dispatch_stalled__{int(at)}"
        alert = await alert_repo.create_alert(
            alert_id,
            ALERT_CATEGORY,
            severity=severity_critical,
            message=alert_message(snap.queued, quiet, idle or 0, at),
        )
        watch.alerted = alert_id
        watch.episodes += 1
        if alert is not None:
            alert_events.alerts.on_next(alert)
        logger.error(
            "F-463: the dispatcher is not auctioning — %s; alert " "[%s] raised",
            why,
            alert_id,
        )
        return STALLED
    # OK
    if watch.alerted is not None:
        resolved = await alert_repo.resolve_alert(watch.alerted)
        logger.info(
            "F-463: the dispatcher auctions again (%s) — alert [%s] " "resolved",
            why,
            watch.alerted,
        )
        watch.alerted = None
        if resolved is not None:
            alert_events.alerts.on_next(resolved)
        return "resolved"
    if not watch.swept and snap.last_heard is not None:
        # an alert an earlier life of this server left open is resolved the
        # first time THIS life hears the dispatcher and finds it in order
        watch.swept = True
        for stale in await alert_repo.resolve_alerts_by_prefix(ALERT_PREFIX):
            alert_events.alerts.on_next(stale)
            logger.info(
                "F-463: an earlier life's alert [%s] resolved — the "
                "dispatcher is in order (%s)",
                stale.id,
                why,
            )
        return "swept"
    return None


WATCH = DispatchWatch()


async def maintenance_loop() -> None:
    # pylint: disable=import-outside-toplevel
    from api_server.models import tortoise_models as ttm
    from api_server.rmf_io import alert_events
    from api_server.routes.internal import alert_repo

    while True:
        try:
            await process(
                WATCH,
                live_floor.DISPATCHER,
                live_floor.FLEETS,
                alert_repo,
                alert_events,
                ttm.Alert.Severity.Critical,
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception("F-463: the dispatcher-liveness pass failed")
        await asyncio.sleep(PERIOD_S)
