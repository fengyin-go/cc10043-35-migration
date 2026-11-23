"""检查点时间模型的单元测试：精度解析、时刻比较、30 天清退边界语义。

仅依赖标准库，在 backend 目录下运行：
    .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.services.checkpoint_time import (
    compare_points,
    evaluate_expiry,
    parse_time_point,
)

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)


class ParseTimePointTest(unittest.TestCase):
    def test_day_precision_preserves_date_only_fact(self) -> None:
        point, error = parse_time_point("2026-10-24")
        self.assertIsNone(error)
        self.assertIsNotNone(point)
        self.assertEqual(point.precision, "day")
        # 规范化后仍是纯日期，不被提升成带零点的时刻。
        self.assertEqual(point.iso_text(), "2026-10-24")
        self.assertIsNone(point.tz_offset)
        self.assertEqual(point.raw, "2026-10-24")

    def test_instant_with_offset_converts_to_utc_and_keeps_offset(self) -> None:
        point, error = parse_time_point("2026-10-25T10:00:00+08:00")
        self.assertIsNone(error)
        self.assertEqual(point.precision, "instant")
        self.assertEqual(point.tz_offset, "+08:00")
        self.assertEqual(
            point.instant.astimezone(timezone.utc).isoformat(),
            "2026-10-25T02:00:00+00:00")

    def test_z_suffix_is_utc(self) -> None:
        point, error = parse_time_point("2026-11-01T09:00:00Z")
        self.assertIsNone(error)
        self.assertEqual(point.precision, "instant")
        self.assertEqual(point.tz_offset, "+00:00")

    def test_naive_datetime_is_quarantined_not_guessed(self) -> None:
        point, error = parse_time_point("2026-11-01T09:00:00")
        self.assertIsNone(point)
        self.assertIsNotNone(error)
        self.assertIn("时区", error)

    def test_garbage_and_invalid_date_are_quarantined(self) -> None:
        for bad in ("2026-13-40 无法解析的坏历史值", "not-a-time", "", None):
            with self.subTest(bad=bad):
                point, error = parse_time_point(bad)
                self.assertIsNone(point)
                self.assertIsNotNone(error)


class ComparePointsTest(unittest.TestCase):
    def test_two_instants_compare_by_actual_instant(self) -> None:
        older, _ = parse_time_point("2026-10-25T10:00:00+08:00")
        newer, _ = parse_time_point("2026-10-25T03:00:00+00:00")
        self.assertEqual(compare_points(older, newer), "before")
        self.assertEqual(compare_points(newer, older), "after")
        same, _ = parse_time_point("2026-10-25T02:00:00+00:00")
        self.assertEqual(compare_points(older, same), "equal")

    def test_same_calendar_day_mixed_precision_is_unknown_not_two_midnights(self) -> None:
        instant, _ = parse_time_point("2026-10-25T10:00:00+08:00")
        day, _ = parse_time_point("2026-10-25")
        self.assertEqual(compare_points(instant, day), "same_day_unknown")
        self.assertEqual(compare_points(day, instant), "same_day_unknown")

    def test_two_day_values_on_same_day_are_unknown(self) -> None:
        left, _ = parse_time_point("2026-10-25")
        right, _ = parse_time_point("2026-10-25")
        self.assertEqual(compare_points(left, right), "same_day_unknown")

    def test_day_values_on_different_days_are_decidable(self) -> None:
        older, _ = parse_time_point("2026-10-24")
        newer, _ = parse_time_point("2026-10-25")
        self.assertEqual(compare_points(older, newer), "before")
        self.assertEqual(compare_points(newer, older), "after")

    def test_instant_inside_vs_day_adjacent_days(self) -> None:
        # 10-25 凌晨 UTC 仍晚于 10-24 整个自然日。
        instant, _ = parse_time_point("2026-10-25T00:30:00+00:00")
        day, _ = parse_time_point("2026-10-24")
        self.assertEqual(compare_points(day, instant), "before")


class EvaluateExpiryTest(unittest.TestCase):
    def test_instant_keep_and_expire(self) -> None:
        young, _ = parse_time_point("2026-10-25T02:00:00+00:00")
        self.assertEqual(evaluate_expiry(young, NOW)["decision"], "keep")
        old, _ = parse_time_point("2026-09-01T08:00:00+00:00")
        self.assertEqual(evaluate_expiry(old, NOW)["decision"], "expire")

    def test_day_straddling_threshold_goes_to_review(self) -> None:
        # 基准 11-23 12:00，30 天边界为 10-24 12:00；10-24 自然日横跨边界。
        boundary, _ = parse_time_point("2026-10-24")
        verdict = evaluate_expiry(boundary, NOW)
        self.assertEqual(verdict["decision"], "review")
        min_age, max_age = verdict["age_window_days"]
        self.assertLess(min_age, 30)
        self.assertGreaterEqual(max_age, 30)

    def test_day_fully_young_keeps(self) -> None:
        young, _ = parse_time_point("2026-10-25")
        verdict = evaluate_expiry(young, NOW)
        self.assertEqual(verdict["decision"], "keep")
        self.assertLessEqual(max(verdict["age_window_days"]), 30)

    def test_day_fully_old_expires(self) -> None:
        old, _ = parse_time_point("2026-10-23")
        verdict = evaluate_expiry(old, NOW)
        self.assertEqual(verdict["decision"], "expire")
        self.assertGreater(min(verdict["age_window_days"]), 30)


if __name__ == "__main__":
    unittest.main()
