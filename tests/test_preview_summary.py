from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType

from app.config import Settings
from app.main import SummaryBot
from app.time_utils import to_iso, utc_now


OWNER_ID = 777
GROUP_ID = -1001234567890
SUMMARY_OUTPUT = """測試群組 Summary | 2026-07-29 to 2026-07-30 | 2 則訊息

1. 🧪 **部署討論**
- **Alice**：確認今天上線
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


def build_update(*, user_id: int, chat_id: int, chat_type: str) -> tuple[SimpleNamespace, FakeMessage]:
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
    )
    return update, message


class PreviewSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = Settings(
            telegram_bot_token="test-token",
            openai_api_key="test-key",
            owner_telegram_user_id=OWNER_ID,
            sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
            preview_window_hours=24,
        )
        self.bot = SummaryBot(settings)
        self.summarizer = RecordingSummarizer(SUMMARY_OUTPUT)
        self.bot.summarizer = self.summarizer
        self.telegram_bot = FakeBot()
        self.bot.application = SimpleNamespace(bot=self.telegram_bot)
        await self.bot.db.connect()

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

    async def test_preview_only_dms_owner_and_keeps_summary_state(self) -> None:
        await self._save_message(message_id=1, text="視窗外的舊訊息", age=timedelta(hours=30))
        await self._save_message(message_id=2, text="視窗內的新訊息", age=timedelta(hours=1))

        update, message = build_update(
            user_id=OWNER_ID,
            chat_id=GROUP_ID,
            chat_type=ChatType.SUPERGROUP,
        )
        await self.bot.preview_summary(update, None)

        self.assertEqual(message.replies, [])
        self.assertEqual({sent.chat_id for sent in self.telegram_bot.sent}, {OWNER_ID})

        transcript = self.summarizer.calls[0]["transcript"]
        self.assertIn("視窗內的新訊息", transcript)
        self.assertNotIn("視窗外的舊訊息", transcript)

        result = self.telegram_bot.sent[-1]
        self.assertEqual(result.parse_mode, "HTML")
        self.assertIn("🔍 <b>預覽（未發佈到群組）</b>", result.text)
        self.assertIn(f"測試群組 · chat_id {GROUP_ID}", result.text)
        self.assertIn("<blockquote expandable>", result.text)

        self.assertIsNone(await self.bot.db.get_last_summarized_at(GROUP_ID))

    async def test_preview_reports_empty_window_without_calling_model(self) -> None:
        await self._save_message(message_id=1, text="視窗外的舊訊息", age=timedelta(hours=30))

        update, _ = build_update(
            user_id=OWNER_ID,
            chat_id=GROUP_ID,
            chat_type=ChatType.SUPERGROUP,
        )
        await self.bot.preview_summary(update, None)

        self.assertEqual(self.summarizer.calls, [])
        self.assertIn("沒有可摘要的文字訊息", self.telegram_bot.sent[-1].text)
        self.assertEqual({sent.chat_id for sent in self.telegram_bot.sent}, {OWNER_ID})

    async def test_preview_rejects_non_owner(self) -> None:
        await self._save_message(message_id=1, text="視窗內的新訊息", age=timedelta(hours=1))

        update, message = build_update(
            user_id=OWNER_ID + 1,
            chat_id=GROUP_ID,
            chat_type=ChatType.SUPERGROUP,
        )
        await self.bot.preview_summary(update, None)

        self.assertEqual(message.replies, ["你沒有權限執行這個指令。"])
        self.assertEqual(self.telegram_bot.sent, [])
        self.assertEqual(self.summarizer.calls, [])

    async def test_preview_in_private_chat_asks_owner_to_use_the_group(self) -> None:
        update, message = build_update(
            user_id=OWNER_ID,
            chat_id=OWNER_ID,
            chat_type=ChatType.PRIVATE,
        )
        await self.bot.preview_summary(update, None)

        self.assertEqual(len(message.replies), 1)
        self.assertIn("請在要預覽的群組內輸入 /preview", message.replies[0])
        self.assertEqual(self.telegram_bot.sent, [])
        self.assertEqual(self.summarizer.calls, [])


if __name__ == "__main__":
    unittest.main()
