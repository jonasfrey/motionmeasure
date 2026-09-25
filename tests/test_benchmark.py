import unittest

from benchmark_analysis import compare_events


def events(*spans):
    return {'segments': [{'start': a, 'end': b} for a, b in spans]}


class ComparisonTests(unittest.TestCase):
    def test_misses_extras_splits_and_coverage(self):
        result = compare_events(events((1, 5), (10, 12)), events((1, 2), (3, 5), (20, 21)))
        self.assertEqual(result['missed_baseline_events'], 1)
        self.assertEqual(result['unmatched_candidate_events'], 1)
        self.assertEqual(result['split_baseline_events'], 1)
        self.assertEqual(result['baseline_time_covered'], .5)
        self.assertAlmostEqual(result['temporal_iou'], 3/7)

    def test_merged_events_and_empty_baseline(self):
        result = compare_events(events((1, 2), (3, 4)), events((1, 4)))
        self.assertEqual(result['merged_candidate_events'], 1)
        self.assertEqual(result['missed_baseline_events'], 0)
        self.assertAlmostEqual(result['temporal_iou'], 2/3)
        self.assertIsNone(compare_events(events(), events((1, 2)))['baseline_time_covered'])
        self.assertEqual(compare_events(events(), events())['temporal_iou'], 1)
