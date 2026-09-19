import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple, cast

from fastapi import Body, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from reactivex import operators as rxops

from api_server import dispatch_horizon
from api_server import cordon
from api_server import models as mdl
from api_server.app_config import app_config
from api_server.cancel_route import (
    ROUTE_ALREADY_CANCELED,
    ROUTE_DISPATCHER,
    ROUTE_TERMINAL,
    ROUTE_UNKNOWN,
    cancel_route,
    status_tail,
)
from api_server.dependencies import (
    between_query,
    finish_time_between_query,
    pagination_query,
    sio_user,
    start_time_between_query,
)
from api_server.fast_io import FastIORouter, SubscriptionRequest
from api_server.models import tortoise_models as ttm
from api_server.models.building_map import BuildingMap
from api_server.models.rmf_api.task_state import Cancellation
from api_server.models.rmf_api.task_state import Status2 as DispatchStatus
from api_server.repositories import FleetRepository, TaskRepository, task_repo_dep
from api_server.response import RawJSONResponse
from api_server.rmf_io import cancellation as task_cancellation
from api_server.rmf_io import task_events, tasks_service
from api_server.routes import zones as zones_routes
from api_server.routes.tasks import dispatch_guard

router = FastIORouter(tags=["Tasks"])
logger = logging.getLogger("api_server.tasks")


@router.get("/{task_id}/request", response_model=mdl.TaskRequest)
async def get_task_request(
    task_repo: TaskRepository = Depends(task_repo_dep),
    task_id: str = Path(..., description="task_id"),
):
    result = await task_repo.get_task_request(task_id)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.get("", response_model=List[mdl.TaskState])
async def query_task_states(
    task_repo: TaskRepository = Depends(task_repo_dep),
    task_id: Optional[str] = Query(
        None, description="comma separated list of task ids"
    ),
    category: Optional[str] = Query(
        None, description="comma separated list of task categories"
    ),
    assigned_to: Optional[str] = Query(
        None, description="comma separated list of assigned robot names"
    ),
    start_time_between: Optional[Tuple[datetime, datetime]] = Depends(
        start_time_between_query
    ),
    finish_time_between: Optional[Tuple[datetime, datetime]] = Depends(
        finish_time_between_query
    ),
    status: Optional[str] = Query(None, description="comma separated list of statuses"),
    label: str | None = Query(
        None,
        description="comma separated list of labels, each item must be in the form <key>=<value>, multiple items will filter tasks with all the labels",
    ),
    recorded_between: Optional[str] = Query(
        None,
        description=(
            "Fork (F-37/FR-18): 'X,Y' unix millis filtering on the wall-clock "
            "time this database RECORDED the task — survives sim restarts, "
            "unlike the sim-clocked start/finish filters"
        ),
    ),
    pagination: mdl.Pagination = Depends(pagination_query),
):
    recorded_range = None
    if recorded_between is not None:
        try:
            lo, hi = (int(p) for p in recorded_between.split(","))
            recorded_range = (
                datetime.fromtimestamp(lo / 1e3),
                datetime.fromtimestamp(hi / 1e3),
            )
        except ValueError as e:
            raise HTTPException(
                422, "recorded_between must be 'X,Y' in unix millis"
            ) from e
    return await task_repo.query_task_states(
        task_id=task_id.split(",") if task_id else None,
        category=category.split(",") if category else None,
        assigned_to=assigned_to.split(",") if assigned_to else None,
        start_time_between=start_time_between,
        finish_time_between=finish_time_between,
        recorded_between=recorded_range,
        status=status.split(",") if status else None,
        label=mdl.Labels.from_strings(label.split(",")) if label else None,
        pagination=pagination,
    )


@router.get("/kpis")
async def get_task_kpis(
    task_repo: TaskRepository = Depends(task_repo_dep),
    days: int = Query(7, ge=1, le=90, description="window size in days"),
):
    """FR-18 KPI aggregates (P11; distance deferred per OD-5/F-52).

    Computed over the F-37 `created_at` provenance column so the window
    survives sim restarts. Utilization uses task DURATIONS
    (finish - start): both stamps ride the same clock, so the delta is
    valid even where the absolute times are not. Cancellation provenance
    (F-71) counts a completed-but-canceled task as canceled.
    """
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    rows = await ttm.TaskState.filter(created_at__gte=cutoff).values_list(
        "data", "created_at"
    )
    per_day: dict = {}
    outcomes = {"completed": 0, "canceled": 0, "failed": 0}
    active_millis = 0
    for data, created_at in rows:
        day = created_at.date().isoformat()
        per_day[day] = per_day.get(day, 0) + 1
        task_status = str(data.get("status"))
        if task_status == "completed" and data.get("cancellation") is not None:
            task_status = "canceled"  # F-71 provenance
        if task_status in outcomes:
            outcomes[task_status] += 1
        start = data.get("unix_millis_start_time")
        finish = data.get("unix_millis_finish_time")
        if start is not None and finish is not None:
            duration = finish - start
            if 0 < duration < 24 * 3600 * 1000:
                active_millis += duration
    fleets = await FleetRepository(task_repo.user).get_all_fleets()
    robot_count = sum(len(f.robots or {}) for f in fleets)
    window_millis = days * 24 * 3600 * 1000
    terminal = sum(outcomes.values())
    return {
        "window_days": days,
        "tasks_per_day": sorted(
            ({"date": d, "count": c} for d, c in per_day.items()),
            key=lambda x: x["date"],
        ),
        "total_tasks": len(rows),
        "outcomes": outcomes,
        "completion_rate": (outcomes["completed"] / terminal) if terminal else None,
        "utilization": (
            active_millis / (robot_count * window_millis) if robot_count else None
        ),
        "robot_count": robot_count,
        "method": {
            "window": "created_at wall clock (F-37)",
            "utilization": "sum(task durations) / (robots x window)",
            "cancellations": "provenance-corrected (F-71)",
        },
    }


@router.get("/{task_id}/state", response_model=mdl.TaskState)
async def get_task_state(
    task_repo: TaskRepository = Depends(task_repo_dep),
    task_id: str = Path(..., description="task_id"),
):
    """
    Available in socket.io
    """
    result = await task_repo.get_task_state(task_id)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.sub("/{task_id}/state", response_model=mdl.TaskState)
async def sub_task_state(req: SubscriptionRequest, task_id: str):
    user = sio_user(req)
    task_repo = TaskRepository(user)
    obs = task_events.task_states.pipe(rxops.filter(lambda x: x.booking.id == task_id))
    current_state = await get_task_state(task_repo, task_id)
    if current_state:
        return obs.pipe(rxops.start_with(current_state))
    return obs


@router.get("/{task_id}/log", response_model=mdl.TaskEventLog)
async def get_task_log(
    task_repo: TaskRepository = Depends(task_repo_dep),
    task_id: str = Path(..., description="task_id"),
    between: Tuple[int, int] = Depends(between_query),
):
    """
    Available in socket.io
    """

    result = await task_repo.get_task_log(task_id, between)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.sub("/{task_id}/log", response_model=mdl.TaskEventLog)
async def sub_task_log(_req: SubscriptionRequest, task_id: str):
    return task_events.task_event_logs.pipe(
        rxops.filter(lambda x: x.task_id == task_id)
    )


@router.post("/activity_discovery", response_model=mdl.ActivityDiscovery)
async def post_activity_discovery(
    request: mdl.ActivityDiscoveryRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/cancel_task", response_model=mdl.TaskCancelResponse)
async def post_cancel_task(
    request: mdl.CancelTaskRequest = Body(...),
    task_repo: TaskRepository = Depends(task_repo_dep),
):
    # F-293: a mission the api-server is still holding for its start was
    # never sent to the fleet — its cancel is ours alone.
    if request.task_id.startswith(DEFERRED_PREFIX):
        return await _cancel_deferred(request.task_id,
                                      list(request.labels or []))
    # F-285: an honest answer needs the task's shape first. A task the
    # dispatcher still holds (scheduled for later, or not yet bid on) is
    # answered by NOBODY on the task API topic on this pin — the fleet
    # adapter only answers for tasks it owns — so that cancel used to
    # time out into a 500 whether or not it took effect. Route by state:
    # unknown -> 404; already canceled -> 200 (idempotent); completed or
    # failed -> 409; dispatcher-held -> the dispatcher's ROS service,
    # falling back to the fleet path if it was awarded meanwhile.
    stored = await task_repo.get_task_state(request.task_id)
    route = cancel_route(
        stored.status if stored is not None else None,
        stored.dispatch.status
        if stored is not None and stored.dispatch is not None else None,
        stored.assigned_to if stored is not None else None,
    )
    if route == ROUTE_UNKNOWN:
        raise HTTPException(
            404, detail=f"task [{request.task_id}] is not known to this site")
    if route == ROUTE_ALREADY_CANCELED:
        return RawJSONResponse(
            json.dumps({"success": True,
                        "detail": f"task is already {status_tail(stored.status)}"}
                       ).encode())
    if route == ROUTE_TERMINAL:
        raise HTTPException(
            409,
            detail=(f"task [{request.task_id}] is already "
                    f"{status_tail(stored.status)} and cannot be canceled"))
    cancellation = Cancellation(
        unix_millis_request_time=round(datetime.now().timestamp() * 1e3),
        labels=list(request.labels or []),
    )
    # F-71(2): record the cancellation at REQUEST time — whether the
    # fleet core ends the task `canceled` or (dead-robot race) wipes it
    # to `completed`, displays keep the truth of how it ended (F-67)
    task_cancellation.latch(request.task_id, cancellation)
    if route == ROUTE_DISPATCHER:
        closed = await _cancel_at_dispatcher(request.task_id, cancellation,
                                             stored, task_repo)
        if closed is not None:
            return closed
    try:
        return RawJSONResponse(
            await tasks_service().call(
                request.model_dump_json(exclude_none=True))
        )
    except HTTPException as e:
        # F-141 (E6 run-2 blocker 3): a cancel the core never answers is
        # the signature of a task the restarted core does not know — the
        # row would stay an un-cancelable 'Executing' phantom forever.
        # If the row is non-terminal and has been silent long enough
        # that a live task would have re-announced, close it locally
        # with honest provenance instead of bouncing the operator.
        if e.status_code != 500:
            raise
        closed = await task_repo.close_silent_task_locally(
            request.task_id, labels=list(request.labels or []))
        if closed is None:
            raise
        task_events.task_states.on_next(closed)
        return RawJSONResponse(
            json.dumps(
                {
                    "success": True,
                    "detail": (
                        "The fleet core no longer tracks this mission "
                        "(it was interrupted by a coordination "
                        "restart); it has been closed as canceled."
                    ),
                }
            ).encode()
        )


async def _cancel_at_dispatcher(task_id: str, cancellation: Cancellation,
                                stored: mdl.TaskState,
                                task_repo: TaskRepository):
    """F-285: cancel a task the DISPATCHER still holds through its ROS
    service (rmf_task_msgs/srv/CancelTask). On success the dispatcher
    moves the task to canceled_in_flight and announces it; we close the
    row here as well so the answer and the ledger agree at once. Returns
    the response, or None when the dispatcher no longer holds the task
    (awarded meanwhile, or unknown) so the caller falls through to the
    fleet path."""
    import asyncio

    from rmf_task_msgs.srv import CancelTask as RmfCancelTask

    from api_server.gateway import rmf_gateway

    client = rmf_gateway().cancel_task_client
    if not client.service_is_ready():
        return None
    # An rclpy Future is not awaitable by asyncio (the gateway's own
    # call_service raises "Task got bad yield" on it — F-285 found that
    # too); bridge it: the rclpy spin thread completes the ROS future,
    # the loop's future is resolved thread-safely.
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    ros_future = client.call_async(
        RmfCancelTask.Request(requester="gentlefleet-api-server",
                              task_id=task_id))
    ros_future.add_done_callback(
        lambda f: loop.call_soon_threadsafe(
            lambda: done.done() or done.set_result(f)))
    try:
        finished = await asyncio.wait_for(done, timeout=3)
    except asyncio.TimeoutError:
        return None
    try:
        resp = finished.result()
    except Exception:  # pylint: disable=broad-except
        return None
    if not getattr(resp, "success", False):
        return None
    stored.status = mdl.TaskStatus.canceled
    stored.cancellation = cancellation
    if stored.dispatch is not None:
        stored.dispatch.status = DispatchStatus.canceled_in_flight
    await task_repo.save_task_state(stored)
    task_events.task_states.on_next(stored)
    return RawJSONResponse(
        json.dumps({
            "success": True,
            "detail": "canceled before dispatch — the task had not been "
                      "assigned to any robot yet (F-285)",
        }).encode())


# F-34 dispatch guard: reject a patrol whose final destination is
# already occupied by a parked robot (collision course on hardware
# with finishing_request "nothing"; fails open on missing data).
async def guard_patrol_destination(
    request: mdl.TaskRequest,
    task_repo: TaskRepository,
    exclude: Optional[str] = None,
):
    place = dispatch_guard.patrol_final_place(request)
    if place is None:
        return
    ttm_map = await ttm.BuildingMap.first()
    if ttm_map is None:
        return
    building_map = BuildingMap.from_tortoise(ttm_map)
    # F-111: refuse before the planner sees it — a place a zone has
    # isolated crashes the fleet adapter and takes the whole fleet's
    # bidding down with it. Checked against the DERIVED graph: the
    # building map still carries the lanes the zone closed.
    nav_graph = zones_routes.derived_nav_graph()
    stranded = (
        dispatch_guard.isolated_place(
            nav_graph, dispatch_guard.patrol_places(request)
        )
        if nav_graph
        else None
    )
    if stranded is not None:
        raise HTTPException(
            409,
            detail=(
                f"[{stranded}] has no lanes to it right now — a no-go "
                "zone has closed every route to that waypoint (F-111). "
                "Remove or redraw the zone, or send the mission "
                "somewhere else."
            ),
        )
    vertex = dispatch_guard.find_vertex(building_map, place)
    if vertex is None:
        return
    fleets = await FleetRepository(task_repo.user).get_all_fleets()
    occupier = dispatch_guard.parked_robot_near(fleets, *vertex, exclude=exclude)
    if occupier is not None:
        raise HTTPException(
            409,
            detail=(
                f"destination [{place}] is occupied by parked "
                f"robot [{occupier}] (F-34); dispatch rejected"
            ),
        )


# ----------------------------------------------------------------------
# F-293 (FR-4 amendment): the dispatch horizon. A one-off mission that
# starts beyond the derived horizon is held here and released at
# start - horizon; one more than a shift ahead is refused. See
# api_server/dispatch_horizon.py.
# ----------------------------------------------------------------------

DEFERRED_PREFIX = "deferred-"
DEFERRED_LABEL = "gf:deferred-of"


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def _deferral_view(row: ttm.DeferredDispatch) -> dict:
    body = row.body if isinstance(row.body, dict) else json.loads(row.body)
    request = body.get("request") or {}
    return {
        "id": row.public_id(),
        "type": row.request_type,
        "robot": body.get("robot"),
        "fleet": body.get("fleet"),
        "category": request.get("category"),
        "labels": request.get("labels") or [],
        "earliest_start_ms": _ms(row.earliest_start),
        "dispatch_at_ms": _ms(row.dispatch_at),
        "status": row.status,
        "task_id": row.task_id,
        "detail": row.detail,
        "created_by": row.created_by,
    }


async def _horizon_gate(request_type: str, request, user) -> Optional[JSONResponse]:
    """None to dispatch now; a 202 JSONResponse when the mission was
    deferred; raises 422 when it starts more than a shift ahead."""
    start_ms = request.request.unix_millis_earliest_start_time
    now_ms = round(time.time() * 1000)
    horizon, how = dispatch_horizon.horizon_s(zones_routes.derived_nav_graph())
    max_lead = float(app_config.dispatch_max_lead_s)
    verdict = dispatch_horizon.classify(start_ms, now_ms, horizon, max_lead)
    if verdict == dispatch_horizon.NOW:
        return None
    lead = (int(start_ms) - now_ms) / 1000.0
    if verdict == dispatch_horizon.REFUSE:
        raise HTTPException(
            422,
            detail=(
                f"this mission starts in {dispatch_horizon.human(lead)}, more "
                f"than one shift ({dispatch_horizon.human(max_lead)}) ahead — "
                "one-off missions are not held that far out (F-293); create "
                "a schedule for it instead"
            ),
        )
    dispatch_at = datetime.fromtimestamp(int(start_ms) / 1000.0 - horizon,
                                         tz=timezone.utc)
    row = await ttm.DeferredDispatch.create(
        request_type=request_type,
        body=json.loads(request.model_dump_json(exclude_none=True)),
        earliest_start=datetime.fromtimestamp(int(start_ms) / 1000.0,
                                              tz=timezone.utc),
        dispatch_at=dispatch_at,
        created_by=user.username,
    )
    detail = (
        f"this mission starts in {dispatch_horizon.human(lead)}, beyond the "
        f"{dispatch_horizon.human(horizon)} dispatch horizon ({how}); it is "
        f"held by GentleFleet and sent to the fleet in "
        f"{dispatch_horizon.human(lead - horizon)}, so no robot waits for "
        f"it (F-293). Cancel it with task id {row.public_id()}."
    )
    logger.info("F-293: deferred %s (%s) — %s", row.public_id(),
                request_type, detail)
    return JSONResponse(
        status_code=202,
        content={"success": True, "deferred": _deferral_view(row),
                 "horizon_s": horizon, "detail": detail},
    )


async def guard_cordon(request: mdl.TaskRequest, fleet: Optional[str] = None,
                       robot: Optional[str] = None):
    """F-332: refuse a mission into an operator's cordon BEFORE queueing it.

    RMF does not refuse it. For a direct task request it estimates the
    finish state, fails, logs "Unable to estimate final state for direct
    task request ... still added", and queues it anyway — the operator
    sees a mission accepted and a robot that never arrives. A failed
    estimate through a cordon is a refusal, with the reason named.

    `fleet` narrows the check to one fleet (a direct robot task); without
    it every fleet's cordon is consulted, and a place a fleet's graph does
    not contain is that fleet's business, not ours. Fail-open like the
    rest of this module.
    """
    places = dispatch_guard.patrol_places(request)
    if not places:
        return
    fleets = [fleet] if fleet else list(cordon.known_fleets())
    for name in fleets:
        why = cordon.cordon_refusal(name, places)
        if why is not None:
            logger.info("F-332: refusing dispatch into a cordon — %s", why)
            raise HTTPException(409, detail=why)
    # F-338 (G ruling 2026-09-19): a destination whose lanes are open but
    # whose ROUTE is cut by the cordon is refused too. On this pin a task
    # whose next stop cannot be routed aborts the whole fleet adapter when
    # it starts (rmf_task_sequence GoToPlace::generate_header throws), and
    # a direct request is never bid, so nothing upstream refuses it first.
    # The check starts from where the robot (or, for a dispatch, ANY robot)
    # is standing; a robot not on a vertex, or a site with lifts, cannot be
    # judged and is not.
    if not cordon.known_fleets():
        return
    try:
        # pylint: disable=import-outside-toplevel
        from api_server.routes.site_config import robot_positions
        positions = await robot_positions()
    except Exception:  # noqa: BLE001 — cannot see, so cannot refuse
        return
    if robot is not None:
        positions = [p for p in positions if str(p.get("name")) == robot]
    xy = [(float(p["x"]), float(p["y"])) for p in positions]
    has_lifts = False
    try:
        row = await ttm.BuildingMap.first()
        if row is not None and isinstance(row.data, dict):
            has_lifts = bool(row.data.get("lifts"))
    except Exception:  # noqa: BLE001
        has_lifts = False
    for name in fleets:
        why = cordon.reachability_refusal(name, places, xy, has_lifts=has_lifts)
        if why is not None:
            logger.info("F-338: refusing a mission the cordon cut off — %s", why)
            raise HTTPException(409, detail=why)


async def _dispatch_task_now(request: mdl.DispatchTaskRequest,
                             task_repo: TaskRepository) -> mdl.TaskDispatchResponse:
    await guard_patrol_destination(request.request, task_repo)
    await guard_cordon(request.request)
    resp = mdl.TaskDispatchResponse.model_validate_json(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )
    if resp.root.success:
        task_state = cast(mdl.TaskDispatchResponse1, resp.root).state
        await task_repo.save_task_state(task_state)
        await task_repo.save_task_request(task_state.booking.id, request.request)
    return resp


async def _robot_task_now(request: mdl.RobotTaskRequest,
                          task_repo: TaskRepository) -> mdl.RobotTaskResponse:
    # Same F-34 guard as dispatch_task, minus the target robot itself —
    # a robot already parked at its destination (send-to-charger from the
    # charger, F-62) is not in its own way.
    await guard_patrol_destination(
        request.request, task_repo, exclude=f"{request.fleet}/{request.robot}"
    )
    await guard_cordon(request.request, fleet=request.fleet, robot=request.robot)
    resp = mdl.RobotTaskResponse.model_validate_json(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )
    if resp.root.root.success:
        await task_repo.save_task_state(
            cast(mdl.TaskDispatchResponse1, resp.root.root).state
        )
    return resp


@router.post(
    "/dispatch_task",
    response_model=mdl.TaskDispatchResponse,
    responses={400: {"model": mdl.TaskDispatchResponse}},
)
async def post_dispatch_task(
    request: mdl.DispatchTaskRequest = Body(...),
    task_repo: TaskRepository = Depends(task_repo_dep),
):
    deferred = await _horizon_gate("dispatch_task_request", request,
                                   task_repo.user)
    if deferred is not None:
        return deferred
    resp = await _dispatch_task_now(request, task_repo)
    if not resp.root.success:
        return RawJSONResponse(resp.model_dump_json(), 400)
    return resp


@router.post(
    "/robot_task",
    response_model=mdl.RobotTaskResponse,
    responses={400: {"model": mdl.RobotTaskResponse}},
)
async def post_robot_task(
    request: mdl.RobotTaskRequest = Body(...),
    task_repo: TaskRepository = Depends(task_repo_dep),
):
    deferred = await _horizon_gate("robot_task_request", request,
                                   task_repo.user)
    if deferred is not None:
        return deferred
    resp = await _robot_task_now(request, task_repo)
    if not resp.root.root.success:
        return RawJSONResponse(resp.model_dump_json(), 400)
    return resp


@router.get("/deferred")
async def get_deferred_tasks(
    status: Optional[str] = Query(
        None, description="pending | dispatched | canceled | failed; all when omitted"),
):
    """F-293: missions the api-server is holding for their start, and
    what became of the recent ones."""
    query = ttm.DeferredDispatch.all()
    if status:
        query = query.filter(status=status)
    rows = await query.order_by("dispatch_at").limit(500)
    return [_deferral_view(row) for row in rows]


async def _cancel_deferred(public_id: str, labels: List[str]):
    try:
        row_id = int(public_id[len(DEFERRED_PREFIX):])
    except ValueError:
        raise HTTPException(404, detail=f"task [{public_id}] is not known to this site")
    claimed = await ttm.DeferredDispatch.filter(
        id=row_id, status=ttm.deferred_dispatch.PENDING).update(
        status=ttm.deferred_dispatch.CANCELED,
        detail="canceled before dispatch: " + ("; ".join(labels) or "no reason given"))
    row = await ttm.DeferredDispatch.get_or_none(id=row_id)
    if row is None:
        raise HTTPException(404, detail=f"task [{public_id}] is not known to this site")
    if claimed:
        logger.info("F-293: deferred %s canceled before dispatch", public_id)
        return RawJSONResponse(json.dumps({
            "success": True,
            "detail": "canceled before dispatch — the mission was held by "
                      "GentleFleet and was never sent to the fleet (F-293)",
        }).encode())
    if row.status == ttm.deferred_dispatch.CANCELED:
        return RawJSONResponse(json.dumps(
            {"success": True, "detail": "task is already canceled"}).encode())
    if row.status == ttm.deferred_dispatch.DISPATCHED and row.task_id:
        raise HTTPException(
            409, detail=(f"[{public_id}] has already been sent to the fleet as "
                         f"[{row.task_id}] — cancel that task"))
    raise HTTPException(
        409, detail=f"[{public_id}] is {row.status} and cannot be canceled")


async def dispatch_due_deferrals(log: logging.Logger,
                                 now: Optional[datetime] = None) -> int:
    """Send every deferred mission whose release time has come. The gate
    is bypassed (a released mission is by construction inside the
    horizon; re-deriving it against a changed graph must never defer it
    twice). Returns how many were released."""
    from api_server.models import User

    now = now or datetime.now(timezone.utc)
    due = await ttm.DeferredDispatch.filter(
        status=ttm.deferred_dispatch.PENDING, dispatch_at__lte=now
    ).order_by("dispatch_at")
    released = 0
    for row in due:
        claimed = await ttm.DeferredDispatch.filter(
            id=row.id, status=ttm.deferred_dispatch.PENDING
        ).update(status=ttm.deferred_dispatch.DISPATCHING)
        if not claimed:
            continue            # canceled or taken meanwhile
        body = row.body if isinstance(row.body, dict) else json.loads(row.body)
        body = json.loads(json.dumps(body))
        request_body = body.setdefault("request", {})
        labels = list(request_body.get("labels") or [])
        labels.append(f"{DEFERRED_LABEL}={row.public_id()}")
        request_body["labels"] = labels
        user = await User.load_from_db(row.created_by)
        status, task_id, detail = ttm.deferred_dispatch.FAILED, None, None
        if user is None:
            detail = f"user [{row.created_by}] no longer exists"
        else:
            repo = TaskRepository(user)
            try:
                if row.request_type == "robot_task_request":
                    resp = await _robot_task_now(
                        mdl.RobotTaskRequest(**body), repo)
                    root = resp.root.root
                else:
                    resp = await _dispatch_task_now(
                        mdl.DispatchTaskRequest(**body), repo)
                    root = resp.root
                if root.success:
                    status = ttm.deferred_dispatch.DISPATCHED
                    task_id = cast(mdl.TaskDispatchResponse1, root).state.booking.id
                else:
                    detail = "the fleet refused it: " + json.dumps(
                        json.loads(resp.model_dump_json()).get("errors"))
            except HTTPException as exc:
                detail = f"refused at dispatch time: {exc.detail}"
            except Exception as exc:  # pylint: disable=broad-except
                detail = f"dispatch failed: {exc}"
        await ttm.DeferredDispatch.filter(id=row.id).update(
            status=status, task_id=task_id, detail=detail)
        if status == ttm.deferred_dispatch.DISPATCHED:
            released += 1
            log.info("F-293: released %s as [%s]", row.public_id(), task_id)
        else:
            log.warning("F-293: deferred %s was NOT dispatched — %s",
                        row.public_id(), detail)
    return released


async def recover_interrupted_deferrals(log: logging.Logger) -> None:
    """A row caught mid-dispatch by a restart cannot be known to have
    reached the fleet: it is marked failed with that said, never silently
    re-sent (a duplicate mission) or silently dropped."""
    interrupted = await ttm.DeferredDispatch.filter(
        status=ttm.deferred_dispatch.DISPATCHING)
    for row in interrupted:
        await ttm.DeferredDispatch.filter(id=row.id).update(
            status=ttm.deferred_dispatch.FAILED,
            detail="the api-server restarted while dispatching it; check the "
                   f"task ledger for label {DEFERRED_LABEL}={row.public_id()}")
        log.warning("F-293: deferred %s was interrupted mid-dispatch by a "
                    "restart — marked failed", row.public_id())


@router.post("/interrupt_task", response_model=mdl.TaskInterruptionResponse)
async def post_interrupt_task(
    request: mdl.TaskInterruptionRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/kill_task", response_model=mdl.TaskKillResponse)
async def post_kill_task(
    request: mdl.TaskKillRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/resume_task", response_model=mdl.TaskResumeResponse)
async def post_resume_task(
    request: mdl.TaskResumeRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/rewind_task", response_model=mdl.TaskRewindResponse)
async def post_rewind_task(
    request: mdl.TaskRewindRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/skip_phase", response_model=mdl.SkipPhaseResponse)
async def post_skip_phase(
    request: mdl.TaskPhaseSkipRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/task_discovery", response_model=mdl.TaskDiscovery)
async def post_task_discovery(
    request: mdl.TaskDiscoveryRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/undo_skip_phase", response_model=mdl.UndoPhaseSkipResponse)
async def post_undo_skip_phase(
    request: mdl.UndoPhaseSkipRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )
