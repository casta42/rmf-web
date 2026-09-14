"""F-293 (FR-4 amendment, G ruling 2026-09-14): a dispatch whose earliest
start lies beyond the derived dispatch horizon is held HERE, on the
api-server's clock, and dispatched at start - horizon — so a far-future
task never sits in a robot's queue (on the Humble pin it re-commands
the robot at 1 Hz while it waits and rides inside every allocation).

A row is the operator's handle on the mission until it is dispatched:
it is listed at GET /tasks/deferred and canceled through the ordinary
POST /tasks/cancel_task with its id ("deferred-<n>"). Once dispatched,
the real task carries the label gf:deferred-of=<id> and the row records
its task id.
"""

from tortoise.fields import (
    CharField,
    DatetimeField,
    IntField,
    JSONField,
    TextField,
)
from tortoise.models import Model

PENDING = "pending"
DISPATCHING = "dispatching"
DISPATCHED = "dispatched"
CANCELED = "canceled"
FAILED = "failed"


class DeferredDispatch(Model):
    id = IntField(pk=True)
    # "dispatch_task_request" or "robot_task_request"
    request_type = CharField(64)
    # the full request body as the client sent it (robot/fleet included)
    body = JSONField()
    earliest_start = DatetimeField(index=True)
    dispatch_at = DatetimeField(index=True)
    status = CharField(16, default=PENDING, index=True)
    task_id = CharField(255, null=True)
    detail = TextField(null=True)
    created_by = CharField(255)
    created_at = DatetimeField(auto_now_add=True)

    def public_id(self) -> str:
        return f"deferred-{self.id}"


__all__ = ["DeferredDispatch"]
