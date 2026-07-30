"""FR-31 alert model v2 route tests (GentleFleet fork).

Covers the archive-on-resolve lifecycle: created open -> acknowledged in
place -> resolved in place, never deleted; and the filterable list.
"""

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
