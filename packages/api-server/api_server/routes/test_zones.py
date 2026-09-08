"""GentleFleet fork: /zones read-only route tests (DR-3/FR-10, D-24)."""

import os
import tempfile

from api_server.app_config import app_config
from api_server.test import AppFixture

NAV_GRAPH_YAML = """
building_name: testsite_a
levels:
  L1:
    vertices:
    - [1.5, 1.5, {name: gentle_bot_1_charger, is_charger: true}]
    - [3.0, 1.5, {name: j_sw}]
    - [5.0, 3.0, {gf_generated: spill}]
    lanes:
    - [0, 1, {}]
    - [1, 0, {}]
    - [1, 2, {gf_generated: spill}]
    - [2, 1, {gf_generated: spill}]
    - [2, 0, {speed_limit: 0.3}]
"""

ZONES_YAML = """
site: testsite_a
level: L1
no_go_zones:
  - name: rack_a
    polygon: [[6.0, 5.0], [16.0, 5.0], [16.0, 6.2], [6.0, 6.2]]
speed_zones:
  - name: narrow_aisle
    limit: 0.3
    lanes:
      - [j_na, j_s3]
mutex_zones:
  - name: narrow_aisle_de
    entries: [j_na, j_s3]
    lanes:
      - [j_na, j_s3]
    polygon: [[25.2, 4.0], [27.0, 4.0], [27.0, 16.0], [25.2, 16.0]]
"""


class TestZonesRoute(AppFixture):
    def test_serves_zones_yaml(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(ZONES_YAML)
            path = f.name
        try:
            old = app_config.zones_file
            app_config.zones_file = path
            resp = self.client.get("/zones")
            self.assertEqual(200, resp.status_code, resp.content)
            zones = resp.json()
            self.assertEqual("testsite_a", zones["site"])
            self.assertEqual("rack_a", zones["no_go_zones"][0]["name"])
            self.assertEqual(0.3, zones["speed_zones"][0]["limit"])
            self.assertEqual("narrow_aisle_de", zones["mutex_zones"][0]["name"])
        finally:
            app_config.zones_file = old
            os.unlink(path)

    def test_404_when_unconfigured(self):
        old = app_config.zones_file
        app_config.zones_file = None
        try:
            self.assertEqual(404, self.client.get("/zones").status_code)
        finally:
            app_config.zones_file = old

    def test_serves_the_derived_nav_graph(self):
        """D-24: /zones/nav_graph is the graph the fleet drives —
        directed file entries become undirected lanes with a
        bidirectional flag, names come out of params, provenance params
        (gf_generated) survive."""
        with tempfile.TemporaryDirectory() as site_dir:
            zones_path = os.path.join(site_dir, "zones.yaml")
            with open(zones_path, "w", encoding="utf8") as f:
                f.write(ZONES_YAML)
            os.mkdir(os.path.join(site_dir, "nav_graphs"))
            with open(
                os.path.join(site_dir, "nav_graphs", "0.yaml"), "w", encoding="utf8"
            ) as f:
                f.write(NAV_GRAPH_YAML)
            old = app_config.zones_file
            app_config.zones_file = zones_path
            try:
                resp = self.client.get("/zones/nav_graph")
                self.assertEqual(200, resp.status_code, resp.content)
                graph = resp.json()
                self.assertEqual("L1", graph["level"])
                self.assertEqual(3, len(graph["vertices"]))
                self.assertEqual("gentle_bot_1_charger", graph["vertices"][0]["name"])
                self.assertTrue(graph["vertices"][0]["params"]["is_charger"])
                self.assertEqual("", graph["vertices"][2]["name"])
                self.assertEqual("spill", graph["vertices"][2]["params"]["gf_generated"])
                lanes = {(lane["a"], lane["b"]): lane for lane in graph["lanes"]}
                self.assertEqual(3, len(lanes))
                self.assertTrue(lanes[(0, 1)]["bidirectional"])
                self.assertTrue(lanes[(1, 2)]["bidirectional"])
                self.assertEqual("spill", lanes[(1, 2)]["params"]["gf_generated"])
                # one directed entry only -> one-way
                self.assertFalse(lanes[(2, 0)]["bidirectional"])
                self.assertEqual(0.3, lanes[(2, 0)]["params"]["speed_limit"])
            finally:
                app_config.zones_file = old

    def test_nav_graph_404_when_missing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(ZONES_YAML)
            path = f.name
        old = app_config.zones_file
        app_config.zones_file = path
        try:
            self.assertEqual(404, self.client.get("/zones/nav_graph").status_code)
        finally:
            app_config.zones_file = old
            os.unlink(path)


class TestMutexStateRoute(AppFixture):
    """FR-9d / D-58 (F-259) — the aisle's holder, and the guarded release.

    Proven both ways at this layer: the read path serves what the adapter
    reported and REFUSES to answer from a stale report; the release is
    admin-only, needs a reason, refuses over a body naming it, refuses an
    already-free zone, and refuses when the evidence is missing. A
    control that acts without evidence is the F-191 class.
    """

    def setUp(self):
        super().setUp()
        from api_server.routes import zones as zones_route

        self.zones_route = zones_route
        zones_route._zone_states = {}
        zones_route._zone_states_at = None
        self.published = []
        zones_route._release_pub = _FakePub(self.published)

    def tearDown(self):
        self.zones_route._release_pub = None
        self.zones_route._zone_states = {}
        self.zones_route._zone_states_at = None
        super().tearDown()

    def _report(self, zone):
        import json
        import time

        self.zones_route.on_zone_states(
            json.dumps({"fleet": "gentle_fleet", "zones": [zone]})
        )
        self.zones_route._zone_states_at = time.monotonic()

    HELD = {
        "name": "narrow_aisle_de",
        "holder": "gentle_bot_3",
        "holder_entered": True,
        "held_s": 42.0,
        "waiters": [{"robot": "gentle_bot_5", "waiting_s": 12.0}],
        "bodies_inside": [],
    }

    # -- read path -----------------------------------------------------
    def test_serves_the_holder_and_the_queue(self):
        self._report(self.HELD)
        resp = self.client.get("/zones/mutex_state")
        self.assertEqual(200, resp.status_code, resp.content)
        zone = resp.json()["zones"][0]
        self.assertEqual("gentle_bot_3", zone["holder"])
        self.assertEqual("gentle_bot_5", zone["waiters"][0]["robot"])

    def test_503_before_the_adapter_has_ever_reported(self):
        resp = self.client.get("/zones/mutex_state")
        self.assertEqual(503, resp.status_code)
        self.assertIn("unknown", resp.json()["detail"])

    def test_503_when_the_report_is_stale(self):
        import time

        self._report(self.HELD)
        self.zones_route._zone_states_at = time.monotonic() - 60.0
        resp = self.client.get("/zones/mutex_state")
        self.assertEqual(503, resp.status_code)
        self.assertIn("stale", resp.json()["detail"])

    def test_undecodable_report_is_ignored_not_believed(self):
        self.zones_route.on_zone_states("{not json")
        self.assertEqual(503, self.client.get("/zones/mutex_state").status_code)

    # -- the release ---------------------------------------------------
    def test_release_publishes_the_command_with_actor_and_reason(self):
        import json

        self._report(self.HELD)
        resp = self.client.post(
            "/zones/mutex/narrow_aisle_de/release",
            json={"reason": "bot_3 wedged, recovering by hand"},
        )
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual("gentle_bot_3", resp.json()["released_from"])
        sent = json.loads(self.published[0])
        self.assertEqual("narrow_aisle_de", sent["zone"])
        self.assertEqual("admin", sent["actor"])
        self.assertEqual("bot_3 wedged, recovering by hand", sent["reason"])

    def test_release_is_refused_while_a_body_is_inside_naming_it(self):
        held = dict(self.HELD)
        held["bodies_inside"] = ["gentle_bot_3"]
        self._report(held)
        resp = self.client.post(
            "/zones/mutex/narrow_aisle_de/release", json={"reason": "stuck"}
        )
        self.assertEqual(409, resp.status_code)
        self.assertIn("gentle_bot_3", resp.json()["detail"])
        self.assertEqual([], self.published)

    def test_release_of_a_free_zone_is_refused(self):
        free = dict(self.HELD)
        free["holder"] = None
        self._report(free)
        resp = self.client.post(
            "/zones/mutex/narrow_aisle_de/release", json={"reason": "tidy up"}
        )
        self.assertEqual(409, resp.status_code)
        self.assertEqual([], self.published)

    def test_release_needs_a_reason(self):
        self._report(self.HELD)
        resp = self.client.post(
            "/zones/mutex/narrow_aisle_de/release", json={"reason": "   "}
        )
        self.assertEqual(422, resp.status_code)
        self.assertEqual([], self.published)

    def test_release_of_an_unknown_zone_is_404(self):
        self._report(self.HELD)
        resp = self.client.post(
            "/zones/mutex/no_such_aisle/release", json={"reason": "x"}
        )
        self.assertEqual(404, resp.status_code)

    def test_release_without_evidence_is_refused(self):
        """No adapter report: the control has nothing to check the aisle
        against, so it must not act (F-191)."""
        resp = self.client.post(
            "/zones/mutex/narrow_aisle_de/release", json={"reason": "x"}
        )
        self.assertEqual(503, resp.status_code)
        self.assertEqual([], self.published)

    def test_release_is_admin_only(self):
        self._report(self.HELD)
        self.client.set_user("operator1")
        try:
            resp = self.client.post(
                "/zones/mutex/narrow_aisle_de/release", json={"reason": "x"}
            )
            self.assertEqual(403, resp.status_code)
            self.assertEqual([], self.published)
        finally:
            self.client.set_user("admin")

    def test_reading_the_state_is_not_admin_only(self):
        """An operator must be able to SEE who holds the aisle — that is
        the DR-3 defect being closed. Only acting is privileged."""
        self._report(self.HELD)
        self.client.set_user("operator1")
        try:
            self.assertEqual(200, self.client.get("/zones/mutex_state").status_code)
        finally:
            self.client.set_user("admin")


class _FakePub:
    def __init__(self, sink):
        self.sink = sink

    def publish(self, msg):
        self.sink.append(msg.data)
