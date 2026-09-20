"""FR-42 (D-66) — WATCH-ONLY / commissioning: the dashboard's surface.

  GET  /robots/commissioning[?fleet=]     every configured robot's release
                                          and admission state, plus the live
                                          readiness facts of the ones the
                                          fleet may not command (FR-42 (c):
                                          a watch-only robot is not an RMF
                                          member, so it is rendered from
                                          here, not from /fleets)
  POST /robots/{fleet}/{robot}/release    admin only, one robot at a time,
                                          hard-confirmed by the dashboard;
                                          refused by name on any FR-42 (d)
                                          condition; logged with actor and
                                          typed reason (FR-42 (e))

A harness (simulation rehearsal) releases through the SAME route with
`harness: true`: the checklist is recorded as NOT affirmed and the actor
as `harness:<user>` (FR-42 (i)). No bulk path exists.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api_server import robot_releases
from api_server.authenticator import user_dep
from api_server.logger import logger as base_logger
from api_server.models import User

router = APIRouter(tags=["Robots"])
logger = base_logger.getChild("Robots")


class Checklist(BaseModel):
    supervised: bool = False
    estop_tested: bool = False
    deadman_ready: bool = False


class ReleaseBody(BaseModel):
    reason: str = ""
    checklist: Checklist = Field(default_factory=Checklist)
    # FR-42 (i): a simulation/test harness releasing a SIMULATED robot —
    # the checklist is false for it and is recorded as not affirmed
    harness: bool = False


@router.get("/commissioning")
async def get_commissioning(
    fleet: Optional[str] = None, _user: User = Depends(user_dep)
) -> Dict[str, Any]:
    if fleet is not None:
        return robot_releases.status(fleet)
    return {
        "site": robot_releases.site(),
        "fleets": [robot_releases.status(name) for name in robot_releases.fleets()],
    }


@router.post("/{fleet}/{robot}/release")
async def post_release(
    fleet: str, robot: str, body: ReleaseBody, user: User = Depends(user_dep)
) -> Dict[str, Any]:
    if not user.is_admin:
        raise HTTPException(
            403,
            "releasing a robot from commissioning is an admin action (FR-42 (d))",
        )
    try:
        record = await robot_releases.release(
            fleet,
            robot,
            user.username,
            body.reason,
            body.checklist.model_dump(),
            harness=body.harness,
        )
    except robot_releases.ReleaseRefused as refused:
        raise HTTPException(refused.status_code, detail=refused.detail) from refused
    # `released` in the status is the whole set; the row just written is `record`
    return {**robot_releases.status(fleet), "record": record}
