from tortoise.contrib.pydantic.creator import pydantic_model_creator
from tortoise.fields import (
    CharField,
    DatetimeField,
    ForeignKeyField,
    ForeignKeyRelation,
    JSONField,
    ReverseRelation,
)
from tortoise.models import Model

from .log import LogMixin


class TaskRequest(Model):
    id_ = CharField(255, pk=True, source_field="id")
    request = JSONField()


class TaskState(Model):
    id_ = CharField(255, pk=True, source_field="id")
    data = JSONField()
    category = CharField(255, null=True, index=True)
    assigned_to = CharField(255, null=True, index=True)
    unix_millis_start_time = DatetimeField(null=True, index=True)
    unix_millis_finish_time = DatetimeField(null=True, index=True)
    status = CharField(255, null=True, index=True)
    unix_millis_request_time = DatetimeField(null=True, index=True)
    requester = CharField(255, null=True, index=True)
    # F-37: wall-clock row provenance. The task's own unix_millis_* fields
    # are on RMF's clock, which is the sim clock under use_sim_time and
    # restarts from zero at every sim bringup — so ids and timestamps from
    # different runs interleave in a database that outlives them. These two
    # fields record when THIS database saw the row, on the one clock that
    # never restarts. NULL means the row predates the upgrade.
    created_at = DatetimeField(auto_now_add=True, null=True, index=True)
    updated_at = DatetimeField(auto_now=True, null=True)
    labels: ReverseRelation["TaskLabel"]


class TaskLabel(Model):
    state = ForeignKeyField("models.TaskState", null=True, related_name="labels")
    label_name = CharField(255, null=False, index=True)
    label_value = CharField(255, null=True, index=True)


class TaskEventLog(Model):
    task_id = CharField(255, pk=True)
    log: ReverseRelation["TaskEventLogLog"]
    phases: ReverseRelation["TaskEventLogPhases"]


class TaskEventLogLog(Model, LogMixin):
    task: ForeignKeyRelation[TaskEventLog] = ForeignKeyField(
        "models.TaskEventLog", related_name="log"
    )

    class Meta:  # type: ignore
        unique_together = ("task", "seq")


class TaskEventLogPhases(Model):
    task: ForeignKeyRelation[TaskEventLog] = ForeignKeyField(
        "models.TaskEventLog", related_name="phases"
    )
    phase = CharField(255)
    log: ReverseRelation["TaskEventLogPhasesLog"]
    events: ReverseRelation["TaskEventLogPhasesEvents"]


class TaskEventLogPhasesLog(Model, LogMixin):
    phase: ForeignKeyRelation[TaskEventLogPhases] = ForeignKeyField(
        "models.TaskEventLogPhases", related_name="log"
    )

    class Meta:  # type: ignore
        unique_together = ("id", "seq")


class TaskEventLogPhasesEvents(Model):
    phase: ForeignKeyRelation[TaskEventLogPhases] = ForeignKeyField(
        "models.TaskEventLogPhases", related_name="events"
    )
    event = CharField(255)
    log: ReverseRelation["TaskEventLogPhasesEventsLog"]


class TaskEventLogPhasesEventsLog(Model, LogMixin):
    event: ForeignKeyRelation[TaskEventLogPhasesEvents] = ForeignKeyField(
        "models.TaskEventLogPhasesEvents", related_name="log"
    )

    class Meta:  # type: ignore
        unique_together = ("id", "seq")


class TaskFavorite(Model):
    id = CharField(255, pk=True, source_field="id")
    name = CharField(255, null=False, index=True)
    unix_millis_earliest_start_time = DatetimeField(null=True, index=True)
    priority = JSONField(null=True)
    category = CharField(255, null=False, index=True)
    description = JSONField()
    user = CharField(255, null=False, index=True)


TaskFavoritePydantic = pydantic_model_creator(TaskFavorite)
