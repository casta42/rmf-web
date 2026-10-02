"""F-465 (G ruling 2026-10-02, second sheet, item 2): a replayed dispatch
request never creates a mission — deduplicated by request id against the
ledger.

`task_api_requests` is latched (upstream's QoS). A restarted dispatcher is
handed this api-server's last requests again, takes a `dispatch_task_request`
among them as new, and answers it with a NEW task id: on release f1-n68 a
phantom mission appeared 16 to 46 ms after every dispatcher start, and was
counted as a mission failed by allocation. Had a fleet answered its auction,
a robot would have run a copy of the last mission dispatched.

The dispatcher's own memory of request ids does not survive its restart;
the ledger does. Every dispatch request is recorded before it is published
(models DispatchRequest) and closed with the task the dispatcher gave it.
A response that names a request the ledger has CLOSED — answered with
another task, or given up — is a replay's: its task is canceled at the
dispatcher (the same service an operator's cancel uses, F-285; at the
fleet, if it was already awarded) and its row is removed, so it is never a
mission. Its first state reaches the ledger before its response does (the
dispatcher publishes them in that order), so the row is deleted, not
merely refused; later states of that id are dropped.

Never a conviction without the ledger's word (the both-ways rule):
  - a request id the ledger does not know is not ours (another client of
    the dispatcher) and is left alone;
  - a request still pending is the live answer arriving, not a replay;
  - the same task id answered again is the dispatcher repeating itself;
  - a replay's task is removed from the ledger only once its cancel is
    CONFIRMED — if it cannot be canceled it stays a visible mission, with
    an error line, rather than a robot working unseen.
"""

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Tuple
from uuid import uuid4

# a child of the app's logger (api_server/logger.py configures "fastapi"):
# its lines carry a time stamp in the server's log, which a plain
# module logger's do not (found on FR-39a's alert lines, f1-n68)
logger = logging.getLogger("fastapi.DispatchLedger")

PENDING = "pending"
ANSWERED = "answered"
CLOSED = "closed"

NOT_OURS = "not-ours"
STILL_PENDING = "pending"
SAME = "same-answer"
REPLAY = "replay"

# how long the ledger keeps a request: far longer than a latched topic can
# replay one (its last ten requests, within one life of this server)
KEEP_S = 7 * 24 * 3600.0
# ... and how many of the newest it keeps WHATEVER their age: the latched
# topic replays by count, not by age (its last ten requests, for as long as
# this server lives) — on a quiet site a week-old dispatch is still there.
KEEP_NEWEST = 200
CANCEL_TRIES = 4
PENDING_RECHECK_S = 1.0
CANCEL_RETRY_S = 0.5


def new_request_id() -> str:
    return str(uuid4())


def judge(
    row_outcome: Optional[str], row_task_id: Optional[str], response_task_id: str
) -> str:
    """What a dispatcher's answer means, given the ledger's row for its
    request id (None: no such row)."""
    if row_outcome is None:
        return NOT_OURS
    if row_outcome == PENDING:
        return STILL_PENDING
    if row_outcome == ANSWERED and row_task_id == response_task_id:
        return SAME
    return REPLAY


def task_of_response(json_msg: str) -> Optional[str]:
    """The task id a dispatch answer names, or None when the message is
    not a successful dispatch answer."""
    try:
        body = json.loads(json_msg)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("success") is not True:
        return None
    state = body.get("state")
    booking = state.get("booking") if isinstance(state, dict) else None
    task_id = booking.get("id") if isinstance(booking, dict) else None
    return task_id if isinstance(task_id, str) and task_id else None


class Refused:
    """Task ids made by replays. Thread-safe and bounded."""

    def __init__(self, limit: int = 4096):
        self._lock = threading.Lock()
        self._ids: "OrderedDict[str, str]" = OrderedDict()
        self._limit = limit

    def add(self, task_id: str, request_id: str) -> None:
        with self._lock:
            self._ids[task_id] = request_id
            while len(self._ids) > self._limit:
                self._ids.popitem(last=False)

    def discard(self, task_id: str) -> None:
        with self._lock:
            self._ids.pop(task_id, None)

    def __contains__(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._ids

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)


REFUSED = Refused()


# ---- the ledger's rows ---------------------------------------------------


async def opened(request_id: str) -> None:
    # pylint: disable=import-outside-toplevel
    from api_server.models.tortoise_models import DispatchRequest

    await DispatchRequest.create(request_id=request_id, outcome=PENDING)


async def answered(request_id: str, task_id: str) -> None:
    # pylint: disable=import-outside-toplevel
    from api_server.models.tortoise_models import DispatchRequest

    await DispatchRequest.filter(request_id=request_id).update(
        outcome=ANSWERED, task_id=task_id, closed_at=datetime.now(timezone.utc)
    )


async def closed(request_id: str, why: str) -> None:
    # pylint: disable=import-outside-toplevel
    from api_server.models.tortoise_models import DispatchRequest

    await DispatchRequest.filter(request_id=request_id).update(
        outcome=CLOSED, why=why[:255], closed_at=datetime.now(timezone.utc)
    )


async def prune(keep_s: float = KEEP_S, keep_newest: int = KEEP_NEWEST) -> int:
    # pylint: disable=import-outside-toplevel
    from api_server.models.tortoise_models import DispatchRequest

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=keep_s)
    newest = (
        await DispatchRequest.all()
        .order_by("-sent_at")
        .limit(keep_newest)
        .values_list("request_id", flat=True)
        if keep_newest > 0
        else []
    )
    old = DispatchRequest.filter(sent_at__lt=cutoff)
    if newest:
        old = old.exclude(request_id__in=list(newest))
    return await old.delete()


async def _row(request_id: str) -> Tuple[Optional[str], Optional[str]]:
    # pylint: disable=import-outside-toplevel
    from api_server.models.tortoise_models import DispatchRequest

    row = await DispatchRequest.get_or_none(request_id=request_id)
    return (None, None) if row is None else (row.outcome, row.task_id)


async def _note_refused(request_id: str, task_id: str) -> None:
    # pylint: disable=import-outside-toplevel
    from api_server.models.tortoise_models import DispatchRequest

    row = await DispatchRequest.get_or_none(request_id=request_id)
    if row is None:
        return
    seen = list(row.refused_tasks) if isinstance(row.refused_tasks, list) else []
    if task_id not in seen:
        seen.append(task_id)
        row.refused_tasks = seen
        await row.save()


# ---- the guard -----------------------------------------------------------


async def handle_unclaimed(
    request_id: str,
    json_msg: str,
    cancel: Callable[[str], Awaitable[Optional[str]]],
    remove_row: Callable[[str], Awaitable[int]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    row_of: Optional[Callable[[str], Awaitable[Tuple]]] = None,
    note: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> Optional[str]:
    """One answer nobody was waiting for. `cancel(task_id)` cancels the
    task wherever it is and returns how ("dispatcher", "fleet") or None;
    `remove_row(task_id)` deletes its ledger row and returns how many.
    `row_of` and `note` are the ledger (seams for tests). Returns the
    verdict."""
    row_of = _row if row_of is None else row_of
    note = _note_refused if note is None else note
    task_id = task_of_response(json_msg)
    if task_id is None:
        return None
    outcome, known_task = await row_of(request_id)
    what = judge(outcome, known_task, task_id)
    if what == STILL_PENDING:
        # nobody is waiting for it and the ledger still says pending: the
        # caller has just given up (its timeout) and is about to close the
        # row. Look once more before calling it the live answer.
        await sleep(PENDING_RECHECK_S)
        outcome, known_task = await row_of(request_id)
        what = judge(outcome, known_task, task_id)
    if what != REPLAY:
        return what
    # the id is refused from this moment: no state of it is stored again
    REFUSED.add(task_id, request_id)
    how = None
    for attempt in range(CANCEL_TRIES):
        how = await cancel(task_id)
        if how is not None:
            break
        if attempt + 1 < CANCEL_TRIES:
            await sleep(CANCEL_RETRY_S)
    if how is None:
        # not canceled: it may be auctioned and run. It must be SEEN.
        REFUSED.discard(task_id)
        logger.error(
            "F-465: the dispatcher made task [%s] from a REPLAYED request "
            "[%s] (the ledger closed it%s) and it could NOT be canceled — "
            "it is left a visible mission; cancel it by hand",
            task_id,
            request_id,
            f" as [{known_task}]" if known_task else " with no mission",
        )
        return "replay-not-canceled"
    removed = await remove_row(task_id)
    await note(request_id, task_id)
    logger.warning(
        "F-465: the dispatcher made task [%s] from a REPLAYED request [%s] "
        "(the ledger closed it%s) — canceled at the %s, %s; it is not a "
        "mission",
        task_id,
        request_id,
        f" as [{known_task}]" if known_task else " with no mission",
        how,
        "its row removed" if removed else "no row had been written",
    )
    # its first state may have been on its way into the ledger when the id
    # was refused: look once more, after any such write has landed
    await sleep(CANCEL_RETRY_S)
    if await remove_row(task_id):
        logger.warning(
            "F-465: a state of the replay's task [%s] was written while it "
            "was being refused — removed",
            task_id,
        )
    return REPLAY


# The ROS thread hands unclaimed answers to the app's loop through here.
_loop: Optional[asyncio.AbstractEventLoop] = None
_inbox: Optional[asyncio.Queue] = None


def on_unclaimed_response(request_id: str, json_msg: str) -> None:
    """Called from the rclpy thread (rmf_service.py) for every answer no
    caller is waiting for. Cheap: only successful dispatch answers go on."""
    loop, inbox = _loop, _inbox
    if loop is None or inbox is None:
        return
    if '"state"' not in json_msg:
        return
    try:
        loop.call_soon_threadsafe(inbox.put_nowait, (request_id, json_msg))
    except RuntimeError:
        pass  # the loop is closing


async def _cancel_anywhere(task_id: str) -> Optional[str]:
    """Cancel a replay's task: at the dispatcher while it is being
    auctioned, at the fleet when it was already awarded."""
    # pylint: disable=import-outside-toplevel
    from rmf_task_msgs.srv import CancelTask as RmfCancelTask

    from api_server.gateway import rmf_gateway
    from api_server.rmf_io import tasks_service

    client = rmf_gateway().cancel_task_client
    if client.service_is_ready():
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        ros_future = client.call_async(
            RmfCancelTask.Request(requester="gentlefleet-replay-guard", task_id=task_id)
        )
        ros_future.add_done_callback(
            lambda f: loop.call_soon_threadsafe(
                lambda: done.done() or done.set_result(f)
            )
        )
        try:
            finished = await asyncio.wait_for(done, timeout=3)
            if getattr(finished.result(), "success", False):
                return "dispatcher"
        except Exception:  # pylint: disable=broad-except
            pass
    # the dispatcher no longer holds it: awarded. The fleet that has it
    # answers a cancel request.
    try:
        answer = json.loads(
            await tasks_service().call(
                json.dumps(
                    {
                        "type": "cancel_task_request",
                        "task_id": task_id,
                        "labels": ["gf:replayed-request (F-465)"],
                    }
                ),
                timeout=3,
            )
        )
        if isinstance(answer, dict) and answer.get("success") is True:
            return "fleet"
    except Exception:  # pylint: disable=broad-except
        pass
    return None


async def _remove_row(task_id: str) -> int:
    # pylint: disable=import-outside-toplevel
    from api_server.models import tortoise_models as ttm

    await ttm.TaskRequest.filter(id_=task_id).delete()
    return await ttm.TaskState.filter(id_=task_id).delete()


_judging: set = set()


async def _judge(request_id: str, json_msg: str) -> None:
    try:
        await handle_unclaimed(request_id, json_msg, _cancel_anywhere, _remove_row)
    except asyncio.CancelledError:
        raise
    except Exception:  # pylint: disable=broad-except
        logger.exception("F-465: judging an unclaimed dispatch answer failed")


async def guard_loop() -> None:
    """Started with the app: every unclaimed dispatch answer is judged
    against the ledger; once an hour old rows are pruned."""
    global _loop, _inbox  # pylint: disable=global-statement
    _loop = asyncio.get_running_loop()
    _inbox = asyncio.Queue()
    last_prune = 0.0
    while True:
        try:
            try:
                request_id, json_msg = await asyncio.wait_for(_inbox.get(), timeout=600)
            except asyncio.TimeoutError:
                request_id = None
            if request_id is not None:
                # each answer on its own: a replay's cancel must not wait
                # behind another replay's retries (a fleet that is up
                # answers an auction within a fraction of a second)
                task = asyncio.create_task(_judge(request_id, json_msg))
                _judging.add(task)
                task.add_done_callback(_judging.discard)
            now = asyncio.get_running_loop().time()
            if now - last_prune > 3600:
                last_prune = now
                await prune()
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            logger.exception("F-465: judging an unclaimed dispatch answer " "failed")
