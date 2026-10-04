"""F-386 (G ruling 2026-09-22, D-82) — the Charging verdict is relayed from
the fleet adapter, never re-derived here.

Both ways: a fresh verdict is served as the adapter gave it (true, false,
and null = cannot tell); a feed that never arrived, stopped, or arrived
malformed is NOT "not charging" and NOT "charging" — the route says it
cannot tell and why (F-191).
"""

import json
import unittest

from . import charging_truth as ct
from .test import AppFixture


def _payload(**robots):
    return json.dumps(
        {"fleet": "gentle_fleet", "unix_millis_time": 0, "robots": robots}
    )


class TestChargingRelay(unittest.TestCase):
    def setUp(self):
        ct._reset_for_test()

    def tearDown(self):
        ct._reset_for_test()

    def test_the_verdicts_are_served_as_the_adapter_gave_them(self):
        ct.on_charging(
            _payload(
                gentle_bot_4={
                    "charging": True,
                    "docked": True,
                    "moving": False,
                    "why": "on its charger, SoC rising +0.126 over 120 s",
                },
                gentle_bot_5={
                    "charging": False,
                    "docked": False,
                    "moving": False,
                    "why": "6.30 m from its charger",
                },
                gentle_bot_6={
                    "charging": None,
                    "docked": True,
                    "moving": False,
                    "why": "on its charger — no rise seen yet (10 s of 120 s)",
                },
            ),
            now=100.0,
        )
        snap = ct.snapshot(now=101.0)
        self.assertTrue(snap["available"])
        by = {r["robot"]: r for r in snap["robots"]}
        self.assertIs(True, by["gentle_bot_4"]["charging"])
        self.assertIs(False, by["gentle_bot_5"]["charging"])
        self.assertIsNone(by["gentle_bot_6"]["charging"])
        self.assertEqual("gentle_fleet/gentle_bot_5", by["gentle_bot_5"]["key"])
        self.assertIn("6.30 m", by["gentle_bot_5"]["why"])

    def test_no_feed_is_cannot_tell(self):
        snap = ct.snapshot(now=100.0)
        self.assertFalse(snap["available"])
        self.assertEqual([], snap["robots"])
        self.assertIn("unknown", snap["reason"])

    def test_a_stopped_feed_is_cannot_tell(self):
        ct.on_charging(_payload(gentle_bot_4={"charging": True}), now=100.0)
        snap = ct.snapshot(now=100.0 + ct.MAX_AGE_S + 1)
        self.assertFalse(snap["available"])
        self.assertEqual([], snap["robots"])
        self.assertIn("stopped", snap["reason"])

    def test_malformed_input_never_becomes_a_verdict(self):
        ct.on_charging("not json", now=100.0)
        ct.on_charging(json.dumps({"robots": {}}), now=100.0)  # no fleet
        self.assertFalse(ct.snapshot(now=100.0)["available"])
        ct.on_charging(
            _payload(gentle_bot_4={"charging": "yes"}, gentle_bot_5="garbage"),
            now=100.0,
        )
        rows = ct.snapshot(now=100.0)["robots"]
        self.assertEqual(["gentle_bot_4"], [r["robot"] for r in rows])
        self.assertIsNone(rows[0]["charging"], "only a bool is a verdict")


class TestBatteryTruth(unittest.TestCase):
    """F-483 (G ruling 2026-10-03, D-90/D-91): the robot's real battery
    reading rides the same feed — the readouts never show the planner's
    spoofed 0.0. Both ways: a real reading is served with whether the
    fleet state is spoofed; no reading, a bad one, or an older adapter
    that does not say is "unknown" (None), never a number."""

    def setUp(self):
        ct._reset_for_test()

    def tearDown(self):
        ct._reset_for_test()

    def _rows(self, **robots):
        ct.on_charging(_payload(**robots), now=100.0)
        return {r["robot"]: r for r in ct.snapshot(now=100.5)["robots"]}

    def test_the_f1_n72_strand_reads_its_real_sixty_percent(self):
        # gentle_bot_5, stranded off the route network: RMF cannot place
        # it, the fleet state says battery 0.0, the pack is at 60 %
        by = self._rows(
            gentle_bot_5={
                "charging": False,
                "docked": False,
                "moving": False,
                "why": "9.10 m from its charger",
                "soc": 0.6,
                "soc_published": "spoofed",
            }
        )
        self.assertEqual(0.6, by["gentle_bot_5"]["battery"])
        self.assertIs(True, by["gentle_bot_5"]["battery_spoofed"])

    def test_a_robot_in_service_reads_its_reading_and_not_spoofed(self):
        by = self._rows(
            gentle_bot_1={"charging": None, "soc": 0.8312, "soc_published": "real"}
        )
        self.assertEqual(0.8312, by["gentle_bot_1"]["battery"])
        self.assertIs(False, by["gentle_bot_1"]["battery_spoofed"])

    def test_a_real_zero_is_a_reading(self):
        # a pack that IS empty is not the spoof: served as 0.0, not spoofed
        by = self._rows(
            gentle_bot_2={"charging": False, "soc": 0.0, "soc_published": "real"}
        )
        self.assertEqual(0.0, by["gentle_bot_2"]["battery"])
        self.assertIs(False, by["gentle_bot_2"]["battery_spoofed"])

    def test_no_reading_is_unknown_never_a_number(self):
        by = self._rows(
            none={"charging": None, "soc": None, "soc_published": "spoofed"},
            nan={"charging": None, "soc": float("nan"), "soc_published": "real"},
            high={"charging": None, "soc": 1.7, "soc_published": "real"},
            negative={"charging": None, "soc": -0.1, "soc_published": "real"},
            text={"charging": None, "soc": "60 %", "soc_published": "real"},
            flag={"charging": None, "soc": True, "soc_published": "real"},
        )
        for name in ("none", "nan", "high", "negative", "text", "flag"):
            self.assertIsNone(by[name]["battery"], name)
        self.assertIs(True, by["none"]["battery_spoofed"])

    def test_an_older_adapter_that_does_not_say_is_unknown_on_both(self):
        # the feed of a fleet adapter before F-483: no soc, no flag
        by = self._rows(gentle_bot_4={"charging": True, "docked": True})
        self.assertIsNone(by["gentle_bot_4"]["battery"])
        self.assertIsNone(by["gentle_bot_4"]["battery_spoofed"])
        by = self._rows(gentle_bot_4={"charging": True, "soc_published": "maybe"})
        self.assertIsNone(by["gentle_bot_4"]["battery_spoofed"])


class TestChargingRoute(AppFixture):
    def tearDown(self):
        ct._reset_for_test()
        super().tearDown()

    def test_the_route_serves_the_relay(self):
        ct._reset_for_test()
        resp = self.client.get("/fleets/charging")
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertFalse(resp.json()["available"])
        ct.on_charging(
            _payload(
                gentle_bot_4={
                    "charging": True,
                    "docked": True,
                    "moving": False,
                    "why": "rising",
                }
            )
        )
        body = self.client.get("/fleets/charging").json()
        self.assertTrue(body["available"])
        self.assertEqual("gentle_bot_4", body["robots"][0]["robot"])
        # F-483: the row carries the battery fields (unknown here)
        self.assertIn("battery", body["robots"][0])
        self.assertIn("battery_spoofed", body["robots"][0])


if __name__ == "__main__":
    unittest.main()
