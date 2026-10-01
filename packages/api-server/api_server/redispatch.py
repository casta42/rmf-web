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

A NO-BID AUCTION is the third situation, and it is not a hand-back at
all (G ruling 2026-10-01 item 6, F-410/F-412 class): "A timed-out
auction never becomes a failed mission: re-auction with backoff, and
fail only after N attempts with the reason named." When an auction
closes with no submissions the dispatcher fails the task and says so
with its own error (code 10, "No fleet adapters offered a bid"). Until
this ruling that was the end of a first-generation mission — only a
re-dispatched child got one more try (F-291) — so an operator's mission
died of a fleet adapter that answered a moment late. Now ANY dispatched
mission whose auction gets no bid is auctioned again: at most
NO_BID_MAX_ATTEMPTS auctions in a row, NO_BID_BACKOFF_S apart, and only
the last one's failure is a failed mission.

It is its own class with its own labels, shaped like the charge hold,
because the two budgets must not spend each other:

  * a no-bid retry never touches `gf:redispatch-gen` (nor the
    hand-back's reason, nor its waiting-since clock). Nobody won that
    auction, so no robot "kept winning a mission it cannot run", and
    four unanswered auctions must not cost a mission four of its eight
    hand-backs;
  * a hand-back CLEARS the no-bid count. The mission was awarded, so the
    fleet answered, and "N auctions in a row" starts again from one. A
    count that survived awards would fail a long, healthy chain for
    auctions that went unanswered an hour apart.

Both stay bounded by construction: at most NO_BID_MAX_ATTEMPTS auctions
between two awards, and the awards themselves are bounded by the two
rules above. The count is read from the STORED request, the same truth
the hand-back generation is read from, so a task state that lost its
labels cannot restart it.

"Never a failed mission" is kept on the ROW, not only in the retry.
While attempts remain, the attempt that got no bid is recorded the way
a hand-back is — `canceled`, with `gf:redispatch` first in its
cancellation labels and a plain reason after it — so the ledger, the
queue and the bell read it as a mission that went back on the floor
(an Info line), never as a failure. The dispatcher's own verdict
(`dispatch.status`, `dispatch.errors`) stays on the row as provenance,
and it is also what tells this module that the row is a no-bid attempt
and not a hand-back, so the rewritten row leads to exactly one
re-auction and never to a hand-back hop as well. If the re-auction then
cannot be made (the request is not stored, the dispatch is refused) the
promise on the row was false: the row is amended back to `failed` and
the reason is named in a Critical alert — a refused re-auction is a
failed mission, never a silently dropped one.

What is NOT covered: the wait between attempts is an in-process sleep,
as it is for the two hand-back classes. An api-server restart during it
drops the pending re-auction, and the last attempt stays `canceled`
with no successor.

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
                              which auction in a row this is (2..5);
                              absent on a row that follows an award
  gf:redispatch-nobid-since=<unix s>
                              when the first auction of that run opened

The folding rule: a row's chain is the value of `gf:redispatch-root`,
or the row's own id when it has none. The chain's outcome is the status
of its LAST row, the one no other row names in `gf:redispatch-of`.
Every earlier row is `canceled` with `gf:redispatch` in its cancellation
labels: a superseded attempt, not a canceled mission, and not counted.
A chain whose last row is itself a marked cancel has no successor — the
hand-back bound stopped it, its re-dispatch was refused, or the
api-server restarted during the wait — and counts as LOST, not as
canceled. (A chain already in flight when this was deployed has no root
label on its early rows; its later rows name, as their root, the row
that was live at that moment.)

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
# idle) and nothing else ever did. Let the fleet's queues settle first.
SETTLE_DELAY_S = 5.0
# The dispatcher's own error on an auction that closed with no
# submissions (rmf_task_ros2 Dispatcher.cpp conclude_bid).
NO_BID_CODE = 10
# G ruling 2026-10-01 item 6 (F-410/F-412 class): how many auctions in a
# row one mission gets before no-bid is a failure, and the wait before
# attempts 2..5. With the auction closing early once every fleet has
# answered, a healthy auction takes milliseconds and a no-bid means the
# fleet adapter did not answer for the whole timeout — so a mission the
# fleet never answers fails 37 s plus five timeouts after it was
# dispatched, and one the fleet answers a moment late is back on the
# floor in two seconds.
NO_BID_MAX_ATTEMPTS = 5
NO_BID_BACKOFF_S = (2.0, 5.0, 10.0, 20.0)
NO_BID_ATTEMPT_LABEL = "gf:redispatch-nobid-attempt="
NO_BID_SINCE_LABEL = "gf:redispatch-nobid-since="
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


class NoBid(NamedTuple):
    """One auction that closed with no submissions.

    `attempt` is which auction in a row it was (1 for a mission's first,
    and for the first after an award); `delay_s` is the wait before the
    next one, None when this was the last the mission gets; `answered`
    says the fleet did answer — with a refusal — rather than stay
    silent."""
    attempt: int
    delay_s: Optional[float]
    answered: bool

    @property
    def final(self) -> bool:
        return self.delay_s is None


def no_bid_attempt_of(labels: Optional[Iterable[str]]) -> int:
    """Which auction in a row a mission with these labels is. A mission
    with no count is on its first; garbage reads as the first too, and
    the chain is still bounded because every retry writes a real one."""
    for label in labels or []:
        if label.startswith(NO_BID_ATTEMPT_LABEL):
            try:
                return max(1, int(label[len(NO_BID_ATTEMPT_LABEL):]))
            except ValueError:
                return 1
    return 1


def no_bid_since_of(labels: Optional[Iterable[str]]) -> Optional[float]:
    for label in labels or []:
        if label.startswith(NO_BID_SINCE_LABEL):
            try:
                return float(label[len(NO_BID_SINCE_LABEL):])
            except ValueError:
                return None
    return None


def no_bid_backoff(attempt: int) -> Optional[float]:
    """Wait this long after auction number `attempt` got no bid, then
    auction again. None when that auction was the last one."""
    attempt = max(1, attempt)
    if attempt >= NO_BID_MAX_ATTEMPTS:
        return None
    return NO_BID_BACKOFF_S[min(attempt, len(NO_BID_BACKOFF_S)) - 1]


def no_bid_verdict(status_value, booking_labels: Optional[Iterable[str]],
                   dispatch_errors: Optional[Iterable],
                   cancellation_labels: Optional[Iterable[str]] = None
                   ) -> Optional[NoBid]:
    """Did this task's auction close with no submissions, and if so which
    attempt was it? None for everything else.

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
    codes = [_code_of(err) for err in dispatch_errors or []]
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
    return NoBid(attempt, no_bid_backoff(attempt),
                 len(codes) > codes.count(NO_BID_CODE))


def no_bid_summary(verdict: NoBid) -> str:
    """What happened at this auction, in an operator's words. A fleet
    that answered with a refusal is not a fleet that stayed silent, and
    the line says which it was."""
    what = ("no robot offered to take this mission" if verdict.answered
            else "no robot answered the auction")
    return f"{what} (attempt {verdict.attempt} of {NO_BID_MAX_ATTEMPTS})"


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
    mission somebody canceled, and the last attempt are left alone.

    G ruling 2026-10-01 item 6: this used to answer only for a
    re-dispatched child (F-291, once per generation). It now answers for
    ANY dispatched mission, first generation included."""
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
    a mission that only needed to wait.

    A hand-back also ends any run of no-bid auctions (G ruling
    2026-10-01 item 6): the mission was awarded, so the fleet answered,
    and the no-bid labels are dropped — see the module docstring."""
    original = list(original_labels or [])
    kept = [lab for lab in original
            if not lab.startswith((ORIGIN_LABEL, GENERATION_LABEL,
                                   REASON_LABEL, WAITING_LABEL,
                                   ROOT_LABEL, CLASS_LABEL,
                                   NO_BID_ATTEMPT_LABEL,
                                   NO_BID_SINCE_LABEL))]
    gen = generation_of(original) + 1
    chain_class = CLASS_HAND_BACK
    if is_charge_hold(reason):
        chain_class = CLASS_CHARGE_HOLD
        if now_s is None:
            now_s = time.time()
        # the clock starts at the FIRST hand-back of this chain
        since = waiting_since_of(original)
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
    kept.append(f"{ROOT_LABEL}{root_of(original) or origin_id}")
    kept.append(f"{CLASS_LABEL}{chain_class}")
    return kept


def next_no_bid_labels(original_labels: Optional[Iterable[str]],
                       origin_id: str, since_s: float
                       ) -> Optional[List[str]]:
    """Labels for a mission auctioned again after a no-bid: everything it
    came with is kept — the operator's labels AND the hand-back
    bookkeeping (generation, reason, waiting-since), because a no-bid
    retry is not a hand-back and must neither spend that budget nor lose
    that clock. Only the link to the row it replaces, the class and the
    no-bid count are written. None when the mission has had its last
    auction.

    `since_s` is when the first auction of this run opened; it is used
    only when the mission does not already carry one."""
    original = list(original_labels or [])
    attempt = no_bid_attempt_of(original) + 1
    if attempt > NO_BID_MAX_ATTEMPTS:
        return None
    since = no_bid_since_of(original)
    if since is None:
        since = since_s
    kept = [lab for lab in original
            if not lab.startswith((ORIGIN_LABEL, ROOT_LABEL, CLASS_LABEL,
                                   NO_BID_ATTEMPT_LABEL,
                                   NO_BID_SINCE_LABEL))]
    kept.append(f"{ORIGIN_LABEL}{origin_id}")
    kept.append(f"{ROOT_LABEL}{root_of(original) or origin_id}")
    kept.append(f"{CLASS_LABEL}{CLASS_NO_BID}")
    kept.append(f"{NO_BID_ATTEMPT_LABEL}{attempt}")
    kept.append(f"{NO_BID_SINCE_LABEL}{since:.0f}")
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
    not a failed mission. Applied at ingest, before the state is stored,
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
    `redispatched` (back on the floor) and `refused` (chain bound hit,
    request not stored, dispatch refused). No-bid auctions: `reauctioned`
    (auctioned again), `no_bid_exhausted` (the last attempt also got no
    bid — the mission failed) and `no_bid_abandoned` (attempts remained
    but the re-auction could not be made — the mission failed)."""

    def __init__(self, dispatch, load_request, logger: logging.Logger,
                 cap: int = 2048, abandon=None, first_seen=None,
                 clock=time.time):
        self._dispatch = dispatch          # async (request) -> new task id
        self._load_request = load_request  # async (task_id) -> TaskRequest
        self._logger = logger
        # async (task_id, reason): the re-auction promised on a
        # superseded row cannot be made — fail the row, page the operator
        self._abandon = abandon
        # async (task_id) -> unix s the ledger first recorded the task
        self._first_seen = first_seen
        self._clock = clock
        self._seen: List[str] = []
        self._cap = cap
        self.redispatched = 0
        self.refused = 0
        self.reauctioned = 0
        self.no_bid_exhausted = 0
        self.no_bid_abandoned = 0

    def _mark(self, task_id: str) -> bool:
        if task_id in self._seen:
            return False
        self._seen.append(task_id)
        while len(self._seen) > self._cap:
            self._seen.pop(0)
        return True

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
        """G ruling 2026-10-01 item 6: auction a mission nobody bid on
        again, after its backoff. The hand-back bookkeeping is carried,
        never advanced."""
        if not self._mark(task_id):
            return None
        if verdict.final:
            self.no_bid_exhausted += 1
            self._logger.error(
                "re-auction: [%s] got no bid at %d auctions in a row — the "
                "mission failed (G ruling 2026-10-01 item 6: the fleet is "
                "not answering dispatches)", task_id, verdict.attempt)
            return None
        closed_at = self._clock()
        await sleep(verdict.delay_s)
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
        labels = next_no_bid_labels(request.labels, task_id, since)
        if labels is None:
            # the state lost its labels and read as an early attempt;
            # the stored request is the truth, and it says this was the
            # last one
            self.no_bid_exhausted += 1
            await self._give_up(
                task_id,
                f"no robot answered {NO_BID_MAX_ATTEMPTS} auctions in a "
                "row, and the mission has no auction left")
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
            "(attempt %d of %d)", task_id, new_id,
            no_bid_attempt_of(labels), NO_BID_MAX_ATTEMPTS)
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
        delay = SETTLE_DELAY_S
        if is_charge_hold(reason):
            # F-319: give the fleet time to stop being all-held. Retrying
            # after SETTLE_DELAY_S meets the same robot and the same
            # auction, which is how four missions died in the drill.
            delay = charge_hold_backoff(generation_of(booking_labels))
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
