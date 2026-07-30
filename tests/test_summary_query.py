from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.summary_query import (
    ParsedQuery,
    SummaryQuery,
    SummaryQueryError,
    resolve_query,
)


NOW_UTC = datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)  # 12:00 in UTC+8


class ResolveQueryTests(unittest.TestCase):
    def _resolve(self, parsed: ParsedQuery, *, tz: str = "UTC+8") -> SummaryQuery:
        return resolve_query(
            parsed,
            timezone_text=tz,
            now_utc=NOW_UTC,
            retention_days=30,
        )

    def test_relative_range_converts_local_bounds_to_utc(self) -> None:
        parsed = ParsedQuery(
            has_time_range=True,
            start_local="2026-07-17 00:00",
            end_local="2026-07-31 12:00",
            topic="露營",
        )

        query = self._resolve(parsed)

        self.assertEqual(query.topic, "露營")
        self.assertEqual(query.start_utc, datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(query.end_utc, datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc))
        self.assertIn("2026-07-17 00:00", query.range_label)

    def test_no_time_range_keeps_pending_window(self) -> None:
        parsed = ParsedQuery(has_time_range=False, start_local="", end_local="", topic="出遊")

        query = self._resolve(parsed)

        self.assertIsNone(query.start_utc)
        self.assertIsNone(query.end_utc)
        self.assertEqual(query.topic, "出遊")
        self.assertEqual(query.range_label, "沿用上次摘要後的訊息")

    def test_topic_only_when_model_leaves_range_empty(self) -> None:
        parsed = ParsedQuery(has_time_range=True, start_local="", end_local="", topic="報帳")

        query = self._resolve(parsed)

        self.assertIsNone(query.start_utc)
        self.assertIsNone(query.end_utc)
        self.assertEqual(query.topic, "報帳")

    def test_blank_topic_becomes_none(self) -> None:
        parsed = ParsedQuery(
            has_time_range=True,
            start_local="2026-07-30 00:00",
            end_local="2026-07-31 12:00",
            topic="   ",
        )

        query = self._resolve(parsed)

        self.assertIsNone(query.topic)

    def test_open_ended_end_defaults_to_now(self) -> None:
        parsed = ParsedQuery(
            has_time_range=True,
            start_local="2026-07-29 00:00",
            end_local="",
            topic="",
        )

        query = self._resolve(parsed)

        self.assertEqual(query.end_utc, NOW_UTC)

    def test_missing_start_labels_as_up_to_end(self) -> None:
        parsed = ParsedQuery(
            has_time_range=True,
            start_local="",
            end_local="2026-07-20 23:59",
            topic="通知",
        )

        query = self._resolve(parsed)

        self.assertIsNone(query.start_utc)
        self.assertTrue(query.range_label.startswith("到 "))

    def test_reversed_range_raises(self) -> None:
        parsed = ParsedQuery(
            has_time_range=True,
            start_local="2026-07-31 12:00",
            end_local="2026-07-17 00:00",
            topic="露營",
        )

        with self.assertRaises(SummaryQueryError):
            self._resolve(parsed)

    def test_range_entirely_beyond_retention_raises(self) -> None:
        parsed = ParsedQuery(
            has_time_range=True,
            start_local="2026-01-01 00:00",
            end_local="2026-01-05 00:00",
            topic="尾牙",
        )

        with self.assertRaises(SummaryQueryError):
            self._resolve(parsed)

    def test_invalid_datetime_raises(self) -> None:
        parsed = ParsedQuery(
            has_time_range=True,
            start_local="上週一",
            end_local="2026-07-31 12:00",
            topic="露營",
        )

        with self.assertRaises(SummaryQueryError):
            self._resolve(parsed)


if __name__ == "__main__":
    unittest.main()
