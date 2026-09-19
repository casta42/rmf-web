"""F-343 / F-338 (G ruling 2026-09-19): a mission whose REMAINING stop the
operator's cordon has cut off must end `canceled` with an honest reason —
never `completed`, never a silent wait in the permanent record.

Applied at the one place the cut happens: `POST /lanes/closures`. Every
non-terminal task of the fleet is read from the ledger; the places its
remaining phases go to (phase categories `Go to [place:X]` from the
active phase onward) are checked over OPEN lanes from the vertex its robot
stands on, and a task with an unroutable remaining stop is canceled at
the fleet with the lanes named. The rules are pure (`cut_missions`) and
proven both ways in test_cordon_cut.py; the ledger and fleet I/O live in
`cancel_cut_missions`.

Fail-open like the rest of the cordon code: a robot off any vertex, a
task with no places in its phases, or an unknown place is left alone.
"""

import re
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from . import cordon
from .logger import logger as base_logger

logger = base_logger.getChild("CordonCut")

_PLACE = re.compile(r"\[place:([^\]]+)\]")
NON_TERMINAL = ("queued", "standby", "underway", "delayed", "blocked", "error")
CUT_LABEL = "gf:cordon-cut"


def remaining_places(task_state: Dict[str, Any]) -> List[str]:
    """Places of the phases not yet completed, in order. Pure."""
    phases = task_state.get("phases") or {}
    done = set(int(i) for i in (task_state.get("completed") or []))
    out: List[str] = []
    for pid in sorted(phases, key=lambda k: int(k)):
        if int(pid) in done:
            continue
        category = str((phases[pid] or {}).get("category") or "")
        out.extend(_PLACE.findall(category))
    return out


def cut_missions(
    graph: Optional[dict],
    closed: FrozenSet[int],
    tasks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """`tasks`: [{"id", "robot_xy": (x, y) | None, "places": [..]}] ->
    [{"id", "from", "to"}] for every task with an unroutable remaining
    stop. Pure."""
    out: List[Dict[str, Any]] = []
    if not graph or not closed:
        return out
    for task in tasks:
        xy = task.get("robot_xy")
        places = task.get("places") or []
        if xy is None or not places:
            continue
        start = cordon.nearest_vertex(graph, float(xy[0]), float(xy[1]))
        if start is not None:
            hop = cordon.unreachable_hop(graph, closed, [start], places)
        else:
            # mid-lane between vertices (the first live run, f1-n32,
            # skipped exactly this: the robot was driving to its first
            # stop when the lanes closed). The hop INTO the first stop
            # cannot be judged from here; the hops after it can, from the
            # first stop itself.
            first = cordon._vertex_index(graph, places[0])
            hop = (
                cordon.unreachable_hop(graph, closed, [first], places[1:])
                if first is not None and len(places) > 1
                else None
            )
            if hop is not None:
                hop = (places[0], hop[1])  # named for the stop, not "the robot"
        if hop is None:
            continue
        out.append({"id": task["id"], "from": hop[0], "to": hop[1]})
    return out


def cut_reason(hop: Dict[str, Any], closed: FrozenSet[int]) -> str:
    return (
        f"canceled: its next stop [{hop['to']}] cannot be reached from "
        f"{hop['from']} — lanes {sorted(closed)} were closed by an operator "
        f"after the mission was accepted (F-343/F-338). A mission that "
        f"cannot route its next stop is ended with this reason rather than "
        f"reported completed or left waiting."
    )


async def cancel_cut_missions(
    fleet: str, closed_after: FrozenSet[int]
) -> List[Dict[str, Any]]:
    """Read the ledger, judge, cancel at the fleet. Returns what was
    canceled (id, from, to). Never raises."""
    # pylint: disable=import-outside-toplevel
    try:
        from . import models as mdl
        from .models import tortoise_models as ttm
        from .rmf_io import tasks_service
        from .routes.site_config import robot_positions

        graph = cordon.graph_of(fleet)
        if not graph or not closed_after:
            return []
        xy = {
            str(p["name"]): (float(p["x"]), float(p["y"]))
            for p in await robot_positions()
        }
        rows = await ttm.TaskState.filter(status__in=list(NON_TERMINAL))
        tasks: List[Dict[str, Any]] = []
        for row in rows:
            data = row.data if isinstance(row.data, dict) else {}
            robot = str(row.assigned_to or "")
            if "/" in robot:
                robot = robot.split("/", 1)[1]
            tasks.append(
                {
                    "id": row.id_,
                    "robot_xy": xy.get(robot),
                    "places": remaining_places(data),
                }
            )
        hops = cut_missions(graph, closed_after, tasks)
        for hop in hops:
            reason = cut_reason(hop, closed_after)
            try:
                await tasks_service().call(
                    mdl.CancelTaskRequest(
                        type="cancel_task_request",
                        task_id=str(hop["id"]),
                        labels=[f"{CUT_LABEL}={sorted(closed_after)}", reason],
                    ).model_dump_json(exclude_none=True),
                    timeout=10,
                )
                logger.warning("F-343: %s — %s", hop["id"], reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning("F-343: could not cancel [%s]: %s", hop["id"], exc)
        return hops
    except Exception as exc:  # noqa: BLE001
        logger.warning("F-343 cordon-cut sweep skipped: %s", exc)
        return []
