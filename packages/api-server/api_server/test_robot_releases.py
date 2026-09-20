# FR-42 (D-66) class guards, api-server half — proven BOTH WAYS without
# ROS. The ledger writes run against an in-memory double of the two
# tortoise models so migration (h), identity (j) and the release write
# (d)/(e)/(i) are exercised end to end on the host.

import asyncio
import json
import math

import pytest

from api_server import robot_releases as rr

FLEET = "gentle_fleet"
SITE = "testsite_a"


class _Rows:
    """The subset of the tortoise QuerySet surface the module uses."""

    def __init__(self, store, model, **filters):
        self.store, self.model, self.filters = store, model, filters

    def _match(self, row):
        return all(getattr(row, k) == v for k, v in self.filters.items())

    def __await__(self):
        async def go():
            return [r for r in self.store[self.model] if self._match(r)]

        return go().__await__()

    async def delete(self):
        self.store[self.model] = [
            r for r in self.store[self.model] if not self._match(r)
        ]


class _Row:
    _next = 1
    # the model's defaults (a create() that omits them gets them, as tortoise does)
    DEFAULTS = {
        "reason": "",
        "checklist_supervised": False,
        "checklist_estop_tested": False,
        "checklist_deadman_ready": False,
    }

    def __init__(self, **kw):
        self.id = _Row._next
        _Row._next += 1
        for k, v in {**self.DEFAULTS, **kw}.items():
            setattr(self, k, v)


def _fake_ttm(fail=False):
    store = {"RobotRelease": [], "ReleaseMigration": []}

    class _Model:
        name = ""

        @classmethod
        def filter(cls, **kw):
            if fail:
                raise RuntimeError("postgres refused")
            return _Rows(store, cls.name, **kw)

        @classmethod
        async def create(cls, **kw):
            if fail:
                raise RuntimeError("postgres refused")
            row = _Row(**kw)
            store[cls.name].append(row)
            return row

    class RobotRelease(_Model):
        name = "RobotRelease"

    class ReleaseMigration(_Model):
        name = "ReleaseMigration"

    class TaskState:
        """No task history by default — a fresh ledger. The migration
        tests that mean "this site predates FR-42" say so with the
        marker, exactly as install.sh upgrade records it."""

        @classmethod
        def all(cls):
            class _Q:
                def limit(self, _n):
                    return self

                async def count(self):
                    return 0

            return _Q()

    class Alert:
        class Severity:
            Info = "info"
            Critical = "critical"

    class ttm:
        pass

    ttm.RobotRelease = RobotRelease
    ttm.ReleaseMigration = ReleaseMigration
    ttm.TaskState = TaskState
    ttm.Alert = Alert
    ttm.store = store
    return ttm


class _Alerts:
    def __init__(self):
        self.created, self.resolved = [], []

    async def create_alert(
        self, alert_id, category, severity=None, fleet=None, robot=None, message=None
    ):
        self.created.append((alert_id, category, severity, fleet, robot, message))
        return None

    async def resolve_alert(self, alert_id, resolved_by="system"):
        self.resolved.append(alert_id)
        return None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rr._reset_for_test()
    monkeypatch.setattr(rr, "ttm", _fake_ttm())
    published = []
    rr.set_publisher(lambda fleet, body: published.append((fleet, body)))
    yield published
    rr._reset_for_test()
    rr.set_publisher(None)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _status(
    configured=("gentle_bot_1", "gentle_bot_2"),
    admitted=(),
    robots=None,
    store_state="synced",
    fleet=FLEET,
):
    return json.dumps(
        {
            "fleet": fleet,
            "site": SITE,
            "store_state": store_state,
            "store_detail": "",
            "configured": list(configured),
            "admitted": list(admitted),
            "released": {},
            "robots": robots or {},
        }
    )


READY = {
    "x": 3.0,
    "y": 10.0,
    "pose_stale": False,
    "pose_age_s": 0.1,
    "battery": 0.8,
    "battery_valid": True,
    "localization": "live",
    "charger_reachable": True,
    "nav_served": True,
    "schedule_visible": True,
    "admitted": False,
    "released": False,
}


# ---- the publisher: always answers, the empty set included --------------


def test_graph_arrival_publishes_the_empty_set_as_an_answer(_clean):
    rr.configure(SITE)
    rr.on_graph(FLEET)
    ((fleet, body),) = _clean
    assert fleet == FLEET and body["store"] == "ok" and body["released"] == {}
    assert body["site"] == SITE


def test_no_site_is_a_named_fault_and_releases_nobody(_clean):
    rr.configure(None)
    rr.on_graph(FLEET)
    ((_, body),) = _clean
    assert body["store"] == "fault" and "GF_SITE" in body["detail"]
    assert body["released"] == {}
    assert rr.instrument_state(FLEET) and "GF_SITE" in rr.instrument_state(FLEET)


def test_unreadable_store_is_a_named_fault_not_a_silent_hold(monkeypatch, _clean):
    rr.configure(SITE)
    monkeypatch.setattr(rr, "ttm", _fake_ttm(fail=True))
    _run(rr.load())
    assert rr.instrument_state(FLEET) and "unreadable" in rr.instrument_state(FLEET)
    rr.on_graph(FLEET)
    assert _clean[-1][1]["store"] == "fault"
    with pytest.raises(rr.ReleaseRefused) as refused:
        _run(rr.release(FLEET, "gentle_bot_1", "admin", "why", {}, harness=True))
    assert refused.value.status_code == 503


def test_first_status_from_a_fleet_answers_it_at_once(_clean):
    rr.configure(SITE)
    rr.on_watch_only(_status())
    assert len(_clean) == 1 and _clean[0][0] == FLEET
    rr.on_watch_only(_status())  # the second one is silent
    assert len(_clean) == 1
    rr.on_watch_only("not json")  # garbage is ignored
    assert rr.fleets() == [FLEET]


# ---- FR-42 (d): refusals both ways --------------------------------------


def test_refusals_fire_by_name_and_pass_on_a_ready_robot():
    assert rr.release_refusals(READY) == []
    names = {
        c
        for c, _ in rr.release_refusals(
            {
                "pose_stale": True,
                "pose_age_s": 30.0,
                "localization": "fallback",
                "charger_reachable": False,
                "battery_valid": False,
                "battery_detail": "NaN",
            }
        )
    }
    assert names == {
        "pose_stale",
        "localization_fallback",
        "charger_unreachable",
        "battery_invalid",
    }
    # cannot tell never convicts (F-191)
    assert (
        rr.release_refusals(
            {
                "pose_stale": None,
                "localization": "unknown",
                "charger_reachable": None,
                "battery_valid": None,
            }
        )
        == []
    )


def test_readiness_states_each_fact_or_cannot_tell():
    rows = {r["key"]: r for r in rr.readiness(READY, 0.5)}
    assert rows["pose"]["value"] is True and "live" in rows["pose"]["text"]
    assert rows["battery"]["text"] == "valid, 80 %"
    assert rows["nav"]["text"] == "being served"
    assert rows["schedule"]["value"] is True
    rows = {r["key"]: r for r in rr.readiness(None, None)}
    assert all(
        r["value"] is None and r["text"].startswith("cannot tell")
        for r in rows.values()
    )
    rows = {r["key"]: r for r in rr.readiness(READY, rr.STATUS_MAX_AGE_S + 1)}
    assert all(r["value"] is None for r in rows.values())
    stale = {
        **READY,
        "pose_stale": True,
        "pose_age_s": 42.0,
        "battery": float("nan"),
        "battery_valid": False,
        "battery_detail": "battery reading is NaN",
        "nav_served": False,
        "charger_reachable": None,
        "schedule_visible": False,
    }
    rows = {r["key"]: r for r in rr.readiness(stale, 0.5)}
    assert rows["pose"]["text"].startswith("STALE") and "42" in rows["pose"]["text"]
    assert rows["battery"]["text"].startswith("INVALID")
    assert rows["nav"]["value"] is False and rows["charger"]["value"] is None
    assert not math.isnan(0.0)  # keep math imported for the NaN case above


def test_release_is_refused_on_a_named_condition_and_writes_nothing(_clean):
    rr.configure(SITE)
    rr.on_watch_only(
        _status(robots={"gentle_bot_1": {**READY, "localization": "fallback"}})
    )
    with pytest.raises(rr.ReleaseRefused) as refused:
        _run(
            rr.release(
                FLEET,
                "gentle_bot_1",
                "admin",
                "commissioning done",
                {"supervised": True, "estop_tested": True, "deadman_ready": True},
            )
        )
    assert refused.value.status_code == 409
    assert refused.value.detail["conditions"] == ["localization_fallback"]
    assert rr.released(FLEET) == {}
    assert rr.ttm.store["RobotRelease"] == []


def test_release_requires_reason_and_the_full_checklist(_clean):
    rr.configure(SITE)
    rr.on_watch_only(_status(robots={"gentle_bot_1": READY}))
    with pytest.raises(rr.ReleaseRefused) as refused:
        _run(rr.release(FLEET, "gentle_bot_1", "admin", "  ", {}))
    assert refused.value.status_code == 422 and "reason" in refused.value.detail
    with pytest.raises(rr.ReleaseRefused) as refused:
        _run(
            rr.release(
                FLEET,
                "gentle_bot_1",
                "admin",
                "ok",
                {"supervised": True, "estop_tested": True},
            )
        )
    assert refused.value.status_code == 422 and "dead-man" in refused.value.detail
    assert rr.released(FLEET) == {}


def test_admin_release_writes_the_record_publishes_and_audits(_clean):
    rr.configure(SITE)
    alerts = _Alerts()
    rr.set_alert_repository(alerts)
    rr.on_watch_only(_status(robots={"gentle_bot_1": READY}))
    record = _run(
        rr.release(
            FLEET,
            "gentle_bot_1",
            "gerardo",
            "commissioning complete",
            {"supervised": True, "estop_tested": True, "deadman_ready": True},
        )
    )
    assert record["by"] == "gerardo" and record["kind"] == "admin"
    assert record["checklist"] == {
        "supervised": True,
        "estop_tested": True,
        "deadman_ready": True,
    }
    assert rr.released(FLEET)["gentle_bot_1"]["reason"] == "commissioning complete"
    fleet, body = _clean[-1]
    assert body["released"]["gentle_bot_1"]["by"] == "gerardo"
    # FR-42 (e): in the FR-31 archive as an info entry, resolved at once
    ((alert_id, category, severity, _, robot, message),) = alerts.created
    assert category == "fleet" and severity == "info" and robot == "gentle_bot_1"
    assert "checklist affirmed" in message and alerts.resolved == [alert_id]
    # a second release of the same robot is refused
    with pytest.raises(rr.ReleaseRefused) as refused:
        _run(
            rr.release(
                FLEET,
                "gentle_bot_1",
                "gerardo",
                "again",
                {"supervised": True, "estop_tested": True, "deadman_ready": True},
            )
        )
    assert refused.value.status_code == 409


def test_harness_release_records_the_harness_and_an_unaffirmed_checklist(_clean):
    rr.configure(SITE)
    alerts = _Alerts()
    rr.set_alert_repository(alerts)
    rr.on_watch_only(_status(robots={"gentle_bot_2": READY}))
    record = _run(
        rr.release(FLEET, "gentle_bot_2", "operator", "sim rehearsal", {}, harness=True)
    )
    assert record["by"] == "harness:operator" and record["kind"] == "harness"
    assert record["checklist"] == {
        "supervised": False,
        "estop_tested": False,
        "deadman_ready": False,
    }
    assert "NOT affirmed" in alerts.created[0][5]


def test_release_of_a_robot_not_in_the_config_is_refused(_clean):
    rr.configure(SITE)
    rr.on_watch_only(_status(configured=("gentle_bot_1",)))
    with pytest.raises(rr.ReleaseRefused) as refused:
        _run(rr.release(FLEET, "gentle_bot_9", "admin", "x", {}, harness=True))
    assert refused.value.status_code == 409 and "configuration" in refused.value.detail


# ---- FR-42 (h)/(j): migration once, identity by config name ------------


def test_migration_runs_once_and_never_covers_a_later_name(_clean):
    rr.configure(SITE, rr.MIGRATION_PENDING)  # a site that predates FR-42
    alerts = _Alerts()
    rr.set_alert_repository(alerts)
    rr.on_watch_only(_status(configured=("gentle_bot_1", "gentle_bot_2")))
    assert _run(rr.migrate_if_first(FLEET)) == ["gentle_bot_1", "gentle_bot_2"]
    assert set(rr.released(FLEET)) == {"gentle_bot_1", "gentle_bot_2"}
    assert rr.released(FLEET)["gentle_bot_1"]["kind"] == "migration"
    assert len(rr.ttm.store["ReleaseMigration"]) == 1
    assert any("migration" in a[0] for a in alerts.created)
    # a robot added to the config later: NOT released by the migration
    rr.on_watch_only(
        _status(configured=("gentle_bot_1", "gentle_bot_2", "gentle_bot_7"))
    )
    assert _run(rr.migrate_if_first(FLEET)) is None
    assert "gentle_bot_7" not in rr.released(FLEET)
    assert len(rr.ttm.store["ReleaseMigration"]) == 1


def test_migration_does_not_run_on_a_site_that_already_has_rows(_clean):
    rr.configure(SITE)
    rr.on_watch_only(_status(robots={"gentle_bot_1": READY}))
    _run(rr.release(FLEET, "gentle_bot_1", "admin", "x", {}, harness=True))
    assert _run(rr.migrate_if_first(FLEET)) is None
    assert "gentle_bot_2" not in rr.released(FLEET)


def test_migration_waits_for_a_roster_and_skips_on_a_fault(monkeypatch, _clean):
    rr.configure(SITE)
    assert _run(rr.migrate_if_first(FLEET)) is None  # no status yet
    monkeypatch.setattr(rr, "ttm", _fake_ttm(fail=True))
    _run(rr.load())
    rr.on_watch_only(_status())
    assert _run(rr.migrate_if_first(FLEET)) is None  # unreadable store


def test_identity_a_robot_removed_from_the_config_loses_its_record(_clean):
    rr.configure(SITE, rr.MIGRATION_PENDING)
    rr.on_watch_only(_status(configured=("gentle_bot_1", "gentle_bot_2")))
    _run(rr.migrate_if_first(FLEET))
    rr.on_watch_only(_status(configured=("gentle_bot_1",)))
    assert _run(rr.reconcile_identity(FLEET)) == ["gentle_bot_2"]
    assert set(rr.released(FLEET)) == {"gentle_bot_1"}
    assert _clean[-1][1]["released"].keys() == {"gentle_bot_1"}
    # an EMPTY roster is "cannot tell", never "delete everything"
    rr.on_watch_only(_status(configured=()))
    assert _run(rr.reconcile_identity(FLEET)) == []
    assert set(rr.released(FLEET)) == {"gentle_bot_1"}


# ---- the instrument alert, both ways ------------------------------------


def test_instrument_alert_raised_once_per_episode_and_resolved(_clean):
    rr.configure(SITE)
    alerts = _Alerts()
    rr.set_alert_repository(alerts)
    rr.on_watch_only(_status(store_state="no_authority"))
    _run(rr._maintain_instrument_alert(FLEET))
    _run(rr._maintain_instrument_alert(FLEET))
    assert len(alerts.created) == 1 and alerts.created[0][1] == "instrument"
    assert alerts.created[0][2] == "critical"
    rr.on_watch_only(_status(store_state="synced"))
    _run(rr._maintain_instrument_alert(FLEET))
    assert alerts.resolved == [alerts.created[0][0]]
    # a healthy fleet never raises one (the boring case)
    _run(rr._maintain_instrument_alert(FLEET))
    assert len(alerts.created) == 1


def test_status_carries_facts_refusals_and_release_state(_clean):
    rr.configure(SITE)
    rr.on_watch_only(_status(robots={"gentle_bot_1": {**READY, "pose_stale": True}}))
    st = rr.status(FLEET)
    assert st["adapter_reporting"] is True and st["instrument_fault"] is None
    robot = st["robots"]["gentle_bot_1"]
    assert robot["released"] is False
    assert [r["condition"] for r in robot["refusals"]] == ["pose_stale"]
    assert any(r["key"] == "pose" for r in robot["readiness"])
    assert rr.status("nobody")["adapter_reporting"] is False


def test_watch_only_positions_are_the_unadmitted_bodies(_clean):
    rr.configure(SITE)
    rr.on_watch_only(
        _status(
            robots={
                "gentle_bot_1": READY,
                "gentle_bot_2": {**READY, "admitted": True},
                "gentle_bot_3": {**READY, "x": None, "y": None},
            }
        )
    )
    rows = rr.watch_only_positions()
    assert [r["name"] for r in rows] == ["gentle_bot_1"]
    assert rows[0]["watch_only"] is True and rows[0]["x"] == 3.0


def test_identity_never_retires_an_admitted_robot_on_a_shrunken_roster(_clean):
    """The f1-n39 first-boot shape: the adapter's roster shrank to the
    unadmitted robots while the admitted ones were absent from it. An
    admitted robot is in the config by construction; its row stays."""
    rr.configure(SITE, rr.MIGRATION_PENDING)
    rr.on_watch_only(
        _status(configured=("gentle_bot_1", "gentle_bot_2", "gentle_bot_3"))
    )
    _run(rr.migrate_if_first(FLEET))
    rr.on_watch_only(
        _status(configured=("gentle_bot_3",), admitted=("gentle_bot_1", "gentle_bot_2"))
    )
    assert _run(rr.reconcile_identity(FLEET)) == []
    assert set(rr.released(FLEET)) == {"gentle_bot_1", "gentle_bot_2", "gentle_bot_3"}
    # a robot truly gone (neither configured nor admitted) is still retired
    rr.on_watch_only(_status(configured=("gentle_bot_3",), admitted=("gentle_bot_1",)))
    assert _run(rr.reconcile_identity(FLEET)) == ["gentle_bot_2"]


# ---- FR-42 (h) / F-350: WHICH sites may migrate -------------------------


def _with_history(monkeypatch, rows):
    """A ledger that answers the task-history question with `rows`."""
    ttm = _fake_ttm()

    class _TaskState:
        @classmethod
        def all(cls):
            class _Q:
                def limit(self, _n):
                    return self

                async def count(self):
                    if rows is None:
                        raise RuntimeError("database refused")
                    return rows

            return _Q()

    ttm.TaskState = _TaskState
    monkeypatch.setattr(rr, "ttm", ttm)
    return ttm


def test_marker_off_never_migrates_even_with_a_roster(monkeypatch, _clean):
    _with_history(monkeypatch, 12)  # history says "old site"; the marker wins
    rr.configure(SITE, "off")
    rr.on_watch_only(_status(configured=("gentle_bot_1", "gentle_bot_2")))
    assert _run(rr.migrate_if_first(FLEET)) is None
    assert rr.released(FLEET) == {}
    allowed, why = _run(rr.may_migrate(FLEET))
    assert allowed is False and "created at or after FR-42" in why
    assert rr.status(FLEET)["migration_marker"] == "off"


def test_marker_pending_migrates_once(monkeypatch, _clean):
    _with_history(monkeypatch, 0)  # empty ledger; the marker still wins
    rr.configure(SITE, "pending")
    rr.on_watch_only(_status(configured=("gentle_bot_1", "gentle_bot_2")))
    assert _run(rr.migrate_if_first(FLEET)) == ["gentle_bot_1", "gentle_bot_2"]
    assert set(rr.released(FLEET)) == {"gentle_bot_1", "gentle_bot_2"}
    assert "predates FR-42" in rr.status(FLEET)["migration_rule"]
    # spent: a name added later is never covered
    rr.on_watch_only(
        _status(configured=("gentle_bot_1", "gentle_bot_2", "gentle_bot_7"))
    )
    assert _run(rr.migrate_if_first(FLEET)) is None
    assert "gentle_bot_7" not in rr.released(FLEET)


def test_no_marker_falls_back_to_the_ledger_both_ways(monkeypatch, _clean):
    # an unmanaged stack against a database with history: it predates FR-42
    _with_history(monkeypatch, 5)
    rr.configure(SITE, None)
    rr.on_watch_only(_status(configured=("gentle_bot_1",)))
    allowed, why = _run(rr.may_migrate(FLEET))
    assert allowed is True and "ledger holds task history" in why
    assert _run(rr.migrate_if_first(FLEET)) == ["gentle_bot_1"]
    # ...and against an empty one: a fresh site, nobody migrates
    rr._reset_for_test()
    _with_history(monkeypatch, 0)
    rr.set_publisher(lambda fleet, body: None)
    rr.configure(SITE, None)
    rr.on_watch_only(_status(configured=("gentle_bot_1",)))
    allowed, why = _run(rr.may_migrate(FLEET))
    assert allowed is False and "fresh site" in why
    assert _run(rr.migrate_if_first(FLEET)) is None
    assert rr.released(FLEET) == {}


def test_a_ledger_that_cannot_be_read_migrates_nobody_and_says_so(monkeypatch, _clean):
    _with_history(monkeypatch, None)  # the count raises
    rr.configure(SITE, None)
    rr.on_watch_only(_status(configured=("gentle_bot_1",)))
    allowed, why = _run(rr.may_migrate(FLEET))
    assert allowed is False
    assert "could not be read" in why and "GF_RELEASE_MIGRATION=pending" in why
    assert _run(rr.migrate_if_first(FLEET)) is None


def test_a_nonsense_marker_is_ignored_not_obeyed(monkeypatch, _clean):
    _with_history(monkeypatch, 0)
    rr.configure(SITE, "yes-please")
    assert rr.status(FLEET)["migration_marker"] is None
    rr.on_watch_only(_status(configured=("gentle_bot_1",)))
    allowed, why = _run(rr.may_migrate(FLEET))
    assert allowed is False and "fresh site" in why
