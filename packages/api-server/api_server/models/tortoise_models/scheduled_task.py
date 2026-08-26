import logging
import os
from datetime import datetime
from enum import Enum

import pytz
import schedule
from schedule import Job
from tortoise.fields import (
    CharEnumField,
    CharField,
    DatetimeField,
    ForeignKeyField,
    ForeignKeyRelation,
    IntField,
    JSONField,
    ReverseRelation,
    SmallIntField,
)
from tortoise.models import Model

# F-137: the dashboard's schedule picker writes site-local wall times
# ("every day at 00:15" means 00:15 on the warehouse floor), but this
# process runs on the container clock (UTC) and the `schedule` library
# evaluates bare at-strings against that clock — the stored 00:15
# silently fired at 18:15 local during E6 run-2. Every at-string is
# therefore evaluated in the SITE timezone (IANA name via GF_SITE_TZ,
# set by the compose stack). Stored strings stay honest: they are, and
# always were, the local wall time the operator read in the dialog —
# existing rows need no migration, only this reinterpretation (each
# job's next run is logged at (re)schedule time as the audit trail).
# Unset/invalid GF_SITE_TZ falls back to the server clock (dev parity)
# with a loud log line.


def _site_tz() -> str | None:
    name = os.environ.get("GF_SITE_TZ")
    if not name:
        logging.getLogger("app").warning(
            "GF_SITE_TZ is not set — schedule at-times will be evaluated "
            "on the server clock (F-137); set it to the site's IANA "
            "timezone in the deployment"
        )
        return None
    try:
        pytz.timezone(name)
        return name
    except pytz.exceptions.UnknownTimeZoneError:
        logging.getLogger("app").error(
            f"GF_SITE_TZ [{name}] is not a valid IANA timezone — "
            "falling back to the server clock (F-137)"
        )
        return None


SITE_TZ = _site_tz()


class ScheduledTask(Model):
    task_request = JSONField()
    created_by = CharField(255)
    schedules: ReverseRelation["ScheduledTaskSchedule"]
    last_ran = DatetimeField(null=True)
    except_dates = JSONField(null=True)


class ScheduledTaskSchedule(Model):
    """
    The schedules for a scheduled task request.
    A scheduled task may have multiple schedules.
    """

    class Period(str, Enum):
        Monday = "monday"
        Tuesday = "tuesday"
        Wednesday = "wednesday"
        Thursday = "thursday"
        Friday = "friday"
        Saturday = "saturday"
        Sunday = "sunday"
        Day = "day"
        Hour = "hour"
        Minute = "minute"

    _id = IntField(pk=True, source_field="id")
    scheduled_task: ForeignKeyRelation[ScheduledTask] = ForeignKeyField(
        "models.ScheduledTask", related_name="schedules"
    )
    every = SmallIntField(null=True)
    start_from = DatetimeField(null=True)
    until = DatetimeField(null=True)
    period = CharEnumField(Period)
    at = CharField(255, null=True)

    def get_id(self) -> int:
        return self._id

    def to_job(self) -> Job:
        if self.every is not None:
            job = schedule.every(self.every)
        else:
            job = schedule.every()
        if self.until is not None:
            # schedule uses `datetime.now()`, which is tz naive
            # Assuming self.until is a datetime object with timezone information
            # Convert the timestamp to datetime without changing the timezone
            job = job.until(datetime.utcfromtimestamp(self.until.timestamp()))

        if self.period in (
            ScheduledTaskSchedule.Period.Monday,
            ScheduledTaskSchedule.Period.Tuesday,
            ScheduledTaskSchedule.Period.Wednesday,
            ScheduledTaskSchedule.Period.Thursday,
            ScheduledTaskSchedule.Period.Friday,
            ScheduledTaskSchedule.Period.Saturday,
            ScheduledTaskSchedule.Period.Sunday,
        ):
            job = getattr(job, self.period)
        elif self.period == ScheduledTaskSchedule.Period.Day:
            job = job.days
        elif self.period == ScheduledTaskSchedule.Period.Hour:
            job = job.hours
        elif self.period == ScheduledTaskSchedule.Period.Minute:
            job = job.minutes
        else:
            raise ValueError("invalid period")

        # Hashable value in order to tag the job with a unique identifier
        job.tag(self._id)
        if self.at is not None:
            # F-137: at-strings are site-local wall times
            if SITE_TZ is not None:
                job = job.at(self.at, SITE_TZ)
            else:
                job = job.at(self.at)

        return job
