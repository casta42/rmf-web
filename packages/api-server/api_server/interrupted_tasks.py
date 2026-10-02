"""F-141 (E6 run-2 blocker 3): honest closure of missions a fleet
coordination restart orphaned.

rmf-core keeps no task state across a restart (drill 2): every mission
that was underway simply stops being announced. The api-server's rows
then sit in their last non-terminal state forever — six 'Executing'
phantoms after the 2026-08-25 drill, un-cancelable because the restarted
core no longer knows the ids. The ledger must stay coherent: a mission
either completes honestly or terminates honestly.

D-86 (3a) and (4) (G close-out rulings 2026-10-01): the fail-closed design
RESTARTS rmf-core whenever the traffic schedule node dies, and "robots
resume only when the schedule is back"; "a refused re-dispatch never drops
a mission; it returns to the waiting queue under F-435". So a mission the
restart interrupted is NOT failed: its row is closed exactly like a
hand-back — `canceled`, the gf:redispatch marker first in its cancellation
labels, INTERRUPTED_REASON after it, the INTERRUPTED_LABEL kept on its
booking — and the api-server re-dispatches it as a new attempt of the same
chain, shown as "Waiting for a robot" until it is placed. Never 'Executing'
forever, never dropped.

F-454 (G ruling 2026-10-02, item 1): the sweep acts ONLY on a real
coordination restart, and on each mission once. Until f1-n66 it inferred
the restart from silence — a 30 s gap in the fleet-state cadence, and a
rolling "untouched for 300 s" pass that needed no restart at all — and the
D-86 rewrite then called a queued row nobody was assigned to "lost". On
f1-n66 that re-dispatched healthy missions waiting in the dispatcher's
bidding queue 946 times in 3 h with rmf-core never restarting. Both
inferences are gone. The restart is now a stated fact: the core's boot
identity (core_incarnation.py). Under a new identity every non-terminal row
created before the new core started belongs to the core that is gone — no
status, age or silence is weighed, so nothing here can convict a mission
of a core that is still running.

What a real restart does to each such row (decide()):

  somebody asked for it to stop       closed canceled with that request's
                                      labels, never sent again
  its request is not stored           closed failed, the reason named in
  (a direct robot task, the           one Warning: nothing can be re-sent
  fleet's own task)
  the core was away longer than       closed failed, the reason named in
  the resume window, or nobody        the same Warning: nobody can say it
  can say how long                    is still wanted (F-435's window; a
                                      database restored from an old backup
                                      lands here)
  otherwise                           closed like a hand-back and sent
                                      again as the next attempt of its
                                      chain

The fleet's own ChargeBattery rows stay the F-12 reaper's.

Pure logic lives here (no app imports) so the class is testable without a
running server.
"""

from typing import Optional

# Terminal states never need closing.
TERMINAL_STATUSES = {"completed", "failed", "canceled", "killed", "skipped"}
# The booking label that carries the provenance into the stored state.
INTERRUPTED_LABEL = "gf:interrupted=coordination-restart"
# D-86 (3a)/(4): the reason an interrupted mission is back on the floor —
# the hand-back reason of its closed row (and so its waiting reason).
INTERRUPTED_REASON = (
    "interrupted by a coordination restart — the restarted fleet core no "
    "longer knew it; sent to the fleet again"
)

LEAVE_TO_CHARGE_REAPER = "charge-reaper"
CLOSE_CANCELED = "canceled-as-requested"
FAIL_NOT_STORED = "failed-not-stored"
FAIL_LONG_OUTAGE = "failed-long-outage"
SEND_AGAIN = "send-again"


def status_tail(status_value) -> Optional[str]:
    """'TaskStatus.underway' / 'underway' / enum member -> 'underway'."""
    if status_value is None:
        return None
    return str(status_value).split(".")[-1].strip().lower()


def decide(
    task_id: str,
    cancel_requested: bool,
    request_stored: bool,
    outage_s: Optional[float],
    resume_window_s: float,
) -> str:
    """What a real coordination restart does to one non-terminal row the
    old core held — see the module docstring. `outage_s` is how long the
    core was away (None: unknown)."""
    if str(task_id).startswith("Charge"):
        return LEAVE_TO_CHARGE_REAPER
    if cancel_requested:
        return CLOSE_CANCELED
    if not request_stored:
        return FAIL_NOT_STORED
    if outage_s is None or outage_s > resume_window_s:
        return FAIL_LONG_OUTAGE
    return SEND_AGAIN


def away_for(outage_s: Optional[float]) -> str:
    """'for 3 h 12 min' — for the operator's Warning."""
    if outage_s is None:
        return "for an unknown time"
    minutes = int(outage_s // 60)
    if minutes < 1:
        return f"for {int(outage_s)} s"
    if minutes < 60:
        return f"for {minutes} min"
    return f"for {minutes // 60} h {minutes % 60} min"
