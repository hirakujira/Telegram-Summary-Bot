from __future__ import annotations

import unittest

from app.summary_format import (
    build_message_link,
    build_transcript,
    format_summary_for_telegram,
)


class SummaryLinkTests(unittest.TestCase):
    def test_formats_summary_topics_as_expandable_blockquotes(self) -> None:
        summary = """掌孫大集合 Summary | 2026-07-16 to 2026-07-17 | 164 則訊息

1. 🧪 **測試 <主題>**
- **Alice & Bob**：討論 API <限制>
- **Carol**：決定明天執行
[💬 回到討論](https://t.me/c/1234567890/42)

2. 📌 **第二個主題**
- **Dave**：保留重要數字 123
[💬 回到討論](https://t.me/example_group/43)
"""

        formatted = format_summary_for_telegram(summary)

        self.assertIn("<b>掌孫大集合 Summary</b>", formatted)
        self.assertIn(
            "<code>2026-07-16 to 2026-07-17 · 164 則訊息</code>",
            formatted,
        )
        self.assertIn("<b>1. 🧪 測試 &lt;主題&gt;</b>", formatted)
        self.assertIn(
            "• <b>Alice &amp; Bob</b>：討論 API &lt;限制&gt;",
            formatted,
        )
        self.assertIn(
            '<a href="https://t.me/c/1234567890/42">💬 回到討論</a>',
            formatted,
        )
        self.assertEqual(formatted.count("<blockquote expandable>"), 2)
        self.assertEqual(formatted.count("</blockquote>"), 2)

    def test_does_not_render_untrusted_discussion_link(self) -> None:
        summary = """測試群組 Summary | 2026-07-17 to 2026-07-17 | 1 則訊息

1. 🔒 **安全測試**
- **Alice**：測試外部連結
[💬 回到討論](https://example.com/phishing)
"""

        formatted = format_summary_for_telegram(summary)

        self.assertNotIn('<a href="https://example.com/phishing">', formatted)
        self.assertIn(
            "[💬 回到討論](https://example.com/phishing)",
            formatted,
        )

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
            "[討論連結: https://t.me/c/1234567890/41]",
            transcript,
        )
        self.assertIn(
            "[討論連結: https://t.me/c/1234567890/42]",
            transcript,
        )
        self.assertNotIn("訊息 ID", transcript)


if __name__ == "__main__":
    unittest.main()
