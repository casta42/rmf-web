"""F-71(2): cancellation provenance for task displays (F-67 honesty).

Whether a cancellation ends a Humble task as `canceled` or `completed`
is a race on the dead-robot path: the same FR-29 auto-cancel produced
`canceled` (with `cancellation` populated) in one run and `completed`
(with `cancellation: null` — provenance wiped) in another. The queue,
and E4 History after it, must still tell the truth about how the task
ended.

Latch every cancellation we can observe — the api-server's own
/tasks/cancel_task route at request time, and the `cancellation` field
of any interim task state — and re-stamp it onto every later state of
that task whose own field arrived empty. The stamped field is the
schema-native `cancellation`, so stored rows and sio broadcasts carry
it identically.

D-114 (2): a latch is a decision's record (F-292's follow-through, the
restart sweep's "somebody asked it to stop"), so it lives as long as its
task — kept until KEEP_AFTER_END_S after the task's first terminal state
(the fleet may re-send a terminal state, and each must be stamped), never
wiped wholesale by count (the old guard cleared every latch at 5000,
live ones with them). `_MAX_LATCHED` is a memory backstop far above any
live population; it drops ended tasks first, oldest first, and says so if
it ever has to drop a live one.

Limits (documented, best-effort): the latch is in-memory, so a server
restart mid-task loses the latch itself — the restart sweep therefore reads
the task row's own stored `cancellation` too (a stamped state was saved
with it); a cancel that no state carried before the restart is the one
case left, said in the sweep.
"""

import logging
import time
from collections import OrderedDict

from api_server.models.rmf_api.task_state import Cancellation, TaskState

_MAX_LATCHED = 5000
KEEP_AFTER_END_S = 3600.0
_TERMINAL = {"canceled", "completed", "failed", "killed", "skipped"}

_latched: "OrderedDict[str, Cancellation]" = OrderedDict()
_ended_at: "OrderedDict[str, float]" = OrderedDict()  # in end order
_logger = logging.getLogger("api_server.cancellation")


def _status(task_state: TaskState) -> str:
    return str(task_state.status).split(".")[-1].lower()


def _bound(now: float) -> None:
    while _ended_at:
        task_id, ended = next(iter(_ended_at.items()))
        if now - ended <= KEEP_AFTER_END_S:
            break
        _ended_at.pop(task_id, None)
        _latched.pop(task_id, None)
    while len(_latched) > _MAX_LATCHED:
        ended = next((t for t in _latched if t in _ended_at), None)
        if ended is None:
            gone, _ = _latched.popitem(last=False)
            _logger.error(
                "D-114 (2): more than %d live cancellations latched — [%s]'s "
                "is forgotten (a memory bound, never evidence)",
                _MAX_LATCHED,
                gone,
            )
        else:
            _latched.pop(ended, None)
            _ended_at.pop(ended, None)


def latch(task_id: str, cancellation: Cancellation, now=None) -> None:
    _latched.pop(task_id, None)
    _latched[task_id] = cancellation
    _bound(time.time() if now is None else now)


def apply(task_state: TaskState, now=None) -> None:
    """Latch from, or stamp onto, an ingested task state (mutates it)."""
    now = time.time() if now is None else now
    task_id = task_state.booking.id
    if task_state.cancellation is not None:
        _latched.pop(task_id, None)
        _latched[task_id] = task_state.cancellation
    elif task_id in _latched:
        task_state.cancellation = _latched[task_id]
    if task_id in _latched and _status(task_state) in _TERMINAL:
        _ended_at.setdefault(task_id, now)
    _bound(now)


def requested(task_id: str) -> bool:
    """True when a cancellation has been latched for this task (F-292:
    the dispatcher can award a task it already canceled in flight; the
    fleet then needs the cancel a second time)."""
    return task_id in _latched
