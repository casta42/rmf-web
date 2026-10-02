# NOTE: This will eventually replace `gateway.py``
import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from api_server import models as mdl
from api_server import phantom_completion, waiting_missions
from api_server.app_config import app_config
from api_server.dispatch_reason import (
    dispatch_failure_reason,
    no_bid_failure_reason,
    no_bid_judged_failure_reason,
)
from api_server.fleet_state_cadence import FleetStateCadence
from api_server.internal_tasks import pages_operator
from api_server.interrupted_tasks import (
    INTERRUPTED_LABEL,
    RunBoundary,
    is_interrupted_row,
    tasks_named_by,
)
from api_server.logger import logger as base_logger
from api_server.models import tortoise_models as ttm
from api_server.models.rmf_api.robot_state import Status as RobotStatus
from api_server.redispatch import (
    CLASS_NO_BID,
    REASON_LABEL,
    REFUSED_LABEL,
    Held,
    Redispatcher,
    Refusal,
    class_of,
    no_bid_since_of,
    no_bid_verdict_of,
    origin_of,
    root_of,
    status_tail,
    supersede,
    unmark_hand_back,
    unsupersede,
    wants_redispatch,
)
from api_server.repositories import AlertRepository, FleetRepository, TaskRepository
from api_server.rmf_io import alert_events
from api_server.rmf_io import cancellation as task_cancellation
from api_server.rmf_io import fleet_events, rmf_events, task_events
from api_server.shielded import shielded

# Fault issue categories the fleet adapter raises (RobotCommandHandle
# `_faults`): these mean the robot is out of service and its tasks were
# failed per FR-29. Traffic-condition issues (blocked_by_no_go,
# waiting_on_occupied_waypoint) are deliberately excluded.
ROBOT_FAULT_CATEGORIES = frozenset(
    {"robot_offline", "robot_unresponsive", "robot_down"}
)

router = APIRouter(tags=["_internal"])
logger = base_logger.getChild("RmfGatewayApp")
user: mdl.User = mdl.User(username="__rmf_internal__", is_admin=True)
task_repo = TaskRepository(user)
alert_repo = AlertRepository(user, task_repo)
# F-395: when a fleet-state message reaches the dashboards, and when it
# rides the 1 s cycle (database, heartbeat, alert rules, reapers)
fleet_state_cadence = FleetStateCadence()


class _AcceptanceWatch:
    """F-441: the task repository as the dispatch path sees it, noting the
    moment the dispatcher ACCEPTED the request — `_dispatch_task_now`
    saves the state only after a success. An error after that moment
    (the save itself) is not a refusal: the mission is already with the
    dispatcher, and sending it again would make a duplicate."""

    def __init__(self, repo: TaskRepository):
        self._repo = repo
        self.accepted: Optional[str] = None

    def __getattr__(self, name):
        return getattr(self._repo, name)

    async def save_task_state(self, task_state: mdl.TaskState) -> None:
        self.accepted = task_state.booking.id
        await self._repo.save_task_state(task_state)


def _body_of(resp) -> dict:
    try:
        body = json.loads(bytes(getattr(resp, "body", b"") or b"{}"))
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


# the dispatcher's one refusal at submission (rmf_task_ros2 Dispatcher.cpp:
# the request fails the dispatch_task_request schema)
_INVALID_REQUEST_CODE = 5


def _dispatcher_refusal(body: dict) -> Refusal:
    """F-441: the dispatcher answered success=false. Its only such answer
    is a request that fails its schema — deterministic, so the stored
    request can never be sent again (permanent). Any other shape is read
    as the site as it is now."""
    errors = body.get("errors") if isinstance(body.get("errors"), list) else []
    details = "; ".join(
        str(e.get("detail") or e.get("category") or e)
        for e in errors if isinstance(e, dict)) or "no reason given"
    if any(isinstance(e, dict) and e.get("code") == _INVALID_REQUEST_CODE
           for e in errors):
        return Refusal(
            f"the dispatcher refuses its stored request as invalid: {details}",
            permanent=True)
    return Refusal(f"the dispatcher refused it: {details}")


async def _redispatch_request(request: mdl.TaskRequest) -> str:
    """FR-12 governor: put a returned mission back on the floor through
    the operator's own dispatch path (F-34 guard, request stored,
    state saved). Imported lazily: routes.tasks imports this package.

    F-441: every outcome is named for the Redispatcher. A guard's or the
    horizon's HTTP refusal (409, 422) and the dispatcher not answering
    (500 "rmf service timed out") are Refusals of the moment; the
    dispatcher's success=false is _dispatcher_refusal; a mission the
    horizon holds for its start is Held, never a refusal. An error after
    the dispatcher accepted returns the new id: the mission is out."""
    from api_server.routes.tasks.tasks import post_dispatch_task

    watch = _AcceptanceWatch(task_repo)
    try:
        resp = await post_dispatch_task(
            mdl.DispatchTaskRequest(type="dispatch_task_request",
                                    request=request),
            watch,  # type: ignore[arg-type]
        )
    except HTTPException as exc:
        raise Refusal(str(exc.detail)) from exc
    except Exception:
        if watch.accepted is None:
            raise
        logger.exception(
            "F-441: the dispatcher accepted the re-dispatch as [%s], and it "
            "could not be recorded — not sent again", watch.accepted)
        return watch.accepted
    if isinstance(resp, mdl.TaskDispatchResponse):
        return resp.root.state.booking.id  # type: ignore[union-attr]
    body = _body_of(resp)
    if getattr(resp, "status_code", None) == 202 and body.get("deferred"):
        deferred = body["deferred"] if isinstance(body["deferred"], dict) \
            else {}
        raise Held(deferred.get("id"),
                   str(body.get("detail") or "held for its start (F-293)"))
    raise _dispatcher_refusal(body)


async def _load_request(task_id: str):
    return await task_repo.get_task_request(task_id)


async def _first_recorded_s(task_id: str) -> Optional[float]:
    """Wall-clock second this database first recorded the task (the F-37
    `created_at` column) — for a dispatched mission, when its auction
    opened. None when the row cannot be read; never a guess."""
    try:
        stamps = await ttm.TaskState.filter(id_=task_id).values_list(
            "created_at", flat=True)
    except Exception:  # noqa: BLE001 — cannot see, so cannot say
        return None
    if not stamps or stamps[0] is None:
        return None
    return stamps[0].timestamp()


async def _fail_abandoned_reauction(task_id: str, reason: str) -> None:
    """G ruling 2026-10-01 item 6 (F-410/F-412 class), narrowed by F-441
    (D-86 (4)): an attempt was recorded as put back on the floor — a
    no-bid ("auctioned again in N s") or a hand-back — and NOTHING can
    ever be re-sent: the request is not stored, or the dispatcher refuses
    the stored request itself. (Every other refusal keeps the mission
    waiting.) That mission is not waiting for anything: it failed, and it
    must not sit in the ledger as a tidy cancel with no successor. The row
    is amended to `failed` and the operator is paged with the reason; its
    broadcast takes the chain out of the waiting registry (F-435)."""
    state = await task_repo.get_task_state(task_id)
    if state is None or not (unsupersede(state) or unmark_hand_back(state)):
        return
    await task_repo.save_task_state(state)
    task_events.task_states.on_next(state)
    # keyed like every task alert — the row's own booking id (F-374), which
    # is also what the alert catalogue reads (F3.2)
    alert_id = state.booking.id
    alert = await alert_repo.create_alert(
        alert_id,
        "task",
        severity=ttm.Alert.Severity.Critical,
        message=f"Task {alert_id} failed: {reason}",
    )
    if alert is not None:
        alert_events.alerts.on_next(alert)


# F-435 (G ruling 2026-10-01, ruling 2): the missions waiting for a robot
waiting = waiting_missions.registry

async def _redispatch_refused(task_id: str, why: str, retry_in_s: float) -> None:
    """F-441 (G close-out ruling 2026-10-01, D-86 (4)): "a refused
    re-dispatch never drops a mission; it returns to the waiting queue
    under F-435." The Redispatcher sends it again after `retry_in_s`; here
    the mission is shown waiting with the refusal as its reason — in the
    registry (GET /tasks/waiting, the one alert) and on the attempt's row
    (REFUSED_LABEL, re-stamped each time, so the ledger says why it still
    waits and a restart resumes it with that reason)."""
    reason = waiting_missions.refused_reason(why)
    state = await task_repo.get_task_state(task_id)
    marked = state is not None and state.cancellation is not None and \
        wants_redispatch(state.status, state.cancellation.labels) is not None
    entry = waiting.find_task(task_id)
    if entry is None and marked:
        root_id = root_of(state.booking.labels) or task_id
        entry = waiting.get(root_id)
        if entry is None:
            # the registry never saw this attempt put back (it is rebuilt
            # at start): a mission whose re-dispatch is being retried IS
            # waiting, so it is listed
            entry = waiting.adopt(waiting_missions.WaitingMission(
                root_id=root_id, task_id=task_id, since_unix=time.time(),
                reason=reason))
            asyncio.get_running_loop().create_task(_enrich_waiting(entry))
    if entry is not None and entry.task_id == task_id:
        entry.reason = reason
    logger.warning("F-441: [%s] is still waiting for a robot — %s; sent "
                   "again in %.0f s", task_id, reason, retry_in_s)
    if not marked:
        return
    state.cancellation.labels = [
        lab for lab in state.cancellation.labels
        if not lab.startswith(REFUSED_LABEL)] + [f"{REFUSED_LABEL}{why[:200]}"]
    await task_repo.save_task_state(state)
    task_events.task_states.on_next(state)


async def _redispatch_held(task_id: str, detail: str) -> None:
    """F-441: the re-dispatch was accepted and is held for its start by the
    dispatch horizon (F-293): the mission is not waiting for a robot any
    more but for its time, and GET /tasks/deferred shows it. The chain
    leaves the waiting list; its alert, if raised, is closed."""
    entry = waiting.find_task(task_id)
    if entry is None:
        return
    waiting.leave(entry.root_id)
    logger.warning("F-441: mission %s is held for its start (F-293): %s",
                   entry.root_id, detail)
    await _resolve_waiting_alert(entry)


redispatcher = Redispatcher(
    _redispatch_request, _load_request, logger.getChild("Redispatch"),
    abandon=_fail_abandoned_reauction, first_seen=_first_recorded_s,
    wanted=waiting.wanted, refused=_redispatch_refused,
    held=_redispatch_held)


_request_labels: Dict[str, Optional[list]] = {}


async def _preserve_booking_labels(task_state: mdl.TaskState) -> None:
    task_id = task_state.booking.id
    if task_state.booking.labels:
        return
    if task_id not in _request_labels:
        if len(_request_labels) > 8192:
            _request_labels.clear()
        request = await task_repo.get_task_request(task_id)
        _request_labels[task_id] = list(request.labels) if request and \
            request.labels else None
    labels = _request_labels.get(task_id)
    if labels:
        task_state.booking.labels = list(labels)


_followed_through: set = set()
_NON_TERMINAL_FOR_FOLLOW = {"queued", "standby", "underway", "delayed",
                            "blocked", "uninitialized"}


async def _follow_through_cancel(task_state: mdl.TaskState) -> None:
    task_id = task_state.booking.id
    status = str(task_state.status).split(".")[-1].lower()
    if status not in _NON_TERMINAL_FOR_FOLLOW:
        return
    if task_state.assigned_to is None:
        return
    if not task_cancellation.requested(task_id):
        return
    if task_id in _followed_through:
        return
    _followed_through.add(task_id)
    if len(_followed_through) > 4096:
        _followed_through.clear()
    logger.warning(
        "F-292: [%s] arrived %s on [%s] after its cancellation was "
        "requested — the dispatcher awarded a task it had canceled in "
        "flight; cancelling it at the fleet",
        task_id, status, task_state.assigned_to.name)
    from api_server.rmf_io import tasks_service

    async def _send():
        try:
            await tasks_service().call(
                mdl.CancelTaskRequest(
                    type="cancel_task_request", task_id=task_id,
                    labels=["canceled before dispatch; the dispatcher "
                            "awarded it anyway (F-292)"],
                ).model_dump_json(exclude_none=True), timeout=10)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("F-292 follow-through cancel of [%s]: %s",
                           task_id, exc)

    asyncio.get_running_loop().create_task(_send())


async def _redispatch_later(task_state: mdl.TaskState) -> None:
    try:
        errors = None
        if task_state.dispatch is not None and task_state.dispatch.errors:
            # F-435: the detail is what tells a permanent answer ("no robot
            # can ever take it") from a transient one; F-442: the category
            # is what marks the fleet's naming of every robot
            errors = [{"code": e.code, "category": e.category,
                       "detail": e.detail}
                      for e in task_state.dispatch.errors]
        task_id = task_state.booking.id
        new_id = await redispatcher.maybe_redispatch(
            task_id,
            task_state.status,
            task_state.cancellation.labels
            if task_state.cancellation is not None else None,
            booking_labels=task_state.booking.labels,
            dispatch_errors=errors,
        )
        if new_id is not None:
            waiting.redispatched(
                root_of(task_state.booking.labels) or task_id, task_id,
                new_id)
    except Exception:  # pylint: disable=broad-except
        logger.exception("re-dispatch of [%s] failed", task_state.booking.id)


# ----------------------------------------------------------------------
# F-435 (G ruling 2026-10-01, ruling 2): "a mission never fails because a
# robot is busy or charging. It waits, shown to the operator as 'waiting
# for a robot' with its age, and raises one alert after a threshold."
# The registry (waiting_missions.py) follows every task state the
# api-server broadcasts — whichever path wrote it (the fleet's feed, the
# abandoned re-auction, the interrupted-mission sweep, a local close) —
# so a chain that starts or ends by any route leaves it.
# ----------------------------------------------------------------------
WAITING_ALERT_PERIOD_S = 5.0
# resume_waiting_chains: how far back a restart looks for chains it would
# otherwise drop, and how many rows it reads at most to find them. One hour
# by default (GF_WAITING_RESUME_WINDOW_S): it covers an api-server restart
# and an upgrade's downtime; a tail older than that belongs to an earlier
# shift — or, on the first start after this release, was stopped by the old
# 900 s / 8-hop bounds and already reported lost — and re-dispatching it
# hours later would surprise the operator more than it would help.
RESUME_WINDOW_S = float(os.environ.get("GF_WAITING_RESUME_WINDOW_S", "3600"))
RESUME_SCAN_LIMIT = 5000
_LIVE_RAW = {"queued", "standby", "uninitialized"}


def _request_category_places(request: Any) -> Tuple[Optional[str], list]:
    """The mission as the operator dispatched it: its category and its
    stops, from the stored request (a model or its JSON)."""
    if request is None:
        return None, []
    if not isinstance(request, dict):
        request = request.model_dump(mode="json")
    description = request.get("description")
    places = description.get("places") if isinstance(description, dict) else None
    places = [p for p in places if isinstance(p, str)] if isinstance(
        places, list) else []
    category = request.get("category")
    return (str(category) if category else None), places


async def _enrich_waiting(entry: waiting_missions.WaitingMission) -> None:
    """Fill in what the task state does not carry: since when the mission
    waits (the ledger's first record of its root) and what it is (the
    stored request). Best effort — a row that cannot be read leaves the
    first-seen time and an empty route, never a guess."""
    try:
        since = await _first_recorded_s(entry.root_id)
        if since is not None:
            entry.since_unix = min(entry.since_unix, since)
        request = await task_repo.get_task_request(entry.task_id)
        if request is None and entry.task_id != entry.root_id:
            request = await task_repo.get_task_request(entry.root_id)
        entry.category, entry.places = _request_category_places(request)
        entry.enriched = True
    except Exception:  # pylint: disable=broad-except
        logger.exception("F-435: [%s] waiting entry could not be read",
                         entry.root_id)


async def _resolve_waiting_alert(entry: waiting_missions.WaitingMission) -> None:
    """The chain started or ended: its one alert is closed (the shape of
    every episode alert in this module)."""
    if entry.alerted is None:
        return
    resolved = await alert_repo.resolve_alert(entry.alerted)
    if resolved is not None:
        alert_events.alerts.on_next(resolved)


def _on_task_state(task_state: mdl.TaskState) -> None:
    """task_events.task_states observer: keep the registry in step with
    every broadcast state. Synchronous, so the registry never lags the
    feed; the database work it needs runs on the loop. Never raises into
    the emitter."""
    try:
        change = waiting.observe(task_state)
        if change is None:
            return
        loop = asyncio.get_running_loop()
        if change.kind == "left":
            if change.entry.alerted is not None:
                loop.create_task(_resolve_waiting_alert(change.entry))
        elif not change.entry.enriched:
            loop.create_task(_enrich_waiting(change.entry))
    except RuntimeError:
        return      # no running loop: a synchronous caller, nothing to schedule
    except Exception:  # pylint: disable=broad-except
        logger.exception("F-435: the waiting registry missed a task state")


task_events.task_states.subscribe(_on_task_state)


async def process_waiting_alerts(now_s: Optional[float] = None) -> int:
    """F-435: ONE Warning per chain that has waited GF_WAITING_ALERT_S
    (waiting_missions.py says why 900 s), never a second. Returns how many
    it raised."""
    now_s = time.time() if now_s is None else now_s
    raised = 0
    for entry in waiting.due_alerts(now_s, waiting_missions.WAITING_ALERT_S):
        alert_id = f"waiting__{entry.root_id}"
        alert = await alert_repo.create_alert(
            alert_id,
            "fleet",
            severity=ttm.Alert.Severity.Warning,
            message=waiting_missions.alert_message(entry, now_s),
        )
        entry.alerted = alert_id
        raised += 1
        if alert is not None:
            alert_events.alerts.on_next(alert)
        if waiting.get(entry.root_id) is not entry:
            # the chain started or ended while the alert was written
            await _resolve_waiting_alert(entry)
    return raised


async def withdraw_waiting(task_id: str, labels: list):
    """POST /tasks/cancel_task on an attempt that is already `canceled`:
    when it is an attempt a chain is WAITING on (between auctions), the
    operator is canceling the mission, not that row. The chain leaves the
    registry, the pending re-dispatch is not made (Redispatcher `wanted`),
    and the row is recorded as the operator's cancel — the gf:redispatch
    marker removed, so the ledger reads a canceled mission (not a lost
    one) and a restart does not resume it. An attempt the chain has
    already moved past forwards the cancel to the one it waits on now.
    Returns the response, or None when no waiting chain is concerned."""
    state = await task_repo.get_task_state(task_id)
    if state is None:
        return None
    cancel_labels = state.cancellation.labels if state.cancellation else None
    if wants_redispatch(state.status, cancel_labels) is None:
        return None
    entry = waiting.get(root_of(state.booking.labels) or task_id)
    if entry is not None and entry.task_id != task_id:
        from api_server.routes.tasks.tasks import post_cancel_task

        return await post_cancel_task(
            mdl.CancelTaskRequest(
                type="cancel_task_request", task_id=entry.task_id,
                labels=labels),
            task_repo,
        )
    note = (f"it was waiting for a robot — {entry.reason}" if entry
            else "it was waiting for a robot")
    entry = waiting.withdraw(task_id, [*labels, note])
    if entry is None:
        return None
    from api_server.models.rmf_api.task_state import Cancellation

    state.cancellation = Cancellation(
        unix_millis_request_time=round(time.time() * 1e3),
        labels=waiting.withdrawn_labels(task_id) or [],
    )
    task_cancellation.latch(task_id, state.cancellation)
    await task_repo.save_task_state(state)
    task_events.task_states.on_next(state)
    await _resolve_waiting_alert(entry)
    logger.warning("F-435: [%s] (mission %s) canceled by an operator while "
                   "it waited for a robot", task_id, entry.root_id)
    return {"success": True,
            "detail": "canceled — the mission was waiting for a robot and "
                      "will not be dispatched again"}


def _apply_withdrawal(task_state: mdl.TaskState) -> None:
    """A withdrawn attempt stays the operator's cancel: a later broadcast
    of it (a fleet re-sends terminal states) must not bring the marker
    back."""
    labels = waiting.withdrawn_labels(task_state.booking.id)
    if labels is None or task_state.cancellation is None:
        return
    task_state.cancellation.labels = labels


def _resume_reason(labels: Optional[list]) -> str:
    """The latest reason of a chain found waiting on a LIVE attempt at
    start: the hand-back's own reason rides in the attempt's labels; a
    no-bid's answer does not, so it is named generically."""
    if class_of(labels) == CLASS_NO_BID:
        return "no robot took it at its last auction"
    for label in labels or []:
        if label.startswith(REASON_LABEL) and label[len(REASON_LABEL):]:
            return label[len(REASON_LABEL):]
    return "handed back by the fleet"


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def resume_waiting_chains(now_s: Optional[float] = None) -> list:
    """F-435, at api-server start: rebuild the registry from the ledger and
    RESUME what the restart would otherwise have dropped. The wait before
    a re-dispatch is an in-process sleep, so a restart inside it left the
    chain's last row a marked cancel with no successor — the mission
    silently gone. Every such TAIL created in the last RESUME_WINDOW_S is
    re-dispatched through the ordinary path, after the ordinary backoff;
    a chain whose latest attempt is live (queued, assigned) is registered
    as waiting on it. Bounded: at most RESUME_SCAN_LIMIT rows are read.
    Returns the tail ids it re-dispatched."""
    now_s = time.time() if now_s is None else now_s
    cutoff = datetime.fromtimestamp(now_s - RESUME_WINDOW_S, tz=timezone.utc)
    rows = await ttm.TaskState.filter(created_at__gte=cutoff).order_by(
        "-created_at").limit(RESUME_SCAN_LIMIT).values_list(
        "id_", "status", "created_at")
    if len(rows) >= RESUME_SCAN_LIMIT:
        logger.warning(
            "F-435: the resume scan read its limit of %d rows; chains older "
            "than the oldest of them are not resumed", RESUME_SCAN_LIMIT)
    status_of = {str(i): status_tail(s) for i, s, _ in rows}
    created_of = {str(i): c for i, _, c in rows}
    labels_of: Dict[str, list] = {}
    requests: Dict[str, dict] = {}
    ids = list(status_of)
    for at in range(0, len(ids), 500):
        for task_id, raw in await ttm.TaskRequest.filter(
                id___in=ids[at:at + 500]).values_list("id_", "request"):
            request = _as_dict(raw)
            requests[str(task_id)] = request
            labels = request.get("labels")
            labels_of[str(task_id)] = labels if isinstance(labels, list) else []
    replaced = {origin_of(labels) for labels in labels_of.values()} - {None}

    def root(task_id: str) -> str:
        return root_of(labels_of.get(task_id)) or task_id

    canceled_in = {}
    for task_id, status in status_of.items():
        if status in waiting_missions.CANCELED:
            canceled_in.setdefault(root(task_id), []).append(task_id)
    tails: list = []
    candidates = [i for i in ids if status_of[i] in waiting_missions.CANCELED
                  and i not in replaced]
    for at in range(0, len(candidates), 200):
        for task_id, data in await ttm.TaskState.filter(
                id___in=candidates[at:at + 200]).values_list("id_", "data"):
            try:
                state = mdl.TaskState(**_as_dict(data))
            except Exception:  # noqa: BLE001 — a corrupt row is skipped
                continue
            reason = waiting_missions.waiting_reason(state)
            if reason is None:
                continue        # an operator's cancel: the mission ended
            tails.append(state)
    first_seen: Dict[str, float] = {}

    async def since_of(chain: str, task_id: str) -> float:
        if chain not in first_seen:
            since = await _first_recorded_s(chain)
            if since is None and created_of.get(task_id) is not None:
                since = created_of[task_id].timestamp()
            first_seen[chain] = since if since is not None else now_s
        return first_seen[chain]

    for state in tails:
        task_id = state.booking.id
        chain = root(task_id)
        category, places = _request_category_places(requests.get(task_id))
        waiting.adopt(waiting_missions.WaitingMission(
            root_id=chain, task_id=task_id,
            since_unix=await since_of(chain, task_id),
            reason=waiting_missions.waiting_reason(state) or "",
            attempts=len(canceled_in.get(chain, [])) or 1,
            category=category, places=places, enriched=True))
    for task_id in ids:
        labels = labels_of.get(task_id)
        # a live attempt that is not the chain's first: the chain waits on it
        if status_of[task_id] not in _LIVE_RAW or root_of(labels) is None \
                or task_id in replaced:
            continue
        chain = root(task_id)
        category, places = _request_category_places(requests.get(task_id))
        waiting.adopt(waiting_missions.WaitingMission(
            root_id=chain, task_id=task_id,
            since_unix=await since_of(chain, task_id),
            reason=_resume_reason(labels),
            attempts=len(canceled_in.get(chain, [])) or 1,
            category=category, places=places, enriched=True))
    # the one alert per chain survives the restart: a chain whose alert was
    # already raised (open, or resolved by an operator) is not paged again;
    # an open one whose chain is no longer waiting is closed
    for entry in waiting.entries():
        alert_id = f"waiting__{entry.root_id}"
        if await alert_repo.resolved_millis(alert_id) != 0:
            entry.alerted = alert_id
    stale = await ttm.Alert.filter(
        original_id__startswith=waiting_missions.ALERT_PREFIX,
        unix_millis_resolved_time__isnull=True).values_list("id", flat=True)
    for alert_id in stale:
        root_id = str(alert_id)[len(waiting_missions.ALERT_PREFIX):]
        if waiting.get(root_id) is None:
            resolved = await alert_repo.resolve_alert(str(alert_id))
            if resolved is not None:
                alert_events.alerts.on_next(resolved)
    resumed = []
    loop = asyncio.get_running_loop()
    for state in tails:
        resumed.append(state.booking.id)
        loop.create_task(_redispatch_later(state))
    if resumed or len(waiting):
        logger.warning(
            "F-435: %d mission(s) waiting for a robot at start; %d of them "
            "had been dropped by the restart and are back on the floor: %s",
            len(waiting), len(resumed), ", ".join(resumed[:12]))
    return resumed


async def waiting_maintenance_loop() -> None:
    """Started with the app: resume once, then raise the waiting alerts
    that come due."""
    try:
        await resume_waiting_chains()
    except Exception:  # pylint: disable=broad-except
        logger.exception("F-435: resuming the waiting missions failed")
    while True:
        try:
            await process_waiting_alerts()
        except Exception:  # pylint: disable=broad-except
            logger.exception("F-435: the waiting-mission alert pass failed")
        await asyncio.sleep(WAITING_ALERT_PERIOD_S)

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

# FR-36/D-30 layers 2-4: the fleet adapter proves a deadlock (wait-for
# cycle) or a blocked chain and raises ONE issue ticket per condition
# episode; this module folds it into ONE operator alert — the anti-shape
# is F-125's four unrelated "has not moved" rows for a single deadlock.
FR36_CATEGORIES = frozenset(
    {"fleet_deadlock", "fleet_blocked", "fleet_blocked_escalation"}
)
_fr36_alerted: Dict[str, Tuple[str, str]] = {}  # episode -> (alert_id, cat)
_fr36_actions: Dict[str, str] = {}  # episode -> last announced action
_fr36_stale_swept: set = set()  # fleet names swept on first sighting
# per fleet, wall-clock time of the last ghost charge task reap (F-12)
_last_charge_reap: Dict[str, float] = {}
# robots whose stale stuck alerts from a previous server life were swept (F-29)
_stuck_stale_swept: set = set()

# F-139/E6 run-2: resolving a condition alert must not silence the CONDITION.
# When an operator resolves a robot_stuck / low_battery / robot_fault / FR-36
# row while the episode trackers above still see the condition, the episode
# re-fires as a fresh alert row once the resolution is this old. Grace gives
# the operator's on-site action time to clear the condition before we page
# again.
ALERT_REFIRE_GRACE_MILLIS = 60_000


def refire_due(resolved_at_millis: Optional[int], now_millis: int) -> bool:
    """F-139: True when a tracked episode alert was resolved (by anyone)
    at least ALERT_REFIRE_GRACE_MILLIS ago. None means the row is still
    open — never re-fire an open alert."""
    return (
        resolved_at_millis is not None
        and now_millis - resolved_at_millis >= ALERT_REFIRE_GRACE_MILLIS
    )


async def _refire_due(alert_id: str, now_millis: int) -> bool:
    return refire_due(await alert_repo.resolved_millis(alert_id), now_millis)


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


TASK_LOG_ERROR_TEXT = "reported an error in its event log"


def no_bid_final_reason(
    task_state: mdl.TaskState, now_s: Optional[float] = None
) -> Optional[str]:
    """G ruling 2026-10-01 item 6 (F-410/F-412 class): the named reason
    for a mission whose LAST auction also got no bid — how many auctions,
    over how long, and whether the fleet was silent or answered with a
    refusal. None for every other state, including a no-bid attempt that
    still has auctions left (supersede() has recorded that one as put
    back on the floor, and it raises no failure).

    F-442 (D-86 (4)): the last auction is the fifth in a row on which the
    fleet named every robot it considered as unable ever to take it, and
    the reason names each of them and why."""
    verdict = no_bid_verdict_of(task_state)
    if verdict is None or not verdict.final or task_state.dispatch is None:
        return None
    since = no_bid_since_of(task_state.booking.labels)
    over_s = None
    if since is not None:
        over_s = (time.time() if now_s is None else now_s) - since
    return no_bid_judged_failure_reason(
        task_state.dispatch.errors, verdict.attempt, over_s
    ) or no_bid_failure_reason(task_state.dispatch.errors, verdict.attempt, over_s)


async def alert_on_task_state(task_state: mdl.TaskState, repo):
    """F-22: only a failed or canceled task alerts, once per task (terminal
    states may be re-broadcast). F-95: a failure carries the WHY when the
    dispatcher knows it — a task refused at dispatch time has the
    adapter's structured errors in dispatch.errors; execution failures do
    not.

    G ruling 2026-10-01 item 6: an auction nobody bid on is a failure —
    and a Critical alert — only when it was the mission's last one (since
    F-435, only after the fifth answer that no robot can ever take it);
    the reason then names the count and the span. Every earlier attempt
    reaches this function already recorded as `canceled` (supersede() in
    process_msg) and, like a hand-back, raises nothing: the mission is
    waiting (F-435, below).

    F-374: one row per task, keyed by the task id (the dashboard's
    caption for a task alert IS that id). The only row this may replace
    is an OPEN log-error row for the same task: the terminal verdict,
    with its reason, says more than "reported an error". A row the
    operator has resolved, or a terminal alert already raised, is left
    alone — replacing it would re-open it, because create_alert resets
    the ack and the resolution."""
    if task_state.status not in (mdl.TaskStatus.failed, mdl.TaskStatus.canceled):
        return None
    # F-435 (G ruling 2026-10-01, ruling 2): an attempt the fleet put back
    # on the floor — a hand-back, or a no-bid auction superseded at ingest —
    # is not a canceled mission and does not ring the bell. The mission is
    # WAITING: the queue shows it, and it "raises one alert after a
    # threshold" (waiting_missions.py) — never one line per attempt, which
    # for a mission that waits an hour would be one every minute.
    if wants_redispatch(
        task_state.status,
        task_state.cancellation.labels if task_state.cancellation else None,
    ):
        return None
    task_id = task_state.booking.id
    # F-379: the fleet's own tasks (charging trips, holds, retreats) page
    # the operator only when they FAIL — a cancel is the fleet at work
    if not pages_operator(task_id, task_state.status == mdl.TaskStatus.failed):
        return None
    existing = await repo.get_alert(task_id)
    if existing is not None and (
        existing.unix_millis_resolved_time is not None
        or TASK_LOG_ERROR_TEXT not in (existing.message or "")
    ):
        return None
    assigned = task_state.assigned_to
    reason = None
    if task_state.dispatch is not None:
        reason = no_bid_final_reason(task_state) or dispatch_failure_reason(
            task_state.dispatch.errors
        )
    message = f"Task {task_id} {task_state.status.value}"
    if task_state.status == mdl.TaskStatus.failed and reason:
        message = f"{message}: {reason}"
    return await repo.create_alert(
        task_id,
        "task",
        severity=(
            ttm.Alert.Severity.Critical
            if task_state.status == mdl.TaskStatus.failed
            else ttm.Alert.Severity.Info
        ),
        fleet=assigned.group if assigned is not None else None,
        robot=assigned.name if assigned is not None else None,
        message=message,
    )


async def alert_on_task_log(task_log: mdl.TaskEventLog, repo):
    """A task whose event log carries an error alerts ONCE (F-374). The
    first cut re-created the row on every log update with an error: the
    same id as the task's failed/canceled alert, and create_alert resets
    the ack and the resolution — so a task alert the operator resolved
    came back on the next log update ("the bell will not clear"), and a
    failure alert's reason was overwritten by this generic line."""
    if not task_log_has_error(task_log):
        return None
    # F-379: an error line in the fleet's own task's log is not a failure;
    # if that task does fail, its terminal state raises the alert
    if not pages_operator(task_log.task_id, failed=False):
        return None
    if await repo.alert_exists(task_log.task_id):
        return None
    return await repo.create_alert(
        task_log.task_id,
        "task",
        severity=ttm.Alert.Severity.Critical,
        message=f"Task {task_log.task_id} {TASK_LOG_ERROR_TEXT}",
    )


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
    # F-268: a robot whose position feed has frozen has not moved less
    # than STUCK_MOVE_EPSILON — it has not been SEEN. Judging it stuck
    # would page an operator about an instrument fault dressed as a robot
    # fault; the sentinel and the map already say what is really wrong.
    # The episode is dropped, not paused: when the feed resumes the robot
    # is wherever it actually is, and the clock starts from there.
    from api_server.routes.fleets import position_is_stale  # noqa: PLC0415

    fleet_name, _, robot_name = robot_id.partition("/")
    if position_is_stale(fleet_name, robot_name):
        state = _stuck_states.pop(robot_id, None)
        resolved = state.alert_id if state is not None else None
        return None, resolved
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
        # The prefix is NOT the test: the adapter also raises
        # `robot_blocked_by_no_go` and `robot_waiting_on_occupied_waypoint`,
        # which are Warning-tier traffic conditions, not faults. Treating
        # them as faults told the operator a queueing robot was faulted and
        # that "its missions were canceled" (both false), and suppressed
        # that robot's low-battery alert — a draining robot stuck in a
        # queue lost its battery warning, one ingredient of the F-112
        # cascade. Only the adapter's real fault keys count (F-38/F-40/
        # FR-29): robot_offline, robot_unresponsive, robot_down.
        fault_categories = sorted(
            {
                str(issue.category)
                for issue in (robot.issues or [])
                if str(issue.category or "") in ROBOT_FAULT_CATEGORIES
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
        # F-139: episode still stuck (tracker holds an alert id and did not
        # re-arm this tick) but the row was resolved — re-raise after grace
        stuck_state = _stuck_states.get(robot_id)
        if (
            stuck_new is None
            and stuck_state is not None
            and stuck_state.alert_id is not None
            and await _refire_due(stuck_state.alert_id, now_millis)
        ):
            stuck_new = (
                f"robot_stuck__{fleet_state.name}__{robot_name}__{now_millis}"
            )
            stuck_state.alert_id = stuck_new
        # F-139: battery still below threshold but the row was resolved —
        # re-raise after grace (skipped while faulted: F-42 spoofs SoC 0)
        open_low = _low_battery_alerted.get(robot_id)
        if (
            battery_new is None
            and open_low is not None
            and not fault_categories
            and robot.battery is not None
            and robot.battery < app_config.low_battery_threshold
            and await _refire_due(open_low, now_millis)
        ):
            battery_new = (
                f"low_battery__{fleet_state.name}__{robot_name}__{now_millis}"
            )
            _low_battery_alerted[robot_id] = battery_new
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
        # F-139: fault persists but the row was resolved — re-raise after
        # grace by dropping the episode entry so the create branch runs
        if (
            fault_categories
            and fault_alert_id is not None
            and await _refire_due(fault_alert_id, now_millis)
        ):
            _fault_alerted.pop(robot_id, None)
            fault_alert_id = None
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

    await process_fr36_conditions(fleet_state, now_millis)
    await process_charger_conditions(fleet_state, now_millis)
    await process_stranded_conditions(fleet_state, now_millis)


def _fr36_message(category: str, detail: dict) -> str:
    tasks = detail.get("tasks") or []
    missions = (
        f" Missions affected: {', '.join(tasks)}." if tasks else ""
    )
    if category == "fleet_deadlock":
        cycle = detail.get("cycle") or []
        arrows = " -> ".join(cycle + cycle[:1])
        return (
            f"Deadlock detected: {arrows} — each robot is waiting for "
            f"space held by the next; no yield can resolve it. "
            f"Resolving automatically.{missions}"
        )
    if category == "fleet_blocked":
        waiting = ", ".join(detail.get("waiting") or [])
        return (
            f"{detail.get('blocker')} is idle on a waypoint that "
            f"{waiting} need{'s' if ',' not in waiting else ''} — moving "
            f"it to a clear waypoint.{missions}"
        )
    where = detail.get("where") or ""
    waiting = ", ".join(detail.get("waiting") or [])
    return (
        f"Blocked route needs an operator: {detail.get('reason')}. "
        f"Blocker: {detail.get('blocker')} {where}. Waiting: {waiting}."
        f"{missions}"
    )


async def process_fr36_conditions(
    fleet_state: mdl.FleetState, now_millis: int
) -> None:
    """One alert per FR-36 condition episode, resolved when the episode
    clears; each resolution ACTION additionally lands as one info row so
    the operator sees what the fleet did on their behalf."""
    fleet = fleet_state.name
    if fleet not in _fr36_stale_swept:
        _fr36_stale_swept.add(fleet)
        await alert_repo.resolve_alerts_by_prefix(f"fr36__{fleet}__")
    live: Dict[str, Tuple[str, dict, str]] = {}
    for robot_name, robot in (fleet_state.robots or {}).items():
        for issue in robot.issues or []:
            category = str(issue.category or "")
            if category not in FR36_CATEGORIES:
                continue
            detail = issue.detail if isinstance(issue.detail, dict) else {}
            episode = str(detail.get("episode") or "")
            if not episode:
                continue
            previous = live.get(episode)
            # escalation supersedes the condition row it grew out of
            if previous is None or category == "fleet_blocked_escalation":
                live[episode] = (category, detail, robot_name)
    # alert ids become URL path segments ("/alerts/{id}/resolve") — a
    # slash inside the episode key breaks the route, leaving the row
    # unresolvable (found live: three action rows stuck open).
    live = {f"{fleet}--{episode}".replace("/", "-"): value
            for episode, value in live.items()}
    for episode, (category, detail, robot_name) in live.items():
        action = str(detail.get("action") or "")
        if action and _fr36_actions.get(episode) != action:
            _fr36_actions[episode] = action
            alert = await alert_repo.create_alert(
                f"fr36__{fleet}__{episode}__action_{now_millis}",
                "fleet",
                severity=ttm.Alert.Severity.Info,
                fleet=fleet,
                robot=robot_name,
                message=f"Traffic recovery: {action}.",
            )
            if alert is not None:
                alert_events.alerts.on_next(alert)
        alerted = _fr36_alerted.get(episode)
        if alerted is not None and alerted[1] == category:
            # F-139: the condition episode is still live but its row was
            # resolved — re-raise the same category as a fresh row after
            # grace (an operator resolving a live escalation must not
            # silence it while robots are still blocked)
            if await _refire_due(alerted[0], now_millis):
                _fr36_alerted.pop(episode, None)
                alerted = None
            else:
                continue
        if alerted is not None:  # category changed (escalation)
            resolved = await alert_repo.resolve_alert(alerted[0])
            if resolved is not None:
                alert_events.alerts.on_next(resolved)
        alert_id = f"fr36__{fleet}__{episode}__{now_millis}"
        _fr36_alerted[episode] = (alert_id, category)
        severity = (
            ttm.Alert.Severity.Warning
            if category == "fleet_blocked"
            else ttm.Alert.Severity.Critical
        )
        alert = await alert_repo.create_alert(
            alert_id,
            "fleet",
            severity=severity,
            fleet=fleet,
            robot=robot_name,
            message=_fr36_message(category, detail),
        )
        if alert is not None:
            alert_events.alerts.on_next(alert)
    for episode in list(_fr36_alerted):
        if episode in live or not episode.startswith(f"{fleet}--"):
            continue
        alert_id, _ = _fr36_alerted.pop(episode)
        _fr36_actions.pop(episode, None)
        resolved = await alert_repo.resolve_alert(alert_id)
        if resolved is not None:
            alert_events.alerts.on_next(resolved)


# F-338 / F-337 (G ruling 2026-09-19): the fleet adapter raises ONE issue
# per episode when a robot cannot reach its charger (a cordon in the way)
# or is on a charger that does not charge it. Each becomes ONE operator
# alert naming the robot, the charger and what is in the way, resolved
# when the adapter drops the issue.
CHARGER_CATEGORIES = frozenset(
    {"charger_unreachable", "charger_unreachable_critical", "charger_dead"}
)
_charger_alerted: Dict[str, Tuple[str, str]] = {}  # episode -> (alert_id, cat)
_charger_stale_swept: set = set()


def _charger_message(category: str, detail: dict) -> str:
    robot = str(detail.get("robot") or "?")
    charger = str(detail.get("charger") or "its charger")
    minutes = detail.get("minutes_to_floor")
    left = (f" It has about {int(minutes)} min of charge left."
            if isinstance(minutes, (int, float)) and minutes >= 0 else "")
    if category == "charger_unreachable_critical":
        # F-345: the rescue line is crossed while held — the robot can no
        # longer reach its charger above the arrival floor even with the
        # way open. The hold stands; only the operator can still act.
        lanes = detail.get("lanes") or []
        via = (f" — lanes {list(lanes)} are closed" if lanes
               else " — no route to it on the current graph")
        return (
            f"URGENT: {robot} is held with no route to its charger [{charger}]"
            f"{via} and is now past the point where it could still get there"
            f" on its own.{left} It will stop where it stands. Reopen the "
            f"lanes NOW, or send it somewhere it can reach from the robot's "
            f"page."
        )
    if category == "charger_unreachable":
        lanes = detail.get("lanes") or []
        via = (f" — lanes {list(lanes)} are closed" if lanes
               else " — no route to it on the current graph")
        # The remedy is stated in full every time. The conditional tail
        # this replaced ("cancel its hold task <id> first") depended on a
        # hold_task the adapter could not yet know when the issue is
        # raised, so in practice it never appeared and the operator was
        # told to move a robot whose hold task would refuse them — and a
        # raw task id was never the thing to act on anyway: the robot's
        # own page is (F-338 UI review, 2026-09-19).
        return (
            f"{robot} cannot reach its charger [{charger}]{via}. It is holding "
            f"where it is and taking no work.{left} Reopen the lanes, or send "
            f"it somewhere it can reach from the robot's page — the fleet "
            f"releases the hold for that trip."
        )
    return (
        f"{robot} is on its charger [{charger}] and is NOT charging — its "
        f"battery has been falling while docked (SoC "
        f"{detail.get('soc', '?')}).{left} Check the charger's power; there is "
        f"nowhere else the fleet can send it."
    )


async def process_charger_conditions(
    fleet_state: mdl.FleetState, now_millis: int
) -> None:
    """One alert per charger-condition episode, resolved when it clears
    (the same shape as process_fr36_conditions, kept separate so a
    charger alert can never be mistaken for a traffic one)."""
    fleet = fleet_state.name
    if fleet not in _charger_stale_swept:
        _charger_stale_swept.add(fleet)
        await alert_repo.resolve_alerts_by_prefix(f"charger__{fleet}__")
    live: Dict[str, Tuple[str, dict, str]] = {}
    for robot_name, robot in (fleet_state.robots or {}).items():
        for issue in robot.issues or []:
            category = str(issue.category or "")
            if category not in CHARGER_CATEGORIES:
                continue
            detail = issue.detail if isinstance(issue.detail, dict) else {}
            episode = str(detail.get("episode") or "")
            if not episode:
                continue
            live[f"{fleet}--{episode}".replace("/", "-")] = (
                category, detail, robot_name)
    for episode, (category, detail, robot_name) in live.items():
        alerted = _charger_alerted.get(episode)
        if alerted is not None and alerted[1] == category:
            if await _refire_due(alerted[0], now_millis):
                _charger_alerted.pop(episode, None)
                alerted = None
            else:
                continue
        if alerted is not None:
            resolved = await alert_repo.resolve_alert(alerted[0])
            if resolved is not None:
                alert_events.alerts.on_next(resolved)
        alert_id = f"charger__{fleet}__{episode}__{now_millis}"
        _charger_alerted[episode] = (alert_id, category)
        alert = await alert_repo.create_alert(
            alert_id,
            "robot",
            severity=ttm.Alert.Severity.Critical,
            fleet=fleet,
            robot=robot_name,
            message=_charger_message(category, detail),
        )
        if alert is not None:
            alert_events.alerts.on_next(alert)
    for episode in list(_charger_alerted):
        if episode in live or not episode.startswith(f"{fleet}--"):
            continue
        alert_id, _ = _charger_alerted.pop(episode)
        resolved = await alert_repo.resolve_alert(alert_id)
        if resolved is not None:
            alert_events.alerts.on_next(resolved)


# F-391 (G ruling 2026-09-28): a robot the fleet has REFUSED TO RECOVER.
# The adapter tried to settle it back to the route network and could not —
# every way home crosses a keep-out, or nothing it may rest on is
# reachable — so only a person can free it. One alert per episode, the
# same shape as the charger conditions, resolved when the adapter drops
# the issue. Before this, the condition was diagnosed in the adapter log
# and nowhere else: the overview read "Idle · No active task" and the
# bell was empty while the robot sat stuck (measured 2026-09-22).
STRANDED_CATEGORY = "robot_stranded_off_graph"
_stranded_alerted: Dict[str, str] = {}  # episode -> alert_id
_stranded_stale_swept: set = set()


def _stranded_message(detail: dict) -> str:
    robot = str(detail.get("robot") or "?")
    zone = str(detail.get("zone") or "")
    position = detail.get("position")
    where = (
        f" at ({float(position[0]):.1f}, {float(position[1]):.1f})"
        if isinstance(position, (list, tuple)) and len(position) >= 2
        else ""
    )
    why = (
        f" — every way back to the route network crosses the keep-out "
        f"area [{zone}]"
        if zone
        else " — no waypoint it may rest on can be reached from where it is"
    )
    return (
        f"{robot} is off the route network{where} and the fleet cannot "
        f"drive it back{why}. It is taking no work and nothing the fleet "
        f"can do will move it: someone must move the robot clear of the "
        f"area, onto a lane, or remove the zone from Site settings."
    )


async def process_stranded_conditions(
    fleet_state: mdl.FleetState, now_millis: int
) -> None:
    """One alert per stranded episode, resolved when the robot is back on
    the graph (the adapter drops the issue)."""
    fleet = fleet_state.name
    if fleet not in _stranded_stale_swept:
        # a previous server life's episodes cannot be resolved by this one
        _stranded_stale_swept.add(fleet)
        await alert_repo.resolve_alerts_by_prefix(f"stranded__{fleet}__")
    live: Dict[str, Tuple[dict, str]] = {}
    for robot_name, robot in (fleet_state.robots or {}).items():
        for issue in robot.issues or []:
            if str(issue.category or "") != STRANDED_CATEGORY:
                continue
            detail = issue.detail if isinstance(issue.detail, dict) else {}
            episode = str(detail.get("episode") or "")
            if not episode:
                continue
            live[f"{fleet}--{episode}".replace("/", "-")] = (detail, robot_name)
    for episode, (detail, robot_name) in live.items():
        if episode in _stranded_alerted:
            if not await _refire_due(_stranded_alerted[episode], now_millis):
                continue
            _stranded_alerted.pop(episode, None)
        alert_id = f"stranded__{fleet}__{episode}__{now_millis}"
        _stranded_alerted[episode] = alert_id
        alert = await alert_repo.create_alert(
            alert_id,
            "robot",
            severity=ttm.Alert.Severity.Critical,
            fleet=fleet,
            robot=robot_name,
            message=_stranded_message(detail),
        )
        if alert is not None:
            alert_events.alerts.on_next(alert)
    for episode in list(_stranded_alerted):
        if episode in live or not episode.startswith(f"{fleet}--"):
            continue
        alert_id = _stranded_alerted.pop(episode)
        resolved = await alert_repo.resolve_alert(alert_id)
        if resolved is not None:
            alert_events.alerts.on_next(resolved)


def _reset_stranded_for_test() -> None:
    _stranded_alerted.clear()
    _stranded_stale_swept.clear()


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


_run_boundary = RunBoundary()
INTERRUPTED_CLOSE_LIMIT = 200


async def reap_interrupted_tasks() -> None:
    """F-141 (E6 run-2 blocker 3): after a fleet coordination restart,
    close every task row the restarted core no longer knows — the
    ledger keeps missions that terminate honestly, never 'Executing'
    phantoms that nothing can cancel. Fires once per outage epoch, a
    grace period after the fleet stream resumes (everything the core
    still knows has re-announced by then)."""
    now_mono = time.monotonic()
    if not _run_boundary.due(now_mono):
        return
    _run_boundary.mark_reaped()
    epoch = _run_boundary.epoch_started_wall
    assert epoch is not None
    await _close_interrupted_rows(epoch)


# F-141 addendum (Act-3 stress, 2026-08-26): a coordination restart
# FASTER than FLEET_SILENCE_GAP leaves no detectable outage boundary
# (the in-container drill-2 respawns in ~4 s), yet the new core still
# knows nothing about the old rows — four missions starved as
# un-cancelable 'underway' phantoms. Class rule: fleet states are
# FLOWING while a non-terminal row goes untouched this long — the core
# is talking, just not about this task.
STALE_TASK_SWEEP_AGE = 300.0
_stale_sweep_last = {"t": 0.0}


async def sweep_stale_tasks() -> None:
    """Close non-terminal rows no live core is updating (see the F-141
    addendum note above). Same honest provenance as the boundary reap;
    auto ChargeBattery ghosts stay the F-12 reaper's job."""
    now_mono = time.monotonic()
    if now_mono - _stale_sweep_last["t"] < 60.0:
        return
    _stale_sweep_last["t"] = now_mono
    epoch = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_TASK_SWEEP_AGE)
    await _close_interrupted_rows(epoch)


async def _tasks_named_by_fleet_states() -> set:
    try:
        rows = await ttm.FleetState.all()
        return tasks_named_by(
            row.data if isinstance(row.data, dict) else {} for row in rows)
    except Exception:  # noqa: BLE001 — cannot see, so cannot exempt
        return set()


async def _close_interrupted_rows(epoch) -> None:
    # exclude terminal rows IN the query: updated_at__lt alone matches
    # every historic row, and the unordered LIMIT then never reaches
    # the live ghosts (found in the Act-3 stress: 4 starved rows,
    # 200-row page full of old completed missions). The column stores
    # the enum repr ('Status.underway'), so match both spellings.
    terminal = [spelling
                for s in ("completed", "failed", "canceled",
                          "killed", "skipped")
                for spelling in (s, f"Status.{s}")]
    rows = await ttm.TaskState.filter(
        updated_at__lt=epoch).exclude(
        status__in=terminal).limit(INTERRUPTED_CLOSE_LIMIT)
    closed = []
    # F-343 (f1-n33): a running task whose next stop has no route goes
    # SILENT — the fleet re-broadcasts only on change — and this sweep
    # closed it as "fleet coordination restarted; the core no longer
    # tracks it" while the robot's own fleet state still named it as its
    # current task. The claim is checked before it is written.
    tracked = await _tasks_named_by_fleet_states()
    for row in rows:
        if str(row.id_).startswith("Charge"):
            continue  # F-12 reaper's jurisdiction
        if not is_interrupted_row(row.status, row.updated_at, epoch):
            continue
        if str(row.id_) in tracked:
            continue  # the core DOES track it: not interrupted, just quiet
        try:
            task_state = mdl.TaskState(**row.data)
        except Exception:  # noqa: BLE001 — a corrupt row must not stop the sweep
            logger.error(f"F-141: cannot parse task row {row.id_}")
            continue
        task_state.status = mdl.TaskStatus.failed
        labels = list(task_state.booking.labels or [])
        if INTERRUPTED_LABEL not in labels:
            labels.append(INTERRUPTED_LABEL)
        task_state.booking.labels = labels
        await task_repo.save_task_state(task_state)
        task_events.task_states.on_next(task_state)
        closed.append(row.id_)
        logger.warning(
            f"F-141: closed interrupted mission {row.id_} "
            "(fleet coordination restarted; the core no longer tracks "
            "it)")
    if closed:
        shown = ", ".join(closed[:6]) + (
            f" and {len(closed) - 6} more" if len(closed) > 6 else "")
        alert = await alert_repo.create_alert(
            f"interrupted__{round(datetime.now().timestamp() * 1e3)}",
            "fleet",
            severity=ttm.Alert.Severity.Warning,
            message=(
                f"{len(closed)} mission(s) running before the fleet "
                "coordination restart did not resume and were closed "
                f"as failed: {shown}. Re-dispatch what is still "
                "needed."
            ),
        )
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
        # F-71(2): latch/stamp cancellation provenance BEFORE persisting so
        # stored rows and broadcasts agree (the canceled-vs-completed race
        # on the dead-robot path can wipe RMF's own field)
        task_cancellation.apply(task_state)
        # F-435: an attempt an operator canceled while its mission waited
        # stays that operator's cancel when the fleet re-sends it
        _apply_withdrawal(task_state)
        # F-343: `completed` is stored only when every phase is completed —
        # the fleet reports the ACTIVE phase's status as the task's, and a
        # patrol whose next stop had no route read "completed" for eight
        # minutes with that stop never begun and the robot still working.
        phantom_completion.apply(task_state)
        # F-295: the fleet adapter's task_state_update carries NO booking
        # labels on this pin (convert() never parses them), so the first
        # fleet update after an award erased every label the request
        # came with — FR-5 priority, the drill's markers, and the
        # gf:redispatch-of provenance the morning-after row matches on.
        # The stored REQUEST is the truth; stamp its labels back.
        await _preserve_booking_labels(task_state)
        # G ruling 2026-10-01 item 6 (F-410/F-412 class): "A timed-out
        # auction never becomes a failed mission." An auction that closed
        # with no bid and still has attempts left is recorded as put back
        # on the floor — canceled, with the gf:redispatch marker and the
        # plain reason, the dispatcher's errors kept — BEFORE anything is
        # stored, broadcast or alerted on, so no consumer ever sees it as
        # a failure. After the labels are restored: the attempt count
        # rides in them. _redispatch_later auctions it again.
        supersede(task_state)
        # F-292: the dispatcher AWARDS a task it canceled in flight while
        # its bidding was still open (measured: canceled 01:55:45, awarded
        # to gentle_bot_1 01:55:51), and the queued future mission then
        # made the fleet planner fail unrelated dispatches. A task whose
        # cancel was already requested and that now arrives from the
        # fleet NON-terminal with a robot is canceled again, at the fleet.
        await _follow_through_cancel(task_state)
        await task_repo.save_task_state(task_state)
        task_events.task_states.on_next(task_state)

        # FR-12 charge governor (F-36/F-286): a mission the fleet canceled
        # to charge its robot comes back to the floor for another robot —
        # after a settle delay, off this handler (F-291), so the fleet
        # feed is never held up behind a dispatch. The same hook auctions
        # a mission nobody bid on again, after its backoff.
        asyncio.get_running_loop().create_task(_redispatch_later(task_state))

        # F-22: alerts are exceptions (FR-17) - a cleanly completed task must
        # NOT leave an open alert. The upstream completed-task alert grew
        # monotonically with dispatch count during the soak (81 open alerts
        # after 2.5 h of traffic) and buried the real ones. Only failed and
        # canceled tasks alert; terminal states may be re-broadcast, so alert
        # once per task.
        alert = await alert_on_task_state(task_state, alert_repo)
        if alert is not None:
            alert_events.alerts.on_next(alert)

    elif payload_type == "task_log_update":
        task_log = mdl.TaskEventLog(**msg["data"])
        await task_repo.save_task_log(task_log)
        task_events.task_event_logs.on_next(task_log)

        alert = await alert_on_task_log(task_log, alert_repo)
        if alert is not None:
            alert_events.alerts.on_next(alert)

    elif payload_type == "fleet_state_update":
        # F-395 (G ruling 2026-09-29, D-84): the adapter pushes at the rate
        # cap; a change an operator can see goes to the dashboards at once,
        # and the database, the heartbeat, the alert rules and the reapers
        # keep the 1 s cycle they had when the adapter pushed once a second.
        emit, full = fleet_state_cadence.decide(msg["data"], time.monotonic())
        if not emit:
            return
        fleet_state = mdl.FleetState(**msg["data"])
        if not full:
            fleet_events.fleet_states.on_next(fleet_state)
            return
        await fleet_repo.save_fleet_state(fleet_state)
        fleet_events.fleet_states.on_next(fleet_state)
        # feeds the health watchdog's robot heartbeats (FR-17 robot offline)
        rmf_events.fleet_states.on_next(fleet_state)
        await process_robot_alerts(fleet_state)
        await reap_charge_ghosts(fleet_state)
        _run_boundary.observe(time.monotonic(),
                              datetime.now(timezone.utc))
        await reap_interrupted_tasks()
        await sweep_stale_tasks()

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
            # F-138 (E6 run-2 blocker 2): when rmf-core dies mid-flood,
            # uvicorn CANCELS this handler while process_msg sits inside
            # an open DB transaction — the rollback never runs and the
            # connection is returned to the pool 'idle in transaction',
            # one per restart wave, until the pool starves and every DB
            # endpoint (alerts included) hangs silently. Shield the
            # processing so the transaction always completes (commit or
            # rollback) even when the socket task is cancelled; the
            # cancellation is re-raised immediately after.
            await shielded(process_msg(msg, fleet_repo))
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
    what this adds is the honest record.

    F-387 (D-82): the guard no longer counts the fleet's own task on a
    stationary robot as a mission, so an apply or upgrade can restart
    over it without a hard-confirm — and the restart would leave its row
    to the F-77 janitor as a "failed" ChargeBattery that never failed.
    `scope`: "all" (the default, the hard-confirmed path) cancels every
    non-terminal task; "fleet" cancels only the fleet's own stationary
    tasks, which the sidecar and the upgrade gate send whenever they
    restart WITHOUT a hard-confirm. Each is labelled for what it is."""
    from datetime import datetime as _datetime

    from api_server import models as _mdl
    from api_server.models.rmf_api.task_state import Cancellation
    from api_server.rmf_io import cancellation as _task_cancellation
    from api_server.rmf_io import tasks_service
    from api_server.routes.site_config import FLEET_TASK_NOTE, mission_census

    _require_internal_token(request)
    body = await request.json()
    applied_by = str(body.get("applied_by") or "admin")
    scope = str(body.get("scope") or "all")
    if scope not in ("all", "fleet"):
        raise HTTPException(422, f"unknown scope '{scope}' (all | fleet)")
    mission_label = (
        "Interrupted by a site configuration change " f"(applied by {applied_by})"
    )
    fleet_label = (
        f"Ended by a coordination restart — {FLEET_TASK_NOTE} "
        f"(applied by {applied_by})"
    )
    census = await mission_census()
    work = [(m, fleet_label) for m in census["fleet_tasks"]]
    if scope == "all":
        work = [(m, mission_label) for m in census["missions"]] + work
    missions = [m for m, _ in work]
    for mission, label in work:
        task_id = str(mission.get("task_id") or "")
        if not task_id:
            continue
        _task_cancellation.latch(
            task_id,
            Cancellation(
                unix_millis_request_time=round(_datetime.now().timestamp() * 1e3),
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
        "D-24: %d task(s) canceled ahead of a site-change restart "
        "(scope %s, applied by %s; %d of them the fleet's own, F-387)",
        len(missions),
        scope,
        applied_by,
        len(census["fleet_tasks"]),
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
    logger.info("D-24 evacuation commanded: %s -> %s", body["robot"], body["waypoint"])
    return {"ok": True}
