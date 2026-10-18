"""时间窗与桶边界：窗口有界、时区必须显式、桶起点在 UTC 下取整。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import parse_moment  # noqa: E402

from backend.observability import (  # noqa: E402
    MAX_POINTS,
    MAX_WINDOW_HOURS,
    SUPPORTED_BUCKETS,
    InvalidBucket,
    InvalidWindow,
    bucket_start_of,
    expected_bucket_starts,
    validate_window,
    within_window,
)

UTC = timezone.utc


def _moment(value: str) -> datetime:
    return parse_moment(value)


class ValidateWindowTest(unittest.TestCase):
    def test_happy_path_normalises_to_utc(self) -> None:
        start, end = validate_window(
            _moment("2026-10-15T08:00:00Z"), _moment("2026-10-15T12:00:00Z"), "1h"
        )
        self.assertEqual(start, _moment("2026-10-15T08:00:00Z"))
        self.assertEqual(end, _moment("2026-10-15T12:00:00Z"))

    def test_offsets_are_converted_not_truncated(self) -> None:
        plus8 = timezone(timedelta(hours=8))
        start, end = validate_window(
            datetime(2026, 10, 15, 16, 0, tzinfo=plus8),
            datetime(2026, 10, 15, 20, 0, tzinfo=plus8),
            "1h",
        )
        self.assertEqual(start, _moment("2026-10-15T08:00:00Z"))
        self.assertEqual(end, _moment("2026-10-15T12:00:00Z"))

    def test_naive_timestamps_are_rejected(self) -> None:
        """没有显式偏移的时间戳无法判定属于哪个窗口 —— 拒绝而不是假定 UTC。"""
        with self.assertRaises(InvalidWindow):
            validate_window(datetime(2026, 10, 15, 8), _moment("2026-10-15T12:00:00Z"), "1h")
        with self.assertRaises(InvalidWindow):
            validate_window(_moment("2026-10-15T08:00:00Z"), datetime(2026, 10, 15, 12), "1h")

    def test_empty_and_reversed_windows_are_rejected(self) -> None:
        for start, end in (
            ("2026-10-15T08:00:00Z", "2026-10-15T08:00:00Z"),
            ("2026-10-15T12:00:00Z", "2026-10-15T08:00:00Z"),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(InvalidWindow):
                    validate_window(_moment(start), _moment(end), "1h")

    def test_non_datetime_is_rejected(self) -> None:
        with self.assertRaises(InvalidWindow):
            validate_window("2026-10-15T08:00:00Z", _moment("2026-10-15T12:00:00Z"), "1h")

    def test_unsupported_bucket_is_rejected(self) -> None:
        for bucket in ("1m", "1w", "1y", "", None, "1H"):
            with self.subTest(bucket=bucket):
                with self.assertRaises(InvalidBucket):
                    validate_window(
                        _moment("2026-10-15T08:00:00Z"), _moment("2026-10-15T12:00:00Z"), bucket
                    )

    def test_window_span_is_bounded_per_bucket(self) -> None:
        """无界时间窗是 Web 层唯一能放大的输入 —— 每个粒度都有跨度硬上限。"""
        for bucket, limit_hours in MAX_WINDOW_HOURS.items():
            with self.subTest(bucket=bucket):
                too_long = _moment("2026-10-01T00:00:00Z") + timedelta(hours=limit_hours + 1)
                with self.assertRaises(InvalidWindow):
                    validate_window(_moment("2026-10-01T00:00:00Z"), too_long, bucket)

    def test_exactly_at_the_limit_is_accepted(self) -> None:
        for bucket, limit_hours in MAX_WINDOW_HOURS.items():
            with self.subTest(bucket=bucket):
                start = _moment("2026-10-01T00:00:00Z")
                end = start + timedelta(hours=limit_hours)
                self.assertEqual(validate_window(start, end, bucket), (start, end))

    def test_five_minute_bucket_cannot_reach_a_year(self) -> None:
        with self.assertRaises(InvalidWindow):
            validate_window(
                _moment("2026-01-01T00:00:00Z"), _moment("2026-12-31T00:00:00Z"), "5m"
            )


class ExpectedBucketStartsTest(unittest.TestCase):
    def test_hourly_buckets_are_floored_to_the_hour(self) -> None:
        starts = expected_bucket_starts(
            _moment("2026-10-15T08:20:00Z"), _moment("2026-10-15T11:05:00Z"), "1h"
        )
        self.assertEqual(
            starts,
            [
                _moment("2026-10-15T08:00:00Z"),
                _moment("2026-10-15T09:00:00Z"),
                _moment("2026-10-15T10:00:00Z"),
                _moment("2026-10-15T11:00:00Z"),
            ],
        )

    def test_daily_buckets_are_floored_to_the_day_in_utc(self) -> None:
        starts = expected_bucket_starts(
            _moment("2026-10-15T08:00:00Z"), _moment("2026-10-17T01:00:00Z"), "1d"
        )
        self.assertEqual(
            starts,
            [
                _moment("2026-10-15T00:00:00Z"),
                _moment("2026-10-16T00:00:00Z"),
                _moment("2026-10-17T00:00:00Z"),
            ],
        )

    def test_five_minute_buckets(self) -> None:
        starts = expected_bucket_starts(
            _moment("2026-10-15T08:00:00Z"), _moment("2026-10-15T08:20:00Z"), "5m"
        )
        self.assertEqual(
            [moment.minute for moment in starts], [0, 5, 10, 15]
        )

    def test_expected_count_never_exceeds_the_hard_point_cap(self) -> None:
        for bucket, limit_hours in MAX_WINDOW_HOURS.items():
            with self.subTest(bucket=bucket):
                start = _moment("2026-10-01T00:00:00Z")
                starts = expected_bucket_starts(
                    start, start + timedelta(hours=limit_hours), bucket
                )
                self.assertLessEqual(len(starts), MAX_POINTS)

    def test_dst_free_by_construction_because_utc(self) -> None:
        """UTC 下取整：不会出现 23/25 小时的「天」，覆盖率因此跨时区可比。"""
        starts = expected_bucket_starts(
            _moment("2026-03-28T23:30:00Z"), _moment("2026-03-31T00:30:00Z"), "1d"
        )
        deltas = {
            (starts[index + 1] - starts[index]) for index in range(len(starts) - 1)
        }
        self.assertEqual(deltas, {timedelta(days=1)})


class BucketStartOfTest(unittest.TestCase):
    def test_flooring_does_not_mutate_the_input(self) -> None:
        moment = _moment("2026-10-15T08:47:31Z")
        floored = bucket_start_of(moment, "1h")
        self.assertEqual(moment, _moment("2026-10-15T08:47:31Z"))
        self.assertEqual(floored, _moment("2026-10-15T08:00:00Z"))

    def test_naive_moment_is_rejected(self) -> None:
        with self.assertRaises(InvalidWindow):
            bucket_start_of(datetime(2026, 10, 15, 8, 0), "1h")

    def test_unsupported_bucket_is_rejected(self) -> None:
        with self.assertRaises(InvalidBucket):
            bucket_start_of(_moment("2026-10-15T08:00:00Z"), "1w")

    def test_supported_buckets_are_the_declared_set(self) -> None:
        self.assertEqual(SUPPORTED_BUCKETS, ("5m", "1h", "1d"))


class WithinWindowTest(unittest.TestCase):
    def test_half_open_interval(self) -> None:
        start = _moment("2026-10-15T08:00:00Z")
        end = _moment("2026-10-15T12:00:00Z")
        self.assertTrue(within_window(start, start, end))       # 左闭
        self.assertFalse(within_window(end, start, end))        # 右开
        self.assertTrue(within_window(end - timedelta(seconds=1), start, end))

    def test_boundary_row_is_excluded_not_counted(self) -> None:
        """恰好在 ``to`` 的行属于下一个窗口 —— 不计入本窗口，也不四舍五入进来。"""
        end = _moment("2026-10-15T12:00:00Z")
        self.assertFalse(within_window(end, _moment("2026-10-15T08:00:00Z"), end))


if __name__ == "__main__":
    unittest.main()
