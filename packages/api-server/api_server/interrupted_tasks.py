"""F-141 (E6 run-2 blocker 3): honest closure of missions a fleet
coordination restart orphaned.

rmf-core keeps no task state across a restart (drill 2): every mission
that was underway simply stops being announced. The api-server's rows
then sit in their last non-terminal state forever — six 'Executing'
phantoms after the 2026-08-25 drill, un-cancelable because the restarted
core no longer knows the ids. The ledger must stay coherent: a mission
either completes honestly or terminates honestly.

Pure logic lives here (no app imports) so the class is testable without
a running server:

  - RunBoundary detects a coordination outage from the CADENCE of fleet
    state updates: the live fleet publishes ~1 Hz; silence of
    FLEET_SILENCE_GAP followed by resumption is a restart epoch. (The
    sim-clock cutoff used by the F-12 charge reaper cannot see drill-2:
    gazebo's clock keeps running while rmf-core restarts.)
  - is_interrupted_row() classifies a task row: non-terminal AND not
    re-announced since the outage began -> the core does not know it ->
    close it as failed, with provenance.
"""
from datetime import datetime
from typing import Optional

# The live fleet publishes states ~1 Hz; this much silence is a
# coordination outage, not jitter.
FLEET_SILENCE_GAP = 30.0
# After resumption, everything the core still knows re-announces within
# seconds; wait this long before declaring anything orphaned.
REANNOUNCE_GRACE = 90.0
# Terminal states never need closing.
TERMINAL_STATUSES = {"completed", "failed", "canceled", "killed",
                     "skipped"}
# The booking label that carries the provenance into the stored state.
INTERRUPTED_LABEL = "gf:interrupted=coordination-restart"


class RunBoundary:
    """Coordination-outage detector over the fleet-state cadence."""

    def __init__(self):
        self.last_seen: Optional[float] = None          # monotonic
        self.epoch_started_mono: Optional[float] = None
        self.epoch_started_wall: Optional[datetime] = None
        self.reaped = True

    def observe(self, now_mono: float, now_wall: datetime) -> None:
        if self.last_seen is not None and \
                now_mono - self.last_seen >= FLEET_SILENCE_GAP:
            self.epoch_started_mono = now_mono
            self.epoch_started_wall = now_wall
            self.reaped = False
        self.last_seen = now_mono

    def due(self, now_mono: float) -> bool:
        return (not self.reaped
                and self.epoch_started_mono is not None
                and now_mono - self.epoch_started_mono
                >= REANNOUNCE_GRACE)

    def mark_reaped(self) -> None:
        self.reaped = True


def status_tail(status_value) -> Optional[str]:
    """'TaskStatus.underway' / 'underway' / enum member -> 'underway'."""
    if status_value is None:
        return None
    return str(status_value).split(".")[-1].strip().lower()


def is_interrupted_row(status_value,
                       updated_at: Optional[datetime],
                       epoch_started_wall: datetime) -> bool:
    """Non-terminal AND silent since the outage began: the restarted
    core does not know this task; its state machine can never close."""
    tail = status_tail(status_value)
    if tail in TERMINAL_STATUSES:
        return False
    if updated_at is None:
        return True
    if updated_at.tzinfo is None and \
            epoch_started_wall.tzinfo is not None:
        updated_at = updated_at.replace(
            tzinfo=epoch_started_wall.tzinfo)
    return updated_at < epoch_started_wall
