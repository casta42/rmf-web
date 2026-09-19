"""F-343 (G ruling 2026-09-19): a mission is `completed` in the permanent
record only when EVERY one of its phases is completed. The fleet's own
word is not enough.

Measured on f1-n30/n31 (`ops/drills/drill_f343.py`, evidence in
`ops/fr40_stress/results/f343/`): a direct patrol [dropoff_2, charger]
whose charger spur was cordoned while it drove reached dropoff_2, and 70 s
after the closure the fleet's task_state_update carried `status:
completed` with `active: 1`, `completed: [1]` and phase 2 present but
never begun — while the same robot's fleet state said `working` on that
very task, and stayed so for eight minutes. Upstream
`rmf_task_sequence::Task::Active::status_overview()` returns the ACTIVE
phase's final-event status, so the moment phase 1's event completes, the
whole task reads "completed" until the phase switch happens — and with
the next stop unroutable that switch never came. The ledger stored the
word as it was sent, and the operator's record said a mission had
succeeded that never reached its stop. That is the phantom-completion
class (F-37, F-67) in the permanent record.

The rule here is small and is applied at ingest, before anything is
persisted or broadcast: `completed` with any phase not in `completed` is
rewritten to `underway` (the robot IS still working), with the reason
logged once per task. A later honest terminal from the fleet — failed,
canceled (the F-338 cordon-cut cancel is what actually ends such a
mission, with its reason) — is stored as sent.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from .logger import logger as base_logger

logger = base_logger.getChild("PhantomCompletion")

_reported: Set[str] = set()


def _int(value: Any) -> int:
    """Phase ids in the rmf_api models are pydantic RootModels (`Id`),
    not ints — the first live run of this guard skipped every update with
    `int() argument must be ... not 'Id'` (f1-n32). Unwrap them."""
    return int(getattr(value, "root", value))


def honest_status(
    status: Optional[str],
    active: Optional[int],
    completed: Optional[List[int]],
    phase_ids: List[int],
) -> Tuple[Optional[str], Optional[str]]:
    """(status to store, reason) — the reason is None when nothing was
    changed. Pure."""
    if status != "completed" or not phase_ids:
        return status, None
    done = set(_int(i) for i in (completed or []))
    missing = sorted(_int(i) for i in phase_ids if _int(i) not in done)
    if not missing:
        return status, None
    where = f"phase {missing[0]} of {len(phase_ids)}"
    if active is not None and _int(active) in done:
        where += f" (the fleet still reports phase {active} active)"
    return "underway", (
        f"the fleet reported `completed` while {where} has not been "
        f"completed — recorded as underway, not as a success (F-343)"
    )


def apply(task_state: Any) -> Optional[str]:
    """Mutates `task_state.status` in place when the completion is not
    honest. Returns the reason, or None. Never raises."""
    try:
        status = getattr(task_state, "status", None)
        status = status.value if hasattr(status, "value") else status
        phases: Dict[Any, Any] = getattr(task_state, "phases", None) or {}
        phase_ids = [_int(k) for k in phases.keys()]
        new_status, reason = honest_status(
            status,
            getattr(task_state, "active", None),
            getattr(task_state, "completed", None),
            phase_ids,
        )
        if reason is None:
            return None
        task_state.status = (
            type(task_state.status)(new_status)
            if hasattr(task_state.status, "value")
            else new_status
        )
        task_id = str(getattr(getattr(task_state, "booking", None), "id", "?"))
        if task_id not in _reported:
            _reported.add(task_id)
            while len(_reported) > 512:
                _reported.pop()
            logger.warning("F-343: [%s] %s", task_id, reason)
        return reason
    except Exception as exc:  # noqa: BLE001 — the feed must never stall on this
        logger.warning("F-343 guard skipped: %s", exc)
        return None


def _reset_for_test() -> None:
    _reported.clear()
