from __future__ import annotations

import unittest

from app.summary_format import build_message_link, build_transcript


class SummaryLinkTests(unittest.TestCase):
    def test_builds_public_group_link(self) -> None:
        link = build_message_link(
            chat_id=-1001234567890,
            message_id=42,
            chat_username="@example_group",
        )

        self.assertEqual(link, "https://t.me/example_group/42")

    def test_builds_private_supergroup_link(self) -> None:
        link = build_message_link(
            chat_id=-1001234567890,
            message_id=42,
            chat_username=None,
        )

        self.assertEqual(link, "https://t.me/c/1234567890/42")

    def test_basic_private_group_has_no_message_link(self) -> None:
        link = build_message_link(
            chat_id=-123456789,
            message_id=42,
            chat_username=None,
        )

        self.assertIsNone(link)

    def test_transcript_includes_a_link_for_each_supergroup_message(self) -> None:
        rows = [
            {
                "message_id": 41,
                "created_at_utc": "2026-07-15T01:00:00+00:00",
                "user_name": "Alice",
                "text": "第一個討論點",
            },
            {
                "message_id": 42,
                "created_at_utc": "2026-07-15T01:01:00+00:00",
                "user_name": "Bob",
                "text": "第二個討論點",
            },
        ]

        transcript = build_transcript(
            rows=rows,
            total_count=2,
            chat_title="測試群組",
            summary_range="2026-07-15 to 2026-07-15",
            chat_id=-1001234567890,
            chat_username=None,
        )

        self.assertIn(
            "[訊息 ID: 41][討論連結: https://t.me/c/1234567890/41]",
            transcript,
        )
        self.assertIn(
            "[訊息 ID: 42][討論連結: https://t.me/c/1234567890/42]",
            transcript,
        )


if __name__ == "__main__":
    unittest.main()
