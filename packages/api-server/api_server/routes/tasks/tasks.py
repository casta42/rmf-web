import json
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, cast

from fastapi import Body, Depends, HTTPException, Path, Query
from reactivex import operators as rxops

from api_server import models as mdl
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
from api_server.cancel_route import (
    ROUTE_ALREADY_CANCELED,
    ROUTE_DISPATCHER,
    ROUTE_TERMINAL,
    ROUTE_UNKNOWN,
    cancel_route,
    status_tail,
)
from api_server.rmf_io import cancellation as task_cancellation
from api_server.rmf_io import task_events, tasks_service
from api_server.routes import zones as zones_routes
from api_server.routes.tasks import dispatch_guard

router = FastIORouter(tags=["Tasks"])


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
    from rmf_task_msgs.srv import CancelTask as RmfCancelTask

    from api_server.gateway import rmf_gateway

    gateway = rmf_gateway()
    try:
        resp = await gateway.call_service(
            gateway.cancel_task_client,
            RmfCancelTask.Request(task_id=task_id), timeout=3)
    except HTTPException:
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


@router.post(
    "/dispatch_task",
    response_model=mdl.TaskDispatchResponse,
    responses={400: {"model": mdl.TaskDispatchResponse}},
)
async def post_dispatch_task(
    request: mdl.DispatchTaskRequest = Body(...),
    task_repo: TaskRepository = Depends(task_repo_dep),
):
    await guard_patrol_destination(request.request, task_repo)
    resp = mdl.TaskDispatchResponse.model_validate_json(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )
    if not resp.root.success:
        return RawJSONResponse(resp.model_dump_json(), 400)
    task_state = cast(mdl.TaskDispatchResponse1, resp.root).state
    await task_repo.save_task_state(task_state)
    await task_repo.save_task_request(task_state.booking.id, request.request)
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
    # Same F-34 guard as dispatch_task, minus the target robot itself —
    # a robot already parked at its destination (send-to-charger from the
    # charger, F-62) is not in its own way.
    await guard_patrol_destination(
        request.request, task_repo, exclude=f"{request.fleet}/{request.robot}"
    )
    resp = mdl.RobotTaskResponse.model_validate_json(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )
    if not resp.root.root.success:
        return RawJSONResponse(resp.model_dump_json(), 400)
    await task_repo.save_task_state(
        cast(mdl.TaskDispatchResponse1, resp.root.root).state
    )
    return resp


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
