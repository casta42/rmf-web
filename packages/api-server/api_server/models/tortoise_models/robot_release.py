"""FR-42 (D-66, G ruling 2026-09-10): the RELEASE STORE — which robots an
admin has released from WATCH-ONLY (commissioning) mode, per site.

One row per (site, fleet, robot): the robot's fleet-config name at that
site. No row means WATCH-ONLY: the fleet reads and renders the robot and
sends it nothing. A row is written by exactly three actors, each named
in `actor_kind`:

  * `admin`     — the dashboard release (FR-42 (d)): hard-confirmed,
                  typed reason, three-line checklist affirmed;
  * `harness`   — a simulation/test harness through the SAME admin API,
                  one robot at a time, checklist NOT affirmed (FR-42 (i):
                  a supervising person, a tested e-stop and a dead-man in
                  hand are false for a simulated robot and never affirmed
                  in a rehearsal);
  * `migration` — the once-per-site first-deployment entry (FR-42 (h)):
                  the robots present in the fleet config at that moment,
                  so existing sites keep running. A name added later is
                  never covered by it.

The row IS the audit record (FR-42 (e)): actor, time, site, robot, typed
reason, checklist. It is also mirrored into the FR-31 alert archive as an
informational entry so the History view shows it.

Why this ledger (NFR-9, D-77): the api-server Postgres ledger is where
operator-authored RUNTIME intent lives — it rides the backup/restore
path F1.5 proved, and it survives every restart FR-42 (f) lists (adapter,
rmf-core, api-server, zone-editor apply, container rebuild) because none
of those touch it. The site config repo reaches the fleet only through a
re-derivation and a coordination restart; a config flag or environment
variable would be the bulk path FR-42 (i) forbids. The api-server is the
ONLY writer of `/gf_robot_releases`, and the fleet adapter admits no
robot before it has read that feed (the F-339 shape).
"""

from tortoise.fields import BigIntField, BooleanField, CharField, IntField, TextField
from tortoise.models import Model


class RobotRelease(Model):
    id = IntField(pk=True)
    site = CharField(255, index=True)
    fleet = CharField(255, index=True)
    robot = CharField(255, index=True)
    released_by = CharField(255)
    actor_kind = CharField(32)  # admin | harness | migration
    unix_millis_release_time = BigIntField()
    reason = TextField(default="")
    checklist_supervised = BooleanField(default=False)
    checklist_estop_tested = BooleanField(default=False)
    checklist_deadman_ready = BooleanField(default=False)

    class Meta:
        unique_together = (("site", "fleet", "robot"),)


class ReleaseMigration(Model):
    """FR-42 (h): recorded ONCE per (site, fleet). Its presence is what
    makes a later-added robot name join WATCH-ONLY: migration never runs
    twice."""

    id = IntField(pk=True)
    site = CharField(255, index=True)
    fleet = CharField(255, index=True)
    unix_millis_time = BigIntField()
    robots = TextField(default="[]")  # JSON list of the names released
    migrated_by = CharField(255, default="migration")

    class Meta:
        unique_together = (("site", "fleet"),)


__all__ = ["RobotRelease", "ReleaseMigration"]
