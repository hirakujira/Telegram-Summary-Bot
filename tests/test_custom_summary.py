from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.main import SummaryBot
from app.summary_query import ParsedQuery, SummaryQueryError
from app.time_utils import parse_timezone, to_iso, utc_now


OWNER_ID = 777
GROUP_ID = -1001234567890
SUMMARY_OUTPUT = """測試群組 Summary | 2026-07-29 to 2026-07-30 | 2 則訊息

1. 🏕️ **露營揪團**
- **Alice**：提議週末去露營
"""


class RecordingSummarizer:
    def __init__(self, output: str):
        self.output = output
        self.calls: list[dict] = []

    async def summarize(self, *, transcript, model, reasoning_effort, response_style, topic=None):
        self.calls.append(
            {
                "transcript": transcript,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "response_style": response_style,
                "topic": topic,
            }
        )
        return self.output


class FakeQueryParser:
    def __init__(self, *, parsed=None, error: Exception | None = None):
        self._parsed = parsed
        self._error = error
        self.calls: list[dict] = []

    async def parse(self, *, text, model, timezone_text, now_utc):
        self.calls.append({"text": text, "model": model, "timezone_text": timezone_text})
        if self._error is not None:
            raise self._error
        if callable(self._parsed):
            return self._parsed(now_utc, timezone_text)
        return self._parsed


class FakeBot:
    def __init__(self):
        self.sent: list[SimpleNamespace] = []

    async def send_message(self, *, chat_id, text, parse_mode=None):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, parse_mode=parse_mode))

    async def get_chat(self, chat_id):
        return SimpleNamespace(title="測試群組", username=None)


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text):
        self.replies.append(text)


def build_update(*, user_id: int, chat_id: int) -> tuple[SimpleNamespace, FakeMessage]:
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
    )
    return update, message


def two_week_window(topic: str):
    def factory(now_utc, timezone_text):
        tz = parse_timezone(timezone_text)
        now_local = now_utc.astimezone(tz)
        start_local = now_local - timedelta(days=14)
        return ParsedQuery(
            has_time_range=True,
            start_local=start_local.strftime("%Y-%m-%d %H:%M"),
            end_local=now_local.strftime("%Y-%m-%d %H:%M"),
            topic=topic,
        )

    return factory


class CustomSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = Settings(
            telegram_bot_token="test-token",
            openai_api_key="test-key",
            owner_telegram_user_id=OWNER_ID,
            sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
        )
        self.bot = SummaryBot(settings)
        self.summarizer = RecordingSummarizer(SUMMARY_OUTPUT)
        self.bot.summarizer = self.summarizer
        self.telegram_bot = FakeBot()
        self.bot.application = SimpleNamespace(bot=self.telegram_bot)
        await self.bot.db.connect()
        await self.bot.db.ensure_chat(GROUP_ID)

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp_dir.cleanup()

    async def _save_message(self, *, message_id: int, text: str, age: timedelta) -> None:
        await self.bot.db.save_text_message(
            chat_id=GROUP_ID,
            message_id=message_id,
            user_id=1,
            user_name="Alice",
            text=text,
            created_at_utc=to_iso(utc_now() - age),
        )

    def _run(self, args: list[str]) -> tuple[SimpleNamespace, FakeMessage]:
        update, message = build_update(user_id=OWNER_ID, chat_id=GROUP_ID)
        context = SimpleNamespace(args=args)
        return update, message, context

    async def test_topic_and_time_range_focus_summary_without_advancing_cursor(self) -> None:
        await self._save_message(message_id=1, text="兩週前的無關閒聊", age=timedelta(days=15))
        await self._save_message(message_id=2, text="我們上週去露營玩得很開心", age=timedelta(hours=2))
        await self.bot.db.set_last_summarized_at(GROUP_ID, to_iso(utc_now() - timedelta(days=1)))
        cursor_before = await self.bot.db.get_last_summarized_at(GROUP_ID)

        self.bot.query_parser = FakeQueryParser(parsed=two_week_window("露營"))

        update, message, context = self._run(["這兩週以來討論到露營的事情"])
        await self.bot.manual_summary(update, context)

        self.assertEqual(self.summarizer.calls[0]["topic"], "露營")
        transcript = self.summarizer.calls[0]["transcript"]
        self.assertIn("我們上週去露營玩得很開心", transcript)
        self.assertNotIn("兩週前的無關閒聊", transcript)

        posted = [s for s in self.telegram_bot.sent if s.chat_id == GROUP_ID]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].parse_mode, "HTML")

        self.assertTrue(any("主題：露營" in r for r in message.replies))
        self.assertEqual(await self.bot.db.get_last_summarized_at(GROUP_ID), cursor_before)

    async def test_custom_summary_keeps_the_chat_configured_style(self) -> None:
        await self.bot.db.update_chat_settings(GROUP_ID, response_style="roast")
        await self._save_message(message_id=1, text="我們上週去露營玩得很開心", age=timedelta(hours=2))

        self.bot.query_parser = FakeQueryParser(parsed=two_week_window("露營"))

        update, message, context = self._run(["這兩週以來討論到露營的事情"])
        await self.bot.manual_summary(update, context)

        call = self.summarizer.calls[0]
        self.assertEqual(call["response_style"], "roast")
        self.assertEqual(call["topic"], "露營")

    async def test_no_args_keeps_default_behavior_and_advances_cursor(self) -> None:
        await self._save_message(message_id=1, text="今天的討論", age=timedelta(hours=1))

        update, message, context = self._run([])
        await self.bot.manual_summary(update, context)

        self.assertEqual(self.summarizer.calls[0]["topic"], None)
        self.assertIsNotNone(await self.bot.db.get_last_summarized_at(GROUP_ID))
        self.assertEqual(len([s for s in self.telegram_bot.sent if s.chat_id == GROUP_ID]), 1)

    async def test_empty_window_reports_topic_without_calling_model(self) -> None:
        await self._save_message(message_id=1, text="兩週前的無關閒聊", age=timedelta(days=15))

        self.bot.query_parser = FakeQueryParser(parsed=two_week_window("露營"))

        update, message, context = self._run(["這兩週以來討論到露營的事情"])
        await self.bot.manual_summary(update, context)

        self.assertEqual(self.summarizer.calls, [])
        self.assertEqual([s for s in self.telegram_bot.sent if s.chat_id == GROUP_ID], [])
        self.assertTrue(any("找不到與「露營」相關的訊息" in r for r in message.replies))

    async def test_parse_error_replies_without_summarizing(self) -> None:
        await self._save_message(message_id=1, text="我們上週去露營玩得很開心", age=timedelta(hours=2))

        self.bot.query_parser = FakeQueryParser(error=SummaryQueryError("看不懂的條件"))

        update, message, context = self._run(["亂七八糟的條件"])
        await self.bot.manual_summary(update, context)

        self.assertEqual(self.summarizer.calls, [])
        self.assertEqual(self.telegram_bot.sent, [])
        self.assertIn("看不懂的條件", message.replies)

    async def test_non_owner_cannot_run_custom_summary(self) -> None:
        self.bot.query_parser = FakeQueryParser(parsed=two_week_window("露營"))
        update, message = build_update(user_id=OWNER_ID + 1, chat_id=GROUP_ID)
        context = SimpleNamespace(args=["這兩週以來討論到露營的事情"])

        await self.bot.manual_summary(update, context)

        self.assertEqual(message.replies, ["你沒有權限執行這個指令。"])
        self.assertEqual(self.summarizer.calls, [])
        self.assertEqual(self.telegram_bot.sent, [])


if __name__ == "__main__":
    unittest.main()
