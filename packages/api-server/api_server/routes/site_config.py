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
  F-86/D-23: this request-time check alone left a seconds-wide window
  (validate + regen + commit run in the sidecar before the restart), so
  the acknowledgment flag is FORWARDED to the sidecar, which re-verifies
  at the actual restart boundary via GET /_internal/active_missions
  (token-authenticated; see routes/internal.py).

The shared-secret header authenticates this proxy to the sidecar; it is
read per-request so a token rotation needs no api-server restart.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api_server.app_config import app_config
from api_server.authenticator import user_dep
from api_server.models import TaskStatus, User
from api_server.models.tortoise_models import FleetState as DbFleetState
from api_server.models.tortoise_models import ScheduledTask as DbScheduledTask
from api_server.models.tortoise_models import TaskFavorite as DbTaskFavorite
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
    # D-20: hard-confirm for closures that make destinations unreachable
    acknowledge_reachability: bool = False


async def robot_positions() -> List[Dict[str, Any]]:
    """Live robot positions, plus whether each robot is PARKED (no
    non-terminal mission assigned). Injected into every candidate so
    the sidecar can plan D-24 §5 evacuations — for parked robots only.
    A robot mid-mission is the D-17 mission guard's business: it can
    neither be "moved first" nor be assumed to still be where this
    snapshot put it (caught live in the D-24 walk: the snapshot grabbed
    a robot TRANSITING the draft zone and planned an evacuation it
    could never complete)."""
    busy = {
        str(row["assigned_to"])
        for row in await DbTaskState.filter(status__in=_NON_TERMINAL_MATCH).values(
            "assigned_to"
        )
        if row["assigned_to"]
    }
    out: List[Dict[str, Any]] = []
    for row in await DbFleetState.all():
        state = row.data if isinstance(row.data, dict) else {}
        for name, robot in (state.get("robots") or {}).items():
            location = (robot or {}).get("location") or {}
            x = location.get("x")
            y = location.get("y")
            if x is None or y is None:
                continue
            out.append(
                {
                    "name": str(name),
                    "x": float(x),
                    "y": float(y),
                    "parked": str(name) not in busy,
                }
            )
    return out


async def _with_positions(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {**candidate, "robot_positions": await robot_positions()}


class RestartBody(BaseModel):
    acknowledge_active_missions: bool = False


# ----------------------------------------------------------------------
# FR-32 / D-22 — a destination name a saved mission still dispatches to
# cannot be retired. The site-config sidecar owns the site graph and
# reports which names a candidate would RETIRE (removed or renamed away);
# the task store lives here, so the cross-check does too (D-19). We
# refuse rather than migrate: rewriting a schedule the operator authored,
# from a different service, to a name they have not seen yet, is a worse
# failure at 03:00 than a refusal at 14:00 that names what to fix.
# ----------------------------------------------------------------------
def _places_of(description: Any) -> List[str]:
    """Waypoint names a stored task request visits (patrol/go-to shape,
    D-15/OD-3: every dashboard mission is a `patrol` with `places`)."""
    if not isinstance(description, dict):
        return []
    out: List[str] = []
    for place in description.get("places") or []:
        if isinstance(place, str):
            out.append(place)
        elif isinstance(place, dict):
            name = place.get("waypoint") or place.get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def _schedule_label(request: Any, created_by: Any = None) -> str:
    """How the admin will recognise this schedule in Missions →
    Schedules, which lists them by route and creator (they have no
    name). Both parts matter: a single-stop schedule's route IS the
    destination name, so "schedule “dock_4”" alone reads as a typo and
    points at nothing findable."""
    places = _places_of(
        (request or {}).get("description") if isinstance(request, dict) else None
    )
    route = " → ".join(places) if places else "scheduled mission"
    who = str(created_by).strip() if created_by else ""
    return f"{route} (created by {who})" if who else route


async def destination_references(
    names: Iterable[str],
) -> Dict[str, List[Dict[str, str]]]:
    """{destination name: [{kind, label}]} for saved templates (FR-4) and
    schedules that still dispatch to it."""
    wanted = {str(name) for name in names}
    found: Dict[str, List[Dict[str, str]]] = {name: [] for name in wanted}
    if not wanted:
        return found
    for row in await DbTaskFavorite.all():
        for place in _places_of(row.description):
            if place in wanted:
                entry = {"kind": "template", "label": str(row.name)}
                if entry not in found[place]:
                    found[place].append(entry)
    for row in await DbScheduledTask.all():
        label = _schedule_label(row.task_request, row.created_by)
        request = row.task_request if isinstance(row.task_request, dict) else {}
        for place in _places_of(request.get("description")):
            if place in wanted:
                entry = {"kind": "schedule", "label": label}
                if entry not in found[place]:
                    found[place].append(entry)
    return found


def _in_use_violations(
    references: Dict[str, List[Dict[str, str]]],
    kind: str = "destination",
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for name in sorted(references):
        users = references[name]
        if not users:
            continue
        listed = ", ".join(f'{u["kind"]} “{u["label"]}”' for u in users[:6])
        if len(users) > 6:
            listed += f", +{len(users) - 6} more"
        if kind == "waypoint":
            # F-186/I-7: the admin did not remove this one — the
            # derivation retires it — so "renaming or removing it" would
            # be describing an action nobody took.
            message = (
                f"[{name}] is still used by {listed}, and this change "
                "RETIRES it: the corridor it sits inside becomes a "
                "directional pair and its junction stops being a "
                "dispatch target. Those missions would have no "
                "destination — update or delete them first."
            )
        else:
            message = (
                f"[{name}] is still used by {listed}. Renaming or "
                "removing it would leave those missions pointing at a "
                "place that no longer exists — update or delete them "
                "first."
            )
        out.append(
            {
                "code": f"{kind}_in_use",
                "message": message,
                "waypoint": name,
            }
        )
    return out


async def _head_destination_names() -> List[str]:
    site = await _proxy("GET", "/site_config")
    return [
        str(d.get("name"))
        for d in (site or {}).get("destinations") or []
        if d.get("name")
    ]


async def _guard_retired_destinations(retired: Sequence[str]) -> None:
    """Backstop for the apply path — validate already blocks the same
    case with a violation, but apply must never depend on the client
    having asked."""
    if not retired:
        return
    references = await destination_references(retired)
    violations = _in_use_violations(references)
    if violations:
        raise HTTPException(
            409,
            {
                "reason": "destination_in_use",
                "message": violations[0]["message"],
                "violations": violations,
            },
        )


async def _guard_retired_waypoints(candidate: Dict[str, Any]) -> None:
    """F-186/I-7 backstop on the apply path: refuse while a template or
    schedule still dispatches to a waypoint this derivation retires.

    The retired set is not in the candidate — the admin did not choose
    it, the derivation did — so it is read back from a validate pass."""
    try:
        report = await _proxy(
            "POST",
            "/site_config/validate",
            json=await _with_positions(candidate),
            timeout=120.0,
        )
    except HTTPException:
        return  # validate's own failure is reported by the apply
    if not isinstance(report, dict):
        return
    retired = [
        str(entry.get("waypoint"))
        for entry in (report.get("retired_waypoints") or [])
        if isinstance(entry, dict) and entry.get("waypoint")
    ]
    if not retired:
        return
    violations = _in_use_violations(
        await destination_references(retired), kind="waypoint"
    )
    if violations:
        raise HTTPException(
            409,
            {
                "reason": "waypoint_in_use",
                "message": violations[0]["message"],
                "violations": violations,
            },
        )


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
        raise HTTPException(503, f"site-config service unreachable: {e}") from e
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


@router.post("/preview")
async def preview(candidate: Dict[str, Any]) -> Any:
    """FR-32: geometry-only preview — where a destination pin lands and
    what road would be generated to reach it. No regen, so the editor can
    call it while the admin is still placing."""
    return await _proxy("POST", "/site_config/preview", json=candidate, timeout=30.0)


@router.post("/validate")
async def validate(candidate: Dict[str, Any]) -> Any:
    # validate runs a scratch nav-graph regen when the graph changes
    report = await _proxy(
        "POST",
        "/site_config/validate",
        json=await _with_positions(candidate),
        timeout=120.0,
    )
    # FR-32/D-22: the sidecar knows which destination names would be
    # retired; only this service knows what still dispatches to them.
    #
    # F-186/I-7, same rule for RETIRED WAYPOINTS. A railed corridor
    # retires its interior junctions, and a template or schedule that
    # still dispatches to one fails at dispatch after the apply — the
    # identical failure D-22 blocks for destinations, so it gets the
    # identical answer. It is a BLOCK rather than a migrate because
    # there is no defined successor: a destination pin re-anchors onto
    # its near rail (FR-32), but a retired junction is replaced by a
    # four-node corner set or a two-node T-pair and which one a mission
    # "meant" is not recoverable — auto-picking would send a robot
    # somewhere plausible and unintended. It is a block rather than an
    # advisory because the review's F-169 warning does not stop a
    # schedule firing at 03:00 into a waypoint that stopped existing.
    if isinstance(report, dict):
        violations = _in_use_violations(
            await destination_references(report.get("retired_destinations") or [])
        )
        violations += _in_use_violations(
            await destination_references(
                [
                    str(entry.get("waypoint"))
                    for entry in (report.get("retired_waypoints") or [])
                    if isinstance(entry, dict) and entry.get("waypoint")
                ]
            ),
            kind="waypoint",
        )
        if violations:
            report["violations"] = list(report.get("violations") or [])
            report["violations"].extend(violations)
            report["ok"] = False
    return report


@router.post("/apply")
async def apply(body: ApplyBody, user: User = Depends(user_dep)) -> Any:
    await _guard_missions(body.acknowledge_active_missions)
    # A candidate without a `destinations` key does not manage them at
    # all (the sidecar keeps HEAD's), so nothing can be retired and there
    # is nothing to fetch.
    submitted = body.candidate.get("destinations")
    if submitted is not None:
        candidate_names = {
            str(d.get("name"))
            for d in submitted
            if isinstance(d, dict) and d.get("name")
        }
        await _guard_retired_destinations(
            [
                name
                for name in await _head_destination_names()
                if name not in candidate_names
            ]
        )
    # F-186/I-7: the same backstop for waypoints the DERIVATION retires.
    # `validate` already blocks this, but apply must never depend on the
    # client having asked — and unlike destinations, retirement is not
    # something the admin typed, so there is no candidate field to read
    # it from. Ask the sidecar what this candidate would retire.
    await _guard_retired_waypoints(body.candidate)
    return await _proxy(
        "POST",
        "/site_config/apply",
        json={
            "candidate": await _with_positions(body.candidate),
            # server-side identity — the body cannot impersonate (NFR-4)
            "applied_by": user.username,
            "acknowledge_fleet_pause": body.acknowledge_fleet_pause,
            "acknowledge_reachability": body.acknowledge_reachability,
            # F-86/D-23: the sidecar re-verifies at the restart boundary;
            # it must know whether interruption was already accepted.
            "acknowledge_active_missions": body.acknowledge_active_missions,
        },
        timeout=60.0,
    )


@router.post("/restart")
async def restart(body: RestartBody, user: User = Depends(user_dep)) -> Any:
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
                json={
                    "requested_by": user.username,
                    # F-86/D-23: boundary re-check needs the ack too
                    "acknowledge_active_missions": body.acknowledge_active_missions,
                },
                timeout=60.0,
            )
    except httpx.HTTPError as e:
        raise HTTPException(503, f"site-config service unreachable: {e}") from e
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()


@router.get("/apply_status")
async def apply_status() -> Any:
    return await _proxy("GET", "/site_config/apply_status")
