"""F-379 (G ruling 2026-09-22): the fleet's OWN tasks never page an
operator unless they fail.

The bell is what an operator must act on. The fleet runs tasks of its own
— upstream's automatic ChargeBattery (`Charge<hex>`) and responsive wait
(`wait.`), and every trip this product issues for itself: the F-36 rescue
and retreat (`f36-`), the F-87 charge sweep (`f87-`), the FR-36 idle and
cut-vertex retreat (`fr36-`), the F-338 hold (`f338-`) — plus a residual
D-29 `ParkRobot`. The fleet cancels and replaces these constantly, by
design; each cancellation raised "Task <id> canceled", and on the dev site
590 of 762 open alerts were exactly that (F3, 2026-09-22), burying the
alerts that needed someone.

The rule: an internal task raises an alert only when it FAILS. A cancel,
or an error line in its event log, is the fleet managing itself. The
prefixes are the fleet adapter's own list (charge_governor
HOMEBOUND_PREFIXES + PARK_ROBOT_PREFIX); test_internal_task_prefixes.py in
the adapter holds the two equal, so neither can drift alone.
"""

from typing import Optional

INTERNAL_TASK_PREFIXES = ("Charge", "wait.", "f36-", "f87-", "fr36-", "f338-", "ParkRobot")

# who the startup sweep records as having resolved a row it archives
SWEEP_RESOLVER = "system (F-379: the fleet's own task, not an operator action)"


def is_internal_task(task_id: Optional[str]) -> bool:
    return bool(task_id) and str(task_id).startswith(INTERNAL_TASK_PREFIXES)


def pages_operator(task_id: Optional[str], failed: bool) -> bool:
    """May this task's terminal state (or log error) raise an alert? Every
    operator/dispatched task: yes. The fleet's own: only a failure."""
    return failed or not is_internal_task(task_id)


async def sweep_internal_task_alerts(alert_model, now_millis: int) -> int:
    """Archive every OPEN alert the rule above would never have raised: a
    task alert for an internal task that is not a failure. Idempotent; run
    at api-server start so a site upgraded past F-379 does not keep the
    backlog the old rule left (the "200" in every F3 screenshot). Failures
    stay open — they are what the bell is for."""
    rows = await alert_model.filter(
        category="task", unix_millis_resolved_time__isnull=True
    )
    n = 0
    for row in rows:
        if not is_internal_task(row.original_id):
            continue
        if " failed" in (row.message or ""):
            continue
        row.resolved_by = SWEEP_RESOLVER
        row.unix_millis_resolved_time = now_millis
        await row.save()
        n += 1
    return n
