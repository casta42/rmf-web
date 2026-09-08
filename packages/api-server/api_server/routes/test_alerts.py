"""FR-31 alert model v2 route tests (GentleFleet fork).

Covers the archive-on-resolve lifecycle: created open -> acknowledged in
place -> resolved in place, never deleted; the filterable list; and
(F-270) that no enum-backed field can answer 500.
"""

from api_server.models import tortoise_models as ttm
from api_server.test import AppFixture


class TestAlertsRoute(AppFixture):
    def test_lifecycle_archives_instead_of_deleting(self):
        alert_id = "robot_offline__gf__bot1__1000"
        resp = self.client.post(
            f"/alerts?alert_id={alert_id}&category=robot",
        )
        self.assertEqual(201, resp.status_code, resp.content)

        # acknowledge in place: same id, no clone row
        resp = self.client.post(f"/alerts/{alert_id}")
        self.assertEqual(201, resp.status_code, resp.content)
        acked = resp.json()
        self.assertEqual(alert_id, acked["id"])
        self.assertEqual("admin", acked["acknowledged_by"])
        # in-place ack: exactly one row for this id, no clone
        rows = [a for a in self.client.get("/alerts").json() if alert_id in a["id"]]
        self.assertEqual(1, len(rows))

        # resolve archives the row
        resp = self.client.post(f"/alerts/{alert_id}/resolve")
        self.assertEqual(200, resp.status_code, resp.content)
        resolved = resp.json()
        self.assertEqual("admin", resolved["resolved_by"])
        self.assertIsNotNone(resolved["unix_millis_resolved_time"])

        # archived, not deleted: absent from open, present in resolved/all
        open_alerts = self.client.get("/alerts?status=open").json()
        self.assertEqual([], [a for a in open_alerts if a["id"] == alert_id])
        resolved_alerts = self.client.get("/alerts?status=resolved").json()
        self.assertIn(alert_id, [a["id"] for a in resolved_alerts])
        self.assertEqual(200, self.client.get(f"/alerts/{alert_id}").status_code)

        # resolving again is a 404 (no OPEN alert with that id)
        resp = self.client.post(f"/alerts/{alert_id}/resolve")
        self.assertEqual(404, resp.status_code)

    def test_filters_and_pagination(self):
        for i in range(3):
            self.client.post(f"/alerts?alert_id=filter_test_{i}&category=task")
        self.client.post(f"/alerts/filter_test_0/resolve")

        open_tasks = self.client.get("/alerts?status=open&category=task").json()
        open_ids = [a["id"] for a in open_tasks]
        self.assertNotIn("filter_test_0", open_ids)
        self.assertIn("filter_test_1", open_ids)

        page = self.client.get("/alerts?status=open&category=task&limit=1").json()
        self.assertEqual(1, len(page))

        resp = self.client.get("/alerts?status=bogus")
        self.assertEqual(422, resp.status_code)


class TestAlertEnumFieldsNeverAnswer500(AppFixture):
    """F-270: `category` and `severity` were plain strings handed to a
    tortoise CharEnumField, which RAISES on a value outside the enum. An
    unknown filter answered 500, and so did an unknown value on the
    CREATE path — where the alert is not rejected, it is LOST.

    That is not a hypothetical about typos. `Alert.Category.Instrument`
    was added for the F-268 referee alerts, and the sentinel posted to it
    for an hour against a column still sized for the older names: every
    one of those alerts answered 500 and vanished, while the sentinel's
    own log said it was raising a critical fault. A guard that reports
    into a 500 is a guard nobody hears.

    Both ways, and the good half is generated FROM the enums — so a
    category added tomorrow is covered without anyone remembering to add
    it here, which is the maintenance both defects were missing.
    """

    def test_every_category_in_the_enum_is_accepted_as_a_filter(self):
        for category in ttm.Alert.Category:
            resp = self.client.get(f"/alerts?category={category.value}")
            self.assertEqual(200, resp.status_code,
                             f"{category.value}: {resp.content}")

    def test_every_severity_in_the_enum_is_accepted_as_a_filter(self):
        for severity in ttm.Alert.Severity:
            resp = self.client.get(f"/alerts?severity={severity.value}")
            self.assertEqual(200, resp.status_code,
                             f"{severity.value}: {resp.content}")

    def test_every_category_in_the_enum_can_be_created_and_read_back(self):
        for category in ttm.Alert.Category:
            alert_id = f"enum-roundtrip-{category.value}"
            resp = self.client.post(
                f"/alerts?alert_id={alert_id}&category={category.value}"
                f"&severity=critical&message=probe")
            self.assertEqual(201, resp.status_code,
                             f"{category.value}: {resp.content}")
            self.assertEqual(category.value, resp.json()["category"])
            listed = self.client.get(f"/alerts?category={category.value}").json()
            self.assertIn(alert_id, [a["id"] for a in listed],
                          f"{category.value} was created but does not come "
                          "back through its own filter")

    def test_an_unknown_category_filter_is_refused_not_a_500(self):
        resp = self.client.get("/alerts?category=nonsense")
        self.assertEqual(422, resp.status_code, resp.content)
        self.assertNotEqual(500, resp.status_code)
        body = resp.text.lower()
        self.assertIn("nonsense", body)
        # the reason has to name what WOULD be accepted
        self.assertTrue(any(c.value in body for c in ttm.Alert.Category),
                        f"the refusal does not say what is allowed: {body}")

    def test_an_unknown_severity_filter_is_refused_not_a_500(self):
        resp = self.client.get("/alerts?severity=nonsense")
        self.assertEqual(422, resp.status_code, resp.content)

    def test_creating_with_an_unknown_category_is_refused_not_a_500(self):
        resp = self.client.post(
            "/alerts?alert_id=should-not-exist&category=nonsense&message=x")
        self.assertEqual(422, resp.status_code, resp.content)
        # and nothing was written under that id
        self.assertEqual(404,
                         self.client.get("/alerts/should-not-exist").status_code)

    def test_creating_with_an_unknown_severity_is_refused_not_a_500(self):
        resp = self.client.post(
            "/alerts?alert_id=should-not-exist-2&category=fleet"
            "&severity=nonsense&message=x")
        self.assertEqual(422, resp.status_code, resp.content)
        self.assertEqual(
            404, self.client.get("/alerts/should-not-exist-2").status_code)

    def test_the_instrument_alert_that_was_lost_now_survives_the_round_trip(self):
        """The exact incident: the sentinel's stale-position fault."""
        alert_id = "stale-position-1788891499-gentle_bot_4"
        resp = self.client.post(
            f"/alerts?alert_id={alert_id}&category=instrument"
            "&severity=critical&fleet=gentle_fleet&robot=gentle_bot_4"
            "&message=SENTINEL+INSTRUMENT+FAULT")
        self.assertEqual(201, resp.status_code, resp.content)
        listed = self.client.get("/alerts?category=instrument").json()
        self.assertIn(alert_id, [a["id"] for a in listed])
        self.assertEqual("instrument",
                         [a for a in listed if a["id"] == alert_id][0]["category"])

    def test_omitting_the_filters_still_returns_everything(self):
        """The boring case: no filter is not an invalid filter."""
        self.client.post("/alerts?alert_id=nofilter-probe&category=fleet")
        resp = self.client.get("/alerts")
        self.assertEqual(200, resp.status_code)
        self.assertIn("nofilter-probe", [a["id"] for a in resp.json()])

