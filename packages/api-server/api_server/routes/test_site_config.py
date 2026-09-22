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


def _census(missions_fn):
    """F-387: the guard reads mission_census(); lift a missions-list fake
    into it (no fleet tasks)."""

    async def census():
        return {"missions": await missions_fn(), "fleet_tasks": []}

    return census


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
            "api_server.routes.site_config.mission_census", _census(fake_active)
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
            "api_server.routes.site_config.mission_census", _census(no_missions)
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
                # fixture, so the robot counts as parked (evacuable).
                # F-353 (d52bfe45): the snapshot carries pose freshness —
                # a fresh fixture pose is judgeable
                [
                    {
                        "name": "gentle_bot_2",
                        "x": 9.0,
                        "y": 3.5,
                        "parked": True,
                        "unjudgeable": False,
                    }
                ],
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
                "api_server.routes.site_config.mission_census", _census(no_missions)
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
            "api_server.routes.site_config.mission_census", _census(no_missions)
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

    # ------------------------------------------------------------------
    # F-387 (G ruling 2026-09-22, D-82): the guard JUDGES BY MOTION. Real
    # TaskState rows (the F-343 lesson: never a permissive double of the
    # model); only the motion feed is stubbed, with the module's own
    # Motion verdicts, and RMF's cancel service.
    # ------------------------------------------------------------------
    def _f387_rows(self, rows):
        from api_server.models import TaskStatus
        from api_server.models.tortoise_models import TaskState as DbTaskState

        portal = self.get_portal()
        for task_id, status, robot in rows:
            portal.call(
                lambda t=task_id, st=status, r=robot: DbTaskState.update_or_create(
                    {"data": {}, "status": getattr(TaskStatus, st), "assigned_to": r},
                    id_=t,
                )
            )

        def cleanup():
            for task_id, _, _ in rows:
                portal.call(lambda t=task_id: DbTaskState.filter(id_=t).delete())

        self.addCleanup(cleanup)
        return {r[0] for r in rows}

    def _f387_motion(self, **by_robot):
        from api_server.robot_motion import MOVING, STATIONARY, UNKNOWN, Motion

        verdicts = {
            "moving": Motion(MOVING, "it moved 4.10 m in the last 10 s"),
            "stationary": Motion(STATIONARY, "it has not moved in 10 s"),
            "stale": Motion(UNKNOWN, "its position is STALE"),
        }

        def fake(robot, fleet=None):
            return verdicts.get(
                by_robot.get(robot, ""),
                Motion(UNKNOWN, "no position has been received for it"),
            )

        return unittest.mock.patch("api_server.routes.fleets.robot_motion", fake)

    def _f387_census(self, ids):
        from api_server.routes.site_config import mission_census

        with_ids = self.get_portal().call(mission_census)
        return (
            [m for m in with_ids["missions"] if m["task_id"] in ids],
            [m for m in with_ids["fleet_tasks"] if m["task_id"] in ids],
        )

    def test_f387_a_robot_charging_on_its_dock_is_not_a_running_mission(self):
        """PASSES: the f1-n51 shape — the fleet's own ChargeBattery on a
        robot standing on its dock. Not a mission, named as the fleet's
        own, and the apply goes through with no hard-confirm."""
        ids = self._f387_rows([("Charge4a8a39", "underway", "gentle_bot_5")])
        with self._f387_motion(gentle_bot_5="stationary"):
            missions, fleet_tasks = self._f387_census(ids)
            self.assertEqual([], missions)
            self.assertEqual(["Charge4a8a39"], [m["task_id"] for m in fleet_tasks])
            self.assertEqual("stationary", fleet_tasks[0]["motion"])
            self.assertIn("re-created after the restart", fleet_tasks[0]["note"])
            calls = []
            fake = _fake_async_client(calls, _FakeResponse(json_body={"state": "validating"}))
            with unittest.mock.patch("api_server.routes.site_config.httpx.AsyncClient", fake):
                resp = self.client.post(
                    "/site_config/apply",
                    json={"candidate": {"base_commit": "abc", "zones": {}},
                          "acknowledge_fleet_pause": True},
                )
            self.assertEqual(200, resp.status_code, resp.content)
            self.assertTrue([c for c in calls if c[1].endswith("/site_config/apply")])

    def test_f387_a_robot_in_motion_on_the_fleets_own_task_is_a_mission(self):
        """FIRES: the same ChargeBattery on a robot DRIVING home blocks —
        with the motion and what was seen, so the admin knows why."""
        ids = self._f387_rows([("Charge77aa01", "underway", "gentle_bot_3")])
        with self._f387_motion(gentle_bot_3="moving"):
            missions, fleet_tasks = self._f387_census(ids)
            self.assertEqual(["Charge77aa01"], [m["task_id"] for m in missions])
            self.assertEqual("moving", missions[0]["motion"])
            self.assertIn("moved", missions[0]["why"])
            self.assertEqual([], fleet_tasks)
            calls = []
            fake = _fake_async_client(calls, _FakeResponse(json_body={"state": "validating"}))
            with unittest.mock.patch("api_server.routes.site_config.httpx.AsyncClient", fake):
                resp = self.client.post(
                    "/site_config/apply",
                    json={"candidate": {"base_commit": "abc", "zones": {}},
                          "acknowledge_fleet_pause": True},
                )
            self.assertEqual(409, resp.status_code, resp.content)
            detail = resp.json()["detail"]
            self.assertIn("Charge77aa01", [m["task_id"] for m in detail["missions"]])
            self.assertEqual(0, len(calls))  # the sidecar was never reached

    def test_f387_an_operator_mission_blocks_whatever_its_robot_is_doing(self):
        """FIRES: a patrol queued on a robot that is standing still is lost
        to a restart as surely as one under way — motion never excuses an
        operator's mission."""
        ids = self._f387_rows([
            ("patrol.dispatch-2001", "queued", "gentle_bot_2"),
            ("a417b316-4ff3-4d5e-b7e3-4387ffd086cd", "underway", "gentle_bot_4"),
        ])
        with self._f387_motion(gentle_bot_2="stationary", gentle_bot_4="stationary"):
            missions, fleet_tasks = self._f387_census(ids)
        self.assertEqual(ids, {m["task_id"] for m in missions})
        self.assertEqual([], fleet_tasks)
        self.assertTrue(all("motion" not in m for m in missions))

    def test_f387_motion_that_cannot_be_seen_counts_as_running(self):
        """A stale or missing pose is not evidence of stillness (F-191):
        the fleet's own task on such a robot still counts, and says why."""
        ids = self._f387_rows([
            ("f36-retreat-gentle_bot_1-7", "underway", "gentle_bot_1"),
            ("f338-hold-gentle_bot_6-9", "underway", "gentle_bot_6"),
        ])
        with self._f387_motion(gentle_bot_1="stale"):
            missions, _ = self._f387_census(ids)
        by_id = {m["task_id"]: m for m in missions}
        self.assertEqual(ids, set(by_id))
        self.assertIn("STALE", by_id["f36-retreat-gentle_bot_1-7"]["why"])
        self.assertIn("no position", by_id["f338-hold-gentle_bot_6-9"]["why"])

    def test_f387_a_fleet_task_on_no_robot_moves_nothing(self):
        ids = self._f387_rows([("wait.unassigned-1", "queued", None)])
        with self._f387_motion():
            missions, fleet_tasks = self._f387_census(ids)
        self.assertEqual([], missions)
        self.assertEqual("no robot", fleet_tasks[0]["motion"])

    def test_f387_the_upgrade_gate_sees_only_what_blocks(self):
        """/_internal/active_missions is what install.sh upgrade counts: the
        same judgment, the same list (shape unchanged — a list)."""
        ids = self._f387_rows([
            ("Charge4a8a40", "underway", "gentle_bot_5"),
            ("Charge77aa02", "underway", "gentle_bot_3"),
        ])
        with self._f387_motion(gentle_bot_5="stationary", gentle_bot_3="moving"):
            resp = self.client.get(
                "/_internal/active_missions",
                headers={"x-gf-internal-token": "test-token"},
            )
        self.assertEqual(200, resp.status_code, resp.content)
        listed = {m["task_id"] for m in resp.json()} & ids
        self.assertEqual({"Charge77aa02"}, listed)

    def test_f387_a_restart_closes_the_fleets_own_tasks_with_their_own_record(self):
        """scope=fleet (sent whenever a restart goes ahead WITHOUT a
        hard-confirm) cancels only the fleet's own stationary tasks, labelled
        as such; scope=all (the hard-confirmed path) cancels everything,
        each labelled for what it is; an unknown scope is refused."""
        ids = self._f387_rows([
            ("Charge4a8a41", "underway", "gentle_bot_5"),
            ("patrol.dispatch-2002", "underway", "gentle_bot_2"),
        ])
        sent = []

        class _Service:
            async def call(self, payload):
                sent.append(payload)
                return "{}"

        headers = {"x-gf-internal-token": "test-token"}
        with self._f387_motion(gentle_bot_5="stationary", gentle_bot_2="moving"), \
                unittest.mock.patch("api_server.rmf_io.tasks_service", lambda: _Service()):
            resp = self.client.post(
                "/_internal/cancel_missions",
                json={"applied_by": "admin", "scope": "fleet"},
                headers=headers,
            )
            self.assertEqual(200, resp.status_code, resp.content)
            self.assertEqual({"Charge4a8a41"}, {m["task_id"] for m in resp.json()} & ids)
            fleet_sent = [p for p in sent if "Charge4a8a41" in p]
            self.assertEqual(1, len(fleet_sent))
            self.assertIn("re-created after the restart", fleet_sent[0])
            self.assertFalse([p for p in sent if "patrol.dispatch-2002" in p])

            sent.clear()
            resp = self.client.post(
                "/_internal/cancel_missions",
                json={"applied_by": "admin"},
                headers=headers,
            )
            self.assertEqual(200, resp.status_code, resp.content)
            self.assertEqual(ids, {m["task_id"] for m in resp.json()} & ids)
            patrol = [p for p in sent if "patrol.dispatch-2002" in p]
            self.assertIn("Interrupted by a site configuration change", patrol[0])

            resp = self.client.post(
                "/_internal/cancel_missions",
                json={"applied_by": "admin", "scope": "everything"},
                headers=headers,
            )
            self.assertEqual(422, resp.status_code, resp.content)

    def test_f387_motion_is_read_from_the_live_fleet_state_feed(self):
        """The wiring, not a stub: `robot_motion` answers from what the
        `/fleet_states` ingest (on_fleet_positions) stored."""
        import time
        from types import SimpleNamespace as NS

        from api_server.routes import fleets as fleets_route

        fleets_route._reset_freshness_for_test()
        self.addCleanup(fleets_route._reset_freshness_for_test)

        def msg(stamp, xs):
            t = NS(sec=int(stamp), nanosec=int((stamp % 1) * 1e9))
            return NS(name="gentle_fleet", robots=[
                NS(name=n, location=NS(t=t, x=x, y=15.5, yaw=0.0, level_name="L1"))
                for n, x in xs.items()
            ])

        start = time.monotonic() - 15.0
        for k in range(150):  # 15 s at 10 Hz, ending now
            fleets_route.on_fleet_positions(
                msg(500.0 + k * 0.1, {"gentle_bot_5": 1.5, "gentle_bot_3": 1.5 + 0.05 * k}),
                now=start + k * 0.1,
            )
        self.assertEqual("stationary", fleets_route.robot_motion("gentle_bot_5").state)
        self.assertEqual("moving", fleets_route.robot_motion("gentle_bot_3").state)
        self.assertEqual("unknown", fleets_route.robot_motion("gentle_bot_9").state)

    def test_non_admin_is_403(self):
        self.client.set_user("operator1")
        try:
            resp = self.client.get("/site_config")
            self.assertEqual(403, resp.status_code)
        finally:
            self.client.set_user("admin")


class TestRememberedRetiredWaypoints(unittest.TestCase):
    """F-243 (apply path): the review's validate tells this service what
    a candidate retires; the apply must not pay a second derivation to
    learn it again. Both ways: a reviewed candidate is answered from
    memory (positions do not change the answer), a candidate never
    reviewed — or one whose zones changed — is not."""

    def setUp(self):
        from api_server.routes import site_config as sc

        self.sc = sc
        sc._RETIRED_CACHE.clear()

    def test_reviewed_candidate_is_remembered_regardless_of_positions(self):
        sc = self.sc
        candidate = {"base_commit": "abc", "zones": {"no_go_zones": [{"name": "z"}]},
                     "destinations": [], "robot_positions": [{"x": 1.0}]}
        report = {"retired_waypoints": [{"waypoint": "j_w1"}, {"waypoint": "j_e1"}]}
        sc.remember_retired(candidate, report)
        moved = dict(candidate, robot_positions=[{"x": 2.0}])
        self.assertEqual(sc.recall_retired(moved), ["j_w1", "j_e1"])

    def test_a_changed_or_unreviewed_candidate_is_not_answered_from_memory(self):
        sc = self.sc
        candidate = {"base_commit": "abc", "zones": {"no_go_zones": []}, "destinations": []}
        self.assertIsNone(sc.recall_retired(candidate))
        sc.remember_retired(candidate, {"retired_waypoints": []})
        self.assertEqual(sc.recall_retired(candidate), [])
        edited = dict(candidate, zones={"no_go_zones": [{"name": "new"}]})
        self.assertIsNone(sc.recall_retired(edited))
        # a report with no retired list (a refusal, a non-dict) is not remembered
        sc.remember_retired(edited, {"ok": False})
        self.assertIsNone(sc.recall_retired(edited))
        sc.remember_retired(edited, "not a report")
        self.assertIsNone(sc.recall_retired(edited))
