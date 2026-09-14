"""F-293 (FR-4 amendment): the dispatch horizon, proven both ways — a
far-future mission is refused or deferred, an ordinary one (now, a few
minutes ahead, in the past) is never touched, and a graph the gate
cannot read is said so rather than guessed at."""

import math
import unittest

from api_server import dispatch_horizon as dh


def graph(points, lanes, bidirectional=True):
    return {
        "vertices": [{"x": x, "y": y, "name": "", "params": {}} for x, y in points],
        "lanes": [
            {"a": a, "b": b, "bidirectional": bidirectional, "params": {}}
            for a, b in lanes
        ],
    }


# testsite_a-sized: a 30 m corridor with a 20 m arm -> longest trip 50 m
SITE = graph(
    [(0, 0), (10, 0), (20, 0), (30, 0), (30, 10), (30, 20)],
    [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)],
)


class DiameterTest(unittest.TestCase):
    def test_longest_shortest_path_along_lanes(self):
        self.assertAlmostEqual(dh.graph_diameter_m(SITE), 50.0)

    def test_lane_direction_is_respected(self):
        one_way = graph([(0, 0), (10, 0)], [(0, 1)], bidirectional=False)
        self.assertAlmostEqual(dh.graph_diameter_m(one_way), 10.0)

    def test_no_graph_no_diameter(self):
        self.assertIsNone(dh.graph_diameter_m(None))
        self.assertIsNone(dh.graph_diameter_m({"vertices": [], "lanes": []}))


class HorizonTest(unittest.TestCase):
    def test_derived_from_the_site(self):
        seconds, how = dh.horizon_s(SITE)
        # 50 m / (0.5 * 0.8) m/s * 1.5 + 10 s bid window, whole seconds up
        self.assertEqual(seconds, math.ceil(50 / 0.4 * 1.5 + 10))
        self.assertIn("50 m", how)

    def test_unreadable_graph_uses_the_stated_bound_and_says_so(self):
        seconds, how = dh.horizon_s(None)
        self.assertEqual(seconds, dh.FALLBACK_HORIZON_S)
        self.assertIn("could not be derived", how)


class ClassifyTest(unittest.TestCase):
    NOW_MS = 1_789_400_000_000
    HORIZON = 200.0

    def at(self, seconds_ahead):
        return dh.classify(
            self.NOW_MS + int(seconds_ahead * 1000), self.NOW_MS, self.HORIZON
        )

    def test_known_bad_the_drill_8_future_task_is_never_dispatched(self):
        # f285's task: start +6 h — deferred, never in a robot's queue
        self.assertEqual(self.at(6 * 3600), dh.DEFER)

    def test_known_bad_more_than_a_shift_ahead_is_refused(self):
        self.assertEqual(self.at(8 * 3600 + 1), dh.REFUSE)
        self.assertEqual(self.at(8 * 3600), dh.DEFER)

    def test_known_good_ordinary_dispatches_are_untouched(self):
        self.assertEqual(dh.classify(0, self.NOW_MS, self.HORIZON), dh.NOW)
        self.assertEqual(dh.classify(None, self.NOW_MS, self.HORIZON), dh.NOW)
        self.assertEqual(self.at(0), dh.NOW)  # the dashboard's now
        self.assertEqual(self.at(-3600), dh.NOW)  # a start in the past
        self.assertEqual(self.at(self.HORIZON), dh.NOW)
        self.assertEqual(self.at(self.HORIZON + 1), dh.DEFER)

    def test_the_shift_is_a_site_setting(self):
        self.assertEqual(
            dh.classify(
                self.NOW_MS + 3 * 3600 * 1000,
                self.NOW_MS,
                self.HORIZON,
                max_lead=2 * 3600,
            ),
            dh.REFUSE,
        )


if __name__ == "__main__":
    unittest.main()
