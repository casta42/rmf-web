# F-339 / F-338 class guards, api-server half — proven BOTH WAYS without ROS
# or a database.
#
# The graph is testsite_a's charger spur as /nav_graphs speaks it (F-333):
# vertex 18 `gentle_bot_3_charger` (1.50,7.60) joined to vertex 4 `j_w1`
# (3.00,7.60) by directed lanes 16 (charger -> j_w1) and 17 (j_w1 ->
# charger). A corridor j_w1 -> j_mid -> dropoff_2 gives a partial-cordon
# case: dropoff_2's OWN lanes stay open while the corridor into it is cut.

import pytest

from api_server import cordon, lane_closures

GRAPH = {
    "vertices": [
        ("v0", 0.0, 0.0),
        ("v1", 1.0, 0.0),
        ("v2", 2.0, 0.0),
        ("v3", 3.0, 0.0),
        ("j_w1", 3.00, 7.60),
    ]
    + [(f"pad{i}", float(i), 99.0) for i in range(5, 18)]
    + [
        ("gentle_bot_3_charger", 1.50, 7.60),  # 18
        ("dropoff_2", 10.0, 10.0),  # 19
        ("j_mid", 9.0, 10.0),  # 20
        ("j_north", 10.0, 11.0),  # 21
    ],
    "edges": [(i, i + 1) for i in range(16)]
    + [
        (18, 4),  # 16: charger -> j_w1
        (4, 18),  # 17: j_w1 -> charger
        (20, 19),  # 18: j_mid -> dropoff_2
        (21, 19),  # 19: j_north -> dropoff_2
        (4, 20),  # 20: j_w1 -> j_mid
        (20, 4),  # 21: j_mid -> j_w1
        (19, 20),  # 22: dropoff_2 -> j_mid
    ],
}
SPUR = frozenset({16, 17})
CORRIDOR = frozenset({20, 21})
CHARGER, J_W1, DROPOFF, J_MID = 18, 4, 19, 20


@pytest.fixture(autouse=True)
def _clean():
    cordon._reset_for_test()
    lane_closures._reset_for_test()
    lane_closures.set_publisher(None)
    yield
    lane_closures.set_publisher(None)


# ---- geometry: a lane is its endpoints, never its index -------------------


def test_a_lane_resolves_by_its_endpoints():
    geometry = cordon.lane_geometry(GRAPH, 17)
    assert (
        geometry["entry"][0] == "j_w1" and geometry["exit"][0] == "gentle_bot_3_charger"
    )
    assert cordon.resolve_lane(GRAPH, (3.0, 7.6), (1.5, 7.6)) == 17
    assert cordon.resolve_lane(GRAPH, (1.5, 7.6), (3.0, 7.6)) == 16


def test_a_lane_that_no_longer_exists_does_not_resolve_onto_a_neighbour():
    # the same endpoints 0.6 m away (an FR-33 sibling's distance) is a
    # different lane
    assert cordon.resolve_lane(GRAPH, (3.0, 8.2), (1.5, 8.2)) is None
    assert cordon.resolve_lane(GRAPH, (50.0, 50.0), (51.0, 50.0)) is None
    assert cordon.lane_geometry(GRAPH, 999) is None
    assert cordon.lane_geometry(None, 0) is None


def test_renumbering_keeps_the_same_lane():
    # the same site re-derived with one lane inserted at the front
    shifted = {"vertices": GRAPH["vertices"], "edges": [(0, 2)] + GRAPH["edges"]}
    assert cordon.resolve_lane(shifted, (3.0, 7.6), (1.5, 7.6)) == 18


# ---- reachability -------------------------------------------------------


def test_reachable_over_open_lanes_and_not_over_closed_ones():
    assert cordon.reachable(GRAPH, frozenset(), J_W1, CHARGER) is True
    assert cordon.reachable(GRAPH, SPUR, J_W1, CHARGER) is False
    assert (
        cordon.reachable(GRAPH, frozenset({16}), J_W1, CHARGER) is True
    )  # 17 still open
    assert cordon.reachable(GRAPH, CORRIDOR, J_W1, DROPOFF) is False
    assert cordon.reachable(GRAPH, CORRIDOR, J_MID, DROPOFF) is True


def test_reachability_skips_when_it_cannot_see():
    assert cordon.reachable(None, SPUR, J_W1, CHARGER) is None
    assert cordon.reachable(GRAPH, SPUR, 999, CHARGER) is None
    assert cordon.reachable(GRAPH, SPUR, CHARGER, CHARGER) is True


def test_a_robot_on_a_vertex_is_placed_and_one_off_graph_is_not():
    assert cordon.nearest_vertex(GRAPH, 3.1, 7.5) == J_W1
    assert cordon.nearest_vertex(GRAPH, 6.0, 7.6) is None  # 3 m from anything


# ---- the direct/dispatch refusal (F-338, api-server half) ---------------


def test_a_partial_cordon_that_cuts_the_route_is_refused():
    # dropoff_2's own lanes are open (cordon_refusal says nothing) but the
    # corridor into it from where the robot stands is closed
    assert cordon.cordoned_place(GRAPH, CORRIDOR, ["dropoff_2"]) is None
    hop = cordon.unreachable_hop(GRAPH, CORRIDOR, [J_W1], ["dropoff_2"])
    assert hop == ("the robot", "dropoff_2")


def test_the_same_mission_is_accepted_with_no_cordon_and_from_the_far_side():
    assert cordon.unreachable_hop(GRAPH, frozenset(), [J_W1], ["dropoff_2"]) is None
    assert cordon.unreachable_hop(GRAPH, CORRIDOR, [J_MID], ["dropoff_2"]) is None


def test_a_later_hop_is_judged_from_the_place_before_it():
    # first stop fine, second stop behind the spur
    hop = cordon.unreachable_hop(GRAPH, SPUR, [J_MID], ["j_w1", "gentle_bot_3_charger"])
    assert hop == ("j_w1", "gentle_bot_3_charger")


def test_the_refusal_fails_open_when_it_cannot_see():
    assert cordon.unreachable_hop(None, SPUR, [J_W1], ["dropoff_2"]) is None
    assert cordon.unreachable_hop(GRAPH, SPUR, [], ["gentle_bot_3_charger"]) is None
    assert (
        cordon.unreachable_hop(GRAPH, frozenset(), [J_W1], ["gentle_bot_3_charger"])
        is None
    )
    # a place this graph does not name belongs to another fleet
    assert cordon.unreachable_hop(GRAPH, SPUR, [J_W1], ["lift_lobby_L2"]) is None


def test_reachability_refusal_reads_the_feeds_and_names_the_lanes():
    cordon._graphs["gentle_fleet"] = GRAPH
    cordon._closed["gentle_fleet"] = CORRIDOR
    why = cordon.reachability_refusal("gentle_fleet", ["dropoff_2"], [(3.0, 7.6)])
    assert why and "dropoff_2" in why and "[20, 21]" in why
    # a site with lifts cannot be judged this way
    assert (
        cordon.reachability_refusal(
            "gentle_fleet", ["dropoff_2"], [(3.0, 7.6)], has_lifts=True
        )
        is None
    )
    # a robot nowhere near a vertex gives no start: no objection
    assert (
        cordon.reachability_refusal("gentle_fleet", ["dropoff_2"], [(6.0, 7.6)]) is None
    )


# ---- the editor warning (F-338): a closure that strands a charger --------

ROBOTS = [
    {"name": "gentle_bot_3", "x": 3.0, "y": 7.6, "charger": "gentle_bot_3_charger"},
    {"name": "gentle_bot_9", "x": 9.0, "y": 10.0, "charger": "nowhere"},
    {"name": "gentle_bot_x", "x": 6.0, "y": 7.6, "charger": "gentle_bot_3_charger"},
]


def test_closing_the_spur_names_the_robot_it_strands():
    strands = cordon.stranded_chargers(GRAPH, SPUR, ROBOTS)
    assert [s["robot"] for s in strands] == ["gentle_bot_3"]
    assert strands[0]["charger"] == "gentle_bot_3_charger"
    assert strands[0]["lanes"] == [17]  # the closed lane on its boundary


def test_a_closure_elsewhere_strands_nobody():
    assert cordon.stranded_chargers(GRAPH, CORRIDOR, ROBOTS) == []
    assert cordon.stranded_chargers(GRAPH, frozenset(), ROBOTS) == []
    # an unknown charger or an off-vertex robot is skipped, not convicted
    assert cordon.stranded_chargers(GRAPH, SPUR, ROBOTS[1:]) == []


# ---- durable intent: publish, confirm, re-assert -------------------------


class _Publisher:
    def __init__(self):
        self.sent = []

    def __call__(self, fleet, close, open_):
        self.sent.append((fleet, list(close), list(open_)))


def _intend(fleet, *lanes):
    for lane in lanes:
        g = cordon.lane_geometry(GRAPH, lane)
        lane_closures._intent.setdefault(fleet, []).append(
            {
                "id": 100 + lane,
                "fleet": fleet,
                "entry_name": g["entry"][0],
                "entry_x": g["entry"][1],
                "entry_y": g["entry"][2],
                "exit_name": g["exit"][0],
                "exit_x": g["exit"][1],
                "exit_y": g["exit"][2],
                "lane_index_at_request": lane,
                "requested_by": "operator",
                "unix_millis_request_time": 0,
                "reason": "spill",
            }
        )


def test_the_whole_intent_is_published_and_resolved_against_the_graph():
    pub = _Publisher()
    lane_closures.set_publisher(pub)
    cordon._graphs["gentle_fleet"] = GRAPH
    _intend("gentle_fleet", 16, 17)
    assert lane_closures.publish("gentle_fleet") == [16, 17]
    assert pub.sent == [("gentle_fleet", [16, 17], [])]
    assert lane_closures.status("gentle_fleet")["intended_lanes"] == [16, 17]


def test_a_restarted_adapter_reporting_nothing_is_re_asserted():
    pub = _Publisher()
    lane_closures.set_publisher(pub)
    cordon._graphs["gentle_fleet"] = GRAPH
    _intend("gentle_fleet", 16, 17)
    # the fleet confirms: in force
    assert (
        lane_closures.on_fleet_confirmation("gentle_fleet", frozenset({16, 17}), 1)
        is False
    )
    assert lane_closures.status("gentle_fleet")["in_force"] is True
    # the adapter restarts and reports an honest empty set
    assert lane_closures.on_fleet_confirmation("gentle_fleet", frozenset(), 2) is True
    assert pub.sent[-1] == ("gentle_fleet", [16, 17], [])
    s = lane_closures.status("gentle_fleet")
    assert s["in_force"] is False and s["missing_from_fleet"] == [16, 17]
    # throttled: a second empty report inside the interval does not spam
    assert lane_closures.on_fleet_confirmation("gentle_fleet", frozenset(), 3) is False


def test_an_open_fleet_with_no_intent_is_never_re_asserted():
    # the boring case: nothing closed, adapter restarts, nothing published
    pub = _Publisher()
    lane_closures.set_publisher(pub)
    cordon._graphs["gentle_fleet"] = GRAPH
    assert lane_closures.on_fleet_confirmation("gentle_fleet", frozenset(), 1) is False
    assert pub.sent == []
    assert lane_closures.status("gentle_fleet")["in_force"] is True


def test_a_re_derived_graph_retires_the_lane_instead_of_guessing():
    pub = _Publisher()
    lane_closures.set_publisher(pub)
    _intend("gentle_fleet", 17)
    # the spur was closed by a zone: the derived graph has no lane 4->18
    cordon._graphs["gentle_fleet"] = {
        "vertices": GRAPH["vertices"],
        "edges": [e for e in GRAPH["edges"] if e not in ((18, 4), (4, 18))],
    }
    resolved, unresolved = lane_closures.resolve("gentle_fleet")
    assert resolved == {} and [r["lane_index_at_request"] for r in unresolved] == [17]
    s = lane_closures.status("gentle_fleet")
    assert s["intended_lanes"] == [] and s["intent"][0]["resolved"] is False
    # and nothing is published for a lane that does not exist
    assert lane_closures.publish("gentle_fleet") == []


def test_without_a_graph_nothing_is_published_or_judged():
    pub = _Publisher()
    lane_closures.set_publisher(pub)
    _intend("gentle_fleet", 17)
    assert lane_closures.publish("gentle_fleet") is None
    assert lane_closures.on_fleet_confirmation("gentle_fleet", frozenset(), 1) is False
    assert pub.sent == []
    assert lane_closures.status("gentle_fleet")["graph_known"] is False


def test_an_empty_intent_is_still_published_when_the_graph_arrives():
    # the boring case: a fresh site with nothing closed. The adapter waits
    # for the first latched message before admitting a robot, so "nothing
    # is closed" must be said out loud (first f1-n28 boot: it was not)
    pub = _Publisher()
    lane_closures.set_publisher(pub)
    cordon._graphs["gentle_fleet"] = GRAPH
    lane_closures.on_graph("gentle_fleet")
    assert pub.sent == [("gentle_fleet", [], [])]
