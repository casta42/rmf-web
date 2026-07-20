import asyncio
from typing import List, Optional

import rclpy.client
from fastapi import APIRouter, Depends, HTTPException
from rmf_task_msgs.srv import GetDispatchStates

from api_server import clock
from api_server.authenticator import user_dep
from api_server.models import Permission, User
from api_server.models.tortoise_models import ResourcePermission
from api_server.ros import ros_node

router = APIRouter()

# F-20 probe client (lazy: the ros node does not exist at import time). The
# dispatcher's get_dispatches service is read-only, so the probe is free of
# side effects. NOTE: the ApiRequest bus cannot be probed instead - on the
# Humble pin the dispatcher's validation-error response path throws before
# publishing (json object assigned to the string json_msg field without
# .dump()), so an invalid dispatch gets no reply, and a valid one creates a
# real task.
_dispatch_states_client: Optional[rclpy.client.Client] = None


def _get_dispatch_states_client() -> rclpy.client.Client:
    global _dispatch_states_client
    if _dispatch_states_client is None:
        _dispatch_states_client = ros_node().create_client(
            GetDispatchStates, "get_dispatches"
        )
    return _dispatch_states_client


@router.get("/user", response_model=User)
async def get_user(user: User = Depends(user_dep)):
    """
    Get the currently logged in user
    """
    return user


@router.get("/permissions", response_model=List[Permission])
async def get_effective_permissions(user: User = Depends(user_dep)):
    """
    Get the effective permissions of the current user
    """
    perms = (
        await ResourcePermission.filter(role__name__in=user.roles)
        .distinct()
        .values("authz_grp", "action")
    )
    return [
        Permission.model_construct(authz_grp=p["authz_grp"], action=p["action"])
        for p in perms
    ]


@router.get("/time", response_model=int)
async def get_time():
    """
    Get the current rmf time in unix milliseconds
    """
    return clock.now()


@router.get("/health/rmf")
async def get_rmf_health():
    """
    End-to-end RMF bus probe (F-20, FR-26): a read-only get_dispatches
    service call to the rmf-core dispatcher through this process's own DDS
    participant. A plain HTTP check stays green when DDS silently dies
    (soak: HTTP fine, bus dead for 22 h); this returns 503 instead, which
    the container healthcheck treats as unhealthy.
    """
    client = _get_dispatch_states_client()
    if not client.service_is_ready():
        raise HTTPException(503, "rmf dispatcher service not discovered")

    loop = asyncio.get_running_loop()
    aio_fut: asyncio.Future = loop.create_future()

    def _resolve(ros_fut):
        # runs on the asyncio loop via call_soon_threadsafe; the aio future
        # may already be done if the probe timed out meanwhile
        if aio_fut.done():
            return
        exc = ros_fut.exception()
        if exc is not None:
            aio_fut.set_exception(exc)
        else:
            aio_fut.set_result(ros_fut.result())

    # the ros future completes on the rclpy spin thread (ros.py); bridge it
    # onto the asyncio loop instead of touching the aio future cross-thread
    client.call_async(GetDispatchStates.Request()).add_done_callback(
        lambda ros_fut: loop.call_soon_threadsafe(_resolve, ros_fut)
    )
    try:
        result = await asyncio.wait_for(aio_fut, timeout=3)
    except asyncio.TimeoutError as e:
        raise HTTPException(503, "rmf dispatcher did not respond") from e
    return {"rmf_api_bus": "ok", "dispatcher_success_field": bool(result.success)}
