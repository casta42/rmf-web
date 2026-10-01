"""G ruling 2026-10-01, ruling 2 (F-435) — a mission "waiting for a robot".

"A mission never fails because a robot is busy or charging. It waits,
shown to the operator as 'waiting for a robot' with its age, and raises
one alert after a threshold. It fails only if NO robot can ever take it
(no route, no capable robot), with the reason named."

redispatch.py keeps such a mission on the floor; this module is what the
operator sees of it. One entry per WAITING CHAIN (a mission is a chain of
ledger rows folded by `gf:redispatch-root`, redispatch.py COUNTING
MISSIONS), keyed by the chain's root id — the id the operator was given:

  * ENTERED when an attempt is superseded — a marked cancel: a hand-back
    of any class, or a no-bid auction put back on the floor;
  * UPDATED with the new attempt's id when it is re-dispatched, and with
    the latest reason and one more attempt when that one is superseded;
  * LEFT when any row of the chain starts (underway, or executing as
    blocked/delayed) or ends (completed, failed), when an attempt is
    canceled WITHOUT the marker (an operator's cancel), and when an
    operator cancels the attempt the chain waits on between auctions
    (`withdraw`; the pending re-dispatch then does not happen).

`since` is when the ledger first recorded the chain's root (the operator's
dispatch), so the age is the mission's, not the latest attempt's.

THE ONE ALERT. A chain that has waited `GF_WAITING_ALERT_S` raises one
Warning, once, and it is resolved when the chain starts or ends. The
default is 900 s (15 min), and the number is a proposal for G with its
reason: a full charge from the retreat line to the resume threshold
(0.19 -> 0.98) takes ~630 s on the compressed sim pack, so a mission that
waits because the robots that could take it are charging is placed within
one charge cycle. A healthy fleet that is only busy charging therefore
does not page anybody, and a mission still waiting after 15 min is
waiting on something a charge did not fix — which is when a person should
look. (A real pack charges slower; a site sets GF_WAITING_ALERT_S to its
own charge cycle plus margin.)

The registry is in memory. routes/internal.py rebuilds it from the ledger
at start (resume_waiting_chains) and resumes what a restart would have
dropped; it keeps no state of its own on disk.
"""

import logging
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional

from api_server.dispatch_reason import no_bid_waiting_reason
from api_server.redispatch import (
    no_bid_verdict_of,
    root_of,
    status_tail,
    wants_redispatch,
)

logger = logging.getLogger(__name__)

ALERT_ENV = "GF_WAITING_ALERT_S"
DEFAULT_ALERT_S = 900.0
# alert ids are `waiting__<root id>`: one row per chain, so a restart
# finds the alert it already raised instead of raising a second one
ALERT_PREFIX = "waiting__"
# statuses that mean the chain STARTED (a robot is on it) or ENDED
STARTED = frozenset({"underway", "blocked", "delayed"})
ENDED = frozenset({"completed", "failed", "skipped", "error"})
CANCELED = frozenset({"canceled", "killed"})
# row ids remembered so a re-broadcast terminal state is folded once
_MEMORY = 8192


def alert_threshold_s(env: Optional[Dict[str, str]] = None) -> float:
    """GF_WAITING_ALERT_S, seconds; the default when it is unset. A value
    that is not a positive number is refused with a warning, never taken
    as "page at once" or "never page"."""
    raw = (os.environ if env is None else env).get(ALERT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_ALERT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s=%r is not a positive number of seconds — using the default " "%.0f s",
            ALERT_ENV,
            raw,
            DEFAULT_ALERT_S,
        )
        return DEFAULT_ALERT_S
    return value


WAITING_ALERT_S = alert_threshold_s()


def alert_id_of(root_id: str) -> str:
    return f"{ALERT_PREFIX}{root_id}"


def alert_message(entry: "WaitingMission", now_s: float) -> str:
    minutes = int(entry.age_s(now_s) // 60)
    return (
        f"Mission {entry.root_id} has been waiting for a robot for {minutes} "
        f"min — {entry.reason}"
    )


@dataclass
class WaitingMission:
    root_id: str
    task_id: str
    since_unix: float
    reason: str
    attempts: int = 1
    category: Optional[str] = None
    places: List[str] = field(default_factory=list)
    # set once the chain's one alert has been raised (or, after a restart,
    # found already raised): it is never raised a second time
    alerted: Optional[str] = None
    # since/category/places have been read from the ledger
    enriched: bool = False

    def age_s(self, now_s: float) -> float:
        return max(0.0, now_s - self.since_unix)

    def view(self, now_s: float) -> dict:
        return {
            "root_id": self.root_id,
            "task_id": self.task_id,
            "category": self.category,
            "places": list(self.places),
            "since_unix": self.since_unix,
            "age_s": round(self.age_s(now_s), 1),
            "reason": self.reason,
            "attempts": self.attempts,
        }


class Change(NamedTuple):
    """What one task state did to the registry: `entered`, `updated` or
    `left`, and the chain it did it to."""

    kind: str
    entry: WaitingMission


def _remember(memory: "OrderedDict[str, object]", key: str, value=None) -> None:
    memory[key] = value
    memory.move_to_end(key)
    while len(memory) > _MEMORY:
        memory.popitem(last=False)


def waiting_reason(task_state) -> Optional[str]:
    """The operator's reason a superseded attempt is waiting, or None when
    the state is not a superseded attempt. A no-bid auction says what the
    fleet answered (dispatch_reason.no_bid_waiting_reason); a hand-back
    says what the fleet said when it handed the mission back — the same
    words the History view shows for that row."""
    cancellation = getattr(task_state, "cancellation", None)
    labels = cancellation.labels if cancellation is not None else None
    reason = wants_redispatch(task_state.status, labels)
    if reason is None:
        return None
    if no_bid_verdict_of(task_state) is not None:
        dispatch = task_state.dispatch
        return no_bid_waiting_reason(dispatch.errors if dispatch else None)
    return reason


class WaitingRegistry:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._chains: Dict[str, WaitingMission] = {}
        # attempts already counted (a fleet re-broadcasts terminal states)
        self._superseded: "OrderedDict[str, object]" = OrderedDict()
        # attempts an operator canceled between auctions -> their labels
        self._withdrawn: "OrderedDict[str, object]" = OrderedDict()

    # -- reading ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._chains)

    def get(self, root_id: str) -> Optional[WaitingMission]:
        return self._chains.get(root_id)

    def find_task(self, task_id: str) -> Optional[WaitingMission]:
        """The chain whose CURRENT attempt is this task id."""
        for entry in self._chains.values():
            if entry.task_id == task_id:
                return entry
        return None

    def entries(self) -> List[WaitingMission]:
        return sorted(self._chains.values(), key=lambda e: e.since_unix)

    def views(self, now_s: Optional[float] = None) -> List[dict]:
        """GET /tasks/waiting: every waiting chain, the longest wait first."""
        now_s = self._clock() if now_s is None else now_s
        return [entry.view(now_s) for entry in self.entries()]

    def due_alerts(self, now_s: float, threshold_s: float) -> List[WaitingMission]:
        """Chains that have waited at least `threshold_s` and have not had
        their one alert."""
        return [
            entry
            for entry in self.entries()
            if entry.alerted is None and entry.age_s(now_s) >= threshold_s
        ]

    def wanted(self, task_id: str) -> bool:
        """redispatch.Redispatcher `wanted`: False once an operator has
        withdrawn the attempt, so it is not dispatched again."""
        return task_id not in self._withdrawn

    # -- writing ------------------------------------------------------------
    def clear(self) -> None:
        self._chains.clear()
        self._superseded.clear()
        self._withdrawn.clear()

    def superseded(
        self,
        root_id: str,
        task_id: str,
        reason: str,
        since_unix: Optional[float] = None,
    ) -> Optional[Change]:
        """An attempt of this chain was put back on the floor. None when
        that attempt was already counted (a re-broadcast) or withdrawn."""
        if task_id in self._superseded or task_id in self._withdrawn:
            return None
        _remember(self._superseded, task_id)
        entry = self._chains.get(root_id)
        if entry is None:
            entry = WaitingMission(
                root_id=root_id,
                task_id=task_id,
                since_unix=self._clock() if since_unix is None else since_unix,
                reason=reason,
            )
            self._chains[root_id] = entry
            return Change("entered", entry)
        entry.task_id = task_id
        entry.reason = reason
        entry.attempts += 1
        return Change("updated", entry)

    def adopt(self, entry: WaitingMission) -> WaitingMission:
        """A chain found waiting in the ledger at start (a restart rebuilds
        the registry). The attempt it waits on counts as seen."""
        _remember(self._superseded, entry.task_id)
        self._chains[entry.root_id] = entry
        return entry

    def redispatched(
        self, root_id: str, old_id: str, new_id: str
    ) -> Optional[WaitingMission]:
        """The chain's attempt `old_id` is back on the floor as `new_id`.
        Nothing when the chain has meanwhile started, ended or moved on."""
        entry = self._chains.get(root_id)
        if entry is None or entry.task_id != old_id:
            return None
        entry.task_id = new_id
        return entry

    def leave(self, root_id: str) -> Optional[WaitingMission]:
        return self._chains.pop(root_id, None)

    def withdraw(self, task_id: str, labels: Iterable[str]) -> Optional[WaitingMission]:
        """An operator canceled the attempt a chain is waiting on, between
        auctions: the chain leaves, and the attempt is remembered with the
        operator's cancellation so it is never dispatched again. None when
        no chain waits on that attempt."""
        entry = self.find_task(task_id)
        if entry is None:
            return None
        self._chains.pop(entry.root_id, None)
        _remember(self._withdrawn, task_id, list(labels))
        return entry

    def withdrawn_labels(self, task_id: str) -> Optional[List[str]]:
        labels = self._withdrawn.get(task_id)
        return list(labels) if isinstance(labels, list) else None

    def observe(self, task_state) -> Optional[Change]:
        """Fold one task state — the real mdl.TaskState, as stored and
        broadcast — into the registry. Never raises: the feed must not
        stall on this, and a state it cannot read changes nothing."""
        try:
            task_id = task_state.booking.id
            root_id = root_of(task_state.booking.labels) or task_id
            status = status_tail(task_state.status)
            if status in STARTED or status in ENDED:
                entry = self.leave(root_id)
                return Change("left", entry) if entry is not None else None
            if status not in CANCELED:
                return None
            reason = waiting_reason(task_state)
            if reason is None:
                # canceled WITHOUT the marker: somebody stopped the mission
                if task_id in self._withdrawn:
                    return None
                entry = self.leave(root_id)
                return Change("left", entry) if entry is not None else None
            return self.superseded(root_id, task_id, reason)
        except Exception as exc:  # noqa: BLE001 — cannot see, so change nothing
            logger.warning("F-435: a task state could not be folded: %r", exc)
            return None


registry = WaitingRegistry()
