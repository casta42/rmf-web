"""F-465 (G ruling 2026-10-02, second sheet, item 2): the ledger of the
dispatch requests this api-server sent — one row per request id.

`task_api_requests` is a latched topic: a restarted dispatcher is handed
the api-server's last requests again and takes a dispatch among them as
new (a phantom mission, milliseconds after it starts). The request id is
the one thing a replay shares with the original, so every dispatch request
is written here BEFORE it is published and closed when it is answered or
given up. A dispatcher that answers a request this ledger has already
closed has acted on a replay (api_server/dispatch_ledger.py).

`outcome`: "pending" while the answer is awaited; "answered" with the
`task_id` the dispatcher gave; "closed" when no mission came of it (no
answer in time, or refused). `refused_tasks` lists the task ids later made
from this request by replays, each canceled and never kept as a mission.
"""

from tortoise.fields import CharField, DatetimeField, JSONField
from tortoise.models import Model


class DispatchRequest(Model):
    request_id = CharField(64, pk=True)
    task_id = CharField(255, null=True, index=True)
    outcome = CharField(32)
    why = CharField(255, null=True)
    refused_tasks = JSONField(null=True)
    sent_at = DatetimeField(auto_now_add=True, index=True)
    closed_at = DatetimeField(null=True)
