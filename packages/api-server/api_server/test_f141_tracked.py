# F-343 (f1-n33): the F-141 stale sweep must not close a row the fleet
# still names as a robot's current task.

from api_server.interrupted_tasks import tasks_named_by


def test_a_task_a_robot_names_is_tracked():
    states = [
        {
            "name": "gentle_fleet",
            "robots": {
                "gentle_bot_3": {"task_id": "abc"},
                "gentle_bot_4": {"task_id": ""},
            },
        }
    ]
    assert tasks_named_by(states) == {"abc"}


def test_nothing_named_means_nothing_tracked():
    assert tasks_named_by([]) == set()
    assert tasks_named_by([{"robots": {}}, None]) == set()
