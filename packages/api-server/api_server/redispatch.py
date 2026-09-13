"""FR-12 charge governor, api-server half (F-36/F-107/F-286, G ruling
2026-09-12): a mission the fleet adapter cancels to charge a robot is
RE-DISPATCHED to the fleet, so another robot does it.

The adapter cannot re-bid a task on the Humble pin — a canceled task is
terminal there — but it knows WHY it canceled and says so: the
cancellation carries the marker label `gf:redispatch` plus a human
reason ("charge preemption (F-36): [gentle_bot_3] at SoC 0.17 ..."). The
api-server stores every dispatched mission's original request (F-19 /
`task_request` rows), so on seeing that marker it submits the same
request again, labelled with its origin and generation, through the very
same /tasks/dispatch_task path an operator uses (F-34 guard included).

Bounded by construction: a re-dispatched mission carries its generation
and the chain stops at MAX_GENERATIONS (the held robot is a
non-candidate the instant it is idle, so a second hop is already rare;
eight is a defect signal, logged as such). Terminal states are
re-broadcast by the fleet, so each origin id is acted on once.

Pure helpers first (unit-tested without a server); the async hook at the
bottom is what routes/internal.py calls after persisting a task state.
"""

import logging
from typing import Iterable, List, Optional

# The marker the fleet adapter puts FIRST in the cancellation labels of
# a mission it wants back on the floor (gentle_fleet_adapter/
# charge_governor.py REDISPATCH_LABEL). Byte-identical on both sides.
REDISPATCH_LABEL = "gf:redispatch"
ORIGIN_LABEL = "gf:redispatch-of="
GENERATION_LABEL = "gf:redispatch-gen="
REASON_LABEL = "gf:redispatch-reason="
MAX_GENERATIONS = 8
_CANCELED = {"canceled", "killed"}
# F-291: a child re-dispatched within a second of the fleet's cancel met
# `[TaskPlanner] Failed to compute assignments` twice (five full robots
# idle) and nothing else ever did. Let the fleet's queues settle first,
# and give a child the dispatcher could not place (code 10, no bid) one
# more try per generation.
SETTLE_DELAY_S = 5.0
RETRY_DELAY_S = 10.0
NO_BID_CODE = 10


def status_tail(status_value) -> Optional[str]:
    if status_value is None:
        return None
    return str(status_value).split(".")[-1].strip().lower()


def wants_retry(status_value, booking_labels: Optional[Iterable[str]],
                dispatch_errors: Optional[Iterable[dict]]) -> Optional[str]:
    """A re-dispatched child that FAILED because no fleet bid on it
    (dispatcher code 10) is retried once per generation; a failure with
    any other cause, or a mission that was never ours, is left alone."""
    if status_tail(status_value) != "failed":
        return None
    labels = list(booking_labels or [])
    if origin_of(labels) is None:
        return None
    for err in dispatch_errors or []:
        code = err.get("code") if isinstance(err, dict) else \
            getattr(err, "code", None)
        if code == NO_BID_CODE:
            for label in labels:
                if label.startswith(REASON_LABEL):
                    return label[len(REASON_LABEL):]
            return "returned to the fleet by the charge governor"
    return None


def wants_redispatch(status_value, cancellation_labels: Optional[Iterable[str]]
                     ) -> Optional[str]:
    """The human reason when this state is a cancellation the fleet wants
    re-dispatched, else None. Only a CANCELED/KILLED state with the
    marker qualifies — a completed or failed mission never comes back."""
    if status_tail(status_value) not in _CANCELED:
        return None
    labels = list(cancellation_labels or [])
    if REDISPATCH_LABEL not in labels:
        return None
    for label in labels:
        if label != REDISPATCH_LABEL and not label.startswith("gf:"):
            return label
    return "returned to the fleet by the charge governor"


def generation_of(labels: Optional[Iterable[str]]) -> int:
    for label in labels or []:
        if label.startswith(GENERATION_LABEL):
            try:
                return int(label[len(GENERATION_LABEL):])
            except ValueError:
                return 0
    return 0


def origin_of(labels: Optional[Iterable[str]]) -> Optional[str]:
    for label in labels or []:
        if label.startswith(ORIGIN_LABEL):
            return label[len(ORIGIN_LABEL):] or None
    return None


def next_labels(original_labels: Optional[Iterable[str]], origin_id: str,
                reason: str) -> Optional[List[str]]:
    """Labels for the re-dispatched request: the operator's own labels
    kept, our bookkeeping labels replaced, generation bumped. None when
    the chain has reached MAX_GENERATIONS."""
    kept = [lab for lab in (original_labels or [])
            if not lab.startswith((ORIGIN_LABEL, GENERATION_LABEL,
                                   REASON_LABEL))]
    gen = generation_of(original_labels) + 1
    if gen > MAX_GENERATIONS:
        return None
    kept.append(f"{ORIGIN_LABEL}{origin_id}")
    kept.append(f"{GENERATION_LABEL}{gen}")
    kept.append(f"{REASON_LABEL}{reason[:200]}")
    return kept


class Redispatcher:
    """Acts once per canceled task id; the dispatch call is injected so
    the class is testable without a server."""

    def __init__(self, dispatch, load_request, logger: logging.Logger,
                 cap: int = 2048):
        self._dispatch = dispatch          # async (request) -> new task id
        self._load_request = load_request  # async (task_id) -> TaskRequest
        self._logger = logger
        self._seen: List[str] = []
        self._cap = cap
        self.redispatched = 0
        self.refused = 0

    def _mark(self, task_id: str) -> bool:
        if task_id in self._seen:
            return False
        self._seen.append(task_id)
        while len(self._seen) > self._cap:
            self._seen.pop(0)
        return True

    async def maybe_redispatch(self, task_id: str, status_value,
                               cancellation_labels, booking_labels=None,
                               dispatch_errors=None,
                               sleep=None) -> Optional[str]:
        reason = wants_redispatch(status_value, cancellation_labels)
        delay = SETTLE_DELAY_S
        if reason is None:
            reason = wants_retry(status_value, booking_labels, dispatch_errors)
            delay = RETRY_DELAY_S
        if reason is None:
            return None
        if not self._mark(task_id):
            return None
        if sleep is None:
            import asyncio
            sleep = asyncio.sleep
        await sleep(delay)
        request = await self._load_request(task_id)
        if request is None:
            self._logger.warning(
                "re-dispatch: [%s] was canceled for charging but its "
                "request is not stored (a direct mission?) — left canceled",
                task_id)
            self.refused += 1
            return None
        labels = next_labels(request.labels, task_id, reason)
        if labels is None:
            self._logger.error(
                "re-dispatch: [%s] has been handed back %d times — chain "
                "stopped (a robot keeps winning a mission it cannot run: "
                "F-36 governor defect signal)", task_id, MAX_GENERATIONS)
            self.refused += 1
            return None
        request = request.model_copy(update={"labels": labels})
        try:
            new_id = await self._dispatch(request)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.warning(
                "re-dispatch of [%s] refused: %s — left canceled with its "
                "provenance", task_id, exc)
            self.refused += 1
            return None
        self.redispatched += 1
        self._logger.warning(
            "re-dispatch: [%s] (%s) is back on the floor as [%s] "
            "(generation %d)", task_id, reason, new_id,
            generation_of(labels))
        return new_id
