"""GentleFleet fork: a mission row's booking labels, kept (F-295, F-464).

The fleet's task states carry no booking labels on this pin, so every
state update would erase the labels a mission was dispatched with — FR-5
priority, the drills' markers, and the `gf:redispatch-of / -root / -class`
provenance that folds a mission's attempts into one chain. The stored
REQUEST is the truth; its labels are stamped back onto every state.

F-464 (G ruling 2026-10-02, second sheet, item 5): the first version
cached the lookup per task id — including a MISS. The dispatcher's first
state for a new task reaches the ledger before the dispatch call has
returned and stored the request, so the lookup found nothing, cached
"no labels", and the row never got them back: 77 of 330 mission rows in an
hour on release f1-n68, one chain in five unattributable. Now:

- the dispatch path calls `remember()` the moment the dispatcher answers,
  before anything is stored, and stamps the labels onto the state it saves;
- a miss is never final while the task is new: it is looked up again on
  every state until MISS_SETTLES_S after it was first missed (a task with
  no stored request — the fleet's own charge tasks, direct robot tasks —
  then stops costing a query per update);
- a request that IS stored and has no labels settles at once.
"""

import time
from typing import Awaitable, Callable, Dict, List, Optional, Set

# a dispatch call returns within its 5 s timeout; six times that
MISS_SETTLES_S = 30.0
LIMIT = 8192

_labels: Dict[str, List[str]] = {}
_first_miss: Dict[str, float] = {}
_settled: Set[str] = set()


def reset() -> None:
    _labels.clear()
    _first_miss.clear()
    _settled.clear()


def remember(task_id: str, labels) -> None:
    """The dispatch path's word: this task was dispatched with these."""
    if not labels:
        return
    if len(_labels) > LIMIT:
        _labels.clear()
    _labels[task_id] = list(labels)
    _first_miss.pop(task_id, None)
    _settled.discard(task_id)


async def lookup(
    task_id: str, load: Callable[[str], Awaitable[object]], now: Optional[float] = None
) -> Optional[List[str]]:
    """The labels of `task_id`, or None. `load(task_id)` returns the
    stored request (anything with `.labels`) or None."""
    hit = _labels.get(task_id)
    if hit is not None:
        return hit
    if task_id in _settled:
        return None
    request = await load(task_id)
    labels = list(getattr(request, "labels", None) or []) if request is not None else []
    if labels:
        remember(task_id, labels)
        return _labels[task_id]
    now = time.monotonic() if now is None else now
    if request is not None:
        settle = True  # stored, and it has no labels
    else:
        if len(_first_miss) > LIMIT:
            _first_miss.clear()
        first = _first_miss.setdefault(task_id, now)
        settle = now - first >= MISS_SETTLES_S
    if settle:
        if len(_settled) > LIMIT:
            _settled.clear()
        _settled.add(task_id)
        _first_miss.pop(task_id, None)
    return None
