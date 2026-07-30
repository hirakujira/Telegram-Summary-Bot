from __future__ import annotations

import unittest

from app.summary_format import (
    build_message_link,
    build_preview_header,
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

    def test_preview_header_identifies_the_group_and_escapes_the_title(self) -> None:
        header = build_preview_header(
            chat_title="測試 <群組> & 夥伴",
            chat_id=-1001234567890,
            window_hours=24,
        )

        self.assertIn("🔍 <b>預覽（未發佈到群組）</b>", header)
        self.assertIn(
            "<code>測試 &lt;群組&gt; &amp; 夥伴 · chat_id -1001234567890 · 過去 24 小時</code>",
            header,
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
        # No replies in this batch, so the reply hint stays out of the prompt.
        self.assertNotIn("回覆關係說明", transcript)


class TranscriptReplyTests(unittest.TestCase):
    @staticmethod
    def _row(message_id, user_name, text, minute, reply_to=None):
        return {
            "message_id": message_id,
            "created_at_utc": f"2026-07-15T01:{minute:02d}:00+00:00",
            "user_name": user_name,
            "text": text,
            "reply_to_message_id": reply_to,
        }

    def _build(self, rows):
        return build_transcript(
            rows=rows,
            total_count=len(rows),
            chat_title="測試群組",
            summary_range="2026-07-15 to 2026-07-15",
            chat_id=-1001234567890,
            chat_username=None,
        )

    def test_marks_reply_edges_for_parallel_threads(self) -> None:
        rows = [
            self._row(41, "Alice", "露營要不要去", 0),
            self._row(42, "Bob", "報稅記得處理", 1),
            self._row(43, "Carol", "我要去露營", 2, reply_to=41),
            self._row(44, "Dave", "報稅我用手機報完了", 3, reply_to=42),
        ]

        transcript = self._build(rows)

        self.assertIn("回覆關係說明", transcript)
        self.assertIn("[m1][2026-07-15T01:00:00+00:00]", transcript)
        self.assertIn("[m3 回覆 m1]", transcript)
        self.assertIn("[m4 回覆 m2]", transcript)
        self.assertNotIn("w1", transcript)

    def test_reply_target_outside_the_window_gets_a_shared_outside_label(self) -> None:
        rows = [
            self._row(50, "Alice", "接續前面的討論", 0, reply_to=10),
            self._row(51, "Bob", "我也回同一則", 1, reply_to=10),
            self._row(52, "Carol", "回另一則舊訊息", 2, reply_to=11),
        ]

        transcript = self._build(rows)

        self.assertIn("[m1 回覆 w1]", transcript)
        self.assertIn("[m2 回覆 w1]", transcript)
        self.assertIn("[m3 回覆 w2]", transcript)

    def test_missing_reply_column_is_treated_as_no_reply(self) -> None:
        rows = [
            {
                "message_id": 41,
                "created_at_utc": "2026-07-15T01:00:00+00:00",
                "user_name": "Alice",
                "text": "單獨的發言",
            }
        ]

        transcript = self._build(rows)

        self.assertIn("[m1][2026-07-15T01:00:00+00:00]", transcript)
        self.assertNotIn("回覆", transcript)


if __name__ == "__main__":
    unittest.main()
