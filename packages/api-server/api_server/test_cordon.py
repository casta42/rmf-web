# F-332 / F-333 class guard, api-server half.
#
# Proven both ways against the REAL testsite_a numbers measured on
# 2026-09-18: the fleet graph (/nav_graphs, 51 vertices / 102 directed
# edges) in which gentle_bot_3's charger spur is edges 16 and 17, which is
# the index space /closed_lanes speaks and NOT the one derived_nav_graph()
# or the building map speak (F-333).

import pytest

from api_server import cordon


# gentle_bot_3's charger spur, verbatim from /nav_graphs: vertex 18
# `gentle_bot_3_charger` (1.50,7.60) joined to vertex 4 `j_w1` (3.00,7.60)
# by directed edges 16 and 17. `dropoff_2` is given two ways in.
GRAPH = {
    "vertices": [
        ("v0", 0.0, 0.0), ("v1", 1.0, 0.0), ("v2", 2.0, 0.0),
        ("v3", 3.0, 0.0), ("j_w1", 3.00, 7.60),
    ] + [(f"pad{i}", float(i), 99.0) for i in range(5, 18)] + [
        ("gentle_bot_3_charger", 1.50, 7.60),
        ("dropoff_2", 10.0, 10.0),
        ("j_mid", 9.0, 10.0),
        ("j_north", 10.0, 11.0),
    ],
    # edges 0..15 are filler between the pads; 16/17 are the spur.
    "edges": [(i, i + 1) for i in range(16)] + [
        (18, 4),   # 16: charger -> j_w1
        (4, 18),   # 17: j_w1 -> charger
        (20, 19),  # 18: j_mid -> dropoff_2
        (21, 19),  # 19: j_north -> dropoff_2
    ],
}
SPUR = frozenset({16, 17})


@pytest.fixture(autouse=True)
def _clean():
    cordon._reset_for_test()
    yield
    cordon._reset_for_test()


# ---- known-bad: the cordon must fire ---------------------------------------

def test_a_destination_whose_every_lane_is_closed_is_refused():
    found = cordon.cordoned_place(GRAPH, SPUR, ["gentle_bot_3_charger"])
    assert found is not None
    place, lanes = found
    assert place == "gentle_bot_3_charger"
    assert lanes == [16, 17]


def test_the_reason_names_the_place_and_the_lanes():
    cordon._graphs["gentle_fleet"] = GRAPH
    cordon._closed["gentle_fleet"] = SPUR
    why = cordon.cordon_refusal("gentle_fleet", ["gentle_bot_3_charger"])
    assert why is not None
    assert "gentle_bot_3_charger" in why
    assert "16" in why and "17" in why
    assert "F-332" in why


def test_a_cordoned_place_anywhere_in_the_patrol_is_caught():
    """Not just the final destination — a waypoint mid-route counts."""
    found = cordon.cordoned_place(
        GRAPH, SPUR, ["v0", "gentle_bot_3_charger", "dropoff_2"])
    assert found is not None and found[0] == "gentle_bot_3_charger"


def test_closing_every_way_into_a_two_lane_destination_is_caught():
    found = cordon.cordoned_place(GRAPH, frozenset({18, 19}), ["dropoff_2"])
    assert found is not None and found[0] == "dropoff_2"


# ---- known-good: it must not block real work -------------------------------

def test_no_closures_no_objection():
    assert cordon.cordoned_place(GRAPH, frozenset(), ["gentle_bot_3_charger"]) is None


def test_one_of_two_ways_in_closed_is_NOT_refused():
    """A partial cordon still leaves a legal route. Refusing here would
    block ordinary work; that case belongs to the planner and, as the last
    line, to the adapter's leg gate."""
    assert cordon.cordoned_place(GRAPH, frozenset({18}), ["dropoff_2"]) is None
    assert cordon.cordoned_place(GRAPH, frozenset({19}), ["dropoff_2"]) is None


def test_an_unrelated_closure_blocks_nothing():
    assert cordon.cordoned_place(GRAPH, frozenset({3, 4}),
                                 ["gentle_bot_3_charger"]) is None


def test_the_spur_closed_does_not_block_a_different_destination():
    assert cordon.cordoned_place(GRAPH, SPUR, ["dropoff_2"]) is None


# ---- cannot see -> skips, never convicts (F-191) ---------------------------

def test_no_graph_yet_is_no_objection():
    assert cordon.cordoned_place(None, SPUR, ["gentle_bot_3_charger"]) is None


def test_an_unknown_place_is_left_to_the_fleet():
    assert cordon.cordoned_place(GRAPH, SPUR, ["somewhere_else"]) is None


def test_no_places_is_no_objection():
    assert cordon.cordoned_place(GRAPH, SPUR, []) is None


def test_a_fleet_we_have_never_heard_from_is_not_judged():
    assert cordon.cordon_refusal("no_such_fleet", ["gentle_bot_3_charger"]) is None
    assert cordon.known_fleets() == []


def test_a_graph_with_no_closure_feed_is_not_a_known_fleet():
    """Having the graph but never a ClosedLanes message means we do not
    know the cordon state — not that there is no cordon."""
    cordon._graphs["gentle_fleet"] = GRAPH
    assert cordon.known_fleets() == []
    cordon._closed["gentle_fleet"] = SPUR
    assert cordon.known_fleets() == ["gentle_fleet"]


def test_an_isolated_vertex_is_left_to_the_f111_guard():
    """A vertex with no lanes at all is F-111's case and has its own
    message; this guard must not claim it as a cordon."""
    lonely = {"vertices": GRAPH["vertices"] + [("orphan", 50.0, 50.0)],
              "edges": GRAPH["edges"]}
    assert cordon.cordoned_place(lonely, SPUR, ["orphan"]) is None


# ---- the feeds -------------------------------------------------------------

class _V:
    def __init__(self, n, x, y):
        self.name, self.x, self.y = n, x, y


class _E:
    def __init__(self, a, b):
        self.v1_idx, self.v2_idx = a, b


class _GraphMsg:
    name = "gentle_fleet"
    vertices = [_V("gentle_bot_3_charger", 1.5, 7.6), _V("j_w1", 3.0, 7.6)]
    edges = [_E(0, 1), _E(1, 0)]


class _ClosedMsg:
    fleet_name = "gentle_fleet"
    closed_lanes = [0, 1]


def test_the_two_feeds_compose_into_a_refusal():
    cordon.on_nav_graph(_GraphMsg())
    cordon.on_closed_lanes(_ClosedMsg())
    assert cordon.closed_lanes_of("gentle_fleet") == frozenset({0, 1})
    why = cordon.cordon_refusal("gentle_fleet", ["gentle_bot_3_charger"])
    assert why is not None and "gentle_bot_3_charger" in why


def test_reopening_lifts_the_refusal():
    cordon.on_nav_graph(_GraphMsg())
    cordon.on_closed_lanes(_ClosedMsg())
    assert cordon.cordon_refusal("gentle_fleet", ["gentle_bot_3_charger"])

    class _Reopened(_ClosedMsg):
        closed_lanes = []

    cordon.on_closed_lanes(_Reopened())
    assert cordon.cordon_refusal("gentle_fleet", ["gentle_bot_3_charger"]) is None


def test_a_malformed_message_does_not_take_the_api_server_down():
    cordon.on_nav_graph(object())
    cordon.on_closed_lanes(object())
    assert cordon.known_fleets() == []
