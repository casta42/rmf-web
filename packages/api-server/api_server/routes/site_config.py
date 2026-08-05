"""GentleFleet fork: zone-editor proxy (FR-10/DR-4, D-17; proposed D-19).

The dashboard talks only to the api-server (DR-1). Zone-editor traffic is
proxied to the localhost-only site-config sidecar, which owns the per-site
config repo, validation/regen and the restart-based apply. This route's
responsibilities — everything user-facing about authority and safety:

- admin gate (FR-19): every endpoint requires an admin user;
- identity: `applied_by` is stamped from the authenticated user, never
  taken from the request body (NFR-4 audit trail);
- D-17 mission guard: apply (and the recovery restart) REFUSES with 409
  and the list of non-terminal missions unless the caller re-sends with
  acknowledge_active_missions=true (the dashboard's hard-confirm flow).

The shared-secret header authenticates this proxy to the sidecar; it is
read per-request so a token rotation needs no api-server restart.
"""

from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api_server.app_config import app_config
from api_server.authenticator import user_dep
from api_server.models import TaskStatus, User
from api_server.models.tortoise_models import TaskState as DbTaskState

TOKEN_HEADER = "x-gf-internal-token"

# A mission still counts as running in every one of these states; the
# fleet pause would interrupt it (D-17). NULL statuses (rows that never
# received a state update) are deliberately not counted — they cannot be
# distinguished from dead pre-cutover rows, and the hard-confirm path
# covers the residual risk.
_TERMINAL = {
    TaskStatus.failed,
    TaskStatus.skipped,
    TaskStatus.canceled,
    TaskStatus.killed,
    TaskStatus.completed,
}
_NON_TERMINAL = [s for s in TaskStatus if s not in _TERMINAL]
# The book keeper str()s the pydantic enum into the status column, so
# live rows read "Status.underway", not "underway" (upstream quirk —
# repositories/tasks.py:154 filters with enum instances for the same
# reason). Match BOTH representations so the guard survives an upstream
# fix that starts storing plain values.
_NON_TERMINAL_MATCH: List[object] = [
    *_NON_TERMINAL,
    *(s.value for s in _NON_TERMINAL),
]


def admin_dep(user: User = Depends(user_dep)):
    if not user.is_admin:
        raise HTTPException(403)


router = APIRouter(tags=["SiteConfig"], dependencies=[Depends(admin_dep)])


class ApplyBody(BaseModel):
    candidate: Dict[str, Any]
    acknowledge_fleet_pause: bool = False
    acknowledge_active_missions: bool = False


class RestartBody(BaseModel):
    acknowledge_active_missions: bool = False


def _sidecar() -> tuple[str, str]:
    url = app_config.site_config_url
    token_file = app_config.site_config_token_file
    if not url or not token_file:
        raise HTTPException(
            404,
            "no site-config service configured for this site "
            "(site_config_url / site_config_token_file)",
        )
    try:
        with open(token_file, "r", encoding="utf8") as f:
            token = f.read().strip()
    except OSError as e:
        raise HTTPException(503, f"site-config token unreadable: {e}") from e
    if not token:
        raise HTTPException(503, "site-config token file is empty")
    return url.rstrip("/"), token


async def _proxy(
    method: str,
    path: str,
    json: Optional[dict] = None,
    timeout: float = 30.0,
) -> Any:
    base, token = _sidecar()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method,
                base + path,
                json=json,
                headers={TOKEN_HEADER: token},
                timeout=timeout,
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            503, f"site-config service unreachable: {e}"
        ) from e
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise HTTPException(resp.status_code, detail)
    return resp.json()


async def active_missions() -> List[Dict[str, Any]]:
    """Non-terminal missions, for the D-17 guard and its 409 payload."""
    rows = await DbTaskState.filter(status__in=_NON_TERMINAL_MATCH).values(
        "id_", "status", "assigned_to"
    )
    return [
        {
            "task_id": row["id_"],
            "status": (row["status"] or "").removeprefix("Status."),
            "robot": row["assigned_to"],
        }
        for row in rows
    ]


async def _guard_missions(acknowledged: bool) -> None:
    missions = await active_missions()
    if missions and not acknowledged:
        raise HTTPException(
            409,
            {
                "reason": "active_missions",
                "message": (
                    f"{len(missions)} mission(s) are still running; "
                    "applying now would interrupt them. Cancel them "
                    "first, or confirm the interruption explicitly."
                ),
                "missions": missions,
            },
        )


@router.get("")
async def get_site_config() -> Any:
    return await _proxy("GET", "/site_config")


@router.post("/validate")
async def validate(candidate: Dict[str, Any]) -> Any:
    # validate runs a scratch nav-graph regen when building.yaml changed
    return await _proxy(
        "POST", "/site_config/validate", json=candidate, timeout=120.0
    )


@router.post("/apply")
async def apply(
    body: ApplyBody, user: User = Depends(user_dep)
) -> Any:
    await _guard_missions(body.acknowledge_active_missions)
    return await _proxy(
        "POST",
        "/site_config/apply",
        json={
            "candidate": body.candidate,
            # server-side identity — the body cannot impersonate (NFR-4)
            "applied_by": user.username,
            "acknowledge_fleet_pause": body.acknowledge_fleet_pause,
        },
        timeout=60.0,
    )


@router.post("/restart")
async def restart(
    body: RestartBody, user: User = Depends(user_dep)
) -> Any:
    # The recovery restart pauses the fleet exactly like an apply (D-17).
    await _guard_missions(body.acknowledge_active_missions)
    base, token = _sidecar()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                base + "/site_config/restart",
                headers={
                    TOKEN_HEADER: token,
                    "x-gf-applied-by": user.username,
                },
                timeout=60.0,
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            503, f"site-config service unreachable: {e}"
        ) from e
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()


@router.get("/apply_status")
async def apply_status() -> Any:
    return await _proxy("GET", "/site_config/apply_status")
