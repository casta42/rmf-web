"""F-454 (G ruling 2026-10-02, item 1): the fleet core incarnation the
api-server last settled — one row (id 1).

`boot_id` is the adapter's latched `gf_core_boot` record
(api_server/core_incarnation.py): a different one is a real coordination
restart, the only thing the F-141 sweep acts on. It lives in the database
so the comparison survives an api-server restart (a host reboot restarts
both). `last_heard_at` is when the core was last heard, kept to within
core_incarnation.HEARD_PERSIST_S: at the next restart it says how long the
core was away, which decides whether what it interrupted is sent again or
left to an operator.
"""

from tortoise.fields import CharField, DatetimeField, IntField
from tortoise.models import Model


class CoreBoot(Model):
    id = IntField(pk=True)
    boot_id = CharField(255)
    started_at = DatetimeField()
    last_heard_at = DatetimeField(null=True)
    settled_at = DatetimeField(auto_now=True)
