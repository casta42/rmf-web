# F-343 / F-338 — a mission whose remaining stop the cordon cut is named
# for cancellation; one whose route is open, or whose robot cannot be
# placed, is left alone. Pure, proven both ways on the F-333 spur graph.

from api_server import cordon, cordon_cut
from api_server.test_lane_closures import CORRIDOR, GRAPH, J_MID, J_W1, SPUR


def test_remaining_places_reads_the_phases_not_yet_completed():
    state = {
        "active": 1,
        "completed": [],
        "phases": {
            "1": {"category": "Go to [place:dropoff_2]"},
            "2": {"category": "Go to [place:gentle_bot_3_charger]"},
        },
    }
    assert cordon_cut.remaining_places(state) == ["dropoff_2", "gentle_bot_3_charger"]
    state["completed"] = [1]
    assert cordon_cut.remaining_places(state) == ["gentle_bot_3_charger"]
    state["completed"] = [1, 2]
    assert cordon_cut.remaining_places(state) == []
    assert cordon_cut.remaining_places({"phases": {"1": {"category": "Charge"}}}) == []


def test_known_bad_the_measured_patrol_is_named():
    tasks = [
        {
            "id": "t1",
            "robot_xy": (3.0, 7.6),  # at j_w1 after dropoff_2
            "places": ["gentle_bot_3_charger"],
        }
    ]
    cut = cordon_cut.cut_missions(GRAPH, SPUR, tasks)
    assert cut == [{"id": "t1", "from": "the robot", "to": "gentle_bot_3_charger"}]
    reason = cordon_cut.cut_reason(cut[0], SPUR)
    assert "canceled" in reason and "[16, 17]" in reason and "F-343" in reason


def test_known_good_open_route_off_vertex_no_places_or_other_cordon_are_left_alone():
    tasks = [
        {"id": "open", "robot_xy": (3.0, 7.6), "places": ["gentle_bot_3_charger"]},
        {"id": "offgrid", "robot_xy": (6.0, 7.6), "places": ["gentle_bot_3_charger"]},
        {"id": "noplaces", "robot_xy": (3.0, 7.6), "places": []},
        {"id": "nopose", "robot_xy": None, "places": ["gentle_bot_3_charger"]},
        {"id": "elsewhere", "robot_xy": (9.0, 10.0), "places": ["dropoff_2"]},
    ]
    assert cordon_cut.cut_missions(GRAPH, frozenset(), tasks) == []
    # the spur closed: only the on-vertex task bound for the charger is cut
    cut = cordon_cut.cut_missions(GRAPH, SPUR, tasks)
    assert [c["id"] for c in cut] == ["open"]
    # the corridor closed instead: the charger-bound one is fine, the
    # dropoff-bound one starts at j_mid, past the cut — fine too
    assert cordon_cut.cut_missions(GRAPH, CORRIDOR, tasks) == []


def test_no_graph_means_no_judgement():
    assert (
        cordon_cut.cut_missions(
            None, SPUR, [{"id": "t", "robot_xy": (3.0, 7.6), "places": ["x"]}]
        )
        == []
    )


def test_a_robot_mid_lane_is_judged_from_its_first_stop_onward():
    # driving to dropoff_2 (nowhere near a vertex) with the charger as its
    # second stop and the spur closed: the second hop is cut
    tasks = [
        {
            "id": "midlane",
            "robot_xy": (6.0, 7.6),
            "places": ["dropoff_2", "gentle_bot_3_charger"],
        }
    ]
    cut = cordon_cut.cut_missions(GRAPH, SPUR, tasks)
    assert [(c["id"], c["from"], c["to"]) for c in cut] == [
        ("midlane", "dropoff_2", "gentle_bot_3_charger")
    ]
    # same robot, one stop only: the hop into it cannot be judged — left alone
    assert (
        cordon_cut.cut_missions(
            GRAPH,
            SPUR,
            [{"id": "one", "robot_xy": (6.0, 7.6), "places": ["gentle_bot_3_charger"]}],
        )
        == []
    )


def test_status_spellings_cover_the_ledger_enum_repr():
    spellings = cordon_cut.status_spellings(("underway",))
    assert "underway" in spellings and "Status.underway" in spellings
