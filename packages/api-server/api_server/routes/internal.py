# NOTE: This will eventually replace `gateway.py``
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api_server import models as mdl
from api_server.app_config import app_config
from api_server.logger import logger as base_logger
from api_server.models.rmf_api.robot_state import Status as RobotStatus
from api_server.repositories import AlertRepository, FleetRepository, TaskRepository
from api_server.rmf_io import alert_events, fleet_events, rmf_events, task_events

router = APIRouter(tags=["_internal"])
logger = base_logger.getChild("RmfGatewayApp")
user: mdl.User = mdl.User(username="__rmf_internal__", is_admin=True)
task_repo = TaskRepository(user)
alert_repo = AlertRepository(user, task_repo)

# FR-17 low battery alerts: a robot re-arms only after its battery rises above
# `low_battery_threshold` plus this margin (battery is a fraction, 0.0-1.0).
LOW_BATTERY_HYSTERESIS = 0.05
# FR-17 stuck robot alerts: movement below this distance (meters) between fleet
# state updates is considered "not moving".
STUCK_MOVE_EPSILON = 0.05
# FR-17 stuck robot alerts: a stuck episode re-arms only after the robot moves
# more than this distance (meters) away from the stuck position.
STUCK_REARM_DISTANCE = 0.2


@dataclass
class _StuckState:
    """Position anchor of a robot used to detect a stuck episode (FR-17)."""

    x: float
    y: float
    since_millis: int
    alerted: bool = False


# per `{fleet}/{robot}`, whether a low battery alert was already created for the
# current low battery episode (FR-17)
_low_battery_alerted: Dict[str, bool] = {}
# per `{fleet}/{robot}` stuck episode tracking (FR-17)
_stuck_states: Dict[str, _StuckState] = {}


def log_phase_has_error(phase: mdl.Phases) -> bool:
    if phase.log:
        for log in phase.log:
            if log.tier == mdl.Tier.error:
                return True
    if phase.events:
        for _, event_logs in phase.events.items():
            for event_log in event_logs:
                if event_log.tier == mdl.Tier.error:
                    return True
    return False


def task_log_has_error(task_log: mdl.TaskEventLog) -> bool:
    if task_log.log:
        for log in task_log.log:
            if log.tier == mdl.Tier.error:
                return True

    if task_log.phases:
        for _, phase in task_log.phases.items():
            if log_phase_has_error(phase):
                return True
    return False


def check_low_battery(
    robot_id: str, robot: mdl.RobotState, now_millis: int
) -> Optional[str]:
    """
    FR-17: Returns a "low_battery" alert id when the robot's battery drops below
    `low_battery_threshold`, once per low battery episode. `RobotState.battery`
    is a fraction, 0.0 (depleted) to 1.0 (fully charged), and so is the
    threshold.
    """
    if robot.battery is None:
        return None
    threshold = app_config.low_battery_threshold
    if _low_battery_alerted.get(robot_id, False):
        if robot.battery > threshold + LOW_BATTERY_HYSTERESIS:
            _low_battery_alerted[robot_id] = False
        return None
    if robot.battery < threshold:
        _low_battery_alerted[robot_id] = True
        fleet, _, robot_name = robot_id.partition("/")
        return f"low_battery__{fleet}__{robot_name}__{now_millis}"
    return None


def _is_executing_task(robot: mdl.RobotState) -> bool:
    if not robot.task_id:
        return False
    return robot.status not in (
        RobotStatus.uninitialized,
        RobotStatus.offline,
        RobotStatus.shutdown,
        RobotStatus.idle,
        RobotStatus.charging,
    )


def check_robot_stuck(
    robot_id: str, robot: mdl.RobotState, now_millis: int
) -> Optional[str]:
    """
    FR-17: Returns a "robot_stuck" alert id when the robot has moved less than
    `STUCK_MOVE_EPSILON` meters over `stuck_timeout` seconds while executing a
    task, once per stuck episode. An episode re-arms only after the robot moves
    more than `STUCK_REARM_DISTANCE` meters away from the stuck position.
    """
    if robot.location is None:
        return None
    state = _stuck_states.get(robot_id)
    if state is None:
        _stuck_states[robot_id] = _StuckState(
            robot.location.x, robot.location.y, now_millis
        )
        return None
    dist = math.hypot(robot.location.x - state.x, robot.location.y - state.y)
    if state.alerted:
        if dist > STUCK_REARM_DISTANCE:
            _stuck_states[robot_id] = _StuckState(
                robot.location.x, robot.location.y, now_millis
            )
        return None
    if dist >= STUCK_MOVE_EPSILON:
        _stuck_states[robot_id] = _StuckState(
            robot.location.x, robot.location.y, now_millis
        )
        return None
    if not _is_executing_task(robot):
        # an idle or charging robot is expected to be stationary
        state.since_millis = now_millis
        return None
    if now_millis - state.since_millis >= app_config.stuck_timeout * 1000:
        state.alerted = True
        fleet, _, robot_name = robot_id.partition("/")
        return f"robot_stuck__{fleet}__{robot_name}__{now_millis}"
    return None


async def process_robot_alerts(fleet_state: mdl.FleetState) -> None:
    """FR-17: low battery and stuck robot alerts derived from fleet states."""
    if fleet_state.name is None or not fleet_state.robots:
        return
    now_millis = round(datetime.now().timestamp() * 1e3)
    for robot_name, robot in fleet_state.robots.items():
        robot_id = f"{fleet_state.name}/{robot_name}"
        for alert_id in (
            check_low_battery(robot_id, robot, now_millis),
            check_robot_stuck(robot_id, robot, now_millis),
        ):
            if alert_id is None:
                continue
            alert = await alert_repo.create_alert(alert_id, "robot")
            if alert is not None:
                alert_events.alerts.on_next(alert)


async def process_msg(msg: Dict[str, Any], fleet_repo: FleetRepository) -> None:
    if "type" not in msg:
        logger.warn(msg)
        logger.warn("Ignoring message, 'type' must include in msg field")
        return
    payload_type: str = msg["type"]
    if not isinstance(payload_type, str):
        logger.warn("error processing message, 'type' must be a string")
        return
    logger.debug(msg)

    if payload_type == "task_state_update":
        task_state = mdl.TaskState(**msg["data"])
        await task_repo.save_task_state(task_state)
        task_events.task_states.on_next(task_state)

        if task_state.status == mdl.TaskStatus.completed:
            alert = await alert_repo.create_alert(task_state.booking.id, "task")
            if alert is not None:
                alert_events.alerts.on_next(alert)
        elif task_state.status in (
            mdl.TaskStatus.failed,
            mdl.TaskStatus.canceled,
        ):
            # FR-17: failed and canceled tasks raise an alert as well. Terminal
            # states may be re-broadcast, only alert once per task.
            if not await alert_repo.alert_exists(task_state.booking.id):
                alert = await alert_repo.create_alert(task_state.booking.id, "task")
                if alert is not None:
                    alert_events.alerts.on_next(alert)

    elif payload_type == "task_log_update":
        task_log = mdl.TaskEventLog(**msg["data"])
        await task_repo.save_task_log(task_log)
        task_events.task_event_logs.on_next(task_log)

        if task_log_has_error(task_log):
            alert = await alert_repo.create_alert(task_log.task_id, "task")
            if alert is not None:
                alert_events.alerts.on_next(alert)

    elif payload_type == "fleet_state_update":
        fleet_state = mdl.FleetState(**msg["data"])
        await fleet_repo.save_fleet_state(fleet_state)
        fleet_events.fleet_states.on_next(fleet_state)
        # feeds the health watchdog's robot heartbeats (FR-17 robot offline)
        rmf_events.fleet_states.on_next(fleet_state)
        await process_robot_alerts(fleet_state)

    elif payload_type == "fleet_log_update":
        fleet_log = mdl.FleetLog(**msg["data"])
        await fleet_repo.save_fleet_log(fleet_log)
        fleet_events.fleet_logs.on_next(fleet_log)


@router.websocket("")
async def rmf_gateway(websocket: WebSocket):
    await websocket.accept()
    fleet_repo = FleetRepository(user)
    try:
        while True:
            msg: Dict[str, Any] = await websocket.receive_json()
            await process_msg(msg, fleet_repo)
    except WebSocketDisconnect:
        pass
