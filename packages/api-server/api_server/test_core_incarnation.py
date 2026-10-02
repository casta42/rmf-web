"""F-454 (G ruling 2026-10-02, item 1): when is a coordination restart
REAL? Only when the fleet core states a boot identity different from the
stored one. Both ways: the boring cases (nothing said, nothing stored, the
same identity again, a record that cannot be read) are never a restart."""

import json
import unittest
from datetime import datetime, timezone

from api_server.core_incarnation import (
    FIRST,
    HEARD_PERSIST_S,
    HEARD_SETTLE_S,
    RESTARTED,
    SAME,
    BootRecord,
    CoreIncarnation,
    outage_s,
    parse,
    verdict,
)


def at(unix):
    return datetime.fromtimestamp(unix, timezone.utc)


class ParseTest(unittest.TestCase):
    def test_the_adapters_record(self):
        record = parse(
            json.dumps(
                {
                    "boot_id": "ab12",
                    "started_unix": 1790929956.419,
                    "started_source": "launch",
                }
            )
        )
        self.assertEqual(record, BootRecord("ab12", 1790929956.419))
        self.assertEqual(record.started_at, at(1790929956.419))

    def test_what_is_not_a_record_is_no_evidence(self):
        for bad in (
            "",
            "not json",
            "[]",
            "{}",
            '{"boot_id": "x"}',
            '{"boot_id": "", "started_unix": 5}',
            '{"boot_id": "   ", "started_unix": 5}',
            '{"boot_id": 7, "started_unix": 5}',
            '{"boot_id": "x", "started_unix": "soon"}',
            '{"boot_id": "x", "started_unix": 0}',
            '{"boot_id": "x", "started_unix": -3}',
            '{"boot_id": "x", "started_unix": NaN}',
            '{"boot_id": "x", "started_unix": Infinity}',
            '{"boot_id": "x", "started_unix": 1e20}',
            '{"boot_id": "%s", "started_unix": 5}' % ("x" * 300),
            None,
            7,
        ):
            self.assertIsNone(parse(bad), repr(bad))


class VerdictTest(unittest.TestCase):
    SEEN = BootRecord("new", 1000.0)

    def test_FIRES_a_different_identity_is_a_restart(self):
        self.assertEqual(verdict("old", self.SEEN), RESTARTED)

    def test_PASSES_nothing_stored_is_a_first_run_not_a_restart(self):
        self.assertEqual(verdict(None, self.SEEN), FIRST)
        self.assertEqual(verdict("", self.SEEN), FIRST)

    def test_PASSES_the_same_identity_again_is_the_same_core(self):
        """An api-server restart and a DDS rediscovery both read the
        latched record again."""
        self.assertEqual(verdict("new", self.SEEN), SAME)


class OutageTest(unittest.TestCase):
    SEEN = BootRecord("new", 1000.0)

    def test_from_the_old_cores_last_word_to_the_new_cores_start(self):
        self.assertEqual(outage_s([at(988.0)], self.SEEN), 12.0)

    def test_a_word_after_the_new_start_is_the_new_cores(self):
        self.assertEqual(outage_s([at(900.0), at(1005.0)], self.SEEN), 100.0)

    def test_the_latest_word_before_the_start_counts(self):
        self.assertEqual(outage_s([at(400.0), at(990.0), None], self.SEEN), 10.0)

    def test_an_unknown_gap_stays_unknown(self):
        self.assertIsNone(outage_s([], self.SEEN))
        self.assertIsNone(outage_s([None], self.SEEN))
        self.assertIsNone(outage_s(None, self.SEEN))
        self.assertIsNone(
            outage_s([at(1005.0)], self.SEEN),
            "only the new core was ever heard: nothing says how long the "
            "old one had been gone",
        )

    def test_naive_stamps_are_utc(self):
        naive = at(940.0).replace(tzinfo=None)
        self.assertEqual(outage_s([naive], self.SEEN), 60.0)


class HolderTest(unittest.TestCase):
    def test_the_latest_readable_record_is_kept(self):
        state = CoreIncarnation()
        self.assertIsNone(state.latest())
        state.on_boot('{"boot_id": "a", "started_unix": 10}')
        self.assertIsNone(state.on_boot("garbage"))
        self.assertEqual(state.latest(), BootRecord("a", 10.0))
        self.assertEqual(state.unreadable, 1)
        state.on_boot('{"boot_id": "b", "started_unix": 20}')
        self.assertEqual(state.latest(), BootRecord("b", 20.0))

    def test_heard(self):
        state = CoreIncarnation()
        self.assertIsNone(state.last_heard())
        self.assertEqual(state.heard_stamps(), [])
        state.heard(at(5.0))
        self.assertEqual(state.last_heard(), at(5.0))

    def test_only_a_settled_stamp_may_be_stored(self):
        """A fresh stamp may be the NEW core's, heard before its boot
        record: stored under the old identity it would erase the old
        core's last word."""
        state = CoreIncarnation()
        for second in range(1000, 1100):  # a fleet talking at 1 Hz
            state.heard(at(float(second)))
        self.assertIsNone(state.settled_heard(at(1010.0)), "nothing is old enough")
        settled = state.settled_heard(at(1099.0))
        self.assertIsNotNone(settled)
        self.assertGreaterEqual(1099.0 - settled.timestamp(), HEARD_SETTLE_S)
        self.assertLess(
            1099.0 - settled.timestamp(), HEARD_SETTLE_S + HEARD_PERSIST_S + 1
        )
        # the stamps kept are the candidates for "the old core's last word"
        stamps = state.heard_stamps()
        self.assertEqual(stamps[-1], at(1099.0))
        self.assertLessEqual(len(stamps), 13)

    def test_the_old_cores_last_word_survives_the_new_cores_first_words(self):
        state = CoreIncarnation()
        for second in range(1000, 1040):  # the old core, then silence
            state.heard(at(float(second)))
        new = BootRecord("new", 1060.0)
        state.heard(at(1062.0))  # the new core is heard first
        self.assertAlmostEqual(outage_s(state.heard_stamps(), new), 20.0, delta=10.0)


if __name__ == "__main__":
    unittest.main()
