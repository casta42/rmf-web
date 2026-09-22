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


if __name__ == "__main__":
    unittest.main()
