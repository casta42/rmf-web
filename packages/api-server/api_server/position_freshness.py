# Copyright 2026 GentleFleet
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""IS THIS POSE CURRENT? — the one answer, for every consumer (F-268).

`/fleet_states` republishes a robot's LAST ACCEPTED pose forever. When
RMF refuses an update — `[RobotUpdateHandle::update_position] The robot
[...] has diverged from its navigation graph`, 8077 of them in one
25-minute window — the position field does not blank, does not mark the
robot unlocated and does not stop arriving. It simply stops being true.
The staleness is right there in the same message: `location.t` is a
per-robot `builtin_interfaces/Time`. No consumer read it. The referee
convicted on it (18 zone violations, all vacated), and the operator's
map drew a robot standing somewhere it was not.

This module is that missing check, written ONCE so the referee and the
product cannot drift into two answers. It is deliberately pure: no ROS,
no I/O, no clock of its own — callers pass the stamps they read and the
wall time they read them at, which is also what makes it testable
without a fleet.

THE MEASURE IS RELATIVE, AND THAT IS THE POINT. A robot's lag is
measured against the NEWEST `location.t` in the same message, never
against a wall clock. Sim time, CPU stalls, container clock skew and a
loaded machine all move every stamp together and cancel out; only a feed
that has stopped tracking ONE robot opens a gap. Measured on a healthy
six-robot testsite_a under load, 25 minutes, 54 798 samples: median lag
0.02 s, p99 0.18 s, WORST 4.64 s — the tail is the dev laptop stalling
(worst gap between messages 7.9 s, worst jump in the clock 5.9 s), and
it is why the floor is not 5 s. The ghosts that started this: 409 s,
953 s, 1093 s, 1472 s.

TWO BARS, AND THEY ARE NOT THE SAME QUESTION. This is the correction
that came out of the live re-run on 2026-09-08, where a robot's feed
froze and a second robot drove the aisle 12 s later — inside the
confirmation window — and the referee convicted on a pose already 7.4 s
old. One threshold cannot serve both of these:

  * MAY I JUDGE A PHYSICAL INVARIANT ON THIS POSE? Tight, and derived
    from physics rather than patience: a pose L seconds old describes a
    robot that may be v_max x L away, and the referee rules on
    separations of half a metre. So it abstains once the pose could be
    more than half a footprint out of date. Abstaining costs a sample;
    convicting on a 7-second-old pose costs the whole instrument's
    credibility, which is what F-268 spent.

  * SHOULD I TELL AN OPERATOR THIS ROBOT IS LOST? Generous, and
    confirmed, because a dev laptop stalls and a loaded fleet is not a
    lost robot. Crying wolf here is how a guard gets ignored.

`unjudgeable()` answers the first. `stale` answers the second, and it is
the one the map and the alerts use.

TWO FAULTS, NOT ONE.

  * `stale` — one robot's stamp lags the newest beyond the threshold.
    That robot is unjudgeable: skip it, say so, and never convict on it.
  * `feed_frozen` — the newest stamp itself stops advancing while wall
    time runs on. Every robot then lags by zero and nothing looks wrong,
    which is exactly why this is checked separately. It is also the only
    thing that can catch a single-robot fleet, where "relative to the
    newest" is relative to itself.

Both are INSTRUMENT faults. Neither is ever evidence about robots
(F-191: a check that cannot see must SKIP and say why — no answer is not
a wrong answer).

WHY THE THRESHOLD IS DERIVED. A fixed number would be wrong on the next
site, the next publish rate, the next loaded machine. The threshold is
`max(floor, factor x period)` where `period` is the observed advance of
the newest stamp — the publish rate as it actually is, not as configured.
UNTIL THAT RATE HAS BEEN OBSERVED, NOTHING IS STALE. Not "the floor
stands in for it" — the floor is a 10 Hz number, and on a 1 Hz feed it
convicted a robot 20 updates behind during the first twenty seconds of
every run, which is a guard that fires on a boring environment. A watch
that has not yet learned the rate has no basis for a threshold, so it
says so and judges nothing (F-191).

The freeze check below is the exception, and deliberately: it is
measured against the WALL clock over a long floor and needs no knowledge
of the rate. It assumes only that this feed is faster than one message
per 15 s, which `/fleet_states` is by construction — RMF publishes it on
a fixed timer. Without that exception a feed frozen from its very first
message would never be reported at all, which is the silence this whole
module exists to end.

AND WHY IT IS CONFIRMED BEFORE IT COUNTS. A publisher that stalls for
several seconds and resumes delivers one fresh robot beside five whose
stamps are all suddenly old — a burst of "stale" that is really one
hiccup. A robot must therefore lag CONTINUOUSLY for `confirm_s` before
it is called stale. This is the boring-environment half of the both-ways
rule: the guard has to pass on a slow machine, not only fire on a ghost.

AND WHEN RMF WILL NOT PLACE A ROBOT, ITS BODY IS STILL THERE (F-469; G
ruling 2026-10-03). A stale stamp says the FLEET STATE's pose stopped
being true — not that nobody knows where the robot is. The fleet adapter
hears every member's odometry whether or not RMF accepts the pose built
from it, and publishes it on `gf_own_poses`. `OwnPoseFeed` at the bottom
of this file is the one answer to "may that pose stand in?", for the
referee and the product alike, and it is the same kind of answer as the
rest of this module: a pose with its own clock, checked, or nothing.
"""

import json
import math

# Floors, set from the measurement above and not from taste: 10 s is
# 2.2x the worst lag a HEALTHY loaded fleet produced and ~40x smaller
# than the shortest ghost in the record. Being generous here costs
# 13 s of detection latency; being tight costs a false conviction, and
# false convictions are what this module exists to end.
STALE_FLOOR_S = 10.0       # never call a pose stale below this lag
STALE_FACTOR = 50.0        # ... nor below this many publish periods
STALE_CONFIRM_S = 3.0      # ... nor until the lag has held that long
# The JUDGING bar (see "TWO BARS" above), from the robot rather than the
# machine: 0.30 m footprint radius, 0.5 m/s v_max (CLAUDE.md spec), and
# a refusal to rule on a pose that could be more than HALF a footprint
# out of date. 0.15 / 0.5 = 0.3 s. Measured healthy lag is 0.02 s median
# and 0.10 s p95, so this abstains on well under 1 % of samples — and
# every one of those it abstains from was a sample it could not see.
JUDGE_MAX_DRIFT_M = 0.15
JUDGE_V_MAX = 0.5
JUDGE_FLOOR_S = JUDGE_MAX_DRIFT_M / JUDGE_V_MAX
JUDGE_FACTOR = 3.0         # ... or three publish periods, whichever is more
FEED_FROZEN_FLOOR_S = 20.0     # newest stamp not advancing: feed is dark
FEED_FROZEN_FACTOR = 150.0
PERIOD_MIN_SAMPLES = 20    # before this, nothing is judged stale at all
PERIOD_WINDOW = 200        # advances kept for the median
PERIOD_CLAMP = (0.005, 2.0)   # s; a "period" outside this is not one


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


class RobotFreshness:
    """One robot's verdict for one sample."""

    __slots__ = ('key', 'stamp', 'lag_s', 'stale', 'judgeable',
                 'suspect_for_s', 'threshold_s', 'judge_bar_s', 'reason')

    def __init__(self, key, stamp, lag_s, stale, judgeable, suspect_for_s,
                 threshold_s, judge_bar_s, reason):
        self.key = key
        self.stamp = stamp
        self.lag_s = lag_s
        self.stale = stale
        self.judgeable = judgeable
        self.suspect_for_s = suspect_for_s
        self.threshold_s = threshold_s
        self.judge_bar_s = judge_bar_s
        self.reason = reason

    def as_dict(self):
        return {
            'robot': self.key,
            'stamp': round(self.stamp, 3),
            'lag_s': round(self.lag_s, 3),
            'stale': self.stale,
            'judgeable': self.judgeable,
            'threshold_s': (None if self.threshold_s is None
                            else round(self.threshold_s, 3)),
            'judge_bar_s': round(self.judge_bar_s, 3),
            'reason': self.reason,
        }


class FreshnessVerdict:
    """The whole message's verdict. `stale_keys` is what a consumer must
    refuse to judge on, and refuse to draw as current."""

    __slots__ = ('robots', 'stale_keys', 'unjudgeable_keys', 'feed_frozen',
                 'feed_frozen_s', 'period_s', 'threshold_s', 'judge_bar_s',
                 'clock_reset', 'newest_stamp')

    def __init__(self, robots, stale_keys, unjudgeable_keys, feed_frozen,
                 feed_frozen_s, period_s, threshold_s, judge_bar_s,
                 clock_reset, newest_stamp):
        self.robots = robots
        self.stale_keys = stale_keys
        self.unjudgeable_keys = unjudgeable_keys
        self.judge_bar_s = judge_bar_s
        self.feed_frozen = feed_frozen
        self.feed_frozen_s = feed_frozen_s
        self.period_s = period_s
        self.threshold_s = threshold_s
        self.clock_reset = clock_reset
        self.newest_stamp = newest_stamp

    def unjudgeable(self, key):
        """True when no physical invariant may be judged on this robot's
        pose: it is measurably behind, or the whole feed is dark.

        Deliberately much easier to trip than `stale`. A robot can be
        unjudgeable for a fraction of a second and never be reported to
        anyone — that is the referee abstaining, not an incident."""
        return self.feed_frozen or key in self.unjudgeable_keys

    def as_dict(self):
        return {
            'robots': [r.as_dict() for r in self.robots],
            'stale': sorted(self.stale_keys),
            'unjudgeable': sorted(self.unjudgeable_keys),
            'judge_bar_s': round(self.judge_bar_s, 3),
            'feed_frozen': self.feed_frozen,
            'feed_frozen_s': (None if self.feed_frozen_s is None
                              else round(self.feed_frozen_s, 2)),
            'period_s': (None if self.period_s is None
                         else round(self.period_s, 4)),
            'threshold_s': (None if self.threshold_s is None
                            else round(self.threshold_s, 3)),
            'clock_reset': self.clock_reset,
        }


class FreshnessWatch:
    """Rolling judgment of `location.t` freshness over a position feed.

    Call `sample(wall_now, stamps)` with every message: `wall_now` is a
    monotonic wall clock in seconds, `stamps` maps a robot key to the
    `location.t` of that robot IN THAT MESSAGE, in seconds. Keys are
    opaque — `(fleet, robot)`, `"fleet/robot"`, anything hashable.
    """

    def __init__(self, floor_s=STALE_FLOOR_S, factor=STALE_FACTOR,
                 confirm_s=STALE_CONFIRM_S,
                 frozen_floor_s=FEED_FROZEN_FLOOR_S,
                 frozen_factor=FEED_FROZEN_FACTOR,
                 judge_floor_s=JUDGE_FLOOR_S,
                 judge_factor=JUDGE_FACTOR):
        self.floor_s = float(floor_s)
        self.factor = float(factor)
        self.confirm_s = float(confirm_s)
        self.judge_floor_s = float(judge_floor_s)
        self.judge_factor = float(judge_factor)
        self.frozen_floor_s = float(frozen_floor_s)
        self.frozen_factor = float(frozen_factor)
        self._advances = []          # recent positive advances of newest
        self._prev_newest = None     # last newest stamp seen
        self._newest_moved_at = None # wall time newest last advanced
        self._suspect_since = {}     # key -> wall time lag first exceeded

    # ------------------------------------------------------------------
    def config(self):
        """What this watch was actually given — so a consumer can publish
        its own settings rather than have a reader assume them."""
        return {
            'stale_floor_s': self.floor_s,
            'stale_factor': self.factor,
            'stale_confirm_s': self.confirm_s,
            'feed_frozen_floor_s': self.frozen_floor_s,
            'feed_frozen_factor': self.frozen_factor,
            'judge_floor_s': self.judge_floor_s,
            'judge_factor': self.judge_factor,
        }

    def period_s(self):
        """Observed publish period, or None before enough of it."""
        if len(self._advances) < PERIOD_MIN_SAMPLES:
            return None
        return _median(self._advances)

    def threshold_s(self):
        """The lag a robot must exceed, or None while the publish rate is
        still unknown — in which case nothing is stale (see module doc)."""
        period = self.period_s()
        if period is None:
            return None
        return max(self.floor_s, self.factor * period)

    def judge_bar_s(self):
        """The lag past which no invariant may be judged on a pose.

        Unlike `threshold_s` this always has an answer, including before
        the publish rate is known — because abstaining is the SAFE
        direction here, and each of the two bars defaults the way that
        cannot hurt."""
        period = self.period_s()
        if period is None:
            return self.judge_floor_s
        return max(self.judge_floor_s, self.judge_factor * period)

    def frozen_after_s(self):
        period = self.period_s()
        if period is None:
            return self.frozen_floor_s
        return max(self.frozen_floor_s, self.frozen_factor * period)

    # ------------------------------------------------------------------
    def sample(self, wall_now, stamps):
        """Judge one message. Returns a FreshnessVerdict.

        An empty fleet is not a fault and not a freeze: there is nothing
        to be stale. It returns a verdict with no robots and the feed
        clock untouched — an empty site must not slowly convince this
        watch that the world has stopped.
        """
        wall_now = float(wall_now)
        if not stamps:
            return FreshnessVerdict([], set(), set(), False, None,
                                    self.period_s(), self.threshold_s(),
                                    self.judge_bar_s(), False, None)

        newest = max(stamps.values())
        clock_reset = False

        if self._prev_newest is None:
            self._newest_moved_at = wall_now
        elif newest < self._prev_newest - 1e-9:
            # The clock went BACKWARDS: a sim restart, a replayed bag, a
            # re-launched fleet. Nothing learned before it is about the
            # same timeline, and every lag measured across it is
            # meaningless. Start again rather than convict on the seam.
            clock_reset = True
            self._advances = []
            self._suspect_since = {}
            self._newest_moved_at = wall_now
        elif newest > self._prev_newest:
            advance = newest - self._prev_newest
            if PERIOD_CLAMP[0] <= advance <= PERIOD_CLAMP[1]:
                self._advances.append(advance)
                if len(self._advances) > PERIOD_WINDOW:
                    self._advances.pop(0)
            self._newest_moved_at = wall_now
        self._prev_newest = newest

        frozen_s = (None if self._newest_moved_at is None
                    else wall_now - self._newest_moved_at)
        feed_frozen = (frozen_s is not None
                       and frozen_s > self.frozen_after_s())

        threshold = self.threshold_s()
        judge_bar = self.judge_bar_s()
        robots, stale_keys, unjudgeable = [], set(), set()
        for key, stamp in stamps.items():
            lag = newest - stamp
            judgeable = lag <= judge_bar
            if not judgeable:
                unjudgeable.add(key)
            over = threshold is not None and lag > threshold
            since = self._suspect_since.get(key)
            if not over:
                self._suspect_since.pop(key, None)
                since = None
            elif since is None:
                since = self._suspect_since[key] = wall_now
            suspect_for = (0.0 if since is None else wall_now - since)
            stale = over and suspect_for >= self.confirm_s
            if stale:
                stale_keys.add(key)
                reason = (f'position stamp is {lag:.1f} s behind the '
                          f'newest in the same message (threshold '
                          f'{threshold:.1f} s, held {suspect_for:.1f} s)')
            elif over:
                reason = (f'lagging {lag:.1f} s, confirming '
                          f'({suspect_for:.1f}/{self.confirm_s:.1f} s)')
            elif not judgeable:
                reason = (f'{lag:.1f} s behind — too old to judge an '
                          f'invariant on (bar {judge_bar:.2f} s)')
            elif threshold is None:
                reason = ('publish rate not observed yet — freshness not '
                          'judged')
            else:
                reason = 'current'
            robots.append(RobotFreshness(key, stamp, lag, stale, judgeable,
                                         suspect_for, threshold, judge_bar,
                                         reason))

        # Robots that vanished from the feed keep no suspicion behind.
        for gone in set(self._suspect_since) - set(stamps):
            del self._suspect_since[gone]

        return FreshnessVerdict(robots, stale_keys, unjudgeable,
                                feed_frozen, frozen_s, self.period_s(),
                                threshold, judge_bar, clock_reset, newest)


# ----------------------------------------------------------------------
# THE ROBOT'S OWN POSE (F-469; G ruling 2026-10-03: "a robot's body is
# never invisible. When RMF won't place a robot, the sentinel and every
# surface use the robot's own reported, freshness-checked pose").
#
# The defect: a fleet member resting a hand's width off its lane cannot
# be placed by RMF, so by D-84 its pose is WITHHELD and the fleet state
# keeps the last pose RMF accepted, with its old stamp. Every rule above
# then does the right thing with the wrong consequence — the map shows
# the robot STALE and the referee skips it — while the robot's odometry
# is alive and says exactly where it stands. Measured on f1-n70
# (2026-10-02): gentle_bot_5 withheld for 46 s, 459 reports, at rest
# 0.9 m from the pose every surface was showing.
#
# `gf_own_poses` (std_msgs/String JSON, the fleet adapter, ~2 Hz) lists
# EVERY member — so "feed alive, robot placed" can be told from "no
# feed" — with the pose, the age of the odometry sample it came from,
# the robot interface's own verdict on that age (`stale`), and whether
# RMF is being told it (`placement`: "placed" | "withheld").
#
# TWO BARS HERE TOO, for the same two questions as above:
#
#   * MAY I JUDGE A PHYSICAL INVARIANT ON IT? The referee's bar is not
#     seconds, it is METRES: it refuses to rule on a pose that could be
#     more than half a footprint (JUDGE_MAX_DRIFT_M) out of date. For a
#     fleet-state pose nothing says how fast the robot is going, so v_max
#     is assumed and the bar comes out as 0.3 s — which a 2 Hz feed can
#     never meet. But this feed SHOWS how fast the robot is going: the
#     distance between its own consecutive reports. So the same bar is
#     applied to what is measured: the pose may be judged while the robot
#     moves no more than JUDGE_MAX_DRIFT_M per feed period (0.3 m/s at
#     2 Hz) over its last two reports — and a robot the feed has not yet
#     shown twice is not judged at all.
#       That is not a corner. A robot RMF cannot place usually stands;
#     the one of the measurement above drove 1.05 m in the first 3 s of
#     its withholding (the leg RMF was about to cancel, 0.44 m/s) and
#     THEN stood for 43 s. Judged on a pose up to 0.6 s old it would have
#     been drawn a quarter of a metre behind its body while it drove;
#     skipped for those seconds, as it always was, costs nothing.
#       And a liveness bar in seconds, for the feed itself: one period,
#     plus the longest adapter stall the stress bar accepts (1 s, D-85
#     bar 3), plus the odometry's own age and delivery. It is also the
#     bar after which the referee already calls a robot it has stopped
#     hearing "lost".
#   * MAY THE MAP DRAW IT AS CURRENT? The operator-facing patience — the
#     same floor below which a fleet-state pose is never called stale —
#     and no drift bar: a moving marker half a second behind is what
#     every marker on a 1 Hz map already is.
#
# Whatever the bar, a pose the robot interface itself calls stale
# (odometry silent past its timeout) is never fresh, and a robot the
# feed does not list, or lists without a pose, is never fresh: absence
# of evidence is not evidence (F-191). The caller then does what it did
# before this existed.
# ----------------------------------------------------------------------
OWN_JUDGE_MAX_AGE_S = 2.0
OWN_JUDGE_MAX_DRIFT_M = JUDGE_MAX_DRIFT_M
OWN_DISPLAY_MAX_AGE_S = STALE_FLOOR_S
# What the feed says of itself (`period_s`); assumed when it does not.
OWN_FEED_PERIOD_S = 0.5
PLACEMENT_PLACED = 'placed'
PLACEMENT_WITHHELD = 'withheld'


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


class OwnPose:
    """One robot's own pose, already judged fresh."""

    __slots__ = ('x', 'y', 'yaw', 'map', 'age_s', 'placement', 'report')

    def __init__(self, x, y, yaw, map_name, age_s, placement, report):
        self.x = x
        self.y = y
        self.yaw = yaw
        self.map = map_name
        self.age_s = age_s
        self.placement = placement
        self.report = report

    @property
    def withheld(self):
        """RMF is NOT being told this pose (it cannot place it)."""
        return self.placement == PLACEMENT_WITHHELD


class OwnPoseFeed:
    """The latest `gf_own_poses` message per fleet, and its judgment.

    Pure like the rest of this module: callers pass the payload and the
    clocks they read. `on_message` is safe to call from a ROS callback —
    it stores, and never raises.
    """

    def __init__(self):
        self._latest = {}     # fleet -> (payload, monotonic time received)
        # (fleet, robot) -> {'at': (x, y, pose_unix), 'speeds': [m/s]}:
        # the robot's last odometry sample and its speed over its last
        # two steps, from its own consecutive reports
        self._motion = {}

    def on_message(self, raw, mono_now):
        """Keep one payload. Returns the fleet it was for, or None when it
        is not a usable feed message (and then nothing is kept)."""
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get('fleet'):
            return None
        if not isinstance(data.get('robots'), dict) \
                or not _number(data.get('unix_millis_time')):
            return None
        fleet = str(data['fleet'])
        self._latest[fleet] = (data, float(mono_now))
        listed = set()
        for name, entry in data['robots'].items():
            listed.add((fleet, str(name)))
            self._track((fleet, str(name)), entry)
        for key in [k for k in self._motion
                    if k[0] == fleet and k not in listed]:
            del self._motion[key]
        return fleet

    def _track(self, key, entry):
        """One robot's motion, from its own consecutive odometry samples.
        A report with no pose forgets what was known (it is not evidence
        of standing still); the SAME sample reported again teaches
        nothing and changes nothing."""
        if not isinstance(entry, dict) or not (
                _number(entry.get('x')) and _number(entry.get('y'))
                and _number(entry.get('pose_unix'))):
            self._motion.pop(key, None)
            return
        sample = (float(entry['x']), float(entry['y']),
                  float(entry['pose_unix']))
        state = self._motion.get(key)
        if state is None:
            self._motion[key] = {'at': sample, 'speeds': []}
            return
        x0, y0, t0 = state['at']
        dt = sample[2] - t0
        if dt <= 0.0:
            return
        speed = math.hypot(sample[0] - x0, sample[1] - y0) / dt
        state['at'] = sample
        state['speeds'] = (state['speeds'] + [speed])[-2:]

    def heard(self, fleet):
        return str(fleet) in self._latest

    def clear(self):
        self._latest = {}
        self._motion = {}

    def pose(self, fleet, robot, wall_now, mono_now, max_age_s,
             max_drift_m=None):
        """(OwnPose, 'current') when this robot's own pose is fresh
        enough to use, else (None, why).

        `max_drift_m` is the referee's bar (see TWO BARS above): given,
        the pose is refused while the robot has moved more than that per
        feed period over either of its last two reports, and while the
        feed has not yet shown two steps of it. Left None (the map), a
        moving robot's pose is as usable as a standing one's.

        The age judged is the age of the ODOMETRY SAMPLE, now: what the
        adapter measured when it published, plus how old the message has
        become since. The message's age is read off both clocks and the
        larger wins — the wall clock catches a latched message from a
        publisher that has stopped publishing (it is delivered on
        subscription, however old), the monotonic clock catches a wall
        clock that stepped — so either one failing reads as OLD, never
        as fresh.
        """
        held = self._latest.get(str(fleet))
        if held is None:
            return None, 'no own-pose feed has been heard from this fleet'
        data, mono_at = held
        entry = data['robots'].get(str(robot))
        if not isinstance(entry, dict):
            return None, 'the own-pose feed does not list this robot'
        if entry.get('stale') is not False:
            return None, ("the robot's own odometry is stale"
                          if entry.get('stale') else
                          'the robot has not reported a pose')
        x, y, age = entry.get('x'), entry.get('y'), entry.get('age_s')
        if not (_number(x) and _number(y) and _number(age)) or age < 0:
            return None, 'the own-pose entry carries no usable pose'
        message_age = max(
            float(wall_now) - float(data['unix_millis_time']) / 1000.0,
            float(mono_now) - mono_at, 0.0)
        pose_age = float(age) + message_age
        if pose_age > float(max_age_s):
            return None, (f'the own pose is {pose_age:.1f} s old '
                          f'(bar {float(max_age_s):.1f} s)')
        if max_drift_m is not None:
            speeds = (self._motion.get((str(fleet), str(robot)))
                      or {}).get('speeds') or []
            if len(speeds) < 2:
                return None, ('the own-pose feed has not yet shown whether '
                              'this robot is moving')
            period = data.get('period_s')
            if not _number(period) or period <= 0:
                period = OWN_FEED_PERIOD_S
            # out of date by the pose's own age, or by the time to the
            # feed's next report, whichever is more (code review of D-89:
            # a 2 s old pose of a robot at 0.3 m/s is 0.6 m behind it)
            drift = max(speeds) * max(float(period), pose_age)
            if drift > float(max_drift_m):
                return None, (
                    f'the robot is moving ({max(speeds):.2f} m/s): its own '
                    f'pose is up to {drift:.2f} m out of date '
                    f'(bar {float(max_drift_m):.2f} m)')
        yaw = entry.get('yaw')
        return OwnPose(float(x), float(y),
                       float(yaw) if _number(yaw) else 0.0,
                       entry.get('map'), pose_age,
                       entry.get('placement'), entry.get('report')), 'current'
