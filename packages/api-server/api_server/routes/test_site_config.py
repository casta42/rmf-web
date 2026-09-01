"""GentleFleet fork: /site_config proxy tests (DR-4, D-17).

The sidecar is mocked at the httpx boundary; what is under test here is
what this layer owns: the admin gate, the D-17 mission guard (refuse →
hard-confirm), and server-side identity stamping."""

import unittest.mock

from api_server.app_config import app_config
from api_server.test import AppFixture


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json


def _fake_async_client(recorder, response: _FakeResponse):
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kwargs):
            recorder.append((method, url, kwargs))
            return response

        async def post(self, url, **kwargs):
            recorder.append(("POST", url, kwargs))
            return response

    return _FakeClient


class TestSiteConfigRoutes(AppFixture):
    def setUp(self):
        super().setUp()
        self.old_url = app_config.site_config_url
        self.old_token = app_config.site_config_token_file
        app_config.site_config_url = "http://127.0.0.1:8100"
        # any readable file works as the token source
        self.token_file = "/tmp/gf-test-token"
        with open(self.token_file, "w", encoding="utf8") as f:
            f.write("test-token\n")
        app_config.site_config_token_file = self.token_file

    def tearDown(self):
        app_config.site_config_url = self.old_url
        app_config.site_config_token_file = self.old_token
        super().tearDown()

    def test_404_when_unconfigured(self):
        app_config.site_config_url = None
        resp = self.client.get("/site_config")
        self.assertEqual(404, resp.status_code)

    def test_get_proxies_with_token(self):
        calls = []
        fake = _fake_async_client(
            calls, _FakeResponse(json_body={"site": "testsite_a"})
        )
        with unittest.mock.patch(
            "api_server.routes.site_config.httpx.AsyncClient", fake
        ):
            resp = self.client.get("/site_config")
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual({"site": "testsite_a"}, resp.json())
        self.assertEqual(1, len(calls))
        method, url, kwargs = calls[0]
        self.assertEqual("http://127.0.0.1:8100/site_config", url)
        self.assertEqual("test-token", kwargs["headers"]["x-gf-internal-token"])

    def test_apply_refused_while_missions_active_then_hard_confirm(self):
        calls = []
        fake = _fake_async_client(
            calls, _FakeResponse(json_body={"state": "validating"})
        )
        missions = [
            {
                "task_id": "patrol.dispatch-1",
                "status": "underway",
                "robot": "gentle_bot_2",
            }
        ]

        async def fake_active():
            return missions

        with unittest.mock.patch(
            "api_server.routes.site_config.httpx.AsyncClient", fake
        ), unittest.mock.patch(
            "api_server.routes.site_config.active_missions", fake_active
        ):
            body = {
                "candidate": {"base_commit": "abc", "zones": {}},
                "acknowledge_fleet_pause": True,
            }
            refused = self.client.post("/site_config/apply", json=body)
            self.assertEqual(409, refused.status_code, refused.content)
            detail = refused.json()["detail"]
            self.assertEqual("active_missions", detail["reason"])
            self.assertEqual("patrol.dispatch-1", detail["missions"][0]["task_id"])
            self.assertEqual(0, len(calls))  # sidecar never reached

            body["acknowledge_active_missions"] = True
            confirmed = self.client.post("/site_config/apply", json=body)
            self.assertEqual(200, confirmed.status_code, confirmed.content)
            # F-186: the apply now also validates first (to learn what
            # the derivation would retire), so what matters is that the
            # sidecar was reached AT ALL after the hard-confirm — not
            # that it was reached exactly once.
            self.assertTrue([c for c in calls if c[1].endswith("/site_config/apply")])

    def test_apply_stamps_authenticated_user(self):
        calls = []
        fake = _fake_async_client(
            calls, _FakeResponse(json_body={"state": "validating"})
        )

        async def no_missions():
            return []

        with unittest.mock.patch(
            "api_server.routes.site_config.httpx.AsyncClient", fake
        ), unittest.mock.patch(
            "api_server.routes.site_config.active_missions", no_missions
        ):
            body = {
                "candidate": {"base_commit": "abc", "zones": {}},
                "applied_by": "mallory",  # must be ignored
                "acknowledge_fleet_pause": True,
            }
            resp = self.client.post("/site_config/apply", json=body)
        self.assertEqual(200, resp.status_code, resp.content)
        # F-186: the apply now also runs a validate first (to learn what
        # the derivation would retire), so pick the apply call by PATH
        # rather than trusting it to be the only one recorded.
        _, _, kwargs = next(c for c in calls if c[1].endswith("/site_config/apply"))
        self.assertEqual("admin", kwargs["json"]["applied_by"])

    def test_active_missions_sees_enum_repr_statuses(self):
        # The book keeper stores str(TaskStatus.underway) ==
        # "Status.underway" — the guard query must match that repr, not
        # just the plain value (the smoke test caught exactly this).
        from api_server.models import TaskStatus
        from api_server.models.tortoise_models import TaskState as DbTaskState
        from api_server.routes.site_config import active_missions

        portal = self.get_portal()
        portal.call(
            lambda: DbTaskState.update_or_create(
                {
                    "data": {},
                    "status": TaskStatus.underway,
                    "assigned_to": "gentle_bot_9",
                },
                id_="e5-guard-enum-repr",
            )
        )
        try:
            missions = portal.call(active_missions)
            match = [m for m in missions if m["task_id"] == "e5-guard-enum-repr"]
            self.assertEqual(1, len(match), missions)
            self.assertEqual("underway", match[0]["status"])
        finally:
            portal.call(lambda: DbTaskState.filter(id_="e5-guard-enum-repr").delete())

    def test_validate_injects_live_robot_positions(self):
        # D-20: the sidecar refuses a no-go over a robot; the proxy must
        # supply where the robots are
        from api_server.models.tortoise_models import FleetState

        portal = self.get_portal()
        portal.call(
            lambda: FleetState.update_or_create(
                {
                    "data": {
                        "name": "gentle_fleet",
                        "robots": {"gentle_bot_2": {"location": {"x": 9.0, "y": 3.5}}},
                    }
                },
                name="gentle_fleet",
            )
        )
        calls = []
        fake = _fake_async_client(calls, _FakeResponse(json_body={"ok": True}))
        try:
            with unittest.mock.patch(
                "api_server.routes.site_config.httpx.AsyncClient", fake
            ):
                resp = self.client.post(
                    "/site_config/validate",
                    json={"base_commit": "abc", "zones": {}},
                )
            self.assertEqual(200, resp.status_code, resp.content)
            _, _, kwargs = calls[0]
            positions = kwargs["json"]["robot_positions"]
            self.assertEqual(
                # D-24 §5: parked=True — no mission rows exist in this
                # fixture, so the robot counts as parked (evacuable)
                [{"name": "gentle_bot_2", "x": 9.0, "y": 3.5, "parked": True}],
                positions,
            )
        finally:
            portal.call(lambda: FleetState.filter(name="gentle_fleet").delete())

    def test_validate_blocks_retiring_a_destination_a_template_uses(self):
        # FR-32/D-22: the sidecar says what would be retired; this layer
        # knows what still dispatches to it and turns that into a
        # blocking violation with the template named.
        from api_server.models.tortoise_models import TaskFavorite

        portal = self.get_portal()
        portal.call(
            lambda: TaskFavorite.update_or_create(
                {
                    "name": "Morning restock",
                    "category": "patrol",
                    "description": {"places": ["dock_3"], "rounds": 1},
                    "user": "admin",
                },
                id="e5-fav-dock3",
            )
        )
        calls = []
        fake = _fake_async_client(
            calls,
            _FakeResponse(
                json_body={
                    "ok": True,
                    "violations": [],
                    "retired_destinations": ["dock_3"],
                }
            ),
        )
        try:
            with unittest.mock.patch(
                "api_server.routes.site_config.httpx.AsyncClient", fake
            ):
                resp = self.client.post(
                    "/site_config/validate",
                    json={"base_commit": "abc", "zones": {}, "destinations": []},
                )
            self.assertEqual(200, resp.status_code, resp.content)
            report = resp.json()
            self.assertFalse(report["ok"])
            self.assertEqual(1, len(report["violations"]))
            message = report["violations"][0]["message"]
            self.assertIn("dock_3", message)
            self.assertIn("Morning restock", message)
            self.assertEqual("destination_in_use", report["violations"][0]["code"])
        finally:
            portal.call(lambda: TaskFavorite.filter(id="e5-fav-dock3").delete())

    def test_validate_blocks_retiring_a_waypoint_a_template_uses(self):
        # F-186/I-7: the same rule as the destination case above, for a
        # WAYPOINT the derivation retires. A railed corridor retires its
        # interior junctions; a template still dispatching to one would
        # fail at dispatch after the apply, which is exactly the failure
        # D-22 blocks for destinations.
        from api_server.models.tortoise_models import TaskFavorite

        portal = self.get_portal()
        portal.call(
            lambda: TaskFavorite.update_or_create(
                {
                    "name": "Corner sweep",
                    "category": "patrol",
                    "description": {"places": ["j_e1"], "rounds": 1},
                    "user": "admin",
                },
                id="f186-fav-je1",
            )
        )
        calls = []
        fake = _fake_async_client(
            calls,
            _FakeResponse(
                json_body={
                    "ok": True,
                    "violations": [],
                    "retired_waypoints": [
                        {
                            "waypoint": "j_e1",
                            "corridor": "j_s2..j_n3",
                            "served_by": "an offset-pair corner set",
                        }
                    ],
                }
            ),
        )
        try:
            with unittest.mock.patch(
                "api_server.routes.site_config.httpx.AsyncClient", fake
            ):
                resp = self.client.post(
                    "/site_config/validate",
                    json={"base_commit": "abc", "zones": {}},
                )
            self.assertEqual(200, resp.status_code, resp.content)
            report = resp.json()
            self.assertFalse(report["ok"])
            self.assertEqual(1, len(report["violations"]))
            violation = report["violations"][0]
            self.assertEqual("waypoint_in_use", violation["code"])
            self.assertIn("j_e1", violation["message"])
            self.assertIn("Corner sweep", violation["message"])
            # the wording must not claim the admin removed it — the
            # derivation did
            self.assertIn("RETIRES", violation["message"])
            self.assertNotIn("Renaming or removing", violation["message"])
        finally:
            portal.call(lambda: TaskFavorite.filter(id="f186-fav-je1").delete())

    def test_validate_allows_retiring_a_waypoint_nothing_uses(self):
        # The block is about MISSIONS, not about retirement itself —
        # retiring a junction no template or schedule targets is the
        # normal case and must sail through.
        calls = []
        fake = _fake_async_client(
            calls,
            _FakeResponse(
                json_body={
                    "ok": True,
                    "violations": [],
                    "retired_waypoints": [
                        {
                            "waypoint": "j_n2",
                            "corridor": "j_s2..j_n3",
                            "served_by": "a T-pair on the rails",
                        }
                    ],
                }
            ),
        )
        with unittest.mock.patch(
            "api_server.routes.site_config.httpx.AsyncClient", fake
        ):
            resp = self.client.post(
                "/site_config/validate",
                json={"base_commit": "abc", "zones": {}},
            )
        self.assertEqual(200, resp.status_code, resp.content)
        report = resp.json()
        self.assertTrue(report["ok"], report["violations"])
        self.assertEqual([], report["violations"])

    def test_apply_refuses_retiring_a_destination_a_schedule_uses(self):
        # Backstop: apply must refuse even if the client never validated.
        from api_server.models.tortoise_models import ScheduledTask

        portal = self.get_portal()
        row = portal.call(
            lambda: ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {"places": ["pickup_1", "dock_3"], "rounds": 1},
                },
                created_by="admin",
            )
        )
        calls = []
        fake = _fake_async_client(
            calls,
            _FakeResponse(
                json_body={
                    "destinations": [
                        {"name": "dock_3", "kind": "dropoff", "x": 12.0, "y": 3.5}
                    ]
                }
            ),
        )

        async def no_missions():
            return []

        try:
            with unittest.mock.patch(
                "api_server.routes.site_config.httpx.AsyncClient", fake
            ), unittest.mock.patch(
                "api_server.routes.site_config.active_missions", no_missions
            ):
                resp = self.client.post(
                    "/site_config/apply",
                    json={
                        "candidate": {
                            "base_commit": "abc",
                            "zones": {},
                            "destinations": [],
                        },
                        "acknowledge_fleet_pause": True,
                    },
                )
            self.assertEqual(409, resp.status_code, resp.content)
            detail = resp.json()["detail"]
            self.assertEqual("destination_in_use", detail["reason"])
            self.assertIn("pickup_1 → dock_3", detail["message"])
            # a schedule has no name, so the admin finds it in Missions →
            # Schedules by route AND creator — both must be in the message
            self.assertIn("created by admin", detail["message"])
            # the site-config HEAD read happened, the apply never did
            self.assertEqual(1, len(calls))
            self.assertTrue(calls[0][1].endswith("/site_config"))
        finally:
            portal.call(lambda: ScheduledTask.filter(id=row.id).delete())

    def test_apply_without_destinations_key_skips_the_guard(self):
        # An older client that does not manage destinations must not pay
        # for a HEAD read — and must not be able to wipe them either
        # (the sidecar keeps HEAD's when the key is absent).
        #
        # F-186 note: the apply DOES now make one extra proxy call, a
        # validate, because the retired-WAYPOINT set is not in the
        # candidate — the admin did not choose it, the derivation did —
        # so there is nothing to read it from. That is a deliberate
        # cost. What this test still pins is the original guarantee:
        # no `GET /site_config` HEAD read when destinations are not
        # managed.
        calls = []
        fake = _fake_async_client(
            calls, _FakeResponse(json_body={"state": "validating"})
        )

        async def no_missions():
            return []

        with unittest.mock.patch(
            "api_server.routes.site_config.httpx.AsyncClient", fake
        ), unittest.mock.patch(
            "api_server.routes.site_config.active_missions", no_missions
        ):
            resp = self.client.post(
                "/site_config/apply",
                json={
                    "candidate": {"base_commit": "abc", "zones": {}},
                    "acknowledge_fleet_pause": True,
                },
            )
        self.assertEqual(200, resp.status_code, resp.content)
        # no destinations HEAD read
        self.assertFalse(
            [c for c in calls if c[0] == "GET" and c[1].endswith("/site_config")]
        )
        # and the apply itself still went through
        self.assertTrue([c for c in calls if c[1].endswith("/site_config/apply")])

    def test_internal_active_missions_needs_the_shared_token(self):
        # F-86/D-23: the boundary-check callback — token-authenticated,
        # never open (the /_internal mount carries no user auth).
        resp = self.client.get("/_internal/active_missions")
        self.assertEqual(403, resp.status_code)
        resp = self.client.get(
            "/_internal/active_missions",
            headers={"x-gf-internal-token": "wrong"},
        )
        self.assertEqual(403, resp.status_code)

    def test_internal_active_missions_lists_non_terminal_tasks(self):
        from api_server.models import TaskStatus
        from api_server.models.tortoise_models import TaskState as DbTaskState

        portal = self.get_portal()
        portal.call(
            lambda: DbTaskState.update_or_create(
                {
                    "data": {},
                    "status": TaskStatus.underway,
                    "assigned_to": "gentle_bot_7",
                },
                id_="f86-boundary-row",
            )
        )
        try:
            resp = self.client.get(
                "/_internal/active_missions",
                headers={"x-gf-internal-token": "test-token"},
            )
            self.assertEqual(200, resp.status_code, resp.content)
            match = [m for m in resp.json() if m["task_id"] == "f86-boundary-row"]
            self.assertEqual(1, len(match), resp.json())
            self.assertEqual("gentle_bot_7", match[0]["robot"])
        finally:
            portal.call(lambda: DbTaskState.filter(id_="f86-boundary-row").delete())

    def test_non_admin_is_403(self):
        self.client.set_user("operator1")
        try:
            resp = self.client.get("/site_config")
            self.assertEqual(403, resp.status_code)
        finally:
            self.client.set_user("admin")
