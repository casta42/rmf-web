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
and the chain stops at MAX_GENERATIONS — eight hand-backs means a robot
keeps winning a mission it cannot run, which is a defect signal and is
logged as one.

EXCEPT for a charge hold, which is not a defect (F-319, 2026-09-16).
That distinction was learned the hard way. The paragraph above used to
read "the held robot is a non-candidate the instant it is idle, so a
second hop is already rare" — true while the governor only intercepted
the rare awarded-while-busy mission. Since D-75 the fleet also refuses
an award to a held robot AT AWARD, and when every healthy robot is busy
the planner picks the held one again the moment the mission is back on
the floor: same robot, same refusal, eight hops in about two minutes,
mission dead. The drill killed four missions that way.

A charge hold is a WAIT, not a defect: the robot IS charging and WILL
resume, and the only thing the mission needs is for that to happen. So
a charge-hold hand-back backs off progressively instead of retrying
straight away, and is bounded by WALL CLOCK from the first hand-back
rather than by a hop count — CHARGE_HOLD_MAX_WAIT_S comfortably exceeds
a full 0.19 -> 0.98 charge (~630 s on the compressed pack). Past that
bound it IS a defect — a robot that has kept a mission off the floor
for a quarter of an hour without resuming is wrong — and it stops with
the same signal. Every other reason keeps the eight-hop cap untouched.

Pure helpers first (unit-tested without a server); the async hook at the
bottom is what routes/internal.py calls after persisting a task state.
"""

import logging
import time
from typing import Iterable, List, Optional

# The marker the fleet adapter puts FIRST in the cancellation labels of
# a mission it wants back on the floor (gentle_fleet_adapter/
# charge_governor.py REDISPATCH_LABEL). Byte-identical on both sides.
REDISPATCH_LABEL = "gf:redispatch"
ORIGIN_LABEL = "gf:redispatch-of="
GENERATION_LABEL = "gf:redispatch-gen="
REASON_LABEL = "gf:redispatch-reason="
MAX_GENERATIONS = 8
# F-319: the reason prefixes the governor uses when it hands a mission
# back because a robot is HELD FOR CHARGING — the refusal at award and
# the first-tick cancel of a stale award. Byte-identical to
# charge_governor.hold_reason / fleet_adapter._refuse_award.
CHARGE_HOLD_MARKERS = ("charge hold (F-319)", "charge hold (F-36)")
WAITING_LABEL = "gf:redispatch-waiting-since="
# A charge-hold hand-back backs off by this much per hop, capped — long
# enough for the fleet's state to actually change between attempts (a
# retry 5 s later meets the same held robot and the same auction).
CHARGE_HOLD_BACKOFF_STEP_S = 15.0
CHARGE_HOLD_MAX_BACKOFF_S = 60.0
# ...and keeps being handed back for at most this long in total. A full
# charge from the retreat line to the 0.98 resume is ~630 s on the
# compressed pack, so 15 min outlasts the condition it is waiting on
# without letting a mission bounce forever.
CHARGE_HOLD_MAX_WAIT_S = 900.0
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


def is_charge_hold(reason: Optional[str]) -> bool:
    """Is this hand-back a robot waiting to charge, rather than a defect?
    Matched on the governor's own reason text, which both sides own."""
    if not reason:
        return False
    return any(marker in reason for marker in CHARGE_HOLD_MARKERS)


def charge_hold_backoff(generation: int) -> float:
    """Wait this long before putting a charge-held mission back on the
    floor. Grows with the hop count so a fleet that is briefly all-held
    is not re-auctioned every few seconds, and is capped so a mission
    still gets several attempts inside CHARGE_HOLD_MAX_WAIT_S."""
    step = CHARGE_HOLD_BACKOFF_STEP_S * max(1, generation)
    return min(CHARGE_HOLD_MAX_BACKOFF_S, step)


def waiting_since_of(labels: Optional[Iterable[str]]) -> Optional[float]:
    for label in labels or []:
        if label.startswith(WAITING_LABEL):
            try:
                return float(label[len(WAITING_LABEL):])
            except ValueError:
                return None
    return None


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
                reason: str, now_s: Optional[float] = None
                ) -> Optional[List[str]]:
    """Labels for the re-dispatched request: the operator's own labels
    kept, our bookkeeping labels replaced, generation bumped. None when
    the chain has run out.

    Two different bounds, because there are two different situations
    (F-319). An ordinary hand-back runs out after MAX_GENERATIONS hops:
    a robot winning a mission it cannot run, eight times, is a defect.
    A CHARGE HOLD runs out on the clock instead — the robot is charging
    and will resume, so hops are the wrong unit and counting them kills
    a mission that only needed to wait."""
    kept = [lab for lab in (original_labels or [])
            if not lab.startswith((ORIGIN_LABEL, GENERATION_LABEL,
                                   REASON_LABEL, WAITING_LABEL))]
    gen = generation_of(original_labels) + 1
    if is_charge_hold(reason):
        if now_s is None:
            now_s = time.time()
        # the clock starts at the FIRST hand-back of this chain
        since = waiting_since_of(original_labels)
        if since is None:
            since = now_s
        if now_s - since > CHARGE_HOLD_MAX_WAIT_S:
            return None
        kept.append(f"{WAITING_LABEL}{since:.0f}")
    elif gen > MAX_GENERATIONS:
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
        if is_charge_hold(reason):
            # F-319: give the fleet time to stop being all-held. Retrying
            # after SETTLE_DELAY_S meets the same robot and the same
            # auction, which is how four missions died in the drill.
            delay = charge_hold_backoff(generation_of(booking_labels))
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
            if is_charge_hold(reason):
                self._logger.error(
                    "re-dispatch: [%s] has been waiting on a charge hold "
                    "for more than %.0f s — chain stopped. A robot that "
                    "holds a mission off the floor for this long without "
                    "resuming is a defect signal (F-319), not a wait",
                    task_id, CHARGE_HOLD_MAX_WAIT_S)
            else:
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
