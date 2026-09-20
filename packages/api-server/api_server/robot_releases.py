"""FR-42 (D-66) — the release store, its one publisher, and the
commissioning status surface.

The fleet adapter reads the released set from `/gf_robot_releases`
BEFORE it admits any robot (the F-339 shape; FR-42 (f)). This module is
the only writer of that topic. It publishes the WHOLE released set for a
fleet, latched, whenever:

  * the ledger is loaded (api-server start),
  * a release is written,
  * the fleet's graph arrives on `/nav_graphs` — the adapter publishes its
    graph at start, so this is the hand-shake that reaches a fleet that
    started after the api-server (ALWAYS published, the empty set
    included: "nobody is released" is an answer the adapter must hear,
    the f1-n28 lesson),
  * the first `gf_watch_only` status from a fleet arrives (the roster is
    known: migration and identity reconciliation can run).

The adapter's `gf_watch_only` feed is the other half: every configured
robot's release/admission state plus the live readiness facts for the
ones the fleet may not command. The release route judges FR-42 (d)
against those facts; the dashboard renders them.

An unreadable store — here (Postgres refused) or at the adapter (no
answer within its bound) — is a NAMED INSTRUMENT ALERT (FR-42 (f)),
raised by the maintenance loop and resolved when the state clears. A
silent fleet-wide hold is not acceptable.

Every in-memory structure is keyed by fleet. ROS-thread callbacks only
store state and publish; every ledger write is async on the app loop.
"""

import asyncio
import json
import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .logger import logger as base_logger
from .models import tortoise_models as ttm

logger = base_logger.getChild("RobotReleases")

# The adapter's status is judged fresh for this long; older facts are
# "cannot tell", never a verdict.
STATUS_MAX_AGE_S = 10.0
MAINTENANCE_PERIOD_S = 3.0

ACTOR_ADMIN = "admin"
ACTOR_HARNESS = "harness"
ACTOR_MIGRATION = "migration"

# Adapter-side store states (mirrors gentle_fleet_adapter.watch_only)
STORE_SYNCED = "synced"
STORE_WAITING = "waiting"
STORE_NO_AUTHORITY = "no_authority"
STORE_FAULT = "fault"

CHECKLIST_LINES = (
    ("supervised", "a person is supervising this robot on the floor"),
    ("estop_tested", "its emergency stop is installed and has been tested"),
    ("deadman_ready", "a dead-man stop is in hand"),
)

_site: Optional[str] = None
_rows: Dict[str, Dict[str, dict]] = {}  # fleet -> robot -> row dict
_migrated: Dict[str, dict] = {}  # fleet -> migration row dict
_store_fault: Optional[str] = None  # this process could not read
_seq: int = 0
_publisher: Optional[Callable[[str, dict], None]] = None
_status: Dict[str, Tuple[dict, float]] = {}  # fleet -> (payload, monotonic)
_published: Dict[str, dict] = {}  # fleet -> last payload
_instrument_alert: Dict[str, str] = {}  # fleet -> open alert id
_alert_repo: Any = None


def configure(site: Optional[str]) -> None:
    global _site  # pylint: disable=global-statement
    _site = site or None
    if _site is None:
        logger.error(
            "FR-42: no site name configured (GF_SITE) — the release store "
            "cannot be keyed; every robot stays WATCH-ONLY until it is"
        )


def set_publisher(fn: Optional[Callable[[str, dict], None]]) -> None:
    global _publisher  # pylint: disable=global-statement
    _publisher = fn


def set_alert_repository(repo: Any) -> None:
    global _alert_repo  # pylint: disable=global-statement
    _alert_repo = repo


def _reset_for_test() -> None:
    global _site, _store_fault, _seq, _alert_repo  # pylint: disable=global-statement
    _site = None
    _rows.clear()
    _migrated.clear()
    _store_fault = None
    _seq = 0
    _status.clear()
    _published.clear()
    _instrument_alert.clear()
    _alert_repo = None


def site() -> Optional[str]:
    return _site


def _row_dict(row: ttm.RobotRelease) -> dict:
    return {
        "id": row.id,
        "site": row.site,
        "fleet": row.fleet,
        "robot": row.robot,
        "by": row.released_by,
        "kind": row.actor_kind,
        "at": row.unix_millis_release_time,
        "reason": row.reason,
        "checklist": {
            "supervised": bool(row.checklist_supervised),
            "estop_tested": bool(row.checklist_estop_tested),
            "deadman_ready": bool(row.checklist_deadman_ready),
        },
    }


async def load() -> None:
    """Read every row for this site (api-server start). A failure here is
    the store fault: nobody is released and the fleet is told so."""
    global _store_fault  # pylint: disable=global-statement
    _rows.clear()
    _migrated.clear()
    if _site is None:
        _store_fault = (
            "no site name configured (GF_SITE) — the release store cannot be keyed"
        )
        return
    try:
        rows = await ttm.RobotRelease.filter(site=_site)
        migrations = await ttm.ReleaseMigration.filter(site=_site)
    except Exception as e:  # noqa: BLE001 — the store being unreadable IS the event
        _store_fault = f"release store unreadable: {e}"
        logger.error("FR-42: %s — nobody is released", _store_fault)
        return
    _store_fault = None
    for row in rows:
        _rows.setdefault(row.fleet, {})[row.robot] = _row_dict(row)
    for mig in migrations:
        _migrated[mig.fleet] = {
            "at": mig.unix_millis_time,
            "robots": json.loads(mig.robots or "[]"),
            "by": mig.migrated_by,
        }
    for fleet in sorted(set(_rows) | set(_migrated)):
        logger.info(
            "FR-42: site [%s] fleet [%s]: %d release row(s)%s",
            _site,
            fleet,
            len(_rows.get(fleet, {})),
            " (migrated)" if fleet in _migrated else "",
        )
        publish(fleet)


def released(fleet: str) -> Dict[str, dict]:
    if _store_fault is not None:
        return {}
    return {k: dict(v) for k, v in _rows.get(fleet, {}).items()}


def payload(fleet: str) -> dict:
    global _seq  # pylint: disable=global-statement
    _seq += 1
    ok = _store_fault is None and _site is not None
    return {
        "site": _site,
        "fleet": fleet,
        "store": "ok" if ok else "fault",
        "detail": (
            ""
            if ok
            else (
                _store_fault
                or "no site name configured (GF_SITE) — the release "
                "store cannot be keyed"
            )
        ),
        "released": {
            name: {"by": r["by"], "kind": r["kind"], "at": r["at"]}
            for name, r in (_rows.get(fleet, {}) if ok else {}).items()
        },
        "seq": _seq,
        "unix_millis_time": int(time.time() * 1000),
    }


def publish(fleet: str) -> Optional[dict]:
    """Publish the whole released set for `fleet`, latched. Always — the
    empty set and the fault state included."""
    if _publisher is None or not fleet:
        return None
    body = payload(fleet)
    _publisher(fleet, body)
    _published[fleet] = body
    return body


def on_graph(fleet: str) -> None:
    """A fleet published its graph: it is (re)starting and waiting on us."""
    sent = publish(fleet)
    if sent is not None:
        logger.info(
            "FR-42: graph for [%s] received — release set published: %s (store %s)",
            fleet,
            sorted(sent["released"]),
            sent["store"],
        )


def on_watch_only(raw: str) -> None:
    """The adapter's `gf_watch_only` status (ROS thread: store only)."""
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("gf_watch_only: undecodable payload")
        return
    if not isinstance(data, dict) or not data.get("fleet"):
        return
    fleet = str(data["fleet"])
    first = fleet not in _status
    _status[fleet] = (data, time.monotonic())
    if first:
        # the fleet is known now: answer it at once, so a fleet that
        # started before us (or after a store reload) hears its set
        publish(fleet)


def fleets() -> List[str]:
    return sorted(set(_status) | set(_rows) | set(_migrated))


def _status_of(fleet: str) -> Tuple[Optional[dict], Optional[float]]:
    entry = _status.get(fleet)
    if entry is None:
        return None, None
    data, at = entry
    return data, time.monotonic() - at


def robot_facts(fleet: str, robot: str) -> Tuple[Optional[dict], Optional[float]]:
    data, age = _status_of(fleet)
    if data is None:
        return None, None
    facts = (data.get("robots") or {}).get(robot)
    return (dict(facts) if isinstance(facts, dict) else None), age


def watch_only_positions() -> List[Dict[str, Any]]:
    """Live positions of every robot the fleet may not command — for the
    zone editor (FR-42 (k)): an apply that would need to MOVE one of these
    is refused, naming it."""
    out: List[Dict[str, Any]] = []
    for fleet, (data, at) in list(_status.items()):
        if time.monotonic() - at > STATUS_MAX_AGE_S:
            continue
        for name, facts in (data.get("robots") or {}).items():
            if not isinstance(facts, dict) or facts.get("admitted"):
                continue
            x, y = facts.get("x"), facts.get("y")
            if x is None or y is None:
                continue
            out.append(
                {
                    "name": str(name),
                    "fleet": fleet,
                    "x": float(x),
                    "y": float(y),
                    "parked": True,
                    "watch_only": True,
                }
            )
    return out


# ---- FR-42 (d): the refusal conditions, from the adapter's facts -------


def release_refusals(facts: dict) -> List[Tuple[str, str]]:
    """The same four conditions the adapter re-checks at admission
    (gentle_fleet_adapter.watch_only.release_refusals). A "cannot tell"
    (None/unknown) is absent from the list: it informs, never blocks
    (F-191)."""
    out: List[Tuple[str, str]] = []
    if facts.get("pose_stale") is True:
        age = facts.get("pose_age_s")
        out.append(
            (
                "pose_stale",
                "its pose is stale"
                + (f" ({age:.0f} s old)" if isinstance(age, (int, float)) else "")
                + " (FR-9g) — the fleet does not know where it is",
            )
        )
    if facts.get("localization") in ("fallback", "none"):
        out.append(
            (
                "localization_fallback",
                "its RMF start would come from the configured-start fallback, "
                "not from its live pose — the fleet cannot place it on the lane "
                "graph where it stands, so a release would move a robot the "
                "fleet cannot locate",
            )
        )
    if facts.get("charger_reachable") is False:
        out.append(
            (
                "charger_unreachable",
                "no charging waypoint is reachable from its position in the "
                "current nav graph (F-186)",
            )
        )
    if facts.get("battery_valid") is False:
        out.append(
            (
                "battery_invalid",
                "its battery reading is invalid — "
                + str(facts.get("battery_detail") or "NaN or outside [0, 1]")
                + " (F-271)",
            )
        )
    return out


def readiness(facts: Optional[dict], age: Optional[float]) -> List[dict]:
    """The facts the release dialog shows, each stated or an explicit
    'cannot tell' (FR-42 (d))."""
    if facts is None or age is None or age > STATUS_MAX_AGE_S:
        why = (
            "the fleet adapter is not reporting commissioning state"
            if facts is None or age is None
            else f"the fleet adapter's report is {age:.0f} s old"
        )
        return [
            {"key": key, "label": label, "value": None, "text": f"cannot tell — {why}"}
            for key, label in (
                ("pose", "Pose source and age"),
                ("battery", "Battery reading"),
                ("nav", "Navigation interface"),
                ("charger", "Charger reachable"),
                ("schedule", "Visible to the traffic schedule"),
            )
        ]
    rows = []
    pose_age = facts.get("pose_age_s")
    if facts.get("x") is None:
        rows.append(
            {
                "key": "pose",
                "label": "Pose source and age",
                "value": None,
                "text": "cannot tell — no pose has been received",
            }
        )
    elif facts.get("pose_stale") is True:
        rows.append(
            {
                "key": "pose",
                "label": "Pose source and age",
                "value": False,
                "text": (
                    f"STALE — last odometry {pose_age:.0f} s ago"
                    if isinstance(pose_age, (int, float))
                    else "STALE"
                ),
            }
        )
    else:
        loc = facts.get("localization")
        loc_text = {
            "live": "live odometry, placed on the lane graph",
            "fallback": "live odometry, but OFF the lane graph (the fleet would fall back to the configured start)",
            "none": "live odometry, but OFF the lane graph and no configured start",
        }.get(str(loc), "live odometry; graph placement unknown")
        rows.append(
            {
                "key": "pose",
                "label": "Pose source and age",
                "value": loc == "live",
                "text": (
                    f"{loc_text}, {pose_age:.1f} s old"
                    if isinstance(pose_age, (int, float))
                    else loc_text
                ),
            }
        )
    soc = facts.get("battery")
    valid = facts.get("battery_valid")
    if valid is None:
        rows.append(
            {
                "key": "battery",
                "label": "Battery reading",
                "value": None,
                "text": "cannot tell — no battery reading has arrived",
            }
        )
    elif valid is False:
        rows.append(
            {
                "key": "battery",
                "label": "Battery reading",
                "value": False,
                "text": f"INVALID — {facts.get('battery_detail') or 'NaN or out of range'}",
            }
        )
    else:
        pct = (
            round(float(soc) * 100)
            if isinstance(soc, (int, float)) and not math.isnan(soc)
            else "?"
        )
        rows.append(
            {
                "key": "battery",
                "label": "Battery reading",
                "value": True,
                "text": f"valid, {pct} %",
            }
        )
    nav = facts.get("nav_served")
    rows.append(
        {
            "key": "nav",
            "label": "Navigation interface",
            "value": nav,
            "text": (
                "cannot tell"
                if nav is None
                else (
                    "being served"
                    if nav
                    else "NOT served — no NavigateToPose server is answering"
                )
            ),
        }
    )
    reach = facts.get("charger_reachable")
    rows.append(
        {
            "key": "charger",
            "label": "Charger reachable",
            "value": reach,
            "text": (
                "cannot tell — needs a pose on the lane graph"
                if reach is None
                else (
                    "yes"
                    if reach
                    else "NO — no charging waypoint is reachable from here (F-186)"
                )
            ),
        }
    )
    vis = facts.get("schedule_visible")
    rows.append(
        {
            "key": "schedule",
            "label": "Visible to the traffic schedule",
            "value": bool(vis),
            "text": (
                "yes — other robots plan around it"
                if vis
                else "not yet — other robots brake at it (body gates) but do not plan around it"
            ),
        }
    )
    return rows


# ---- writes ------------------------------------------------------------


def _emit(alert: Any) -> None:
    """Push an alert onto the live bus when the ROS-backed rmf_io package
    is importable (the server); on the host tests it is not, and the
    archive row already exists — the bus is a mirror, never the record."""
    if alert is None:
        return
    try:
        from .rmf_io import alert_events  # noqa: PLC0415

        alert_events.alerts.on_next(alert)
    except Exception:  # noqa: BLE001
        pass


class ReleaseRefused(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


async def release(
    fleet: str,
    robot: str,
    username: str,
    reason: str,
    checklist: Optional[dict],
    harness: bool = False,
) -> dict:
    """FR-42 (d)/(e)/(i): write ONE release, refused by name on any (d)
    condition. Returns the release record written."""
    if _site is None or _store_fault is not None:
        raise ReleaseRefused(
            503,
            "the release store is not readable — "
            + (_store_fault or "no site name configured")
            + " (instrument fault); nothing can be released until it is",
        )
    reason = (reason or "").strip()
    if not reason:
        raise ReleaseRefused(422, "a typed reason is required")
    data, age = _status_of(fleet)
    configured = list((data or {}).get("configured") or [])
    if data is not None and configured and robot not in configured:
        raise ReleaseRefused(
            409,
            f"[{robot}] is not in fleet [{fleet}]'s configuration — a release "
            "is keyed to a fleet-config entry (FR-42 (f))",
        )
    if robot in _rows.get(fleet, {}):
        raise ReleaseRefused(409, f"[{robot}] is already released at site [{_site}]")
    admitted = list((data or {}).get("admitted") or [])
    if robot in admitted:
        raise ReleaseRefused(409, f"[{robot}] is already a fleet member")
    facts, _ = robot_facts(fleet, robot)
    if facts is not None and age is not None and age <= STATUS_MAX_AGE_S:
        refusals = release_refusals(facts)
        if refusals:
            raise ReleaseRefused(
                409,
                {
                    "message": f"Release of [{robot}] REFUSED: "
                    + "; ".join(text for _, text in refusals)
                    + ". Fix the condition and try again.",
                    "conditions": [c for c, _ in refusals],
                    "readiness": readiness(facts, age),
                },
            )
        facts_seen = True
    else:
        facts_seen = False
    checklist = checklist or {}
    affirmed = {key: bool(checklist.get(key)) for key, _ in CHECKLIST_LINES}
    if not harness and not all(affirmed.values()):
        missing = [label for key, label in CHECKLIST_LINES if not affirmed[key]]
        raise ReleaseRefused(
            422,
            "the release checklist is not affirmed: " + "; ".join(missing),
        )
    actor = f"harness:{username}" if harness else username
    kind = ACTOR_HARNESS if harness else ACTOR_ADMIN
    now_millis = int(time.time() * 1000)
    try:
        row = await ttm.RobotRelease.create(
            site=_site,
            fleet=fleet,
            robot=robot,
            released_by=actor,
            actor_kind=kind,
            unix_millis_release_time=now_millis,
            reason=reason,
            checklist_supervised=affirmed["supervised"] and not harness,
            checklist_estop_tested=affirmed["estop_tested"] and not harness,
            checklist_deadman_ready=affirmed["deadman_ready"] and not harness,
        )
    except Exception as e:  # noqa: BLE001
        raise ReleaseRefused(503, f"the release store refused the write: {e}") from e
    record = _row_dict(row)
    _rows.setdefault(fleet, {})[robot] = record
    publish(fleet)
    checklist_text = (
        "checklist NOT affirmed (harness release of a simulated robot)"
        if harness
        else "checklist affirmed: " + ", ".join(label for _, label in CHECKLIST_LINES)
    )
    logger.warning(
        "FR-42: RELEASED [%s/%s] at site [%s] by [%s] (%s) — reason: %s; %s%s",
        fleet,
        robot,
        _site,
        actor,
        kind,
        reason,
        checklist_text,
        (
            ""
            if facts_seen
            else "; readiness facts were NOT available (adapter not reporting)"
        ),
    )
    await _audit_alert(
        f"fr42-release-{fleet}-{robot}-{now_millis}",
        fleet,
        robot,
        f"{robot} released from commissioning (WATCH-ONLY) by {actor} — reason: "
        f"{reason}; {checklist_text}. From now on the fleet may move it with no task.",
    )
    return record


async def _audit_alert(
    alert_id: str, fleet: str, robot: Optional[str], message: str
) -> None:
    """FR-42 (e): the release is in the FR-31 History view — an INFO
    alert created and resolved at once (the archive keeps it)."""
    if _alert_repo is None:
        return
    try:
        alert = await _alert_repo.create_alert(
            alert_id,
            "fleet",
            severity=ttm.Alert.Severity.Info,
            fleet=fleet,
            robot=robot,
            message=message,
        )
        _emit(alert)
        _emit(await _alert_repo.resolve_alert(alert_id, resolved_by="audit"))
    except (
        Exception
    ) as e:  # noqa: BLE001 — the audit row is the release row; this mirror never blocks it
        logger.warning("FR-42: audit alert %s could not be written: %s", alert_id, e)


# ---- maintenance: migration (h), identity (j), instrument alert (f) ---


async def migrate_if_first(fleet: str) -> Optional[List[str]]:
    """FR-42 (h): ONCE per (site, fleet), on first deployment: the robots
    present in the fleet config at that moment are recorded as released
    by a logged migration entry. Never again — a name added later joins
    WATCH-ONLY. Returns the names released, or None when nothing ran."""
    if _site is None or _store_fault is not None:
        return None
    if fleet in _migrated or _rows.get(fleet):
        return None
    data, _ = _status_of(fleet)
    if data is None:
        return None
    configured = [str(n) for n in (data.get("configured") or [])]
    now_millis = int(time.time() * 1000)
    try:
        await ttm.ReleaseMigration.create(
            site=_site,
            fleet=fleet,
            unix_millis_time=now_millis,
            robots=json.dumps(configured),
            migrated_by=ACTOR_MIGRATION,
        )
        for name in configured:
            row = await ttm.RobotRelease.create(
                site=_site,
                fleet=fleet,
                robot=name,
                released_by=ACTOR_MIGRATION,
                actor_kind=ACTOR_MIGRATION,
                unix_millis_release_time=now_millis,
                reason="first deployment of FR-42 at this site: robot present in "
                "the fleet config at migration time (FR-42 (h))",
            )
            _rows.setdefault(fleet, {})[name] = _row_dict(row)
    except Exception as e:  # noqa: BLE001
        logger.error("FR-42: migration for [%s/%s] failed: %s", _site, fleet, e)
        return None
    _migrated[fleet] = {"at": now_millis, "robots": configured, "by": ACTOR_MIGRATION}
    publish(fleet)
    logger.warning(
        "FR-42: MIGRATION at site [%s] fleet [%s]: %d robot(s) recorded as released "
        "because they were present in the fleet config at first deployment: %s. "
        "A robot added to the config from now on joins WATCH-ONLY.",
        _site,
        fleet,
        len(configured),
        configured,
    )
    await _audit_alert(
        f"fr42-migration-{fleet}-{now_millis}",
        fleet,
        None,
        f"FR-42 first-deployment migration for fleet {fleet} at site {_site}: "
        f"{', '.join(configured) or 'no robots'} recorded as released (present in the "
        "fleet config at migration time). Any robot added later joins WATCH-ONLY.",
    )
    return configured


async def reconcile_identity(fleet: str) -> List[str]:
    """FR-42 (j): a robot removed from the fleet config loses its release
    record; re-adding it joins WATCH-ONLY. Returns the names retired."""
    if _site is None or _store_fault is not None:
        return []
    data, age = _status_of(fleet)
    if data is None or age is None or age > STATUS_MAX_AGE_S:
        return []
    configured = {str(n) for n in (data.get("configured") or [])}
    admitted = {str(n) for n in (data.get("admitted") or [])}
    if not configured:
        return []  # an empty roster is "cannot tell", never "delete everything"
    # a robot the fleet has ADMITTED is in the config by construction —
    # whatever the roster says (the f1-n39 boot published a roster that
    # shrank as robots were admitted); its record is never retired here
    gone = [
        name
        for name in list(_rows.get(fleet, {}))
        if name not in configured and name not in admitted
    ]
    for name in gone:
        try:
            await ttm.RobotRelease.filter(site=_site, fleet=fleet, robot=name).delete()
        except Exception as e:  # noqa: BLE001
            logger.error(
                "FR-42: could not retire release row for [%s/%s]: %s", fleet, name, e
            )
            continue
        _rows[fleet].pop(name, None)
        logger.warning(
            "FR-42: [%s/%s] is no longer in the fleet config — its release record "
            "is deleted (FR-42 (j)); if it is added back it joins WATCH-ONLY",
            fleet,
            name,
        )
    if gone:
        publish(fleet)
    return gone


def instrument_state(fleet: str) -> Optional[str]:
    """The named instrument fault for `fleet`, or None when the store is
    readable at both ends."""
    if _site is None:
        return "no site name configured (GF_SITE) — the release store cannot be keyed"
    if _store_fault is not None:
        return _store_fault
    data, age = _status_of(fleet)
    if data is None:
        return None  # cannot see the adapter: not a store fault
    state = str(data.get("store_state") or "")
    if state == STORE_NO_AUTHORITY:
        return (
            "the fleet adapter admitted nobody: no release authority answered "
            "within its bounded wait (" + str(data.get("store_detail") or "") + ")"
        )
    if state == STORE_FAULT:
        return "the fleet adapter was told the release store is unreadable: " + str(
            data.get("store_detail") or ""
        )
    return None


async def maintain_once() -> None:
    for fleet in fleets():
        await migrate_if_first(fleet)
        await reconcile_identity(fleet)
        await _maintain_instrument_alert(fleet)


async def _maintain_instrument_alert(fleet: str) -> None:
    fault = instrument_state(fleet)
    open_id = _instrument_alert.get(fleet)
    if fault and open_id is None:
        alert_id = f"fr42-release-store-{fleet}-{int(time.time() * 1000)}"
        _instrument_alert[fleet] = alert_id
        logger.error("FR-42 INSTRUMENT FAULT [%s]: %s", fleet, fault)
        if _alert_repo is None:
            return
        try:
            alert = await _alert_repo.create_alert(
                alert_id,
                "instrument",
                severity=ttm.Alert.Severity.Critical,
                fleet=fleet,
                robot=None,
                message=(
                    f"FR-42 release store fault on fleet {fleet}: {fault}. Every "
                    "robot not already admitted stays WATCH-ONLY — this is the "
                    "instrument failing, not a fleet decision. Check the api-server "
                    "and its database; releases resume the moment the store is read."
                ),
            )
            _emit(alert)
        except Exception as e:  # noqa: BLE001
            logger.warning("FR-42: instrument alert could not be raised: %s", e)
    elif not fault and open_id is not None:
        _instrument_alert.pop(fleet, None)
        logger.warning("FR-42: release store fault on [%s] cleared", fleet)
        if _alert_repo is None:
            return
        try:
            _emit(await _alert_repo.resolve_alert(open_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("FR-42: instrument alert could not be resolved: %s", e)


async def maintenance_loop() -> None:
    while True:
        try:
            await maintain_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the loop must outlive one bad tick
            logger.warning("FR-42 maintenance: %s", e)
        await asyncio.sleep(MAINTENANCE_PERIOD_S)


# ---- status (the dashboard surface) -----------------------------------


def status(fleet: str) -> dict:
    data, age = _status_of(fleet)
    fresh = data is not None and age is not None and age <= STATUS_MAX_AGE_S
    robots: Dict[str, dict] = {}
    if data is not None:
        for name, facts in (data.get("robots") or {}).items():
            if not isinstance(facts, dict):
                continue
            entry = dict(facts)
            entry["readiness"] = readiness(facts, age)
            entry["refusals"] = (
                [{"condition": c, "text": t} for c, t in release_refusals(facts)]
                if fresh
                else []
            )
            entry["released"] = name in _rows.get(fleet, {})
            entry["release"] = _rows.get(fleet, {}).get(name)
            robots[name] = entry
    return {
        "site": _site,
        "fleet": fleet,
        "store_fault": _store_fault,
        "instrument_fault": instrument_state(fleet),
        "adapter_reporting": fresh,
        "adapter_store_state": (data or {}).get("store_state"),
        "adapter_store_detail": (data or {}).get("store_detail"),
        "status_age_s": round(age, 1) if age is not None else None,
        "configured": list((data or {}).get("configured") or []),
        "admitted": list((data or {}).get("admitted") or []),
        "released": released(fleet),
        "migrated": _migrated.get(fleet),
        "robots": robots,
    }
