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

A MISSION NEVER FAILS BECAUSE A ROBOT IS BUSY OR CHARGING (G ruling
2026-10-01, ruling 2, F-435): "It waits, shown to the operator as
'waiting for a robot' with its age, and raises one alert after a
threshold. It fails only if NO robot can ever take it (no route, no
capable robot), with the reason named. The 900 s / 8-hop failure path is
removed."

That paragraph replaces two bounds, and why they existed is worth
keeping. An ordinary hand-back used to stop at eight hops ("a robot keeps
winning a mission it cannot run is a defect signal"), and a CHARGE HOLD
hand-back (F-319, 2026-09-16) at 900 s of wall clock from its first hop
("a robot that holds a mission off the floor for a quarter of an hour
without resuming is a defect"). Drill 13 (2026-10-01) is what retired
them: seven of twelve robots were charging at once, the pinned planner
kept awarding missions to held robots (charge-then-task), the fleet
refused each award, and 38 of 88 missions were failed by those bounds —
on a fleet that was healthy and simply busy charging. A hand-back is a
WAIT whatever its class: the robot that refused is busy, charging,
faulted or misplaced, and another robot — or the same one later — takes
the mission. So every hand-back is re-dispatched, without a hop or clock
bound, after a progressive backoff (HAND_BACK_BACKOFF_STEP_S per
generation, capped at HAND_BACK_MAX_BACKOFF_S) that gives the fleet's
state time to change between attempts — a retry 5 s later met the same
held robot and the same auction (F-319), and the F-291 settle the old
5 s wait existed for is inside the first step. The generation is still
counted on every row (`gf:redispatch-gen`), for the operator and the
tooling: it is a measure of how hard the mission was to place, no longer
a budget.

What the bounds caught does not go unseen: the api-server's waiting
registry (waiting_missions.py) shows every chain that is waiting, with
its age and its latest reason, and raises ONE alert per chain past
GF_WAITING_ALERT_S.

A NO-BID AUCTION is the other situation, and it is not a hand-back at
all (G ruling 2026-10-01 item 6, F-410/F-412 class). When an auction
closes with no submissions the dispatcher fails the task and says so with
its own error (code 10, "No fleet adapters offered a bid"), next to
whatever each fleet answered (rmf_task_ros2 Dispatcher.cpp conclude_bid).
F-435 splits it by that answer:

  * PERMANENT — some fleet answered "not feasible" (code 9) with the
    planner's "insufficient battery capacity" detail: no robot can finish
    the mission on one charge even starting full, so no robot can ever
    take it. Item 6's rule, exactly: auctioned again after 2, 5, 10,
    20 s, and the fifth such answer in a row is a failed mission with the
    reason named (NO_BID_MAX_ATTEMPTS).
  * TRANSIENT — everything else: silence (code 10 alone), "insufficient
    initial battery charge" (every robot is too low NOW), any other
    planner refusal, an internal error. The fleet will change; the
    mission waits. Auctioned again forever, after 2, 5, 10, 20 s and
    then every NO_BID_MAX_BACKOFF_S, and never failed.

(An unreachable stop never gets this far: it is refused at dispatch,
F-34/F-111 `isolated_place`.)

It is its own class with its own labels, shaped like the hand-back,
because the two must not spend each other:

  * a no-bid retry never touches `gf:redispatch-gen` (nor the
    hand-back's reason). Nobody won that auction, so no robot "kept
    winning a mission it cannot run";
  * a hand-back CLEARS the no-bid counts. The mission was awarded, so the
    fleet answered, and "N auctions in a row" starts again from one.

The permanent count is read from the STORED request, the same truth the
hand-back generation is read from, so a task state that lost its labels
cannot restart it.

"Never a failed mission" is kept on the ROW, not only in the retry.
While the mission waits, the attempt that got no bid is recorded the way
a hand-back is — `canceled`, with `gf:redispatch` first in its
cancellation labels and a plain reason after it — so the ledger, the
queue and the bell read it as a mission that went back on the floor,
never as a failure. The dispatcher's own verdict (`dispatch.status`,
`dispatch.errors`) stays on the row as provenance, and it is also what
tells this module that the row is a no-bid attempt and not a hand-back,
so the rewritten row leads to exactly one re-auction and never to a
hand-back hop as well. If the re-auction then cannot be made (the request
is not stored, the dispatch is refused) the promise on the row was false:
the row is amended back to `failed` and the reason is named in a Critical
alert — a refused re-auction is a failed mission, never a silently
dropped one.

The wait between attempts is an in-process sleep. An api-server restart
during it would drop the pending attempt, so the api-server RESUMES at
start every chain whose last row is a marked cancel with no successor
(routes/internal.py resume_waiting_chains).

An OPERATOR can stop a waiting mission: a cancel of its live attempt is
an ordinary cancel, and a cancel of an attempt that is between auctions
withdraws the chain (waiting_missions.py) — `wanted` below then says no
and nothing is dispatched again.

COUNTING MISSIONS, NOT ROWS. The dispatcher mints a task id per auction
and there is no mission id, so one mission is a CHAIN of rows. Every
row after the first carries, in its booking labels:

  gf:redispatch-root=<id>     the chain's FIRST row (the id the operator
                              was given)
  gf:redispatch-of=<id>       the row this one replaces — always the
                              immediate predecessor, whichever class
  gf:redispatch-class=...     why this row exists: `no-bid`, `hand-back`
                              or `charge-hold`
  gf:redispatch-gen=N         hand-backs so far in the chain
  gf:redispatch-nobid-attempt=N
                              which auction in a row this is (2, 3, ...);
                              absent on a row that follows an award
  gf:redispatch-nobid-since=<unix s>
                              when the first auction of that run opened
  gf:redispatch-nobid-permanent=K
                              how many auctions in a row before this one
                              were answered "no robot can ever take it";
                              absent when the last answer was not

The folding rule: a row's chain is the value of `gf:redispatch-root`,
or the row's own id when it has none. The chain's outcome is the status
of its LAST row, the one no other row names in `gf:redispatch-of`.
Every earlier row is `canceled` with `gf:redispatch` in its cancellation
labels: a superseded attempt, not a canceled mission, and not counted.
A chain whose last row is itself a marked cancel has no successor — its
re-dispatch was refused or is pending (in flight) — and counts as LOST
once it is old enough not to be pending, not as canceled. (A chain
already in flight when this was deployed has no root label on its early
rows; its later rows name, as their root, the row that was live at that
moment.)

Pure helpers first (unit-tested without a server); the async hook at the
bottom is what routes/internal.py calls after persisting a task state.
"""

import logging
import time
from typing import Iterable, List, NamedTuple, Optional

# The marker the fleet adapter puts FIRST in the cancellation labels of
# a mission it wants back on the floor (gentle_fleet_adapter/
# charge_governor.py REDISPATCH_LABEL). Byte-identical on both sides.
REDISPATCH_LABEL = "gf:redispatch"
ORIGIN_LABEL = "gf:redispatch-of="
GENERATION_LABEL = "gf:redispatch-gen="
REASON_LABEL = "gf:redispatch-reason="
# F-319: the reason prefixes the governor uses when it hands a mission
# back because a robot is HELD FOR CHARGING — the refusal at award and
# the first-tick cancel of a stale award. Byte-identical to
# charge_governor.hold_reason / fleet_adapter._refuse_award. Since F-435
# they choose the chain's class label only; every class waits the same.
CHARGE_HOLD_MARKERS = ("charge hold (F-319)", "charge hold (F-36)")
# The F-319 charge-hold wall clock, removed by F-435. Never written any
# more; stripped from a chain that was in flight when it was deployed.
LEGACY_WAITING_LABEL = "gf:redispatch-waiting-since="
# F-435: every hand-back backs off by this much per generation, capped —
# long enough for the fleet's state to actually change between attempts.
# 15 s, 15 s, 30 s, 45 s, then 60 s for as long as the mission waits.
HAND_BACK_BACKOFF_STEP_S = 15.0
HAND_BACK_MAX_BACKOFF_S = 60.0
_CANCELED = {"canceled", "killed"}
# The dispatcher's own error on an auction that closed with no
# submissions (rmf_task_ros2 Dispatcher.cpp conclude_bid), and the fleet
# adapter's "not feasible" answer (FleetUpdateHandle.cpp make_error_str).
NO_BID_CODE = 10
NOT_FEASIBLE_CODE = 9
# F-435: the one answer that means NO robot can ever take the mission —
# the planner's limited_capacity error, "insufficient battery capacity to
# accommodate one or more requests by any of the robots in this fleet"
# (FleetUpdateHandle.cpp). Byte-identical to dispatch_reason
# _LIMITED_CAPACITY (test_redispatch pins the two together).
PERMANENT_NO_BID_DETAIL = "insufficient battery capacity"
# G ruling 2026-10-01 item 6 (F-410/F-412 class), kept for the PERMANENT
# answer only by F-435: how many such auctions in a row a mission gets
# before it is a failure, and the wait before attempts 2..5. With the
# auction closing early once every fleet has answered, a healthy auction
# takes milliseconds.
NO_BID_MAX_ATTEMPTS = 5
NO_BID_BACKOFF_S = (2.0, 5.0, 10.0, 20.0)
# F-435: a TRANSIENT no-bid waits the same 2, 5, 10, 20 s, then this long
# between auctions for as long as it waits — a mission is never failed
# because every robot was busy, low or silent.
NO_BID_MAX_BACKOFF_S = 60.0
NO_BID_ATTEMPT_LABEL = "gf:redispatch-nobid-attempt="
NO_BID_SINCE_LABEL = "gf:redispatch-nobid-since="
NO_BID_PERMANENT_LABEL = "gf:redispatch-nobid-permanent="
# Chain folding (see COUNTING MISSIONS in the module docstring).
ROOT_LABEL = "gf:redispatch-root="
CLASS_LABEL = "gf:redispatch-class="
CLASS_HAND_BACK = "hand-back"
CLASS_CHARGE_HOLD = "charge-hold"
CLASS_NO_BID = "no-bid"


def status_tail(status_value) -> Optional[str]:
    if status_value is None:
        return None
    return str(status_value).split(".")[-1].strip().lower()


def _code_of(err) -> Optional[int]:
    code = err.get("code") if isinstance(err, dict) else \
        getattr(err, "code", None)
    code = getattr(code, "root", code)
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _detail_of(err) -> str:
    detail = err.get("detail") if isinstance(err, dict) else \
        getattr(err, "detail", None)
    return str(detail) if detail else ""


def no_bid_is_permanent(dispatch_errors: Optional[Iterable]) -> bool:
    """F-435: did some fleet answer that NO robot can ever take this
    mission? Only the planner's limited-capacity refusal says that; every
    other answer — silence, every robot too low right now, any other
    refusal, an internal error — describes the fleet as it is NOW."""
    for err in dispatch_errors or []:
        if _code_of(err) == NOT_FEASIBLE_CODE and \
                PERMANENT_NO_BID_DETAIL in _detail_of(err):
            return True
    return False


class NoBid(NamedTuple):
    """One auction that closed with no submissions.

    `attempt` is which auction in a row it was (1 for a mission's first,
    and for the first after an award); `delay_s` is the wait before the
    next one, None when this was the last the mission gets; `answered`
    says the fleet did answer — with a refusal — rather than stay silent;
    `permanent` says that answer was "no robot can ever take it", and
    `run` is then which such answer in a row this is (1..5; 0 when the
    answer was not permanent)."""
    attempt: int
    delay_s: Optional[float]
    answered: bool
    permanent: bool = False
    run: int = 0

    @property
    def final(self) -> bool:
        return self.delay_s is None


def _label_int(labels: Optional[Iterable[str]], prefix: str
               ) -> Optional[int]:
    for label in labels or []:
        if label.startswith(prefix):
            try:
                return int(label[len(prefix):])
            except ValueError:
                return None
    return None


def no_bid_attempt_of(labels: Optional[Iterable[str]]) -> int:
    """Which auction in a row a mission with these labels is. A mission
    with no count is on its first; garbage reads as the first too."""
    attempt = _label_int(labels, NO_BID_ATTEMPT_LABEL)
    return 1 if attempt is None else max(1, attempt)


def no_bid_permanent_of(labels: Optional[Iterable[str]]) -> int:
    """How many auctions in a row before this one were answered "no robot
    can ever take it". None, garbage or a negative count read as none:
    the stored request, which every retry writes, is the bound's truth."""
    run = _label_int(labels, NO_BID_PERMANENT_LABEL)
    return 0 if run is None else max(0, run)


def no_bid_since_of(labels: Optional[Iterable[str]]) -> Optional[float]:
    for label in labels or []:
        if label.startswith(NO_BID_SINCE_LABEL):
            try:
                return float(label[len(NO_BID_SINCE_LABEL):])
            except ValueError:
                return None
    return None


def no_bid_backoff(attempt: int) -> Optional[float]:
    """PERMANENT answers (item 6's schedule): wait this long after the
    `attempt`-th such answer in a row, then auction again. None when that
    auction was the last one."""
    attempt = max(1, attempt)
    if attempt >= NO_BID_MAX_ATTEMPTS:
        return None
    return NO_BID_BACKOFF_S[min(attempt, len(NO_BID_BACKOFF_S)) - 1]


def transient_no_bid_backoff(attempt: int) -> float:
    """TRANSIENT answers (F-435): wait this long after auction number
    `attempt` got no bid, then auction again — 2, 5, 10, 20 s, then
    NO_BID_MAX_BACKOFF_S every time after. Never None: never the last."""
    attempt = max(1, attempt)
    if attempt <= len(NO_BID_BACKOFF_S):
        return NO_BID_BACKOFF_S[attempt - 1]
    return NO_BID_MAX_BACKOFF_S


def no_bid_verdict(status_value, booking_labels: Optional[Iterable[str]],
                   dispatch_errors: Optional[Iterable],
                   cancellation_labels: Optional[Iterable[str]] = None
                   ) -> Optional[NoBid]:
    """Did this task's auction close with no submissions, and if so which
    attempt was it, and is it the last? None for everything else.

    Two shapes are the same auction: the state as the dispatcher sent it
    (`failed`, code 10 in the dispatch errors), and that state after
    supersede() recorded it as put back on the floor (`canceled` with
    the `gf:redispatch` marker, the dispatcher's errors still on it). A
    task that reached a robot never carries code 10, so the marker plus
    code 10 can only be our own rewrite — which is how a rewritten row
    is kept out of the hand-back path.

    A cancellation WITHOUT the marker is somebody asking for the mission
    to stop (an operator's cancel that raced the auction, F-292): that
    mission is not wanted any more and is never auctioned again.
    `cancellation_labels` is None when the state has no cancellation."""
    errors = list(dispatch_errors or [])
    codes = [_code_of(err) for err in errors]
    if NO_BID_CODE not in codes:
        return None
    marked = cancellation_labels is not None and \
        REDISPATCH_LABEL in list(cancellation_labels)
    status = status_tail(status_value)
    if status == "failed":
        if cancellation_labels is not None and not marked:
            return None
    elif not (status in _CANCELED and marked):
        return None
    attempt = no_bid_attempt_of(booking_labels)
    answered = len(codes) > codes.count(NO_BID_CODE)
    if no_bid_is_permanent(errors):
        run = no_bid_permanent_of(booking_labels) + 1
        return NoBid(attempt, no_bid_backoff(run), answered, True, run)
    return NoBid(attempt, transient_no_bid_backoff(attempt), answered)


def no_bid_summary(verdict: NoBid) -> str:
    """What happened at this auction, in an operator's words. A fleet
    that answered with a refusal is not a fleet that stayed silent, and
    the line says which it was. Only an answer that counts toward a
    failure says how many such answers the mission gets."""
    what = ("no robot offered to take this mission" if verdict.answered
            else "no robot answered the auction")
    if verdict.permanent:
        return f"{what} (attempt {verdict.run} of {NO_BID_MAX_ATTEMPTS})"
    return f"{what} (attempt {verdict.attempt})"


def superseded_reason(verdict: NoBid) -> str:
    """The plain line recorded on an attempt that is being auctioned
    again — what an operator reads as its cancellation reason."""
    return (f"{no_bid_summary(verdict)} — auctioned again in "
            f"{verdict.delay_s:.0f} s")


def wants_retry(status_value, booking_labels: Optional[Iterable[str]],
                dispatch_errors: Optional[Iterable],
                cancellation_labels: Optional[Iterable[str]] = None
                ) -> Optional[str]:
    """The plain reason when this state is an auction nobody bid on that
    is auctioned AGAIN, else None: a failure with any other cause, a
    mission somebody canceled, and the last permanent attempt are left
    alone.

    G ruling 2026-10-01 item 6: this used to answer only for a
    re-dispatched child (F-291, once per generation). It answers for ANY
    dispatched mission, first generation included, and since F-435 for
    every transient answer however many came before it."""
    verdict = no_bid_verdict(status_value, booking_labels, dispatch_errors,
                             cancellation_labels)
    if verdict is None or verdict.final:
        return None
    return superseded_reason(verdict)


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
    """Is this hand-back a robot waiting to charge? Matched on the
    governor's own reason text, which both sides own. It names the
    chain's class (`charge-hold`); it no longer changes how it waits."""
    if not reason:
        return False
    return any(marker in reason for marker in CHARGE_HOLD_MARKERS)


def hand_back_backoff(generation: int) -> float:
    """F-435: wait this long before putting a handed-back mission back on
    the floor, whatever its class. Grows with the generation so a fleet
    that is briefly all-busy or all-held is not re-auctioned every few
    seconds, and is capped so a waiting mission is still offered to the
    fleet every minute."""
    step = HAND_BACK_BACKOFF_STEP_S * max(1, generation)
    return min(HAND_BACK_MAX_BACKOFF_S, step)


def generation_of(labels: Optional[Iterable[str]]) -> int:
    generation = _label_int(labels, GENERATION_LABEL)
    return 0 if generation is None else generation


def origin_of(labels: Optional[Iterable[str]]) -> Optional[str]:
    for label in labels or []:
        if label.startswith(ORIGIN_LABEL):
            return label[len(ORIGIN_LABEL):] or None
    return None


def root_of(labels: Optional[Iterable[str]]) -> Optional[str]:
    for label in labels or []:
        if label.startswith(ROOT_LABEL):
            return label[len(ROOT_LABEL):] or None
    return None


def class_of(labels: Optional[Iterable[str]]) -> Optional[str]:
    for label in labels or []:
        if label.startswith(CLASS_LABEL):
            return label[len(CLASS_LABEL):] or None
    return None


def next_labels(original_labels: Optional[Iterable[str]], origin_id: str,
                reason: str) -> List[str]:
    """Labels for the re-dispatched request: the operator's own labels
    kept, our bookkeeping labels replaced, generation bumped.

    F-435: there is no bound any more — a chain is never stopped by its
    hop count or its age (see the module docstring). The class label
    still says whether a robot was charging.

    A hand-back also ends any run of no-bid auctions (G ruling
    2026-10-01 item 6): the mission was awarded, so the fleet answered,
    and the no-bid labels are dropped — see the module docstring."""
    original = list(original_labels or [])
    kept = [lab for lab in original
            if not lab.startswith((ORIGIN_LABEL, GENERATION_LABEL,
                                   REASON_LABEL, LEGACY_WAITING_LABEL,
                                   ROOT_LABEL, CLASS_LABEL,
                                   NO_BID_ATTEMPT_LABEL,
                                   NO_BID_SINCE_LABEL,
                                   NO_BID_PERMANENT_LABEL))]
    gen = generation_of(original) + 1
    chain_class = CLASS_CHARGE_HOLD if is_charge_hold(reason) \
        else CLASS_HAND_BACK
    kept.append(f"{ORIGIN_LABEL}{origin_id}")
    kept.append(f"{GENERATION_LABEL}{gen}")
    kept.append(f"{REASON_LABEL}{reason[:200]}")
    kept.append(f"{ROOT_LABEL}{root_of(original) or origin_id}")
    kept.append(f"{CLASS_LABEL}{chain_class}")
    return kept


def next_no_bid_labels(original_labels: Optional[Iterable[str]],
                       origin_id: str, since_s: float,
                       permanent: bool = False) -> Optional[List[str]]:
    """Labels for a mission auctioned again after a no-bid: everything it
    came with is kept — the operator's labels AND the hand-back
    bookkeeping (generation, reason), because a no-bid retry is not a
    hand-back and must not spend it. Only the link to the row it
    replaces, the class and the no-bid counts are written.

    `permanent` is whether THIS auction's answer was "no robot can ever
    take it": the permanent count then climbs, and None is returned when
    that was the NO_BID_MAX_ATTEMPTS-th such answer in a row (the mission
    has had its last auction). Any other answer clears the count — the
    fleet said the mission could be taken — and never returns None.

    `since_s` is when the first auction of this run opened; it is used
    only when the mission does not already carry one."""
    original = list(original_labels or [])
    attempt = no_bid_attempt_of(original) + 1
    run = no_bid_permanent_of(original) + 1 if permanent else 0
    if run >= NO_BID_MAX_ATTEMPTS:
        return None
    since = no_bid_since_of(original)
    if since is None:
        since = since_s
    kept = [lab for lab in original
            if not lab.startswith((ORIGIN_LABEL, ROOT_LABEL, CLASS_LABEL,
                                   NO_BID_ATTEMPT_LABEL,
                                   NO_BID_SINCE_LABEL,
                                   NO_BID_PERMANENT_LABEL))]
    kept.append(f"{ORIGIN_LABEL}{origin_id}")
    kept.append(f"{ROOT_LABEL}{root_of(original) or origin_id}")
    kept.append(f"{CLASS_LABEL}{CLASS_NO_BID}")
    kept.append(f"{NO_BID_ATTEMPT_LABEL}{attempt}")
    kept.append(f"{NO_BID_SINCE_LABEL}{since:.0f}")
    if run:
        kept.append(f"{NO_BID_PERMANENT_LABEL}{run}")
    return kept


def _state_parts(task_state):
    booking = getattr(task_state, "booking", None)
    dispatch = getattr(task_state, "dispatch", None)
    cancellation = getattr(task_state, "cancellation", None)
    return (
        getattr(task_state, "status", None),
        getattr(booking, "labels", None),
        getattr(dispatch, "errors", None),
        getattr(cancellation, "labels", None)
        if cancellation is not None else None,
    )


def no_bid_verdict_of(task_state) -> Optional[NoBid]:
    """no_bid_verdict() read off a task state model. Never raises: a
    state this cannot read is not a no-bid."""
    try:
        return no_bid_verdict(*_state_parts(task_state))
    except Exception:  # noqa: BLE001 — cannot see, so cannot convict
        return None


def supersede(task_state, now_ms: Optional[int] = None) -> Optional[NoBid]:
    """G ruling 2026-10-01 item 6: a no-bid auction with attempts left is
    not a failed mission — and since F-435 only a permanent answer ever
    runs out of attempts. Applied at ingest, before the state is stored,
    broadcast or alerted on: `failed` becomes `canceled` with the
    `gf:redispatch` marker and the plain reason, exactly the shape of a
    hand-back, and the dispatcher's own status and errors stay where
    they are. Mutates the state in place.

    Returns the verdict — also for the LAST attempt, which is left
    `failed` — or None when the state is not a no-bid auction. Never
    raises: the feed must not stall on this, and a state it could not
    rewrite simply stays the failure the dispatcher reported."""
    try:
        verdict = no_bid_verdict(*_state_parts(task_state))
        if verdict is None or verdict.final:
            return verdict
        if status_tail(task_state.status) != "failed":
            return verdict      # already recorded as superseded
        # imported here so the pure helpers above load without the models
        from api_server.models.rmf_api.task_state import Cancellation

        if now_ms is None:
            now_ms = round(time.time() * 1e3)
        cancellation = Cancellation(
            unix_millis_request_time=now_ms,
            labels=[REDISPATCH_LABEL, superseded_reason(verdict)])
        task_state.status = type(task_state.status)("canceled")
        task_state.cancellation = cancellation
        return verdict
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "re-auction: the no-bid rewrite was skipped: %s", exc)
        return None


def unsupersede(task_state) -> bool:
    """Undo supersede() on a stored row whose re-auction could not be
    made: the mission DID fail, and the row must say so. Touches only a
    row supersede() wrote (`canceled`, marker, code 10); True when it
    changed the state."""
    try:
        status, booking_labels, errors, cancel_labels = \
            _state_parts(task_state)
        if status_tail(status) not in _CANCELED:
            return False
        if no_bid_verdict(status, booking_labels, errors,
                          cancel_labels) is None:
            return False
        task_state.status = type(task_state.status)("failed")
        task_state.cancellation = None
        return True
    except Exception:  # noqa: BLE001
        return False


class Redispatcher:
    """Acts once per terminal task id; the dispatch call is injected so
    the class is testable without a server.

    Counters, per class, since this process started. Hand-backs:
    `redispatched` (back on the floor) and `refused` (request not stored,
    dispatch refused). No-bid auctions: `reauctioned` (auctioned again),
    `no_bid_exhausted` (the last permanent answer — the mission failed)
    and `no_bid_abandoned` (the re-auction could not be made — the
    mission failed). Both: `withdrawn` (an operator canceled the mission
    while it waited, so it was not dispatched again)."""

    def __init__(self, dispatch, load_request, logger: logging.Logger,
                 cap: int = 2048, abandon=None, first_seen=None,
                 clock=time.time, wanted=None):
        self._dispatch = dispatch          # async (request) -> new task id
        self._load_request = load_request  # async (task_id) -> TaskRequest
        self._logger = logger
        # async (task_id, reason): the re-auction promised on a
        # superseded row cannot be made — fail the row, page the operator
        self._abandon = abandon
        # async (task_id) -> unix s the ledger first recorded the task
        self._first_seen = first_seen
        self._clock = clock
        # (task_id) -> bool: is the mission still wanted after the wait?
        # False when an operator canceled it in the meantime (F-435)
        self._wanted = wanted
        self._seen: List[str] = []
        self._cap = cap
        self.redispatched = 0
        self.refused = 0
        self.reauctioned = 0
        self.no_bid_exhausted = 0
        self.no_bid_abandoned = 0
        self.withdrawn = 0

    def _mark(self, task_id: str) -> bool:
        if task_id in self._seen:
            return False
        self._seen.append(task_id)
        while len(self._seen) > self._cap:
            self._seen.pop(0)
        return True

    def _still_wanted(self, task_id: str) -> bool:
        if self._wanted is None:
            return True
        try:
            wanted = bool(self._wanted(task_id))
        except Exception:  # noqa: BLE001 — cannot see, so cannot convict
            return True
        if not wanted:
            self.withdrawn += 1
            self._logger.warning(
                "re-dispatch: [%s] was canceled by an operator while it "
                "waited for a robot — not dispatched again", task_id)
        return wanted

    async def _give_up(self, task_id: str, reason: str) -> None:
        """The row says "auctioned again" and it will not be: hand it to
        the caller to be recorded as the failure it is."""
        self._logger.error("re-auction: [%s] %s", task_id, reason)
        if self._abandon is None:
            return
        try:
            await self._abandon(task_id, reason)
        except Exception:  # pylint: disable=broad-except
            self._logger.exception(
                "re-auction: [%s] could not be recorded as failed", task_id)

    async def _reauction(self, task_id: str, verdict: NoBid,
                         sleep) -> Optional[str]:
        """Auction a mission nobody bid on again, after its backoff (G
        ruling 2026-10-01 item 6; F-435 for the transient answers). The
        hand-back bookkeeping is carried, never advanced."""
        if not self._mark(task_id):
            return None
        if verdict.final:
            self.no_bid_exhausted += 1
            self._logger.error(
                "re-auction: [%s] — the fleet answered %d auctions in a row "
                "that no robot can ever take this mission (insufficient "
                "battery capacity); the mission failed (F-435)",
                task_id, verdict.run)
            return None
        closed_at = self._clock()
        await sleep(verdict.delay_s)
        if not self._still_wanted(task_id):
            return None
        said = no_bid_summary(verdict)
        request = await self._load_request(task_id)
        if request is None:
            self.no_bid_abandoned += 1
            await self._give_up(
                task_id,
                f"{said}, and it could not be auctioned again: its request "
                "is not stored (it was not dispatched through GentleFleet)")
            return None
        since = no_bid_since_of(request.labels)
        if since is None and self._first_seen is not None:
            try:
                since = await self._first_seen(task_id)
            except Exception:  # pylint: disable=broad-except
                since = None
        if since is None:
            since = closed_at
        labels = next_no_bid_labels(request.labels, task_id, since,
                                    verdict.permanent)
        if labels is None:
            # the state lost its labels and read as an early answer; the
            # stored request is the truth, and it says this was the last
            self.no_bid_exhausted += 1
            await self._give_up(
                task_id,
                f"the fleet answered {NO_BID_MAX_ATTEMPTS} auctions in a row "
                "that no robot can finish this mission on one battery "
                "charge, and the mission has no auction left")
            return None
        request = request.model_copy(update={"labels": labels})
        try:
            new_id = await self._dispatch(request)
        except Exception as exc:  # pylint: disable=broad-except
            why = getattr(exc, "detail", None) or str(exc)
            self.no_bid_abandoned += 1
            await self._give_up(
                task_id,
                f"{said}, and it could not be auctioned again: {why}")
            return None
        self.reauctioned += 1
        self._logger.warning(
            "re-auction: [%s] got no bid; auctioned again as [%s] "
            "(attempt %d%s)", task_id, new_id, no_bid_attempt_of(labels),
            f", {no_bid_permanent_of(labels)} of {NO_BID_MAX_ATTEMPTS} "
            "answered 'no robot can ever take it'" if verdict.permanent
            else "")
        return new_id

    async def maybe_redispatch(self, task_id: str, status_value,
                               cancellation_labels, booking_labels=None,
                               dispatch_errors=None,
                               sleep=None) -> Optional[str]:
        if sleep is None:
            import asyncio
            sleep = asyncio.sleep
        # the no-bid class is asked FIRST: a superseded attempt is a
        # marked cancel too, and must not also take the hand-back path
        verdict = no_bid_verdict(status_value, booking_labels,
                                 dispatch_errors, cancellation_labels)
        if verdict is not None:
            return await self._reauction(task_id, verdict, sleep)
        reason = wants_redispatch(status_value, cancellation_labels)
        if reason is None:
            return None
        if not self._mark(task_id):
            return None
        # F-435: every class waits, progressively, and none is ever
        # stopped — the fleet's state has to change, and it will
        await sleep(hand_back_backoff(generation_of(booking_labels)))
        if not self._still_wanted(task_id):
            return None
        request = await self._load_request(task_id)
        if request is None:
            self._logger.warning(
                "re-dispatch: [%s] was handed back but its request is not "
                "stored (a direct mission?) — left canceled", task_id)
            self.refused += 1
            return None
        labels = next_labels(request.labels, task_id, reason)
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
