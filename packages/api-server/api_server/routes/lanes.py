"""F-339 / F-338 — the operator's cordon: durable lane closures.

  GET  /lanes/closures?fleet=      what is intended, what the fleet confirms
  POST /lanes/closures             {fleet, close: [i], open: [i], reason,
                                    confirm}

The POST is the ONE apply point for a runtime cordon (the api-server is
the only writer of `/lane_closure_requests`), so the F-338 editor rule
lives here: a closure that would leave a robot unable to reach its own
charger is answered 409 with the robot, its charger and the lanes named,
and goes through only with `confirm: true` — the same hard-confirm shape
the zone editor uses for a destination it strands (D-20). Lane indices
are the fleet's own (`/nav_graphs`, F-333); an index the graph does not
have is refused, never guessed.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api_server import cordon, lane_closures
from api_server.authenticator import user_dep
from api_server.models import User

from .site_config import robot_positions

router = APIRouter(tags=["Lanes"])


class ClosureBody(BaseModel):
    fleet: Optional[str] = None
    close: List[int] = Field(default_factory=list)
    open: List[int] = Field(default_factory=list)
    reason: str = ""
    # F-338: acknowledge that this closure strands a robot's charger
    confirm: bool = False


def _fleet_or_409(fleet: Optional[str]) -> str:
    known = [f for f in cordon.known_graphs()]
    if fleet:
        if fleet not in known:
            raise HTTPException(
                409,
                f"the fleet [{fleet}] has not published its graph yet — a "
                "closure cannot be judged against a graph the server has "
                "not seen (F-333), so it is not applied",
            )
        return fleet
    if len(known) == 1:
        return known[0]
    raise HTTPException(
        409,
        "name the fleet: "
        + (
            f"{len(known)} fleets have published graphs {known}"
            if known
            else "no fleet has published its graph yet"
        ),
    )


async def _strands(fleet: str, closed_after: frozenset) -> List[dict]:
    chargers = cordon.chargers_of(fleet)
    if not chargers:
        return []
    robots = [
        {**r, "charger": chargers.get(str(r["name"]), "")}
        for r in await robot_positions()
    ]
    return cordon.stranded_chargers(cordon.graph_of(fleet), closed_after, robots)


@router.get("/closures")
async def get_closures(
    fleet: Optional[str] = None, _user: User = Depends(user_dep)
) -> Dict[str, Any]:
    if fleet is None:
        names = sorted(set(cordon.known_graphs()) | set(lane_closures.fleets()))
        return {"fleets": [lane_closures.status(name) for name in names]}
    return lane_closures.status(fleet)


@router.post("/closures")
async def post_closures(
    body: ClosureBody, user: User = Depends(user_dep)
) -> Dict[str, Any]:
    fleet = _fleet_or_409(body.fleet)
    graph = cordon.graph_of(fleet) or {}
    edges = graph.get("edges") or []
    unknown = sorted(
        i for i in set(body.close) | set(body.open) if i < 0 or i >= len(edges)
    )
    if unknown:
        raise HTTPException(
            409,
            f"lanes {unknown} are not in fleet [{fleet}]'s graph "
            f"({len(edges)} lanes) — refused rather than closing a lane "
            "that does not exist",
        )
    intended = lane_closures.intended_lanes(fleet)
    after = frozenset((intended | set(body.close)) - set(body.open))
    strands = await _strands(fleet, after) if body.close else []
    if strands and not body.confirm:
        names = "; ".join(
            f"{s['robot']} could no longer reach its charger [{s['charger']}] "
            f"(lanes {s['lanes']} in the way)"
            for s in strands
        )
        raise HTTPException(
            409,
            detail={
                "message": (
                    f"This closure would strand a robot's charger: {names}. "
                    "A robot that cannot reach its charger holds where it is "
                    "and raises an alert when its battery runs low (F-338). "
                    "Send confirm=true to apply it anyway."
                ),
                "strands": strands,
            },
        )
    new, already = [], []
    if body.close:
        new, already = await lane_closures.close_lanes(
            fleet, body.close, user.username, body.reason
        )
    released: List[int] = []
    if body.open:
        released = await lane_closures.open_lanes(fleet, body.open, user.username)
    return {
        **lane_closures.status(fleet),
        "closed_now": new,
        "already_closed": already,
        "opened_now": released,
        "strands": strands,
    }
