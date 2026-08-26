"""F-137 class guard (Phase E close triage, 2026-08-26): schedule
at-strings are site-local wall times and must be evaluated in the site
timezone, never the server clock.

E6 run-2: "every day at 00:15" entered in the dashboard was stored and
silently evaluated on the container's UTC clock — it would have fired
at 18:15 on the warehouse floor (host CST, UTC-6) with nothing on
screen to reveal it. The class rule: wherever the server clock and the
site wall clock disagree, the at-string means the wall clock.
"""

import os
import time
import unittest

from api_server.models.tortoise_models import scheduled_task as st_module
from api_server.models.tortoise_models.scheduled_task import (
    ScheduledTaskSchedule,
    _site_tz,
)


class PinnedUtcClock(unittest.TestCase):
    """Pin the process to UTC — the container clock in deployment."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self._site_tz = st_module.SITE_TZ

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()
        st_module.SITE_TZ = self._site_tz


class TestToJobSiteTz(PinnedUtcClock):
    def make_schedule(self, **kwargs):
        sche = ScheduledTaskSchedule(**kwargs)
        sche._id = 1
        return sche

    def test_daily_at_is_evaluated_in_site_tz(self):
        # the reporting case: 00:15 on the floor (Monterrey, UTC-6,
        # no DST) must arm for 06:15 on the server (UTC) clock
        st_module.SITE_TZ = "America/Monterrey"
        job = self.make_schedule(
            period=ScheduledTaskSchedule.Period.Day, at="00:15"
        ).to_job()
        job.do(lambda: None)
        self.assertEqual((6, 15), (job.next_run.hour, job.next_run.minute))

    def test_weekday_at_is_evaluated_in_site_tz(self):
        st_module.SITE_TZ = "America/Monterrey"
        job = self.make_schedule(
            period=ScheduledTaskSchedule.Period.Monday, at="06:30"
        ).to_job()
        job.do(lambda: None)
        self.assertEqual((12, 30), (job.next_run.hour, job.next_run.minute))

    def test_without_site_tz_falls_back_to_server_clock(self):
        st_module.SITE_TZ = None
        job = self.make_schedule(
            period=ScheduledTaskSchedule.Period.Day, at="00:15"
        ).to_job()
        job.do(lambda: None)
        self.assertEqual((0, 15), (job.next_run.hour, job.next_run.minute))


class TestSiteTzResolution(PinnedUtcClock):
    def setUp(self):
        super().setUp()
        self._gf = os.environ.get("GF_SITE_TZ")

    def tearDown(self):
        if self._gf is None:
            os.environ.pop("GF_SITE_TZ", None)
        else:
            os.environ["GF_SITE_TZ"] = self._gf
        super().tearDown()

    def test_valid_tz_is_accepted(self):
        os.environ["GF_SITE_TZ"] = "America/Monterrey"
        self.assertEqual("America/Monterrey", _site_tz())

    def test_invalid_tz_falls_back_loudly_to_none(self):
        os.environ["GF_SITE_TZ"] = "Mars/Olympus_Mons"
        self.assertIsNone(_site_tz())

    def test_unset_tz_is_none(self):
        os.environ.pop("GF_SITE_TZ", None)
        self.assertIsNone(_site_tz())
