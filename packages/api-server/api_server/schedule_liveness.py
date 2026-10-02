"""FR-39a (G close-out ruling 2026-10-01, 3a; D-86; F-445/F-446): ONE
critical alert when traffic coordination is lost.

On release f1-n64 the traffic schedule node segfaulted 2.5 min into drill
13 and the fleet drove for 27 minutes with no schedule while everything
reported healthy. The ruling: on the schedule's loss the fleet adapter
gives no new leg (robots hold at their next vertex), ONE critical alert is
raised, rmf-core restarts (the schedule node is on_exit="shutdown"), and
robots resume when the schedule is back. The alert is raised HERE, in the
api-server, because the adapter is inside the rmf-core container that the
loss restarts: a ticket raised there could die with it.

The signal is the one the adapter's gate reads: DDS liveliness of the
schedule node on /rmf_traffic/heartbeat (liveliness AUTOMATIC, lease =
its heartbeat_period; it never publishes there, so no deadline is
requested). The gateway feeds `on_liveliness` from its ROS thread; the
async loop below raises and resolves the alert on the app's loop.

One alert per OUTAGE: the schedule seen alive, then lost — its alive count
at 0 (a clean stop, a stall past the lease), or its writer REPLACED while
the dead one still counted (a crash and a restart faster than the lease:
the count reads 1 -> 2 -> 1 and never 0). A fresh boot
(api-server and rmf-core coming up in any order) raises nothing; the
rmf-core healthcheck (FR-39/FR-39a) covers a core that never comes up. The
alert resolves when the schedule is alive again. A schedule that drops
and returns before the loop's next pass still raised its alert, and that
alert is then resolved: the operator sees the episode, never a flap of
alerts. An alert left open by a previous api-server life (a restart
mid-outage) is not swept at start — it may still be true — but resolved
the first time this life sees the schedule alive.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

ALERT_CATEGORY = "fleet"
PERIOD_S = 1.0
RAISE = "raise"
RESOLVE = "resolve"
SWEEP = "sweep"
ALERT_PREFIX = "traffic_schedule_lost__"


def alert_id_of(outage: int, lost_at: float) -> str:
    return f"{ALERT_PREFIX}{int(lost_at)}_{outage}"


def alert_message(lost_at: float) -> str:
    when = datetime.fromtimestamp(lost_at).strftime("%H:%M:%S")
    return (
        f"Traffic coordination lost at {when}: the traffic schedule stopped "
        "(FR-39a). No robot is given a new leg — each holds where its "
        "current leg ends — and the fleet core is restarting. Robots resume "
        "by themselves when the schedule is back, and this alert then "
        "resolves. If it stays open for more than a few minutes, the fleet "
        "core is not coming back: call support."
    )


class ScheduleLiveness:
    """Thread-safe: fed from the ROS thread, read by the async loop."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self._alive = False
        self._ever_alive = False
        self._outages = 0
        self._lost_at: Optional[float] = None
        self._pending_lost: Optional[Tuple[int, float]] = None
        self._alerted: Optional[str] = None
        self._swept = False

    def on_liveliness(self, alive_count: int, alive_count_change: int = 0) -> None:
        alive = int(alive_count) > 0
        with self._lock:
            if (alive and self._alive and int(alive_count_change) > 0
                    and int(alive_count) >= 2):
                # REPLACED: a second schedule writer while the first still
                # counts as alive. A node that crashes (SIGSEGV, SIGKILL)
                # disposes nothing, so its writer stays "alive" until its
                # lease runs out; the restarted node matches seconds later
                # and the count reads 1 -> 2 -> 1, never 0 (measured
                # 2026-10-02, f454-liveliness-on-kill). That IS an outage,
                # shorter than the lease: one alert, raised now and
                # resolved on the next pass (the schedule is alive). The
                # dead writer's later drop (2 -> 1) is the same outage.
                self._outages += 1
                if self._alerted is None and self._pending_lost is None:
                    self._pending_lost = (self._outages, self._clock())
                return
            if alive == self._alive:
                return
            self._alive = alive
            if alive:
                self._ever_alive = True
                self._lost_at = None
                return
            if not self._ever_alive:
                return
            self._outages += 1
            self._lost_at = self._clock()
            if self._alerted is None and self._pending_lost is None:
                self._pending_lost = (self._outages, self._lost_at)

    def due(self) -> Optional[Tuple[str, str, Optional[float]]]:
        """(RAISE, outage number, lost_at) for an outage not yet alerted;
        (RESOLVE, alert_id, None) for a raised alert whose schedule is
        back; (SWEEP, prefix, None) once, when the schedule is first seen
        alive; None otherwise."""
        with self._lock:
            if self._alive and not self._swept:
                return (SWEEP, ALERT_PREFIX, None)
            if self._pending_lost is not None:
                outage, lost_at = self._pending_lost
                return (RAISE, str(outage), lost_at)
            if self._alive and self._alerted is not None:
                return (RESOLVE, self._alerted, None)
            return None

    def mark_raised(self, alert_id: str) -> None:
        with self._lock:
            self._pending_lost = None
            self._alerted = alert_id

    def mark_swept(self) -> None:
        with self._lock:
            self._swept = True

    def mark_resolved(self) -> None:
        with self._lock:
            self._alerted = None
            # lost again while the old alert was still open: the next
            # pass raises the new outage's alert
            if not self._alive and self._lost_at is not None:
                self._pending_lost = (self._outages, self._lost_at)

    @property
    def outages(self) -> int:
        with self._lock:
            return self._outages


async def process(state: ScheduleLiveness, alert_repo, alert_events,
                  severity_critical) -> Optional[str]:
    """One pass: raise or resolve what is due. Returns what it did."""
    due = state.due()
    if due is None:
        return None
    kind, alert_id, lost_at = due
    if kind == SWEEP:
        for stale in await alert_repo.resolve_alerts_by_prefix(alert_id):
            alert_events.alerts.on_next(stale)
            logger.info("FR-39a: an earlier life's alert [%s] resolved — the "
                        "schedule is alive", stale.id)
        state.mark_swept()
        return SWEEP
    if kind == RAISE:
        # the id is built HERE, from a literal: the alert catalogue's guard
        # (F3.2, test_alert_catalogue.py) reads every alert's key off its
        # emit site, and the runbook's row for this alert is keyed on it.
        # Same shape as alert_id_of(): the prefix the SWEEP resolves by.
        lost_at = lost_at or time.time()
        alert_id = f"traffic_schedule_lost__{int(lost_at)}_{alert_id}"
        alert = await alert_repo.create_alert(
            alert_id,
            ALERT_CATEGORY,
            severity=severity_critical,
            message=alert_message(lost_at),
        )
        state.mark_raised(alert_id)
        if alert is not None:
            alert_events.alerts.on_next(alert)
        logger.error("FR-39a: traffic coordination lost — alert [%s] raised",
                     alert_id)
        return RAISE
    resolved = await alert_repo.resolve_alert(alert_id)
    state.mark_resolved()
    if resolved is not None:
        alert_events.alerts.on_next(resolved)
    logger.info("FR-39a: traffic coordination back — alert [%s] resolved",
                alert_id)
    return RESOLVE


# The api-server's one watcher: fed by the gateway's heartbeat
# subscription, drained by maintenance_loop (started with the app).
STATE = ScheduleLiveness()


async def maintenance_loop() -> None:
    # pylint: disable=import-outside-toplevel
    from api_server.models import tortoise_models as ttm
    from api_server.rmf_io import alert_events
    from api_server.routes.internal import alert_repo

    while True:
        try:
            await process(STATE, alert_repo, alert_events,
                          ttm.Alert.Severity.Critical)
        except Exception:  # pylint: disable=broad-except
            logger.exception("FR-39a: the traffic-schedule alert pass failed")
        await asyncio.sleep(PERIOD_S)
