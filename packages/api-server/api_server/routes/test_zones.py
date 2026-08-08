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
