"""GentleFleet fork: stale-mission janitor (F-77, approved at E5 review
round 1; FR-29-adjacent).

Task states are a mirror of RMF's live state. When rmf-core (dispatcher +
fleet adapter) restarts, tasks that were non-terminal never receive a
terminal update — the rows stay "queued"/"underway" forever. Those ghosts
then pollute every consumer of non-terminal state, most visibly the D-17
zone-editor mission guard, which refuses applies over missions that no
longer exist.

The janitor fails-over any non-terminal task whose state has not been
updated for `stale_task_timeout` seconds: well beyond every live cadence
(the book keeper rewrites rows on every task-state event; active tasks
update many times a minute), so only truly orphaned rows qualify. Both
the indexed status column and the embedded state JSON are updated, so
the UI tells the same story everywhere.
"""

import logging
from datetime import datetime, timedelta, timezone

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


async def fail_over_stale_tasks(
    timeout_seconds: float, logger: logging.Logger
) -> int:
    """Mark orphaned non-terminal tasks failed; returns how many."""
    if timeout_seconds <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    stale = await DbTaskState.filter(
        status__in=NON_TERMINAL_MATCH, updated_at__lt=cutoff
    )
    for row in stale:
        data = row.data if isinstance(row.data, dict) else {}
        data["status"] = TaskStatus.failed.value
        row.data = data
        row.status = str(TaskStatus.failed)
        await row.save()
        logger.warning(
            "stale-mission janitor: task [%s] had no state update since "
            "[%s] — failed over (orphaned by an rmf-core restart, F-77)",
            row.id_,
            row.updated_at.isoformat() if row.updated_at else "never",
        )
    return len(stale)
