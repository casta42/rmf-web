# F-343 class guard — a task is `completed` only when every phase is.
# Proven both ways on the shape measured 2026-09-19 (drill_f343.py).

import pytest

from api_server import phantom_completion as pc


@pytest.fixture(autouse=True)
def _clean():
    pc._reset_for_test()


def test_known_bad_completed_with_a_phase_never_begun_is_underway():
    # the measured shape: phases 1 and 2, active 1, completed [1]
    status, reason = pc.honest_status("completed", 1, [1], [1, 2])
    assert status == "underway"
    assert reason and "phase 2 of 2" in reason and "F-343" in reason


def test_known_good_completed_with_every_phase_completed_is_completed():
    assert pc.honest_status("completed", 2, [1, 2], [1, 2]) == ("completed", None)
    # a one-phase task
    assert pc.honest_status("completed", 1, [1], [1]) == ("completed", None)


def test_other_statuses_and_phaseless_tasks_are_untouched():
    for s in ("underway", "queued", "failed", "canceled", None):
        assert pc.honest_status(s, 1, [1], [1, 2]) == (s, None)
    # the fleet's transient shape with NO phase list (legacy tasks): no
    # basis to overrule, so no overrule
    assert pc.honest_status("completed", None, [], []) == ("completed", None)


class _Booking:
    id = "task-1"


class _State:
    def __init__(self, status, active, completed, phases):
        self.status = status
        self.active = active
        self.completed = completed
        self.phases = phases
        self.booking = _Booking()


def test_apply_rewrites_in_place_and_reports_once():
    st = _State("completed", 1, [1], {"1": {}, "2": {}})
    assert pc.apply(st) is not None
    assert st.status == "underway"
    st2 = _State("completed", 1, [1], {"1": {}, "2": {}})
    assert pc.apply(st2) is not None  # still rewritten
    assert len(pc._reported) == 1  # logged once per task


def test_apply_leaves_an_honest_completion_alone():
    st = _State("completed", 2, [1, 2], {"1": {}, "2": {}})
    assert pc.apply(st) is None and st.status == "completed"
    st = _State("canceled", 1, [1], {"1": {}, "2": {}})
    assert pc.apply(st) is None and st.status == "canceled"
