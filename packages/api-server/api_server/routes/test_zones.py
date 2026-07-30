"""GentleFleet fork: /zones read-only route tests (DR-3/FR-10)."""

import os
import tempfile

from api_server.app_config import app_config
from api_server.test import AppFixture

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
