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
"""

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
