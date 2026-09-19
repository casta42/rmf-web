"""F-339 (G ruling 2026-09-19): an operator's lane closure is AUTHORED
INTENT, not runtime state, and it must survive any restart of the fleet
adapter — a crash must never be able to erase a safety action.

One row per closed DIRECTED lane. The lane is stored as its two endpoints,
never as its index: an index is only meaningful against the graph the
fleet is driving right now (F-333), and every re-derivation renumbers
them. The api-server resolves each row against the live `/nav_graphs`
whenever it publishes; a row whose endpoints are no longer joined by a
lane is reported unresolved and retired, never guessed onto a neighbour.

Why the api-server ledger and not the site config repo (the alternative
G left open): the site config is authored state that reaches the fleet
through a re-derivation and a coordination restart (D-17/F-87) — a
cordon exists precisely to close an aisle NOW, without that restart.
NFR-9 already places operator-authored runtime intent (the FR-42 release
store, the FR-31 alert archive) in this ledger, so a cordon rides on the
backup and restore path F1.5 proved, and the api-server is already the
one process that reads lane indices in the fleet's own space (F-332/
F-333). The api-server is the ONLY writer of `/lane_closure_requests`.
"""

from tortoise.fields import BigIntField, CharField, FloatField, IntField, TextField
from tortoise.models import Model


class LaneClosure(Model):
    id = IntField(pk=True)
    fleet = CharField(255, index=True)
    entry_name = CharField(255, default="")
    entry_x = FloatField()
    entry_y = FloatField()
    exit_name = CharField(255, default="")
    exit_x = FloatField()
    exit_y = FloatField()
    # the index the lane had in the fleet's graph when the operator closed
    # it — a hint for the log, never the key
    lane_index_at_request = IntField()
    requested_by = CharField(255)
    unix_millis_request_time = BigIntField()
    reason = TextField(default="")


__all__ = ["LaneClosure"]
