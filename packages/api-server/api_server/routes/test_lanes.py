# F-339 / F-338 route guard — the operator's cordon, proven both ways at the
# HTTP surface (the pure rules are proven in test_lane_closures.py).
#
# Runs in the api-server image (see the memory note on api-server tests):
#   python3 -m unittest api_server.routes.test_lanes

from api_server import cordon, lane_closures
from api_server.models import tortoise_models as ttm
from api_server.test import AppFixture

FLEET = "gentle_fleet"
GRAPH = {
    "vertices": [
        ("j_w1", 3.00, 7.60),
        ("gentle_bot_3_charger", 1.50, 7.60),
        ("dropoff_2", 10.0, 10.0),
    ],
    "edges": [
        (1, 0),  # 0: charger -> j_w1
        (0, 1),  # 1: j_w1 -> charger
        (0, 2),  # 2: j_w1 -> dropoff_2
        (2, 0),  # 3: dropoff_2 -> j_w1
    ],
}


class _Publisher:
    def __init__(self):
        self.sent = []

    def __call__(self, fleet, close, open_):
        self.sent.append((fleet, list(close), list(open_)))


class TestLaneClosures(AppFixture):
    def setUp(self):
        super().setUp()
        cordon._reset_for_test()
        lane_closures._reset_for_test()
        cordon._graphs[FLEET] = GRAPH
        cordon.on_chargers(FLEET, {"gentle_bot_3": "gentle_bot_3_charger"})
        self.pub = _Publisher()
        lane_closures.set_publisher(self.pub)

        async def seed():
            await ttm.LaneClosure.all().delete()
            await ttm.FleetState.update_or_create(
                {
                    "data": {
                        "name": FLEET,
                        "robots": {"gentle_bot_3": {"location": {"x": 3.0, "y": 7.6}}},
                    }
                },
                name=FLEET,
            )

        self.get_portal().call(seed)

    def tearDown(self):
        lane_closures.set_publisher(None)
        super().tearDown()

    def post(self, **body):
        return self.client.post("/lanes/closures", json={"fleet": FLEET, **body})

    def test_a_closure_that_strands_a_charger_needs_confirm(self):
        resp = self.post(close=[1], reason="spill")
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["strands"][0]["robot"], "gentle_bot_3")
        self.assertEqual(detail["strands"][0]["charger"], "gentle_bot_3_charger")
        # nothing was persisted or published
        self.assertEqual(self.pub.sent, [])
        self.assertEqual(
            self.client.get(f"/lanes/closures?fleet={FLEET}").json()["intended_lanes"],
            [],
        )

    def test_confirmed_it_is_persisted_published_and_reported(self):
        resp = self.post(close=[1], reason="spill", confirm=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["closed_now"], [1])
        self.assertEqual(body["intended_lanes"], [1])
        self.assertEqual(self.pub.sent[-1], (FLEET, [1], []))
        # the fleet has not confirmed yet: honest
        self.assertFalse(body["in_force"])
        lane_closures.on_fleet_confirmation(FLEET, frozenset({1}), 1)
        status = self.client.get(f"/lanes/closures?fleet={FLEET}").json()
        self.assertTrue(status["in_force"])
        self.assertEqual(status["intent"][0]["requested_by"], "admin")
        # survives a reload from the ledger (an api-server restart)
        lane_closures._intent.clear()
        self.get_portal().call(lane_closures.load)
        self.assertEqual(lane_closures.intended_lanes(FLEET), frozenset({1}))

    def test_a_closure_that_strands_nobody_needs_no_confirm(self):
        resp = self.post(close=[2], reason="works")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["strands"], [])
        self.assertEqual(resp.json()["intended_lanes"], [2])

    def test_opening_releases_the_intent_and_publishes_the_open(self):
        self.post(close=[1], confirm=True)
        resp = self.post(open=[1])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["opened_now"], [1])
        self.assertEqual(resp.json()["intended_lanes"], [])
        self.assertEqual(self.pub.sent[-1], (FLEET, [], [1]))

    def test_an_unknown_lane_or_fleet_is_refused_not_guessed(self):
        self.assertEqual(self.post(close=[99]).status_code, 409)
        resp = self.client.post(
            "/lanes/closures", json={"fleet": "ghosts", "close": [1]}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.pub.sent, [])
