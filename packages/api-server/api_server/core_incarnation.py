"""F-454 (G ruling 2026-10-02, item 1): which incarnation of the fleet core
is running — the ONLY evidence the F-141 restart sweep acts on.

On release f1-n66 the sweep re-dispatched healthy queued missions 946 times
in 3 h while rmf-core never restarted. It convicted on silence and age: a
rolling "untouched for 300 s" pass, and a rule that a queued row nobody was
assigned to "was lost". A mission waiting in the dispatcher's bidding queue
is silent and unassigned for as long as it waits. Silence is absence of
evidence; it never says a restart happened.

A restart is a fact the core can state. The fleet adapter publishes ONE
latched record per process life on `gf_core_boot`:

    {"boot_id": "<random, new at every start>", "started_unix": <float>}

`started_unix` is the wall-clock start of the launch that started every
process of the core (the adapter's parent; rmf_core.launch.xml shuts the
whole launch down when the adapter or the schedule node dies, so a new
adapter is a new dispatcher and a new schedule too). The api-server keeps
the last boot id it settled in the database (CoreBoot), so the comparison
survives its own restarts:

    no record stored       FIRST      there is no old identity, but the
                                      boundary below still holds: a row
                                      created before THIS core started is
                                      not its row. Such rows are closed,
                                      never re-sent (nobody can say how
                                      long ago their core went)
    the same boot id       SAME       the core did not restart (the record
                                      is latched, so an api-server restart
                                      or a DDS rediscovery sees it again):
                                      nothing is swept
    a different boot id    RESTARTED  every mission the old core held is
                                      gone: each non-terminal row CREATED
                                      BEFORE the new core started is handled,
                                      once

"Created before the new core started" is exact, not a heuristic: a row is
created by a task state the core sent, the old core was dead before the
new launch began (one container), and the new core cannot know a task it
never issued. Closing a row makes it terminal, so no pass handles it twice;
the mission's next attempt is a new row, created after the boundary.

Why not the schedule's DDS liveliness (FR-39a's signal): measured
2026-10-02 (ops/e6/evidence/f4/stress-d85/f454-liveliness-on-kill/), a
schedule node killed with SIGKILL or SIGSEGV and replaced 2 s later reads
alive 1 -> 2 -> 1 at a reader — the dead writer is only dropped when its
lease runs out — so "alive went to 0" never happens on the crash it must
catch, and a rediscovery under load could read as a new writer with no
restart at all. An identity cannot be faked by either.

The core and the api-server run on the fleet PC and read one clock (DA-1).

How long the core was away decides what happens to what it interrupted
(`outage_s`): a short restart sends the missions again; after a long one —
or a database restored from an old backup — nobody can say they are still
wanted, so they are closed with the reason and an operator decides (the
F-435 resume window, G ruling 2026-10-01).

Pure logic and one thread-safe holder; no app imports.
"""

import json
import threading
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional

TOPIC = "gf_core_boot"

FIRST = "first"
SAME = "same"
RESTARTED = "restarted"

# how often the "core last heard" stamp is written to the database
HEARD_PERSIST_S = 10.0
# ... and how old a stamp must be before it is written. The new core's boot
# record arrives over DDS and its fleet states over the websocket: if the
# states won that race, a stamp of the NEW core would be stored under the
# OLD identity, the old core's last word would be gone, and every mission
# the restart interrupted would be failed as "away for an unknown time".
# A stamp this old cannot be one the boot record has not caught up with.
HEARD_SETTLE_S = 30.0
# what a boot record may hold (a stored id is a 255-character column)
BOOT_ID_MAX = 128
STARTED_MAX_UNIX = 1e11


class BootRecord(NamedTuple):
    boot_id: str
    started_unix: float

    @property
    def started_at(self) -> datetime:
        return datetime.fromtimestamp(self.started_unix, timezone.utc)


def parse(payload) -> Optional[BootRecord]:
    """The adapter's record, or None when it is not one — a record that
    cannot be read is no evidence of anything."""
    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        boot_id = data["boot_id"]
        started = float(data["started_unix"])
    except (TypeError, ValueError, KeyError):
        return None
    if not isinstance(boot_id, str) or not boot_id.strip():
        return None
    if len(boot_id.strip()) > BOOT_ID_MAX:
        return None
    # NaN, a negative, infinity, a year beyond any clock: not a start time
    if not 0 < started < STARTED_MAX_UNIX:
        return None
    return BootRecord(boot_id.strip(), started)


def verdict(known_boot_id: Optional[str], seen: BootRecord) -> str:
    if not known_boot_id:
        return FIRST
    if known_boot_id == seen.boot_id:
        return SAME
    return RESTARTED


def outage_s(heard, seen: BootRecord) -> Optional[float]:
    """Seconds between the old core's last word and the new core's start.
    `heard` holds the known "the core was last heard" stamps (the stored
    one, this life's); the old core's last word is the latest of them that
    predates the new core's start — a later stamp is the NEW core's word.
    None when no stamp predates it: how long the core was away cannot be
    told, and an unknown gap is never read as a short one."""
    before = []
    for moment in heard or []:
        if moment is None:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment.timestamp() < seen.started_unix:
            before.append(moment.timestamp())
    if not before:
        return None
    return seen.started_unix - max(before)


# heard() keeps this many stamps (HEARD_PERSIST_S apart): about 2 minutes
HISTORY_KEPT = 12


class CoreIncarnation:
    """Thread-safe: fed from the ROS thread (the boot record) and from the
    gateway (the core's last word), read by the async sweep."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest: Optional[BootRecord] = None
        self._unreadable = 0
        self._heard: Optional[datetime] = None
        # one stamp per HEARD_PERSIST_S, the last few minutes of them
        self._history: List[datetime] = []

    def on_boot(self, payload) -> Optional[BootRecord]:
        record = parse(payload)
        with self._lock:
            if record is None:
                self._unreadable += 1
            else:
                self._latest = record
        return record

    def latest(self) -> Optional[BootRecord]:
        with self._lock:
            return self._latest

    def heard(self, when: datetime) -> None:
        with self._lock:
            self._heard = when
            if (
                not self._history
                or (when - self._history[-1]).total_seconds() >= HEARD_PERSIST_S
            ):
                self._history.append(when)
                del self._history[:-HISTORY_KEPT]

    def last_heard(self) -> Optional[datetime]:
        with self._lock:
            return self._heard

    def heard_stamps(self) -> List[datetime]:
        """Every stamp still held, the latest included — the candidates
        for "the old core's last word" at a restart."""
        with self._lock:
            return list(self._history) + (
                [self._heard] if self._heard is not None else []
            )

    def settled_heard(self, now: datetime) -> Optional[datetime]:
        """The newest stamp at least HEARD_SETTLE_S old: the one that may
        be written to the database."""
        with self._lock:
            for stamp in reversed(self._history):
                if (now - stamp).total_seconds() >= HEARD_SETTLE_S:
                    return stamp
        return None

    @property
    def unreadable(self) -> int:
        with self._lock:
            return self._unreadable


# The api-server's one record: fed by the gateway's gf_core_boot
# subscription, drained by routes.internal.reap_interrupted_tasks.
STATE = CoreIncarnation()
