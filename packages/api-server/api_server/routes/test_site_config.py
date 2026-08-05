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
        self.assertEqual(
            "test-token", kwargs["headers"]["x-gf-internal-token"]
        )

    def test_apply_refused_while_missions_active_then_hard_confirm(self):
        calls = []
        fake = _fake_async_client(
            calls, _FakeResponse(json_body={"state": "validating"})
        )
        missions = [
            {"task_id": "patrol.dispatch-1", "status": "underway",
             "robot": "gentle_bot_2"}
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
            self.assertEqual(
                "patrol.dispatch-1", detail["missions"][0]["task_id"]
            )
            self.assertEqual(0, len(calls))  # sidecar never reached

            body["acknowledge_active_missions"] = True
            confirmed = self.client.post("/site_config/apply", json=body)
            self.assertEqual(200, confirmed.status_code, confirmed.content)
            self.assertEqual(1, len(calls))

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
        _, _, kwargs = calls[0]
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
            portal.call(
                lambda: DbTaskState.filter(id_="e5-guard-enum-repr").delete()
            )

    def test_non_admin_is_403(self):
        self.client.set_user("operator1")
        try:
            resp = self.client.get("/site_config")
            self.assertEqual(403, resp.status_code)
        finally:
            self.client.set_user("admin")
