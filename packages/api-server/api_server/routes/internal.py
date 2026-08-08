# NOTE: This will eventually replace `gateway.py``
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from api_server import models as mdl
from api_server.app_config import app_config
from api_server.logger import logger as base_logger
from api_server.models import tortoise_models as ttm
from api_server.models.rmf_api.robot_state import Status as RobotStatus
from api_server.repositories import AlertRepository, FleetRepository, TaskRepository
from api_server.rmf_io import alert_events
from api_server.rmf_io import cancellation as task_cancellation
from api_server.rmf_io import fleet_events, rmf_events, task_events

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
# F-12 ghost charge tasks: reap at most this often per fleet (seconds).
CHARGE_GHOST_REAP_PERIOD = 30.0
CHARGE_GHOST_CATEGORY = "Charge Battery"
# F-12: an idle robot at/above this SoC finished charging even if the auto
# charge task never said so (recharge_soc is 1.0 fleet-side; margin for the
# publish race between battery state and task state).
CHARGE_GHOST_FULL_SOC = 0.9
# F-37: slack when deciding whether a task row was written during the
# current RMF run. Covers sim-clock RTF drift over a 24 h window (1 % of a
# day is ~15 min) — a stale row from a previous run is hours older, so a
# generous margin cannot resurrect one.
CURRENT_RUN_SLACK = timedelta(minutes=30)


@dataclass
class _StuckState:
    """Position anchor of a robot used to detect a stuck episode (FR-17)."""

    x: float
    y: float
    since_millis: int
    alert_id: Optional[str] = None


# per `{fleet}/{robot}`, the open low_battery alert id of the current low
# battery episode (FR-17); resolved and removed on recovery (F-39)
_low_battery_alerted: Dict[str, str] = {}
# robots whose stale low_battery alerts from a previous server life were
# swept (F-39, same reasoning as the F-29 stuck sweep)
_low_battery_stale_swept: set = set()
# per `{fleet}/{robot}` stuck episode tracking (FR-17)
_stuck_states: Dict[str, _StuckState] = {}

# F-68/E6: robot fault issues (robot_offline / robot_unresponsive /
# robot_down raised by the fleet adapter, FR-29/F-40/F-38) must reach the
# alert center, not only the robot views. One alert per fault episode,
# resolved when the robot's fault issues clear.
_fault_alerted: Dict[str, str] = {}
_fault_stale_swept: set = set()
# per fleet, wall-clock time of the last ghost charge task reap (F-12)
_last_charge_reap: Dict[str, float] = {}
# robots whose stale stuck alerts from a previous server life were swept (F-29)
_stuck_stale_swept: set = set()


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
) -> Tuple[Optional[str], Optional[str]]:
    """
    FR-17: Returns `(new_alert_id, resolved_alert_id)` (at most one is set).
    A "low_battery" alert id is returned when the robot's battery drops below
    `low_battery_threshold`, once per low battery episode. `RobotState.battery`
    is a fraction, 0.0 (depleted) to 1.0 (fully charged), and so is the
    threshold.

    F-39: the episode's alert is returned for resolution once the battery
    recovers past threshold + hysteresis — alerts are current exceptions,
    not history (F-22/F-29). The round-three soak ended with low_battery
    alerts from robots that had long recharged (and two stale ones from
    days-old runs) still open.
    """
    if robot.battery is None:
        return None, None
    threshold = app_config.low_battery_threshold
    open_alert = _low_battery_alerted.get(robot_id)
    if open_alert is not None:
        if robot.battery > threshold + LOW_BATTERY_HYSTERESIS:
            del _low_battery_alerted[robot_id]
            return None, open_alert
        return None, None
    if robot.battery < threshold:
        fleet, _, robot_name = robot_id.partition("/")
        alert_id = f"low_battery__{fleet}__{robot_name}__{now_millis}"
        _low_battery_alerted[robot_id] = alert_id
        return alert_id, None
    return None, None


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
) -> Tuple[Optional[str], Optional[str]]:
    """
    FR-17: Returns `(new_alert_id, resolved_alert_id)` (at most one is set).
    A "robot_stuck" alert id is returned when the robot has moved less than
    `STUCK_MOVE_EPSILON` meters over `stuck_timeout` seconds while executing a
    task, once per stuck episode.

    F-29: the episode's alert is returned for resolution — alerts are current
    exceptions, not history (F-22) — as soon as the robot moves more than
    `STUCK_REARM_DISTANCE` meters away from the stuck position OR stops
    executing a task (86 unresolved pages accumulated over the 2026-07-20
    soak, mostly patrols dispatched to the robot's current waypoint that
    finished without any motion). Either way the episode re-arms.
    """
    if robot.location is None:
        return None, None
    state = _stuck_states.get(robot_id)
    if state is None:
        _stuck_states[robot_id] = _StuckState(
            robot.location.x, robot.location.y, now_millis
        )
        return None, None
    dist = math.hypot(robot.location.x - state.x, robot.location.y - state.y)
    if state.alert_id is not None:
        if dist > STUCK_REARM_DISTANCE or not _is_executing_task(robot):
            resolved = state.alert_id
            _stuck_states[robot_id] = _StuckState(
                robot.location.x, robot.location.y, now_millis
            )
            return None, resolved
        return None, None
    if dist >= STUCK_MOVE_EPSILON:
        _stuck_states[robot_id] = _StuckState(
            robot.location.x, robot.location.y, now_millis
        )
        return None, None
    if not _is_executing_task(robot):
        # an idle or charging robot is expected to be stationary
        state.since_millis = now_millis
        return None, None
    if now_millis - state.since_millis >= app_config.stuck_timeout * 1000:
        fleet, _, robot_name = robot_id.partition("/")
        state.alert_id = f"robot_stuck__{fleet}__{robot_name}__{now_millis}"
        return state.alert_id, None
    return None, None


async def process_robot_alerts(fleet_state: mdl.FleetState) -> None:
    """FR-17: low battery and stuck robot alerts derived from fleet states."""
    if fleet_state.name is None or not fleet_state.robots:
        return
    now_millis = round(datetime.now().timestamp() * 1e3)
    for robot_name, robot in fleet_state.robots.items():
        robot_id = f"{fleet_state.name}/{robot_name}"
        if robot_id not in _stuck_stale_swept:
            # F-29: episode tracking is in-memory — open stuck alerts from a
            # previous server life can never be resolved by it, so sweep them
            # on the robot's first sighting.
            _stuck_stale_swept.add(robot_id)
            await alert_repo.resolve_alerts_by_prefix(
                f"robot_stuck__{fleet_state.name}__{robot_name}__"
            )
        if robot_id not in _low_battery_stale_swept:
            # F-39: same for low_battery alerts stranded by a previous
            # server life (two from days-old runs were still open at the
            # round-three soak end); the current episode re-alerts within
            # one fleet-state update if the battery is genuinely low.
            _low_battery_stale_swept.add(robot_id)
            await alert_repo.resolve_alerts_by_prefix(
                f"low_battery__{fleet_state.name}__{robot_name}__"
            )
        # F-68/E6: fault issues raised by the fleet adapter (FR-29/F-40/
        # F-38). Detected FIRST — a faulted robot's telemetry is spoofed
        # (F-42 holds SoC at 0.0), so the low-battery alert must not fire
        # on top of the fault alert.
        fault_categories = sorted(
            {
                str(issue.category)
                for issue in (robot.issues or [])
                if str(issue.category or "").startswith("robot_")
            }
        )
        battery_new, battery_resolved = check_low_battery(robot_id, robot, now_millis)
        if battery_resolved is not None:
            resolved = await alert_repo.resolve_alert(battery_resolved)
            if resolved is not None:
                alert_events.alerts.on_next(resolved)
        stuck_new, stuck_resolved = check_robot_stuck(robot_id, robot, now_millis)
        if stuck_resolved is not None:
            resolved = await alert_repo.resolve_alert(stuck_resolved)
            if resolved is not None:
                alert_events.alerts.on_next(resolved)
        if fault_categories and battery_new is not None:
            # drop the spurious 0 % episode so recovery re-arms cleanly
            _low_battery_alerted.pop(robot_id, None)
            battery_new = None
        if battery_new is not None:
            battery_pct = (
                f"{round(robot.battery * 100)} %"
                if robot.battery is not None
                else "low"
            )
            alert = await alert_repo.create_alert(
                battery_new,
                "robot",
                severity=ttm.Alert.Severity.Warning,
                fleet=fleet_state.name,
                robot=robot_name,
                message=f"Battery at {battery_pct}",
            )
            if alert is not None:
                alert_events.alerts.on_next(alert)
        if stuck_new is not None:
            alert = await alert_repo.create_alert(
                stuck_new,
                "robot",
                severity=ttm.Alert.Severity.Warning,
                fleet=fleet_state.name,
                robot=robot_name,
                message=(
                    f"Robot has not moved for {round(app_config.stuck_timeout)} s "
                    "while on a task"
                ),
            )
            if alert is not None:
                alert_events.alerts.on_next(alert)

        # F-68/E6: fault -> critical alert (one per episode)
        if robot_id not in _fault_stale_swept:
            _fault_stale_swept.add(robot_id)
            await alert_repo.resolve_alerts_by_prefix(
                f"robot_fault__{fleet_state.name}__{robot_name}__"
            )
        fault_alert_id = _fault_alerted.get(robot_id)
        if fault_categories and fault_alert_id is None:
            new_id = f"robot_fault__{fleet_state.name}__{robot_name}__{now_millis}"
            _fault_alerted[robot_id] = new_id
            faults_text = ", ".join(c.removeprefix("robot_") for c in fault_categories)
            alert = await alert_repo.create_alert(
                new_id,
                "robot",
                severity=ttm.Alert.Severity.Critical,
                fleet=fleet_state.name,
                robot=robot_name,
                message=(
                    f"Robot fault: {faults_text} — its missions were "
                    "canceled and it gets no new ones until it recovers. "
                    "Check the robot on site."
                ),
            )
            if alert is not None:
                alert_events.alerts.on_next(alert)
        elif not fault_categories and fault_alert_id is not None:
            _fault_alerted.pop(robot_id, None)
            resolved = await alert_repo.resolve_alert(fault_alert_id)
            if resolved is not None:
                alert_events.alerts.on_next(resolved)


def classify_charge_ghost(robot: mdl.RobotState, task_id: str) -> Optional[str]:
    """F-12: terminal status owed to a standby ChargeBattery task assigned to
    this robot, or None to leave the task alone.

    The Humble fleet adapter's automatic charge tasks never publish a
    terminal state: superseded by the next dispatch they stay `standby`
    forever and survive rmf-core restarts as ghosts (135 accumulated over
    the 2026-07-20 soak). Upstream is off-limits (no hard fork), so the
    api-server closes them out from observed fleet state instead:
      - robot executing a different task -> the charge task was superseded
        -> "killed" (honest: terminated, goal not necessarily reached);
      - robot not executing and back at/above CHARGE_GHOST_FULL_SOC ->
        the charge finished but never said so -> "completed".
    A standby charge task the robot is about to run (idle, still low) is
    left untouched.
    """
    if not task_id or robot.task_id == task_id:
        return None
    if _is_executing_task(robot):
        return "killed"
    if (
        robot.status != RobotStatus.charging
        and robot.battery is not None
        and robot.battery >= CHARGE_GHOST_FULL_SOC
    ):
        return "completed"
    return None


def charge_ghost_stale_cutoff(
    fleet_state: mdl.FleetState, now: datetime
) -> Optional[datetime]:
    """F-37: wall-clock time before which a task row cannot belong to the
    current RMF run. Robot states carry RMF's clock (`unix_millis_time`),
    which under use_sim_time restarts from zero at sim bringup — so
    `now - unix_millis_time` is the bringup time. On real deployments the
    clock is the wall epoch, the cutoff lands in 1970 and nothing is ever
    considered stale. None (no robot reported a clock) disables reaping —
    without a cutoff a reap could complete a task from a previous run
    against today's robot state, which is how the round-three soak grew
    completed rows for four-day-old tasks."""
    clock_ms = [
        r.unix_millis_time
        for r in fleet_state.robots.values()
        if r.unix_millis_time is not None
    ]
    if not clock_ms:
        return None
    return now - timedelta(milliseconds=max(clock_ms)) - CURRENT_RUN_SLACK


async def reap_charge_ghosts(fleet_state: mdl.FleetState) -> None:
    """F-12: close out ghost ChargeBattery tasks (see classify_charge_ghost).
    Runs at most once per CHARGE_GHOST_REAP_PERIOD per fleet. Rows written
    before the current RMF run (or before the F-37 provenance columns
    existed) are left untouched."""
    if fleet_state.name is None or not fleet_state.robots:
        return
    now = datetime.now().timestamp()
    if now - _last_charge_reap.get(fleet_state.name, 0.0) < CHARGE_GHOST_REAP_PERIOD:
        return
    _last_charge_reap[fleet_state.name] = now
    stale_cutoff = charge_ghost_stale_cutoff(fleet_state, datetime.now(timezone.utc))
    if stale_cutoff is None:
        return
    for robot_name, robot in fleet_state.robots.items():
        # NB: the DB column stores the stringified enum ("Status.standby"),
        # so filter with the enum member exactly like query_task_states does.
        ghosts = await ttm.TaskState.filter(
            status=mdl.TaskStatus.standby,
            category=CHARGE_GHOST_CATEGORY,
            assigned_to=robot_name,
        )
        for ghost in ghosts:
            if ghost.created_at is None or ghost.created_at < stale_cutoff:
                continue  # previous-run row (F-37)
            verdict = classify_charge_ghost(robot, ghost.id_)
            if verdict is None:
                continue
            task_state = mdl.TaskState(**ghost.data)
            task_state.status = mdl.TaskStatus(verdict)
            await task_repo.save_task_state(task_state)
            task_events.task_states.on_next(task_state)
            logger.info(
                f"F-12: reaped ghost charge task {ghost.id_} for "
                f"{fleet_state.name}/{robot_name} -> {verdict}"
            )


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
        # F-71(2): latch/stamp cancellation provenance BEFORE persisting so
        # stored rows and broadcasts agree (the canceled-vs-completed race
        # on the dead-robot path can wipe RMF's own field)
        task_cancellation.apply(task_state)
        await task_repo.save_task_state(task_state)
        task_events.task_states.on_next(task_state)

        # F-22: alerts are exceptions (FR-17) - a cleanly completed task must
        # NOT leave an open alert. The upstream completed-task alert grew
        # monotonically with dispatch count during the soak (81 open alerts
        # after 2.5 h of traffic) and buried the real ones. Only failed and
        # canceled tasks alert; terminal states may be re-broadcast, so alert
        # once per task.
        if task_state.status in (
            mdl.TaskStatus.failed,
            mdl.TaskStatus.canceled,
        ):
            if not await alert_repo.alert_exists(task_state.booking.id):
                assigned = task_state.assigned_to
                alert = await alert_repo.create_alert(
                    task_state.booking.id,
                    "task",
                    severity=(
                        ttm.Alert.Severity.Critical
                        if task_state.status == mdl.TaskStatus.failed
                        else ttm.Alert.Severity.Info
                    ),
                    fleet=assigned.group if assigned is not None else None,
                    robot=assigned.name if assigned is not None else None,
                    message=f"Task {task_state.booking.id} {task_state.status.value}",
                )
                if alert is not None:
                    alert_events.alerts.on_next(alert)

    elif payload_type == "task_log_update":
        task_log = mdl.TaskEventLog(**msg["data"])
        await task_repo.save_task_log(task_log)
        task_events.task_event_logs.on_next(task_log)

        if task_log_has_error(task_log):
            alert = await alert_repo.create_alert(
                task_log.task_id,
                "task",
                severity=ttm.Alert.Severity.Critical,
                message=f"Task {task_log.task_id} reported an error in its event log",
            )
            if alert is not None:
                alert_events.alerts.on_next(alert)

    elif payload_type == "fleet_state_update":
        fleet_state = mdl.FleetState(**msg["data"])
        await fleet_repo.save_fleet_state(fleet_state)
        fleet_events.fleet_states.on_next(fleet_state)
        # feeds the health watchdog's robot heartbeats (FR-17 robot offline)
        rmf_events.fleet_states.on_next(fleet_state)
        await process_robot_alerts(fleet_state)
        await reap_charge_ghosts(fleet_state)

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


# ----------------------------------------------------------------------
# F-86 / D-23 — the D-17 mission guard, re-checkable at the RESTART
# BOUNDARY. The request-time guard in routes/site_config.py runs seconds
# before the sidecar actually restarts rmf-core (validate + regen +
# commit sit in between); a task dispatched in that window — routinely an
# auto-issued ChargeBattery — would be interrupted without the
# acknowledgment D-17 promises. The sidecar calls this endpoint
# immediately before `docker restart` and aborts the job if
# unacknowledged missions appeared.
#
# Auth: the /_internal mount carries no user auth (it is the fleet
# adapter's gateway), so this endpoint checks the SAME shared-secret
# token file the api-server uses to authenticate itself to the sidecar —
# symmetric localhost defense-in-depth, the D-19 posture. No token file
# configured -> 404, never an open endpoint by accident.
# ----------------------------------------------------------------------


def _require_internal_token(request: Request) -> None:
    # imported here so the dependency stays one-directional at import
    # time (site_config never imports internal, so no cycle either way)
    import secrets as _secrets

    from api_server.routes.site_config import TOKEN_HEADER

    token_file = app_config.site_config_token_file
    if not token_file:
        raise HTTPException(404, "no site-config token configured")
    try:
        with open(token_file, "r", encoding="utf8") as f:
            expected = f.read().strip()
    except OSError as e:
        raise HTTPException(503, f"token file unreadable: {e}") from e
    provided = request.headers.get(TOKEN_HEADER, "")
    if not expected or not _secrets.compare_digest(provided, expected):
        raise HTTPException(403, "missing or wrong internal token")


@router.get("/active_missions")
async def internal_active_missions(request: Request) -> list:
    from api_server.routes.site_config import active_missions

    _require_internal_token(request)
    return await active_missions()


# ----------------------------------------------------------------------
# D-24 §5 — evacuation. The sidecar plans WHERE a displaced robot goes
# (only it sees the post-apply graph); the fleet adapter's settle-to-
# graph machinery executes the move. This is the bridge: the sidecar
# POSTs the plan here, we publish it to the adapter, and the sidecar
# polls /robot_positions until the robot stands on its target. Same
# token posture as /active_missions (D-19/D-23: symmetric localhost
# defense-in-depth; no new privilege moves — a robot repositioning
# primitive already exists as the adapter's own settle behavior).
# ----------------------------------------------------------------------
_evacuate_pub = None


@router.get("/robot_positions")
async def internal_robot_positions(request: Request) -> list:
    from api_server.routes.site_config import robot_positions

    _require_internal_token(request)
    return await robot_positions()


@router.post("/cancel_missions")
async def internal_cancel_missions(request: Request) -> list:
    """D-24 §6 / D-17 honesty: a hard-confirmed apply is about to
    restart rmf-core over these missions. Cancel them PROPERLY — RMF
    cancellation plus a provenance label — instead of letting the
    restart orphan them into the stale-task janitor as anonymous
    failures. The sidecar calls this immediately before the restart, so
    a job that failed validation or evacuation never cancels anything.
    Best-effort per task: the restart interrupts the mission either way;
    what this adds is the honest record."""
    from datetime import datetime as _datetime

    from api_server import models as _mdl
    from api_server.models.rmf_api.task_state import Cancellation
    from api_server.rmf_io import cancellation as _task_cancellation
    from api_server.rmf_io import tasks_service
    from api_server.routes.site_config import active_missions

    _require_internal_token(request)
    body = await request.json()
    applied_by = str(body.get("applied_by") or "admin")
    label = (
        "Interrupted by a site configuration change "
        f"(applied by {applied_by})"
    )
    missions = await active_missions()
    for mission in missions:
        task_id = str(mission.get("task_id") or "")
        if not task_id:
            continue
        _task_cancellation.latch(
            task_id,
            Cancellation(
                unix_millis_request_time=round(
                    _datetime.now().timestamp() * 1e3
                ),
                labels=[label],
            ),
        )
        try:
            await tasks_service().call(
                _mdl.CancelTaskRequest(
                    type="cancel_task_request", task_id=task_id, labels=[label]
                ).model_dump_json(exclude_none=True)
            )
        except Exception as e:  # noqa: BLE001 — per-task best effort
            logger.warning(
                "cancel_missions: RMF cancel of [%s] failed (%s) — the "
                "restart will interrupt it anyway; provenance stays "
                "latched",
                task_id,
                e,
            )
    logger.info(
        "D-24: %d mission(s) canceled ahead of a site-change restart "
        "(applied by %s)",
        len(missions),
        applied_by,
    )
    return missions


@router.post("/evacuate")
async def internal_evacuate(request: Request) -> dict:
    import json as _json

    import rclpy.qos
    from std_msgs.msg import String as StringMsg

    from api_server import ros

    _require_internal_token(request)
    body = await request.json()
    for key in ("robot", "waypoint", "x", "y"):
        if key not in body:
            raise HTTPException(422, f"evacuate body is missing '{key}'")
    global _evacuate_pub  # pylint: disable=global-statement
    if _evacuate_pub is None:
        _evacuate_pub = ros.ros_node().create_publisher(
            StringMsg,
            "gf_evacuate",
            rclpy.qos.QoSProfile(
                depth=10,
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.VOLATILE,
            ),
        )
    _evacuate_pub.publish(
        StringMsg(
            data=_json.dumps(
                {
                    "robot": str(body["robot"]),
                    "waypoint": str(body["waypoint"]),
                    "x": float(body["x"]),
                    "y": float(body["y"]),
                }
            )
        )
    )
    logger.info(
        "D-24 evacuation commanded: %s -> %s", body["robot"], body["waypoint"]
    )
    return {"ok": True}
