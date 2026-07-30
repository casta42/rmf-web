from enum import Enum

from tortoise.contrib.pydantic.creator import pydantic_model_creator
from tortoise.fields import BigIntField, CharEnumField, CharField, TextField
from tortoise.models import Model


class Alert(Model):
    """
    General alert that can be triggered by events.

    v2 (FR-31, GentleFleet fork): single-row lifecycle — an alert is
    created open, acknowledged in place, and RESOLVED in place (archived),
    never deleted. "Open" means `unix_millis_resolved_time` is null.
    Structured context fields (severity/fleet/robot/message) replace
    parsing the id string; the id format is unchanged for compatibility
    with the F-29/F-39 prefix sweeps.
    """

    class Category(str, Enum):
        Default = "default"
        Task = "task"
        Fleet = "fleet"
        Robot = "robot"

    class Severity(str, Enum):
        Info = "info"
        Warning = "warning"
        Critical = "critical"

    id = CharField(255, pk=True)
    original_id = CharField(255, index=True)
    category = CharEnumField(Category, index=True)
    severity = CharEnumField(Severity, index=True, default=Severity.Warning)
    fleet = CharField(255, null=True, index=True)
    robot = CharField(255, null=True, index=True)
    message = TextField(null=True)
    unix_millis_created_time = BigIntField(null=False, index=True)
    acknowledged_by = CharField(255, null=True, index=True)
    unix_millis_acknowledged_time = BigIntField(null=True, index=True)
    resolved_by = CharField(255, null=True)
    unix_millis_resolved_time = BigIntField(null=True, index=True)


AlertPydantic = pydantic_model_creator(Alert)
