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

Limits (documented, best-effort): the latch is in-memory, so a server
restart mid-task loses provenance for an adapter-initiated cancel that
RMF never echoed in any state; route-initiated cancels re-latch on the
request itself.
"""

from typing import Dict

from api_server.models.rmf_api.task_state import Cancellation, TaskState

_MAX_LATCHED = 5000

_latched: Dict[str, Cancellation] = {}


def latch(task_id: str, cancellation: Cancellation) -> None:
    if len(_latched) >= _MAX_LATCHED:
        # unbounded-growth guard; losing old latches only costs provenance
        # for tasks that terminated long ago
        _latched.clear()
    _latched[task_id] = cancellation


def apply(task_state: TaskState) -> None:
    """Latch from, or stamp onto, an ingested task state (mutates it)."""
    task_id = task_state.booking.id
    if task_state.cancellation is not None:
        _latched[task_id] = task_state.cancellation
    elif task_id in _latched:
        task_state.cancellation = _latched[task_id]
