"""F-339 — durable cordons (G ruling 2026-09-19).

Measured on f1-n26, 2026-09-18: lanes [16, 17] were closed and confirmed,
the fleet adapter crashed (F-338) and restarted, and thirteen seconds later
its cordon tier reported `0 closed lane(s)`. The robot the operator had kept
out of that spur drove in and parked at 0.00 m. Nothing told the operator.

This module makes the operator's closure INTENT durable and the fleet's
report of it a separate fact:

  * intent lives in the api-server ledger (`LaneClosure` rows, stored as
    lane geometry — see the model's docstring for why not the site repo);
  * intent is PUBLISHED as one latched `/lane_closure_requests` message
    carrying the whole intended set, so a fleet adapter that starts later
    hears the cordon before it admits a robot (the adapter waits for it);
  * the fleet's own `/closed_lanes` is the CONFIRMATION, and whenever it
    lacks an intended lane the intent is re-asserted — a restarted adapter
    publishes an honest empty set at start, and that mismatch is the
    trigger;
  * a row whose endpoints no lane joins any more (the graph was re-derived
    and that lane retired with a zone) is reported UNRESOLVED and dropped
    at the next write. It is never guessed onto a neighbouring lane.

The api-server is the only writer of `/lane_closure_requests`. Every
in-memory structure here is keyed by fleet, and every function is safe to
call from the ROS spin thread; only the ledger writes are async.
"""

import logging
import time
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from . import cordon
from .logger import logger as base_logger
from .models import tortoise_models as ttm

logger = base_logger.getChild("LaneClosures")

# A re-assert is sent at most this often per fleet while the fleet's
# confirmation still lacks an intended lane.
REASSERT_INTERVAL_S = 5.0

# fleet -> rows (plain dicts mirroring LaneClosure, so the ROS thread never
# touches an ORM object)
_intent: Dict[str, List[dict]] = {}
# fleet -> (closed set the fleet reported, monotonic time, wall millis)
_confirmed: Dict[str, Tuple[FrozenSet[int], float, int]] = {}
_last_reassert: Dict[str, float] = {}
_last_published_close: Dict[str, FrozenSet[int]] = {}
_publisher: Optional[Callable[[str, List[int], List[int]], None]] = None


def set_publisher(fn: Optional[Callable[[str, List[int], List[int]], None]]) -> None:
    global _publisher  # pylint: disable=global-statement
    _publisher = fn


def _reset_for_test() -> None:
    _intent.clear()
    _confirmed.clear()
    _last_reassert.clear()
    _last_published_close.clear()


def _row_dict(row: ttm.LaneClosure) -> dict:
    return {
        "id": row.id,
        "fleet": row.fleet,
        "entry_name": row.entry_name,
        "entry_x": row.entry_x,
        "entry_y": row.entry_y,
        "exit_name": row.exit_name,
        "exit_x": row.exit_x,
        "exit_y": row.exit_y,
        "lane_index_at_request": row.lane_index_at_request,
        "requested_by": row.requested_by,
        "unix_millis_request_time": row.unix_millis_request_time,
        "reason": row.reason,
    }


async def load() -> None:
    """Read every row from the ledger (api-server start)."""
    rows = await ttm.LaneClosure.all()
    _intent.clear()
    for row in rows:
        _intent.setdefault(row.fleet, []).append(_row_dict(row))
    for fleet in list(_intent):
        logger.info(
            "F-339: loaded %d closure row(s) for fleet [%s]", len(_intent[fleet]), fleet
        )


def resolve(fleet: str) -> Tuple[Dict[int, dict], List[dict]]:
    """(resolved: lane index -> row, unresolved rows) against the fleet's
    CURRENT graph. With no graph yet every row is unresolved, which is
    "cannot say", not "gone"."""
    graph = cordon.graph_of(fleet)
    resolved: Dict[int, dict] = {}
    unresolved: List[dict] = []
    for row in _intent.get(fleet, []):
        index = (
            cordon.resolve_lane(
                graph, (row["entry_x"], row["entry_y"]), (row["exit_x"], row["exit_y"])
            )
            if graph
            else None
        )
        if index is None:
            unresolved.append(row)
        else:
            resolved[index] = row
    return resolved, unresolved


def intended_lanes(fleet: str) -> FrozenSet[int]:
    return frozenset(resolve(fleet)[0])


def publish(fleet: str, opened: List[int] = ()) -> Optional[List[int]]:
    """Publish the whole intended set (plus any lanes just opened). Returns
    the closed list published, or None when there is no publisher or no
    graph to resolve against yet."""
    if _publisher is None or cordon.graph_of(fleet) is None:
        return None
    close = sorted(intended_lanes(fleet))
    open_ = sorted(set(int(i) for i in opened) - set(close))
    _publisher(fleet, close, open_)
    _last_published_close[fleet] = frozenset(close)
    return close


async def close_lanes(
    fleet: str, lanes: List[int], requested_by: str, reason: str
) -> Tuple[List[int], List[int]]:
    """Persist and publish. Returns (newly closed, already intended)."""
    graph = cordon.graph_of(fleet)
    resolved, unresolved = resolve(fleet)
    await _drop_unresolved(fleet, unresolved)
    now_millis = int(time.time() * 1000)
    new: List[int] = []
    already: List[int] = []
    for lane in sorted(set(int(i) for i in lanes)):
        if lane in resolved:
            already.append(lane)
            continue
        geometry = cordon.lane_geometry(graph, lane)
        if geometry is None:
            continue  # the route refused this before we got here
        (entry_name, ex, ey), (exit_name, xx, xy) = geometry["entry"], geometry["exit"]
        row = await ttm.LaneClosure.create(
            fleet=fleet,
            entry_name=entry_name,
            entry_x=ex,
            entry_y=ey,
            exit_name=exit_name,
            exit_x=xx,
            exit_y=xy,
            lane_index_at_request=lane,
            requested_by=requested_by,
            unix_millis_request_time=now_millis,
            reason=reason or "",
        )
        _intent.setdefault(fleet, []).append(_row_dict(row))
        new.append(lane)
    publish(fleet)
    if new:
        logger.warning(
            "F-339: [%s] closed lanes %s by %s — %s",
            fleet,
            new,
            requested_by,
            reason or "(no reason given)",
        )
    return new, already


async def open_lanes(fleet: str, lanes: List[int], requested_by: str) -> List[int]:
    """Drop the rows resolving to `lanes`, publish with them opened.
    Returns the lanes actually released from intent."""
    resolved, unresolved = resolve(fleet)
    await _drop_unresolved(fleet, unresolved)
    released: List[int] = []
    for lane in sorted(set(int(i) for i in lanes)):
        row = resolved.get(lane)
        if row is None:
            continue
        await ttm.LaneClosure.filter(id=row["id"]).delete()
        _intent[fleet] = [r for r in _intent.get(fleet, []) if r["id"] != row["id"]]
        released.append(lane)
    publish(fleet, opened=list(set(int(i) for i in lanes)))
    if released:
        logger.warning(
            "F-339: [%s] opened lanes %s by %s", fleet, released, requested_by
        )
    return released


async def _drop_unresolved(fleet: str, unresolved: List[dict]) -> None:
    """A row the current graph cannot place is retired — with a log line
    that names the lane it used to be. The graph was re-derived under it
    (a zone closed that lane for good) and a cordon on a lane that no
    longer exists protects nothing."""
    if not unresolved or cordon.graph_of(fleet) is None:
        return
    for row in unresolved:
        logger.warning(
            "F-339: retiring closure row %d on [%s] — no lane joins "
            "(%.2f, %.2f) -> (%.2f, %.2f) in the current graph any more "
            "(it was lane %d when %s closed it)",
            row["id"],
            fleet,
            row["entry_x"],
            row["entry_y"],
            row["exit_x"],
            row["exit_y"],
            row["lane_index_at_request"],
            row["requested_by"],
        )
        await ttm.LaneClosure.filter(id=row["id"]).delete()
    ids = {row["id"] for row in unresolved}
    _intent[fleet] = [r for r in _intent.get(fleet, []) if r["id"] not in ids]


def on_fleet_confirmation(fleet: str, closed: FrozenSet[int], now_millis: int) -> bool:
    """The fleet reported its closed set. Re-assert when it lacks any
    intended lane (a restarted adapter reports [] at start — that IS the
    F-339 event). Returns True when a re-assert was sent."""
    now = time.monotonic()
    _confirmed[fleet] = (frozenset(closed), now, now_millis)
    missing = intended_lanes(fleet) - frozenset(closed)
    if not missing:
        return False
    if now - _last_reassert.get(fleet, -1e9) < REASSERT_INTERVAL_S:
        return False
    _last_reassert[fleet] = now
    sent = publish(fleet)
    if sent is not None:
        logger.warning(
            "F-339: fleet [%s] reports closed %s but the operator's cordon is "
            "%s — re-asserting the missing lanes %s",
            fleet,
            sorted(closed),
            sent,
            sorted(missing),
        )
    return sent is not None


def on_graph(fleet: str) -> None:
    """A (new) graph for the fleet arrived: re-resolve and publish the
    intent so a fleet that just started hears it at once."""
    if _intent.get(fleet):
        sent = publish(fleet)
        if sent is not None:
            logger.info(
                "F-339: graph for [%s] received — cordon published: %s", fleet, sent
            )


def status(fleet: str) -> dict:
    resolved, unresolved = resolve(fleet)
    confirmed = _confirmed.get(fleet)
    confirmed_set = confirmed[0] if confirmed else frozenset()
    intended = frozenset(resolved)
    return {
        "fleet": fleet,
        "graph_known": cordon.graph_of(fleet) is not None,
        "intent": [
            {**row, "lane": lane, "resolved": True, "confirmed": lane in confirmed_set}
            for lane, row in sorted(resolved.items())
        ]
        + [
            {**row, "lane": None, "resolved": False, "confirmed": False}
            for row in unresolved
        ],
        "intended_lanes": sorted(intended),
        "confirmed_lanes": sorted(confirmed_set),
        "unix_millis_confirmed_time": confirmed[2] if confirmed else None,
        # the operator-facing verdict: every intended lane is confirmed by
        # the fleet
        "in_force": bool(confirmed) and intended <= confirmed_set,
        "missing_from_fleet": sorted(intended - confirmed_set),
    }


def fleets() -> List[str]:
    return sorted(set(_intent) | set(_confirmed))
