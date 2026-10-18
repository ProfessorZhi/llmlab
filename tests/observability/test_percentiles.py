"""分位数：只从原始样本算；空样本是 ``None``（UNKNOWN），不是 0。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.observability import PERCENTILE_METHOD, percentile  # noqa: E402


class PercentileTest(unittest.TestCase):
    def test_empty_sample_is_unknown_not_zero(self) -> None:
        self.assertIsNone(percentile([], 0.5))
        self.assertIsNone(percentile([], 0.95))
        self.assertIsNot(percentile([], 0.95), 0)

    def test_single_sample_is_itself(self) -> None:
        self.assertEqual(percentile([7.0], 0.5), 7.0)
        self.assertEqual(percentile([7.0], 0.95), 7.0)

    def test_type7_linear_interpolation_on_a_known_series(self) -> None:
        """1..10 的 p50 = 5.5，p95 = 9.55（线性插值 type 7，与 numpy 默认一致）。"""
        samples = [float(value) for value in range(1, 11)]
        self.assertAlmostEqual(percentile(samples, 0.5), 5.5)
        self.assertAlmostEqual(percentile(samples, 0.95), 9.55)

    def test_bounds_are_the_extremes(self) -> None:
        samples = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(percentile(samples, 0.0), 1.0)
        self.assertEqual(percentile(samples, 1.0), 4.0)
        self.assertEqual(percentile(samples, -1.0), 1.0)
        self.assertEqual(percentile(samples, 2.0), 4.0)

    def test_two_samples_interpolate(self) -> None:
        self.assertAlmostEqual(percentile([10.0, 20.0], 0.5), 15.0)
        self.assertAlmostEqual(percentile([10.0, 20.0], 0.95), 19.5)

    def test_method_is_declared_for_audit(self) -> None:
        self.assertEqual(PERCENTILE_METHOD, "linear-interpolation-type7")

    def test_is_monotone_in_p(self) -> None:
        samples = [float(value) for value in (5, 1, 9, 3, 7)]
        ordered = sorted(samples)
        values = [percentile(ordered, p) for p in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)]
        self.assertEqual(values, sorted(values))

    def test_result_is_never_outside_the_sample_range(self) -> None:
        samples = [3.0, 3.0, 8.0, 8.0]
        for p in (0.0, 0.01, 0.5, 0.99, 1.0):
            with self.subTest(p=p):
                value = percentile(samples, p)
                self.assertGreaterEqual(value, 3.0)
                self.assertLessEqual(value, 8.0)


if __name__ == "__main__":
    unittest.main()
